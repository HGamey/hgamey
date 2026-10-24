import os
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_fleet import FleetState, read_lock
from node_lease import LeaseState


class FleetStateTests(unittest.TestCase):
    def test_incomplete_or_stale_lock_is_never_free_and_queue_is_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "devlock.queue").write_text("waiting-agent\n")
            self.assertEqual(read_lock(directory)["queue"], ["waiting-agent"])
            self.assertEqual(read_lock(directory)["status"], "free")
            (root / "devlock.d").mkdir()
            self.assertEqual(read_lock(directory)["status"], "unknown")
            owner = root / "devlock.d" / "owner"
            owner.write_text("measuring since 2026-10-22 10:00:00\n")
            os.utime(owner, (time.time() - 700, time.time() - 700))
            lock = read_lock(directory)
            self.assertEqual((lock["status"], lock["held_by"], lock["stale"]), ("held", "measuring", True))

    def test_unknown_lock_defers_without_touching_phone_and_unknown_is_not_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "devlock.d").mkdir()
            state = FleetState(directory, directory, str(Path(directory) / "shots"))
            try:
                self.assertIsNone(state.snapshot()["devices"])
                self.assertIsNone(state._thread)
                with patch("node_fleet.subprocess.run") as run:
                    result = state.request(wait=1)
                    run.assert_not_called()
                    self.assertIsNone(result["devices"])
                    self.assertTrue(result["deferred"])
            finally:
                state.close()

    def test_storage_contract_blocks_sampling_and_uses_selected_phonefarm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leases = LeaseState({"phonefarm": (str(root / "selected-phonefarm"), directory)}, env={
                "PF_STATE_ROOT": str(root / "fixed-state"), "ADB_BIN": "/fixture/selected-adb",
                "DEVLOCK_LABEL": "inherited-job", "DEVLOCK_INSTANCE_ID": "inherited-instance"})
            value = {"schema_version": 1, "status": "clear", "entries": [], "error": None}
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                self.assertEqual(leases.snapshot(refresh=True, force=True)["status"], "unknown")
            value["storage"] = {"schema_version": 1, "mode": "default", "devlock_dir": str(root / "persistent"),
                                "readiness": "migration_required", "error": "old participants must stop"}
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))) as run:
                    leases.snapshot(refresh=True, force=True)
                    result = state.request(wait=1, observe=True)
                    self.assertEqual(result["lock"]["status"], "unknown")
                    self.assertIsNone(result["devices"])
                    self.assertTrue(result["deferred"])
                    self.assertTrue(all(call.args[0][1:] == ["lease", "status", "--json"]
                                        for call in run.call_args_list))
                self.assertFalse((root / "persistent").exists())
            finally:
                state.close()
            # A real resolved directory supplies queue/history, never an empty old default.
            (root / "persistent").mkdir()
            (root / "persistent" / "devlock.queue").write_text("waiting\n")
            value["storage"].update(readiness="ready", error=None)
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                leases.snapshot(refresh=True, force=True)
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                with patch("node_fleet.subprocess.run", return_value=Mock(
                        returncode=1, stdout='{"schema":"phonefarm/fleet@1","devices": []}')) as run:
                    result = state.request(wait=1)
                    self.assertEqual(result["lock"]["queue"], ["waiting"])
                    self.assertEqual(run.call_args.args[0], [str(root / "selected-phonefarm"), "fleet", "--json"])
                    self.assertEqual(run.call_args.kwargs["env"], {
                        "PF_STATE_ROOT": str(root / "fixed-state"), "ADB_BIN": "/fixture/selected-adb"})
            finally:
                state.close()
            value.update(status="held", entries=[{"label": "nested-owner"}])
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                leases.snapshot(refresh=True, force=True)
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                with patch("node_fleet.subprocess.run") as run:
                    result = state.request(wait=1, observe=True)
                    self.assertEqual(result["lock"]["status"], "held")
                    self.assertIn("占用", result["deferred"])
                    run.assert_not_called()
            finally:
                state.close()

    def test_busy_and_error_keep_success_age_but_valid_empty_sample_replaces_devices(self):
        def response(code, **fields):
            return subprocess.CompletedProcess([], code, json.dumps({"schema": "phonefarm/fleet@1", **fields}), "")

        with tempfile.TemporaryDirectory() as directory:
            state = FleetState(directory, directory, str(Path(directory) / "shots"))
            busy = response(3, observation="deferred", reason="lease busy", devices=None)
            device = {"serial": "fixture-only", "online": True, "thermal": {"soc_max_c": 42}}
            try:
                with patch("node_fleet.subprocess.run", side_effect=[
                    busy, response(0, observation="sampled", devices=[device]), busy,
                    response(3, observation="error", reason="sampling timed out", devices=None),
                    response(1, devices=[]),  # Legacy successful empty response.
                ]) as run:
                    def attempt():
                        with state._condition:
                            state._next_at = 0
                        return state.request(wait=1)

                    first = attempt()
                    self.assertIsNone(first["devices"])
                    self.assertIsNone(first["sampled_at"])
                    self.assertEqual(first["deferred"], "lease busy")
                    sampled = attempt()
                    self.assertEqual(sampled["devices"], [device])
                    with state._condition:
                        state._sampled_at -= 100
                        sampled_at = state._sampled_at
                    deferred = attempt()
                    self.assertEqual(deferred["devices"], [device])
                    self.assertEqual(deferred["sampled_at"], sampled_at)
                    self.assertTrue(deferred["stale"])
                    self.assertIsNone(deferred["error"])
                    self.assertEqual(deferred["deferred"], "lease busy")
                    failed = attempt()
                    self.assertEqual(failed["sampled_at"], sampled_at)
                    self.assertEqual(failed["devices"], [device])
                    self.assertIn("sampling timed out", failed["error"])
                    self.assertIsNone(failed["deferred"])
                    empty = attempt()
                    self.assertEqual(empty["devices"], [])
                    self.assertGreater(empty["sampled_at"], sampled_at)
                    self.assertFalse(empty["stale"])
                    self.assertIsNone(empty["error"])
                    self.assertIsNone(empty["deferred"])
                    self.assertIsNotNone(empty["attempted_at"])
                    self.assertEqual(len(state.history()), 1)
                    self.assertEqual(run.call_count, 5)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
