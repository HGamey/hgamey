"""Real local child processes only; no node service or device access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from artifact_io import SafeRoot
import job_runtime as runtime


class JobRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SafeRoot(self.root)
        self.addCleanup(self.store.close)
        self.sequence = 0

    def job(self):
        self.sequence += 1
        jid = f"{self.sequence:012x}"
        (self.root / jid).mkdir()
        self.store.write_json(jid + "/meta.json", {
            "id": jid, "pid": None, "state": "running", "phase": "starting",
            "execution": {"kind": "supervised-v1"}})
        return jid

    def read(self, jid):
        return self.store.read_json(jid + "/meta.json")

    def until(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("local worker did not reach expected state")

    def ready(self, jid):
        return b"ready" in self.store.read_bytes(jid + "/log", 4096)

    def start(self, jid, program, short_delays=False):
        command = [sys.executable, "-u", "-c", program]
        args = (self.store, jid, str(self.root / jid), command, str(self.root), dict(os.environ))
        if short_delays:
            # Explicit test-only entry into the same worker function. The shipped
            # worker CLI has no delay flag or environment override.
            def test_command(job_dir, lock_fd):
                code = (f"import sys;sys.path.insert(0,{str(NODE)!r});import job_runtime;"
                        f"sys.exit(job_runtime._worker({job_dir!r},{lock_fd},(.12,.12)))")
                return [sys.executable, "-c", code]
            with patch.object(runtime, "_worker_command", test_command):
                return runtime.launch(*args)
        return runtime.launch(*args)

    def finished(self, jid, worker):
        meta = self.until(lambda: (m if (m := self.read(jid)).get("state") == "finished" else None))
        worker.wait(5)
        self.assertFalse(runtime.runner_alive(self.store, jid))
        self.assertEqual(meta["phase"], "finished")
        self.assertNotIn("phase", meta["execution"])
        return meta

    def test_new_launcher_instance_can_cancel_worker_after_original_exits(self):
        jid = self.job()
        program = ("import signal,time,sys;signal.alarm(5);"
                   "signal.signal(signal.SIGINT,lambda *a:(print('restoring',flush=True),sys.exit(0)));"
                   "print('ready',flush=True);time.sleep(5)")
        code = (f"import os,sys;sys.path.insert(0,{str(NODE)!r});"
                "from artifact_io import SafeRoot;import job_runtime;"
                f"s=SafeRoot({str(self.root)!r});"
                f"job_runtime.launch(s,{jid!r},{str(self.root / jid)!r},"
                f"{[sys.executable, '-u', '-c', program]!r},{str(self.root)!r},dict(os.environ));"
                "os._exit(0)")
        subprocess.run([sys.executable, "-c", code], check=True, timeout=5)
        self.until(lambda: self.ready(jid))
        self.store.close()
        self.store = SafeRoot(self.root)  # Same files, entirely new caller state.
        self.addCleanup(self.store.close)
        self.assertTrue(runtime.runner_alive(self.store, jid))
        request = runtime.request_cancel(self.store, jid)
        self.assertEqual(request["mode"], "graceful")
        meta = self.until(lambda: (m if (m := self.read(jid)).get("state") == "finished" else None))
        self.until(lambda: not runtime.runner_alive(self.store, jid))
        self.assertEqual((meta["outcome"], meta["recovery"]["status"], meta["exit"]), ("cancelled", "not_reported", 0))
        self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT"])
        self.assertIn(b"restoring", self.store.read_bytes(jid + "/log", 4096))
        self.assertEqual(self.store.read_json(jid + "/launch.json").keys(), {"command", "cwd"})

    def test_graceful_is_once_and_force_only_upgrades_to_term_then_kill(self):
        jid = self.job()
        program = ("import signal,time;signal.alarm(5);"
                   "signal.signal(signal.SIGINT,lambda *a:print('INT',flush=True));"
                   "signal.signal(signal.SIGTERM,lambda *a:print('TERM',flush=True));"
                   "print('ready',flush=True);time.sleep(5)")
        worker = self.start(jid, program, short_delays=True)
        self.until(lambda: self.ready(jid))
        first = runtime.request_cancel(self.store, jid)
        self.until(lambda: self.read(jid).get("cancel", {}).get("signals"))
        time.sleep(.3)  # Longer than the injected force intervals: graceful never escalates.
        self.assertEqual([s["name"] for s in self.read(jid)["cancel"]["signals"]], ["SIGINT"])
        self.assertEqual(runtime.request_cancel(self.store, jid)["requested_at"], first["requested_at"])
        runtime.request_cancel(self.store, jid, force=True)
        self.assertEqual(runtime.request_cancel(self.store, jid)["mode"], "escalate")
        meta = self.finished(jid, worker)
        self.assertEqual([s["name"] for s in meta["cancel"]["signals"]], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual((meta["outcome"], meta["recovery"]["status"], meta["exit"], meta["execution"]["returncode"]),
                         ("cancelled", "required", 137, -9))
        self.assertEqual(self.store.read_bytes(jid + "/exitcode", 128), b"137")

    def test_cancel_before_execution_and_child_start_failure_have_no_fake_exitcode(self):
        jid = self.job()
        runtime.request_cancel(self.store, jid)
        worker = self.start(jid, "raise RuntimeError('must not run')")
        meta = self.finished(jid, worker)
        self.assertEqual((meta["outcome"], meta["recovery"]["status"]), ("cancelled", "not_needed"))
        self.assertEqual(meta["cancel"]["status"], "not_applied")
        self.assertIsNone(meta["execution"]["child_pid"])
        self.assertFalse((self.root / jid / "exitcode").exists())
        jid = self.job()
        worker = runtime.launch(self.store, jid, str(self.root / jid), [str(self.root / "missing-command")],
                                str(self.root), dict(os.environ))
        meta = self.finished(jid, worker)
        self.assertEqual(meta["outcome"], "failed")
        self.assertIsNone(meta["exit"])
        self.assertIn("FileNotFoundError", meta["error"])
        self.assertFalse((self.root / jid / "exitcode").exists())
        self.assertEqual(self.store.read_bytes(jid + "/log", 128), b"")

    def test_duplicate_launch_blocked_and_child_does_not_keep_dead_worker_lock(self):
        jid = self.job()
        done = self.root / "child-done"
        worker = self.start(jid, f"import time,pathlib;print('ready',flush=True);time.sleep(.5);pathlib.Path({str(done)!r}).touch()")
        self.until(lambda: self.ready(jid))
        self.assertTrue(runtime.runner_alive(self.store, jid))
        with self.assertRaisesRegex(ValueError, "live worker"):
            self.start(jid, "raise RuntimeError('duplicate')")
        worker.kill()  # Actual Popen owned by this test, never a PID recovered from disk.
        worker.wait(5)
        self.assertFalse(runtime.runner_alive(self.store, jid))
        self.assertEqual(self.read(jid)["state"], "running")  # Service must reconcile interruption.
        with self.assertRaisesRegex(ValueError, "already launched"):
            self.start(jid, "raise RuntimeError('replay')")
        self.until(done.exists)


if __name__ == "__main__":
    unittest.main()
