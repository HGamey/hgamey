"""Home reservations show the same device scope that the queue claims."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_service import Jobs


class HomeReservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.jobs = Jobs(temporary.name, {"fixture": (sys.executable, temporary.name)})
        self.addCleanup(self.jobs._read_root.close)
        self.jobs.dispatch.start = lambda: None
        self.board = dash.Dash.__new__(dash.Dash)
        self.board.job_service = self.jobs
        self.phones = {"S1", "S2"}

    def submit(self, argv, resources):
        return self.jobs.dispatch.submit({
            "bin": "fixture", "argv": argv, "resources": resources,
            "device_access": "write", "label": "measurement", "actor_id": "researcher",
        }, b"", None)["id"]

    def assert_device_reservation(self, jid):
        self.assertEqual(self.board._reservations(), [])
        self.assertEqual(self.jobs.dispatch.queue.claim_next(4)["job_id"], jid)
        owners = self.board._reservations()
        mine = self.board._reserved("S1", owners, self.phones)
        self.assertEqual((mine["job_id"], mine["kind"], mine["reserved"], mine["actor_id"]),
                         (jid, "job", True, "researcher"))
        self.assertIsNone(self.board._reserved("S2", owners, self.phones))
        other = self.submit(["-c", "pass", "--serial", "S2"], [])
        self.assertEqual(self.jobs.dispatch.queue.claim_next(4)["job_id"], other)
        owners = self.board._reservations()
        self.assertEqual(self.board._reserved("S1", owners, self.phones)["job_id"], jid)
        self.assertEqual(self.board._reserved("S2", owners, self.phones)["job_id"], other)

    def test_argv_serial_reservation_only_appears_on_its_phone(self):
        jid = self.submit(["-c", "pass", "--serial", "S1"], [])
        self.assert_device_reservation(jid)

    def test_single_device_resource_reservation_only_appears_on_its_phone(self):
        jid = self.submit(["-c", "pass"], ["device:S1"])
        self.assert_device_reservation(jid)

    def test_global_reservation_appears_on_every_phone(self):
        jid = self.submit(["-c", "pass"], [])
        self.assertEqual(self.jobs.dispatch.queue.claim_next(4)["job_id"], jid)
        owners = self.board._reservations()
        for serial in self.phones:
            with self.subTest(serial=serial):
                entry = self.board._reserved(serial, owners, self.phones)
                self.assertEqual((entry["job_id"], entry["kind"], entry["reserved"]),
                                 (jid, "global", True))
        self.submit(["-c", "pass", "--serial", "S2"], [])
        self.assertIsNone(self.jobs.dispatch.queue.claim_next(4))


if __name__ == "__main__":
    unittest.main()
