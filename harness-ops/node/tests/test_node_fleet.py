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
    def test_static_records_are_only_history_without_current_pf_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "devlock.queue").write_text("waiting-agent\n")
            (root / "devlock.d").mkdir()
            (root / "devlock.d/owner").write_text("measuring since 2026-10-22 10:00:00\n")
            (root / "devlock.log").write_text("2026-10-22 10:00:00 release old-measurement\n")
            lock = read_lock(directory)
            self.assertEqual(lock["status"], "unknown")
            self.assertIsNone(lock["held_by"])
            self.assertEqual(lock["queue"], [])
            self.assertEqual(lock["recent"][0]["label"], "old-measurement")

    def test_unknown_lock_allows_bounded_light_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "devlock.d").mkdir()
            state = FleetState(directory, directory, str(Path(directory) / "shots"))
            try:
                self.assertIsNone(state.snapshot()["devices"])
                self.assertIsNone(state._thread)
                empty = subprocess.CompletedProcess([], 1, '{"schema":"phonefarm/fleet@1","devices": []}', "")
                with patch("node_fleet.subprocess.run", return_value=empty) as run:
                    result = state.request(wait=1)
                    run.assert_called_once()
                    self.assertEqual(result["devices"], [])
                    self.assertIsNone(result["deferred"])
            finally:
                state.close()

    def test_storage_states_use_selected_phonefarm_for_light_and_full(self):
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
                def selected_run(command, **kwargs):
                    if command[1:] == ["lease", "status", "--json"]:
                        return Mock(stdout=json.dumps(value))
                    return subprocess.CompletedProcess(command, 1, '{"schema":"phonefarm/fleet@1","devices": []}', "")
                with patch("node_lease.subprocess.run", side_effect=selected_run) as run:
                    leases.snapshot(refresh=True, force=True)
                    result = state.request(wait=1, observe=True)
                    self.assertEqual(result["lock"]["status"], "unknown")
                    self.assertEqual(result["devices"], [])
                    self.assertIsNone(result["deferred"])
                    self.assertEqual(run.call_args.args[0][1:], ["fleet", "--json", "--screenshot-dir", str(root / "shots"), "--screenshot-every", "60"])
                self.assertFalse((root / "persistent").exists())
            finally:
                state.close()
            # The root's permanent v2 fence is not the active global owner.
            # Old queue files are history, never current ownership.
            (root / "persistent").mkdir()
            (root / "persistent" / "devlock.d").write_text('{"schema":"phonefarm/lease-domain-fence@2"}')
            queue_dir = root / "persistent" / "v2" / "global"
            queue_dir.mkdir(parents=True)
            (queue_dir / "devlock.queue").write_text("waiting\n")
            value["storage"].update(readiness="ready", error=None, layout_version=2,
                                    domain_id="d" * 32, queue_dir=str(queue_dir))
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                leases.snapshot(refresh=True, force=True)
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                with patch("node_fleet.subprocess.run", return_value=Mock(
                        returncode=1, stdout='{"schema":"phonefarm/fleet@1","devices": []}')) as run:
                    result = state.request(wait=1)
                    self.assertEqual(result["lock"]["queue"], [])
                    self.assertEqual(result["lock"]["status"], "free")
                    self.assertEqual(result["lock"]["lease"]["storage"]["devlock_dir"], str(root / "persistent"))
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
                empty = Mock(returncode=1, stdout='{"schema":"phonefarm/fleet@1","devices": []}')
                with patch("node_fleet.subprocess.run", return_value=empty) as run:
                    result = state.request(wait=1, observe=True)
                    self.assertEqual(result["lock"]["status"], "held")
                    self.assertIn("--screenshot-dir", run.call_args.args[0])
                    self.assertEqual(result["devices"], [])
                    self.assertIsNone(result["deferred"])
                    run.assert_called_once()
            finally:
                state.close()
            value.update(status="clear", entries=[])
            del value["storage"]["queue_dir"]
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                self.assertEqual(leases.snapshot(refresh=True, force=True)["status"], "clear")
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                empty = Mock(returncode=1, stdout='{"schema":"phonefarm/fleet@1","devices": []}')
                with patch("node_fleet.subprocess.run", return_value=empty) as run:
                    result = state.request(wait=1)
                    self.assertEqual(result["lock"]["status"], "free")
                    self.assertIsNone(result["lock"]["error"])
                    self.assertEqual(result["devices"], [])
                    run.assert_called_once()
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
