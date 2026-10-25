"""Host fixtures for upgrade compatibility and interrupted recovery ownership."""
import json
import os
from pathlib import Path
import shutil
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
        self.jobs = Jobs(temporary.name, {"phonefarm": (sys.executable, temporary.name)}, max_running=1)
        self.jobs.dispatch.start = lambda: None
        self.queue = self.jobs.dispatch.queue

    def submit(self, **spec):
        return self.jobs.start({"bin": "phonefarm", "argv": ["-c", "pass"], **spec}, b"")["id"]

    def terminal(self, jid, phase="finished"):
        meta = self.jobs._read_meta(jid)
        meta.update(phase=phase, recovery={"status": "not_reported"})
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
            self.assertEqual(self.queue.get(parent)["state"], "blocked")
            waiting = self.submit(resources=["device:other"])
            self.assertEqual(self.jobs.status(waiting)["queue"]["waiting"]["kind"], "device_lease")
            host = self.submit(device_access="none", resources=[])
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [host])
            self.queue.set_state(host, "done")
            child = self.submit(device_access="recovery", recovery_for=parent)
            self.terminal(child, phase="running")  # launch exists; no owning worker flock.
            sibling = self.submit(device_access="recovery", recovery_for=parent)
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertTrue(self.jobs.status(child)["lost"])
            self.assertEqual(self.queue.get(child)["state"], "blocked")
            self.assertEqual(self.queue.get(sibling)["state"], "queued")
            launch.assert_not_called()
            with self.assertRaises(RequestConflict):
                self.jobs.release_queue(child, "host lease remains blocked")
            nested = self.submit(device_access="recovery", recovery_for=child)
            with patch.object(self.jobs.dispatch, "launch") as launch:
                self.jobs.dispatch.tick()
            self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [nested])
            self.terminal(nested)
            self.jobs.dispatch.tick()
            self.assertEqual(self.queue.get(nested)["state"], "done")
        self.jobs.cancel(sibling)
        with patch.object(self.jobs.leases, "snapshot", return_value={**blocked, "status": "clear"}):
            with self.assertRaises(RequestConflict):
                self.jobs.release_queue(parent, "child still blocked")
            self.jobs.release_queue(child, "reviewed lost child")
            self.jobs.release_queue(parent, "reviewed original job")
        self.assertEqual(self.queue.get(parent)["state"], "done")
        self.assertEqual(self.jobs.status(child)["recovery"]["status"], "required")

    def test_direct_exit_with_live_group_blocks_host_and_recovery_resources(self):
        root = Path(self.jobs.root) / "group-completion"
        root.mkdir()
        # Explicit host-only bin: no PF, ADB, actual lease reader or transport.
        jobs = Jobs(str(root), {"host": (sys.executable, str(root))}, max_running=1)
        jobs.dispatch.start = lambda: None
        queue = jobs.dispatch.queue
        jobs_with_descendants, known_pids = [], set()
        checked_jobs = []
        failed = False
        descendant = """
import json, os, pathlib, sys, time
out = pathlib.Path(sys.argv[1])
ready = out / 'descendant-ready.json'
temporary = out / 'descendant-ready.tmp'
temporary.write_text(json.dumps({'pid':os.getpid(), 'pgid':os.getpgrp()}))
temporary.replace(ready)
deadline = time.monotonic() + 20
while not (out / 'release').exists() and time.monotonic() < deadline:
    time.sleep(.02)
released = (out / 'release').exists()
temporary = out / 'descendant-done.tmp'
temporary.write_text(json.dumps({'released':released}))
temporary.replace(out / 'descendant-done.json')
sys.exit(0 if released else 1)
"""
        leader = ("import pathlib,subprocess,sys,time;out=pathlib.Path(sys.argv[1]);"
                  f"child=subprocess.Popen([sys.executable,'-c',{descendant!r},str(out)],close_fds=True);"
                  "deadline=time.monotonic()+5\n"
                  "while not (out/'descendant-ready.json').exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                  "assert (out/'descendant-ready.json').exists(), 'descendant startup timed out'\n")

        def submit(program, **spec):
            return jobs.start({"bin": "host", "argv": ["-c", program, "@out"],
                               "device_access": "none", **spec}, b"")["id"]

        def until(predicate, timeout=6):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(.02)
            self.fail("bounded host group fixture did not reach the expected state")

        def terminal(jid):
            jobs.dispatch.tick()
            row = queue.get(jid)
            return jobs.status(jid) if row["state"] in {"done", "blocked"} else None

        def check_group_failure(jid):
            meta = until(lambda: terminal(jid))
            ready = json.loads(Path(jobs.path(jid, "out", "descendant-ready.json")).read_text())
            known_pids.update((ready["pid"], meta["pid"], meta["execution"]["child_pid"]))
            self.assertEqual(ready["pgid"], meta["execution"]["child_pid"])
            os.kill(ready["pid"], 0)  # The same-group descendant really is still live.
            self.assertEqual((meta["exit"], meta["execution"]["returncode"]), (0, 0))
            self.assertEqual((meta["phase"], meta["outcome"], meta["recovery"]["status"]),
                             ("interrupted", "interrupted", "required"))
            group = meta["execution"]["owned_process_group"]
            self.assertEqual((group["scope"], group["status"], group["pgid"]),
                             ("original_process_group", "present", ready["pgid"]))
            self.assertIsInstance(group["checked_at"], (int, float))
            self.assertEqual(meta["queue"]["state"], "blocked")
            checked_jobs.append({"id": jid, "device_access": meta["device_access"], "exit": meta["exit"],
                                 "phase": meta["phase"], "outcome": meta["outcome"],
                                 "queue_state": meta["queue"]["state"], "recovery": meta["recovery"],
                                 "owned_process_group": group, "descendant": ready})

        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        clear = {"schema_version": 1, "status": "clear", "entries": [], "error": None}
        try:
            with patch.object(jobs.leases, "snapshot", return_value=clear):
                parent = submit(leader, resources=["host:exclusive"])
                jobs_with_descendants.append(parent)
                check_group_failure(parent)
                waiting = submit("raise RuntimeError('shared resource must stay reserved')",
                                 resources=["host:exclusive"])
                other = submit("import pathlib,sys;pathlib.Path(sys.argv[1],'completed').touch()",
                               resources=["host:other"])
                other_meta = until(lambda: terminal(other))
                known_pids.update((other_meta["pid"], other_meta["execution"]["child_pid"]))
                self.assertEqual((other_meta["exit"], other_meta["queue"]["state"]), (0, "done"))
                self.assertTrue(Path(jobs.path(other, "out", "completed")).is_file())
                self.assertEqual(queue.get(waiting)["state"], "queued")
                self.assertFalse(Path(jobs.path(waiting, "launch.json")).exists())

                # A targeted recovery may borrow the parent's reservation, but
                # its own unfinished group must not become an ordinary done row.
                recovery = submit(leader, device_access="recovery", recovery_for=parent)
                jobs_with_descendants.append(recovery)
                check_group_failure(recovery)
                sibling = submit("raise RuntimeError('recovery sibling must wait')",
                                 device_access="recovery", recovery_for=parent)
                jobs.dispatch.tick()
                self.assertEqual(queue.get(sibling)["state"], "queued")
                self.assertEqual(queue.get(waiting)["state"], "queued")
                self.assertFalse(Path(jobs.path(sibling, "launch.json")).exists())
                self.assertTrue(jobs.cancel(waiting)["accepted"])
                self.assertTrue(jobs.cancel(sibling)["accepted"])
        except BaseException:
            failed = True
            raise
        finally:
            try:
                for jid in jobs_with_descendants:
                    out = Path(jobs.path(jid, "out"))
                    (out / "release").touch()
                    ready = out / "descendant-ready.json"
                    if ready.exists():
                        known_pids.add(json.loads(ready.read_text())["pid"])
                        until(lambda: (out / "descendant-done.json").exists())
                        self.assertTrue(json.loads((out / "descendant-done.json").read_text())["released"])
                    meta = jobs._read_meta(jid)
                    known_pids.update(p for p in (meta.get("pid"), (meta.get("execution") or {}).get("child_pid"))
                                      if isinstance(p, int))
                until(lambda: all(gone(pid) for pid in known_pids))
            except BaseException:
                failed = True
                raise
            finally:
                jobs._read_root.close()
                # Local acceptance keeps failure originals outside TemporaryDirectory.
                # Standalone CI still reports the exact job metadata in the test failure.
                qa = Path(__file__).resolve().parents[3] / "qa"
                if qa.is_dir():
                    name = f"job-group-host-{os.getpid()}-{time.time_ns()}"
                    summary = {"passed": not failed, "scope": "actual host Jobs/worker and original process groups only",
                               "jobs": checked_jobs, "known_pids": sorted(known_pids),
                               "remaining_known_pids": [pid for pid in sorted(known_pids) if not gone(pid)],
                               "device_transport_calls": 0}
                    if failed:
                        evidence = qa / (name + "-failure")
                        shutil.copytree(root, evidence)
                        summary["failure_evidence"] = str(evidence)
                    path = qa / (name + ".json")
                    path.write_text(json.dumps(summary, indent=2) + "\n")
                    print(f"job group host result: {path}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
