"""Host fixtures for upgrade compatibility and interrupted recovery ownership."""
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
