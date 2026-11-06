"""What a job sends to a phone: every adb call a job makes is written to its device record, then the real adb runs
unchanged (same arguments, input, output and exit status). No phone: the real adb is a fixture script."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import adb_record  # noqa: E402
import job_dispatch  # noqa: E402
import job_runtime  # noqa: E402
from node_service import Jobs  # noqa: E402
from test_job_group_observation import until  # noqa: E402

FAKE_ADB = """#!/bin/sh
echo "real-adb $*"
cat >/dev/null
exit "${FAKE_ADB_EXIT:-0}"
"""


class AdbRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        (self.tmp / "bin").mkdir()
        self.real = self.tmp / "bin" / "adb"
        self.real.write_text(FAKE_ADB)
        self.real.chmod(0o755)
        self.apk = self.tmp / "game.apk"
        self.apk.write_bytes(b"APK!")

    def test_each_call_is_described_by_what_it_sends(self):
        lines = {name: adb_record.describe(args, now=1.0) for name, args in {
            "push": ["-s", "S1", "push", str(self.apk), "/data/local/tmp/game.apk"],
            "install": ["-s", "S1", "install", "-r", "-g", str(self.apk)],
            "shell": ["-s", "S1", "shell", "am", "force-stop", "com.example"],
            "exec": ["exec-out", "screencap", "-p"],
            "pull": ["pull", "/sdcard/a.png", "out/a.png"],
            "other": ["-s", "S1", "reverse", "tcp:8080", "tcp:8080"],
            "devices": ["devices", "-l"], "wait": ["-s", "S1", "wait-for-device"]}.items()}
        file = {"path": str(self.apk), "size": 4, "sha256": hashlib.sha256(b"APK!").hexdigest()}
        self.assertEqual(lines["push"], {"at": 1.0, "serial": "S1", "command": "push", "files": [file], "to": "/data/local/tmp/game.apk"})
        self.assertEqual(lines["install"], {"at": 1.0, "serial": "S1", "command": "install", "files": [file], "flags": ["-r", "-g"]})
        self.assertEqual(lines["shell"]["text"], "am force-stop com.example")
        self.assertEqual((lines["exec"]["command"], lines["exec"]["text"]), ("exec-out", "screencap -p"))
        self.assertEqual((lines["pull"]["source"], lines["pull"]["to"]), ("/sdcard/a.png", "out/a.png"))
        self.assertEqual(lines["other"]["args"], ["tcp:8080", "tcp:8080"])
        self.assertIsNone(lines["devices"], "listing phones sends nothing to one")
        self.assertIsNone(lines["wait"])

    def run_adb(self, args, record, **env):
        return subprocess.run([str(NODE / "tools" / "adb-record" / "adb"), *args], input="stdin text", capture_output=True, text=True,
                              env={**os.environ, "PF_REAL_ADB": str(self.real), "PF_ADB_RECORD": str(record),
                                   "PF_ADB_RECORD_PYTHON": sys.executable, **env}, timeout=30)

    def test_the_real_adb_runs_unchanged_after_the_call_is_recorded(self):
        record = self.tmp / "device-io.jsonl"
        done = self.run_adb(["-s", "S1", "shell", "echo", "hi"], record, FAKE_ADB_EXIT="3")
        self.assertEqual((done.returncode, done.stdout), (3, "real-adb -s S1 shell echo hi\n"))
        self.assertEqual([json.loads(line)["text"] for line in record.read_text().splitlines()], ["echo hi"])
        # A record that cannot be written does not stop the call; a full record stops recording, not the job.
        self.assertEqual(self.run_adb(["shell", "true"], self.tmp / "missing" / "record.jsonl").returncode, 0)
        record.write_bytes(b"x" * adb_record.RECORD_MAX)
        self.assertEqual(self.run_adb(["shell", "true"], record).returncode, 0)
        self.assertEqual(record.stat().st_size, adb_record.RECORD_MAX)

    def test_a_job_finds_the_recorder_first_and_never_records_itself(self):
        env = {"PATH": os.pathsep.join([job_dispatch.ADB_RECORDER, str(self.tmp / "bin"), "/usr/bin"])}
        job_dispatch.record_adb(env, "adb", "/job/out/.device-io.jsonl")
        self.assertEqual(env["PF_REAL_ADB"], str(self.real))
        self.assertEqual(env["PATH"].split(os.pathsep)[:2], [job_dispatch.ADB_RECORDER, str(self.tmp / "bin")])
        bare = {"PATH": "/nonexistent"}
        job_dispatch.record_adb(bare, "adb", "/job/out/.device-io.jsonl")
        self.assertEqual(bare, {"PATH": "/nonexistent"}, "without a real adb nothing changes")

    def test_a_jobs_adb_calls_land_in_its_output_directory(self):
        root = str(self.tmp / "node")
        jobs = Jobs(root, {"host": (sys.executable, root), "adb": (str(self.real), root)}, max_running=1)
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None
        jobs.leases.snapshot = lambda **kwargs: {"status": "clear", "entries": [], "error": None}
        program = ("import pathlib,subprocess,sys; out=pathlib.Path(sys.argv[1]); (out/'lib.so').write_bytes(b'ELF');"
                   "said=subprocess.run(['adb','-s','S1','push',str(out/'lib.so'),'/data/local/tmp/lib.so'],capture_output=True,text=True).stdout;"
                   "subprocess.run(['adb','-s','S1','shell','input','tap','10','20'],check=True); (out/'said.txt').write_text(said)")
        jid = jobs.start({"bin": "host", "argv": ["-c", program, "@out"], "device_access": "write", "resources": ["device:S1"]}, b"")["id"]
        jobs.dispatch.tick()
        until(lambda: jobs._read_meta(jid).get("phase") == "finished" and not job_runtime.runner_alive(jobs._read_root, jid))
        self.assertEqual(jobs._read_meta(jid)["exit"], 0)
        out = Path(jobs.path(jid, "out"))
        self.assertTrue((out / "said.txt").read_text().startswith("real-adb -s S1 push "))
        lines = [json.loads(line) for line in (out / ".device-io.jsonl").read_text().splitlines()]
        self.assertEqual([(line["command"], line["serial"]) for line in lines], [("push", "S1"), ("shell", "S1")])
        self.assertEqual((lines[0]["files"][0]["size"], lines[0]["to"], lines[1]["text"]), (3, "/data/local/tmp/lib.so", "input tap 10 20"))


if __name__ == "__main__":
    unittest.main()
