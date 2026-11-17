"""A job on one phone holds that phone only: jobs on the other phones start, and a job waiting for it names the holder."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_node_games as fixture


def snapshot(global_holder=None, device_holders=None):
    """PF lease status for three registered phones; any device lock also holds the farm lock shared."""
    device_holders = device_holders or {}
    entries = ([{"scope": "global", "device_id": None, "serial": None, "job_id": global_holder}] if global_holder else [])
    entries += [{"scope": "device", "device_id": device, "serial": device.lower() + "1", "job_id": job}
                for device, job in device_holders.items()]
    return {"schema_version": 1, "status": "held" if entries else "clear", "bridge_held": bool(entries),
            "entries": entries, "error": None,
            "device_scopes": {"schema": "phonefarm/device-scopes@1", "registry_revision": 3,
                              "unknown": False, "queue_waiting": False,
                              "global": {"status": "held" if global_holder else "clear", "error": None},
                              "devices": [{"device_id": device, "aliases": [device.lower() + "1"],
                                           "status": "held" if device in device_holders else "clear", "error": None}
                                          for device in ("A", "B", "C")]}}


def game_deploy(serial):
    return {"bin": "phonefarm", "device_access": "write",
            "argv": ["game-deploy", "--request", "request.json", "--serial", serial, "--json"]}


def adb(serial):
    return {"bin": "adb", "device_access": "write", "argv": ["-s", serial, "shell", "true"]}


class DeviceLeaseHolderTests(unittest.TestCase):
    def setUp(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        self.jobs = env.jobs
        self.dispatch = env.jobs.dispatch

    def queue(self, specs):
        ids = {}
        for name, spec in specs.items():
            row, _ = self.dispatch.queue.reserve(name, name, spec, [])
            self.dispatch.queue.ready(row["job_id"])
            ids[name] = row["job_id"]
        return ids

    def tick(self, lease):
        started = []
        with mock.patch.object(self.jobs.leases, "snapshot", return_value=lease), \
                mock.patch.object(self.dispatch, "launch", side_effect=lambda row: started.append(row["job_id"])):
            self.dispatch.tick()
        return started

    def waiting(self, jid, lease):
        return self.dispatch.waiting(self.dispatch.queue.get(jid), lease=lease)

    def test_a_job_holding_one_phone_does_not_hold_the_other_two(self):
        lease = snapshot(device_holders={"A": "holder-on-a"})
        ids = self.queue({"deploy-b": game_deploy("b1"), "adb-c": adb("c1"),
                          "adb-a": adb("a1"), "deploy-a": game_deploy("a1")})
        self.assertEqual(sorted(self.tick(lease)), sorted([ids["deploy-b"], ids["adb-c"]]),
                         "phones B and C start while another job holds phone A")
        for name in ("adb-a", "deploy-a"):
            self.assertEqual(self.dispatch.queue.get(ids[name])["state"], "queued")
            waiting = self.waiting(ids[name], lease)
            self.assertEqual((waiting["kind"], waiting["blocking_jobs"]), ("device_lease", ["holder-on-a"]), waiting)

    def test_the_farm_lock_holder_is_named_to_every_phone_it_holds(self):
        lease = snapshot(global_holder="holder-of-farm")
        ids = self.queue({"deploy-b": game_deploy("b1"), "adb-c": adb("c1")})
        self.assertEqual(self.tick(lease), [])
        for jid in ids.values():
            waiting = self.waiting(jid, lease)
            self.assertEqual((waiting["kind"], waiting["blocking_jobs"]), ("device_lease", ["holder-of-farm"]), waiting)
        whole = {"bin": "adb", "device_access": "write", "argv": ["devices"]}
        row, _ = self.dispatch.queue.reserve("farm", "farm", whole, [])
        self.dispatch.queue.ready(row["job_id"])
        held = snapshot(device_holders={"A": "holder-on-a", "C": "holder-on-c"})
        self.assertEqual(sorted(self.waiting(row["job_id"], held)["blocking_jobs"]), ["holder-on-a", "holder-on-c"],
                         "a call naming no phone waits for every phone in use")


class DeviceLockCommandTests(unittest.TestCase):
    def setUp(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        self.jobs = env.jobs
        for name in ("adb", "fetch_apk", "devlock"):
            self.jobs.bins[name] = ("/usr/bin/" + name, str(env.root))

    def command(self, spec):
        with mock.patch.object(self.jobs.dispatch, "start"):
            jid = self.jobs.start(spec, b"")["id"]
        return json.loads(Path(self.jobs.path(jid, "prepared.json")).read_text())["command"], "job-" + jid

    def test_a_raw_call_naming_one_phone_locks_that_phone(self):
        command, owner = self.command({"bin": "adb", "argv": ["-s", "b1", "shell", "ls", "-s", "/sdcard"],
                                       "device_access": "none"})
        self.assertEqual(command[:6], ["/usr/bin/devlock", "run", owner, "--serial", "b1", "--"], command)
        command, owner = self.command({"bin": "fetch_apk", "argv": ["https://example.invalid/a.apk", "1", "c1", "0" * 64]})
        self.assertEqual(command[:6], ["/usr/bin/devlock", "run", owner, "--serial", "c1", "--"], command)
        command, owner = self.command({"bin": "adb", "argv": ["devices"], "device_access": "none"})
        self.assertEqual(command[:4], ["/usr/bin/devlock", "run", owner, "--"], "naming no phone still locks the farm")


if __name__ == "__main__":
    unittest.main()
