"""Leftover processes on an idle phone are stopped as the node samples it: a shell-user process whose parent is init
and that keeps using the CPU (a `toybox yes` left behind kept a phone at 2% and 51 °C). Each clean-up is recorded for
the phone and shows with it for a day. A phone a job or a remote session holds is not touched."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_fleet import FleetState
from node_lease import LeaseState
import phone_cleanup

SERIAL = "R5CT10ABCDE"
PHONEFARM = """#!/bin/sh
dir=$(dirname "$0")
case "$*" in
  "lease status --json") cat "$dir/lease.json" ;;
  "fleet --json"*) cat "$dir/fleet.json" ;;
  *) exit 2 ;;
esac
"""
# A phone's process table on disk: ps lists it (and the processes in `gone`, which end before a kill reaches them),
# kill -9 takes lines out of it and, as toybox kill does, fails for a process that is no longer there.
ADB = """#!/bin/sh
dir=$(dirname "$0")
echo "$*" >> "$dir/adb.calls"
[ "$1" = "-s" ] || exit 1
shift 2
case "$1 $2" in
  "shell ps") echo "  PID  PPID USER     TIME  ELAPSED ARGS"; cat "$dir/procs"; [ ! -f "$dir/gone" ] || cat "$dir/gone" ;;
  "shell kill") shift 3; status=0
    for pid in "$@"; do
      grep -q "^ *$pid " "$dir/procs" || { echo "kill: $pid: No such process" >&2; status=1; continue; }
      grep -v "^ *$pid " "$dir/procs" > "$dir/procs.new"; mv "$dir/procs.new" "$dir/procs"
    done
    exit $status ;;
  *) exit 1 ;;
esac
"""
PROCS = """ 4242     1 shell    12:31:05 12:40:00 toybox yes
 4243     1 shell    00:00:02 12:40:00 sleep 100000
 4244   900 shell    01:00:00 01:00:00 toybox yes
 4245     1 root   1-05:00:00 2-06:00:00 /system/bin/surfaceflinger
"""


class PhoneCleanupTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        for name, body in (("phonefarm", PHONEFARM), ("adb", ADB)):
            (self.dir / name).write_text(body)
            os.chmod(self.dir / name, 0o755)
        (self.dir / "procs").write_text(PROCS)
        (self.dir / "fleet.json").write_text(json.dumps({"schema": "phonefarm/fleet@1", "observation": "sampled", "devices": [
            {"serial": SERIAL, "online": True, "transport": "adb", "brand": "samsung", "model": "SM-S9180"}]}))
        self.log = self.dir / "data" / "_fleet" / "cleanups.jsonl"

    def lease(self, status, label="job", registered=True):
        """phonefarm's `lease status --json` with this phone held under `label`, or clear. A registered phone is held
        by its own device lock; one not in the registry by the global one."""
        held = status == "held"
        entries = [{"scope": "device" if registered else "global", "role": "device_lease", "serial": SERIAL,
                    "device_id": "phone" if registered else None, "state": "active", "label": label}] if held else []
        devices = [{"device_id": "phone", "aliases": [SERIAL], "status": status, "error": None}] if registered else []
        (self.dir / "lease.json").write_text(json.dumps({
            "schema_version": 1, "status": status, "bridge_held": held and not registered, "entries": entries, "error": None,
            "storage": {"schema_version": 1, "devlock_dir": str(self.dir / "locks"), "error": None},
            "device_scopes": {"schema": "phonefarm/device-scopes@1", "registry_revision": 1, "queue_waiting": False, "unknown": False,
                              "global": {"status": "held" if held and not registered else "clear", "error": None},
                              "devices": devices}}))

    def sample(self):
        leases = LeaseState({"phonefarm": (str(self.dir / "phonefarm"), str(self.dir)), "adb": (str(self.dir / "adb"), str(self.dir))})
        state = FleetState(str(self.dir), None, str(self.dir / "shots"), lease_state=leases, cleanup_log=str(self.log))
        self.addCleanup(state.close)
        state.request(wait=20)
        return state.snapshot()["devices"][0]

    def test_an_idle_phone_loses_its_leftovers_and_the_clean_up_is_kept(self):
        self.lease("clear")
        device = self.sample()
        left = [line.split()[0] for line in (self.dir / "procs").read_text().splitlines()]
        self.assertEqual(left, ["4243", "4244", "4245"], "only the shell process under init that keeps the CPU busy")
        [cleanup] = device["cleanups"]
        self.assertEqual(cleanup["processes"], [{"pid": 4242, "name": "toybox yes", "cpu_s": 12 * 3600 + 31 * 60 + 5, "elapsed_s": 12 * 3600 + 40 * 60}])
        self.assertLess(time.time() - cleanup["at"], 60)
        self.assertEqual([json.loads(line)["serial"] for line in self.log.read_text().splitlines()], [SERIAL])

        # The record outlives the node; one older than a day no longer shows.
        old = {"at": time.time() - 2 * 86400, "serial": SERIAL, "processes": [{"pid": 1, "name": "old", "cpu_s": 99, "elapsed_s": 99}]}
        with open(self.log, "a") as stream:
            stream.write(json.dumps(old) + "\n")
        self.assertEqual(self.sample()["cleanups"], [cleanup])

    def assert_untouched(self, device, why):
        self.assertEqual((self.dir / "procs").read_text(), PROCS, why)
        self.assertNotIn("cleanups", device, why)
        calls = (self.dir / "adb.calls").read_text() if (self.dir / "adb.calls").exists() else ""
        self.assertNotIn("ps", calls, "a held phone is not even looked at: " + why)
        self.assertFalse(self.log.exists(), why)

    def test_a_held_phone_is_not_touched(self):
        self.lease("held")
        self.assert_untouched(self.sample(), "a job")

    def test_a_phone_in_a_remote_control_session_is_not_touched(self):
        # 远程调试 runs `phonefarm remote-control owner`, which holds the phone under the label remote-control: its own
        # device lock when the phone is registered, the global lock when it is not.
        for registered in (True, False):
            self.lease("held", label="remote-control", registered=registered)
            self.assert_untouched(self.sample(), f"remote control, registered={registered}")

    def test_a_process_that_ends_before_its_kill_leaves_the_others_recorded(self):
        (self.dir / "gone").write_text(" 4250     1 shell    00:20:00 00:30:00 toybox yes\n")
        self.lease("clear")
        [cleanup] = self.sample()["cleanups"]
        self.assertEqual([process["pid"] for process in cleanup["processes"]], [4242], "the one stopped; not the one already gone")
        self.assertEqual([line.split()[0] for line in (self.dir / "procs").read_text().splitlines()], ["4243", "4244", "4245"])
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_times_read_as_toybox_prints_them(self):
        self.assertEqual([phone_cleanup.seconds(text) for text in ("05", "01:05", "02:01:05", "1-02:01:05")],
                         [5, 65, 7265, 93665])


if __name__ == "__main__":
    unittest.main()
