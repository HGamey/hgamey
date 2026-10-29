"""Current kernel ownership reaches the Node readers without old marker files."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import node_remote
from node_fleet import FleetState


class FDStatusReaders(unittest.TestCase):
    def test_fleet_ignores_old_owner_queue_and_uses_current_pf_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "devlock.d").mkdir()
            (root / "devlock.d/owner").write_text("old-holder since yesterday\n")
            (root / "devlock.queue").write_text("old-waiter\n")
            value = {"status": "held", "entries": [{"label": "live-holder", "since": "today"}],
                     "storage": {"schema_version": 1, "readiness": "ready", "devlock_dir": tmp}, "error": None}
            leases = Mock()
            leases.snapshot.side_effect = lambda **kw: value
            fleet = FleetState(tmp, None, str(root / "shots"), lease_state=leases)
            try:
                observed = fleet.snapshot()["lock"]
                self.assertEqual(observed["held_by"], "live-holder")
                self.assertEqual(observed["queue"], [])
                value.update(status="clear", entries=[])
                self.assertEqual(fleet.snapshot()["lock"]["status"], "free")
                value.update(status="unknown", error="fd probe failed")
                self.assertEqual(fleet.snapshot()["lock"]["error"], "fd probe failed")
            finally:
                fleet.close()

    def test_remote_reads_current_held_clear_and_error(self):
        value = {"status": "held", "entries": [{"scope": "device", "device_id": "a",
                 "label": "current-a", "serial": "usb-a", "since": "today", "job_id": "job-a"}],
                 "device_scopes": {"devices": [{"device_id": "a", "aliases": ["usb-a", "wifi-a"]}]}}
        leases = Mock()
        leases.snapshot.side_effect = lambda **kw: value
        occupancy = node_remote.Occupancy(node_remote.storage_from_leases(leases))
        owners = occupancy.holders()
        self.assertEqual(owners[0]["label"], "current-a")
        self.assertFalse(owners[0]["global"])
        self.assertIn("wifi-a", owners[0]["aliases"])
        value.update(status="clear", entries=[])
        self.assertEqual(occupancy.holders(), [])
        value.update(status="unknown", error="fd probe failed")
        with self.assertRaises(node_remote.RemoteError) as error:
            occupancy.holders()
        self.assertEqual(error.exception.status, 503)
        self.assertIn("fd probe failed", str(error.exception))
