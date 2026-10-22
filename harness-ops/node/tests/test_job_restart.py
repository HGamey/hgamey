"""A real HTTP process crash must not lose the worker or its cancel channel."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

NODE = str(Path(__file__).resolve().parents[1])
SERVER = '''
import sys
from http.server import ThreadingHTTPServer
sys.path.insert(0, sys.argv[1])
from node_service import Jobs, make_handler
jobs = Jobs(sys.argv[2], {"phonefarm": (sys.executable, sys.argv[2])})
server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, jobs, "restart-test-only"))
print(server.server_port, flush=True)
server.serve_forever()
'''
CHILD = '''
import signal, time, os
from pathlib import Path
def stop(*args):
    print("cleanup marker", flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
assert os.environ["PF_JOB_ID"] == os.environ["DEVLOCK_LABEL"].removeprefix("job-")
assert Path(os.environ["PF_JOB_LEASE_DIR"]).is_dir()
print("ready marker", os.environ["DEVLOCK_LABEL"], flush=True)
while True: time.sleep(0.1)
'''


class JobRestartTests(unittest.TestCase):
    def test_preparation_cancel_remains_terminal_after_late_metadata_and_restart(self):
        sys.path.insert(0, NODE)
        from node_service import Jobs
        with tempfile.TemporaryDirectory() as root:
            jobs = Jobs(root, {"phonefarm": (sys.executable, root)})
            jobs.dispatch.start = lambda: None
            meta = jobs.start({"bin": "phonefarm", "device_access": "none", "argv": ["-c", "pass"]}, b"", request_id="cancel-race")
            stale = jobs._read_meta(meta["id"])
            self.assertTrue(jobs.cancel(meta["id"])["accepted"])
            # Intake finishes its file write, then dies before reconciling the DB.
            jobs._write_meta(meta["id"], stale)
            restarted = Jobs(root, {})
            result = restarted.status(meta["id"])
            self.assertEqual((result["state"], result["phase"], result["outcome"]), ("finished", "finished", "cancelled"))
            self.assertEqual(result["recovery"]["status"], "not_needed")
            self.assertEqual(result["cancel"]["signals"], [])
            self.assertEqual(result["queue"]["state"], "done")

    def test_service_entry_checks_configuration_before_starting_children(self):
        run = subprocess.run([sys.executable, str(Path(NODE) / "node_service.py")],
                             env={**os.environ, "PHONEFARM_NODE_TOKEN": ""}, capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 1)
        self.assertIn("PHONEFARM_NODE_TOKEN", run.stderr)
        self.assertNotIn("Traceback", run.stderr)

    def test_goal_confirmation_keeps_job_before_worker_publishes_pid(self):
        sys.path.insert(0, NODE)
        from dash_goals import Goals
        with tempfile.TemporaryDirectory() as root:
            goals = Goals.__new__(Goals)
            goals.find = lambda rid: (root, False)
            goals.lock = threading.Lock()
            goals._audit = Mock()
            goals.harness_support = lambda: True
            goals.jobs = Mock()
            initial = {"id": "a" * 12, "phase": "starting", "state": "running", "lock_label": "job-" + "a" * 12}
            goals.jobs.start.return_value = initial
            result = goals.confirm("study", "---\ngame: fixture\n---\n", "fixture")
            self.assertTrue(result["started"])
            self.assertEqual(goals._dash_meta(root)["run_job"], initial["id"])
            self.assertEqual(result["lock_label"], initial["lock_label"])
            goals.jobs.status.return_value = initial
            self.assertTrue(goals._run_active(root))
            goals.jobs.cancel.return_value = {"accepted": True, "job": initial}
            self.assertTrue(goals.stop("study", "fixture")["accepted"])
            goals.jobs.cancel.assert_called_once_with(initial["id"], force=False)
            goals.jobs.start.return_value = {**initial, "phase": "finished", "outcome": "failed", "exit": None}
            self.assertFalse(goals.confirm("study", "---\nfixture\n---\n", "fixture")["started"])

    def test_http_restart_preserves_worker_and_cancellation_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            servers, worker = [], None

            def start():
                p = subprocess.Popen([sys.executable, "-u", "-c", SERVER, NODE, root],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                servers.append(p)
                return int(p.stdout.readline().strip())

            def request(path, method="GET", body=b"", headers=None, expected=200):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=body,
                             headers={"Authorization": "Bearer restart-test-only", **(headers or {})})
                res = conn.getresponse()
                content = res.read()
                conn.close()
                self.assertEqual(res.status, expected, content)
                return json.loads(content)

            def until(predicate):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    result = predicate()
                    if result:
                        return result
                    time.sleep(0.05)
                self.fail("fixture did not reach expected state")

            try:
                port = start()
                submission = {"X-Job": json.dumps({"bin": "phonefarm", "device_access": "none", "argv": ["-u", "-c", CHILD]}),
                              "X-Request-ID": "restart:fixture"}
                meta = request("/jobs", "POST", headers=submission)
                jid = meta["id"]
                log = Path(root) / "_jobs" / jid / "log"
                until(lambda: log.exists() and "ready marker" in log.read_text())
                before = request("/jobs/" + jid)
                worker = before["pid"]
                self.assertEqual(before["phase"], "running")
                self.assertIn("job-" + jid, log.read_text())
                with ThreadPoolExecutor(max_workers=4) as pool:
                    repeats = list(pool.map(lambda _: request("/jobs", "POST", headers=submission), range(4)))
                self.assertEqual({r["id"] for r in repeats}, {jid})
                request("/jobs", "POST", headers={**submission, "X-Job": json.dumps(
                    {"bin": "phonefarm", "device_access": "none", "argv": ["-c", "pass"]})}, expected=409)
                next_job = request("/jobs", "POST", headers={"X-Request-ID": "next-job", "X-Job": json.dumps(
                    {"bin": "phonefarm", "device_access": "none", "argv": ["-c", "from pathlib import Path; p=Path('execution-count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]})})
                self.assertEqual(next_job["phase"], "queued")
                self.assertEqual(next_job["queue"]["waiting"]["blocking_jobs"], [jid])
                cancelled = request("/jobs", "POST", headers={"X-Job": json.dumps(
                    {"bin": "phonefarm", "device_access": "none", "argv": ["-c", "from pathlib import Path; Path('must-not-run').touch()"]})})
                pending_cancel = request("/jobs/" + cancelled["id"] + "/cancel", "POST")
                self.assertTrue(pending_cancel["accepted"])
                self.assertEqual(pending_cancel["job"]["outcome"], "cancelled")
                self.assertEqual(pending_cancel["job"]["recovery"]["status"], "not_needed")
                servers[-1].kill()  # Actual service process crash, not a new Jobs object.
                servers[-1].wait(timeout=5)
                port = start()
                after = request("/jobs/" + jid)
                self.assertEqual(request("/requests/restart%3Afixture")["id"], jid)
                self.assertEqual(request("/jobs/" + next_job["id"])["phase"], "queued")
                self.assertEqual(after["pid"], worker)
                self.assertTrue(after["execution"]["worker_alive"])
                receipt = request("/jobs/" + jid + "/cancel", "POST", b'{"force":false}')
                self.assertTrue(receipt["accepted"])
                until(lambda: request("/jobs/" + jid)["state"] == "finished")
                done = request("/jobs/" + jid)
                self.assertEqual((done["phase"], done["outcome"], done["exit"]), ("finished", "cancelled", 0))
                self.assertEqual(done["recovery"]["status"], "not_reported")
                self.assertEqual([s["name"] for s in done["cancel"]["signals"]], ["SIGINT"])
                self.assertIn("cleanup marker", log.read_text())
                self.assertFalse(request("/jobs/" + jid + "/cancel", "POST")["accepted"])
                until(lambda: not request("/jobs/" + jid)["execution"]["worker_alive"])
                until(lambda: request("/jobs/" + next_job["id"])["state"] == "finished")
                self.assertEqual((Path(root) / "execution-count").read_text(), "x")
                self.assertFalse((Path(root) / "must-not-run").exists())
                worker = None
            finally:
                if worker:
                    # Only a failed disposable fixture reaches cleanup; never production PIDs.
                    try:
                        request("/jobs/" + jid + "/cancel", "POST", b'{"force":true}')
                    except (OSError, AssertionError):
                        pass
                for p in servers:
                    if p.poll() is None:
                        p.terminate()
                    p.wait(timeout=5)
                    p.stdout.close()


if __name__ == "__main__":
    unittest.main()
