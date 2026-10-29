"""Host fixtures for upgrade compatibility and interrupted recovery ownership."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_service import Jobs
from job_queue import RequestConflict


class JobRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.jobs = Jobs(temporary.name, {"phonefarm": (sys.executable, temporary.name)}, max_running=1,
                         host_capacity={"cpu": 2, "gpu": 0, "io": 0})
        self.jobs.dispatch.start = lambda: None
        self.queue = self.jobs.dispatch.queue

    def submit(self, **spec):
        return self.jobs.start({"bin": "phonefarm", "argv": ["-c", "pass"], **spec}, b"")["id"]

    def terminal(self, jid, phase="finished"):
        meta = self.jobs._read_meta(jid)
        meta.update(phase=phase, recovery={"status": "not_reported"})
        if phase == "finished":
            # Synthetic completed-worker receipt for this scheduling fixture;
            # this PID is descriptive only, never probed or signalled.
            now, fixture_pid = time.time(), 424242
            meta.update(state="finished", outcome="succeeded", exit=0, ended=now)
            meta["execution"].update(child_pid=fixture_pid, returncode=0,
                process_management={"policy": "process_group", "backend": "posix_process_group",
                                    "completion_scope": "original_process_group", "detached_descendants": "unverified"},
                owned_process_group={"scope": "original_process_group", "status": "absent",
                                     "pgid": fixture_pid, "checked_at": now, "error": None})
            self.jobs._read_root.write_json(jid + "/exitcode", 0)
        self.jobs._write_meta(jid, meta)
        self.jobs._read_root.write_json(jid + "/launch.json", {})
        self.queue.set_state(jid, "claimed")

    def test_legacy_retry_keeps_fingerprint_and_lease_change_does_not_spin(self):
        # This is the exact normalized shape persisted before device_access existed.
        old = {"bin": "phonefarm", "argv": ["-c", "pass"], "label": None, "resources": ["legacy-device"]}
        original = self.jobs.dispatch.submit(old, b"", "before-upgrade")
        retry = self.jobs.start({"bin": "phonefarm", "argv": ["-c", "pass"]}, b"", "before-upgrade")
        self.assertEqual(retry["id"], original["id"])
        self.assertNotIn("device_access", self.queue.get(retry["id"])["spec"])
        # The lease changes between queue eligibility and launch. One tick must yield.
        with patch.object(self.jobs.leases, "snapshot", side_effect=[{"status": "clear"}, {"status": "held"}]) as read:
            self.jobs.dispatch.tick()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(self.queue.get(retry["id"])["state"], "queued")
        self.assertFalse(Path(self.jobs.path(retry["id"], "launch.json")).exists())

    def test_lost_recovery_blocks_siblings_but_targeted_recovery_can_continue(self):
        parent = self.submit()
        self.terminal(parent)  # Exit alone never proves device recovery.
        blocked = {"schema_version": 1, "status": "blocked", "entries": [], "error": None}
        with patch.object(self.jobs.leases, "snapshot", return_value=blocked):
            self.jobs.dispatch.tick()
            self.assertEqual(self.queue.get(parent)["state"], "done")
            self.assertEqual(self.jobs.status(parent)["queue"]["host_capacity"]["state"], "released")
            waiting = self.submit(resources=["device:other"])
            self.assertEqual(self.jobs.status(waiting)["queue"]["waiting"]["kind"], "device_lease")
            host = self.submit(device_access="none", resources=[])
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [host])
            self.jobs.dispatch.finish_unstarted(self.queue.get(host), "cancelled", "fixture mock launch did not execute")
            child = self.submit(device_access="recovery", recovery_for=parent)
            self.terminal(child, phase="running")  # launch exists; no owning worker flock.
            sibling = self.submit(device_access="recovery", recovery_for=parent)
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertTrue(self.jobs.status(child)["lost"])
            self.assertEqual(self.queue.get(child)["state"], "blocked")
            self.assertIsNone(self.queue.get(child)["host_capacity_release"])
            self.assertEqual(self.jobs.status(child)["queue"]["host_capacity"]["state"], "released")
            self.assertEqual(self.queue.get(sibling)["state"], "queued")
            launch.assert_not_called()
            nested = self.submit(device_access="recovery", recovery_for=child)
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            launch.assert_not_called()
            self.assertTrue(self.jobs.reset_queue(child, "fixture: accept missing group record")["released"])
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [sibling])
            self.terminal(sibling)
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [nested])
            self.terminal(nested)
            self.jobs.dispatch.tick()
        for jid in (parent, child, sibling, nested):
            self.assertEqual(self.queue.get(jid)["state"], "done")
        self.assertEqual(self.queue.get(nested)["spec"]["recovery_for"], child)
        self.assertTrue(self.jobs.status(child)["lost"])
        self.assertEqual(self.jobs.status(child)["recovery"]["status"], "required")

    def test_direct_exit_cleans_group_and_releases_exclusive_resource(self):
        from test_job_group_observation import until
        import job_runtime
        root = Path(self.jobs.root) / "group-completion"
        root.mkdir()
        jobs = Jobs(str(root), {"host": (sys.executable, str(root))}, max_running=2)
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None
        queue = jobs.dispatch.queue
        descendant = "import pathlib,sys,time;pathlib.Path(sys.argv[1],'child-ready').touch();time.sleep(20)"
        leader = ("import pathlib,subprocess,sys,time;out=pathlib.Path(sys.argv[1]);"
                  f"subprocess.Popen([sys.executable,'-c',{descendant!r},str(out)]);"
                  "\nwhile not (out/'leader-exit').exists(): time.sleep(.01)\n")
        def submit(program, resource):
            return jobs.start({"bin": "host", "argv": ["-c", program, "@out"],
                               "device_access": "none", "resources": [resource]}, b"")["id"]
        parent = submit(leader, "host:exclusive")
        jobs.dispatch.tick()
        out = Path(jobs.path(parent, "out"))
        try:
            until(lambda: (out/'child-ready').exists())
            waiting = submit("pass", "host:exclusive")
            other = submit("pass", "host:other")
            self.assertEqual(queue.claim_next(2)["job_id"], other)
            self.assertEqual(queue.get(waiting)["state"], "queued")
            self.assertIn(parent, jobs.status(waiting)["queue"]["waiting"]["blocking_jobs"])
            self.assertFalse(Path(jobs.path(waiting, "launch.json")).exists())
            (out/'leader-exit').touch()
            until(lambda: not job_runtime.runner_alive(jobs._read_root, parent))
            jobs.dispatch.finalize(queue.get(parent))
            meta = jobs.status(parent)
            self.assertEqual((meta["exit"], meta["execution"]["returncode"]), (0, 0))
            self.assertEqual((meta["phase"], meta["outcome"], meta["recovery"]["status"]),
                             ("interrupted", "interrupted", "required"))
            self.assertEqual(meta["execution"]["owned_process_group"]["status"], "absent")
            self.assertEqual(queue.get(parent)["state"], "done")
            self.assertEqual(queue.claim_next(2)["job_id"], waiting)
        finally:
            (out/'leader-exit').touch()
            if job_runtime.runner_alive(jobs._read_root, parent):
                jobs.cancel(parent, force=True)
                until(lambda: not job_runtime.runner_alive(jobs._read_root, parent))


if __name__ == "__main__":
    unittest.main()
