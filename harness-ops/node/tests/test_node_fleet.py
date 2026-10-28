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
                    self.assertEqual(run.call_args.args[0][1:], ["fleet", "--json"])
                self.assertFalse((root / "persistent").exists())
            finally:
                state.close()
            # The root's permanent v2 fence is not the active global owner.
            # Only PF's resolved queue directory supplies current queue/history.
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
                    self.assertEqual(result["lock"]["queue"], ["waiting"])
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
                    self.assertEqual(result["devices"], [])
                    self.assertIsNone(result["deferred"])
                    run.assert_called_once()
            finally:
                state.close()
            value.update(status="clear", entries=[])
            del value["storage"]["queue_dir"]
            with patch("node_lease.subprocess.run", return_value=Mock(stdout=json.dumps(value))):
                self.assertEqual(leases.snapshot(refresh=True, force=True)["status"], "unknown")
            state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
            try:
                empty = Mock(returncode=1, stdout='{"schema":"phonefarm/fleet@1","devices": []}')
                with patch("node_fleet.subprocess.run", return_value=empty) as run:
                    result = state.request(wait=1)
                    self.assertEqual(result["lock"]["status"], "unknown")
                    self.assertIn("queue_dir", result["lock"]["error"])
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

    def test_an_abandoned_fleet_observation_is_retired_through_phonefarm_and_nothing_else_is(self):
        # phonefarm keeps the lease of an observation whose observer died (a timeout, a node restart) for
        # idle verification, and every device-write job waits behind it. Fleet only reads, so the sampler
        # retires that one lease through phonefarm's own recovery command and samples again. A job's lease,
        # a live observer or another kind of lease is left alone, and a failed recovery is not hammered.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue_dir = root / "persistent" / "v2" / "global"
            queue_dir.mkdir(parents=True)
            storage = {"schema_version": 1, "mode": "default", "devlock_dir": str(root / "persistent"),
                       "readiness": "ready", "error": None, "layout_version": 2, "domain_id": "d" * 32,
                       "queue_dir": str(queue_dir)}
            lease = {"schema_version": 1, "status": "blocked", "entries": [], "error": None, "storage": storage}
            leases = LeaseState({"phonefarm": (str(root / "pf"), directory)},
                                env={"ADB_BIN": "/fixture/adb", "DEVLOCK_LABEL": "inherited-job"})
            fleet = subprocess.CompletedProcess([], 1, json.dumps({"schema": "phonefarm/fleet@1", "devices": []}), "")
            recovered = {"code": 0}

            def node_run(command, **kwargs):  # node_lease and node_fleet share subprocess.run
                if command[1:] == ["lease", "status", "--json"]:
                    return Mock(stdout=json.dumps(lease))
                if command[1:3] == ["lease", "--recover-abandoned"]:
                    if recovered["code"] == 0:
                        lease.update(status="clear", entries=[])
                    return subprocess.CompletedProcess(command, recovered["code"], "", "")
                return fleet

            def observation(**fields):
                return {"directory": "devlock.d", "serial": "@observation", "label": "fleet", "state": "abandoned",
                        "host_process": {"status": "exited", "pid": 4242}, **fields}

            recover = [str(root / "pf"), "lease", "--recover-abandoned", "--confirm-device-idle",
                       "--serial", "@observation", "--timeout-ms", "5000"]
            def calls(run):
                return [call for call in run.call_args_list if call.args[0][1:] != ["lease", "status", "--json"]]

            with patch("node_lease.subprocess.run", side_effect=node_run):
                for other in (observation(serial="S1", label="job-0123456789ab"), observation(state="active"),
                              observation(directory="phonefarm-lease.d")):
                    lease.update(status="blocked", entries=[other])
                    leases.snapshot(refresh=True, force=True)
                    state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
                    try:
                        with patch("node_fleet.subprocess.run", side_effect=node_run) as run:
                            result = state.request(wait=1)
                            self.assertIsNone(result["deferred"], other)
                            self.assertEqual(result["devices"], [])
                            self.assertEqual([call.args[0][1] for call in calls(run)], ["fleet"])
                    finally:
                        state.close()

                lease.update(status="blocked", entries=[observation()])
                leases.snapshot(refresh=True, force=True)
                state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
                try:
                    with patch("node_fleet.subprocess.run", side_effect=node_run) as run:
                        state.request(wait=2)
                        deadline = time.time() + 5
                        while state.snapshot()["sampled_at"] is None and time.time() < deadline:
                            time.sleep(0.01)  # a successful recovery samples right away
                        sampled = state.snapshot()
                        self.assertTrue(sampled["reclaimed"]["ok"])
                        self.assertEqual((sampled["devices"], sampled["lock"]["status"]), ([], "free"))
                        self.assertEqual([call.args[0][1] for call in calls(run)], ["lease", "fleet"])
                        self.assertEqual(calls(run)[0].args[0], recover)
                        self.assertEqual(calls(run)[0].kwargs["env"], {"ADB_BIN": "/fixture/adb"})
                        self.assertTrue(calls(run)[1].kwargs["start_new_session"])
                finally:
                    state.close()

                # A minute later phonefarm refuses (say the observer's group is still alive): the sampler
                # waits and does not ask again before RECLAIM_EVERY_S.
                leases._reclaim_after = 0.0
                recovered["code"] = 2
                lease.update(status="blocked", entries=[observation()])
                leases.snapshot(refresh=True, force=True)
                state = FleetState(directory, None, str(root / "shots"), lease_state=leases)
                try:
                    with patch("node_fleet.subprocess.run", side_effect=node_run) as run:
                        self.assertFalse(state.request(wait=2)["reclaimed"]["ok"])
                        with state._condition:
                            state._next_at = 0
                        self.assertTrue(state.request(wait=2)["deferred"])
                        self.assertEqual(len(calls(run)), 1)
                finally:
                    state.close()


if __name__ == "__main__":
    unittest.main()
