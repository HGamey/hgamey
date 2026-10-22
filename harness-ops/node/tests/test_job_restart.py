"""A real HTTP process crash must not lose the worker or its cancel channel."""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

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
import signal, time
def stop(*args):
    print("cleanup marker", flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
print("ready marker", flush=True)
while True: time.sleep(0.1)
'''


class JobRestartTests(unittest.TestCase):
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

            def request(path, method="GET", body=b"", headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(method, path, body=body,
                             headers={"Authorization": "Bearer restart-test-only", **(headers or {})})
                res = conn.getresponse()
                content = res.read()
                conn.close()
                self.assertEqual(res.status, 200, content)
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
                meta = request("/jobs", "POST", headers={"X-Job": json.dumps(
                    {"bin": "phonefarm", "argv": ["-u", "-c", CHILD]})})
                jid = meta["id"]
                log = Path(root) / "_jobs" / jid / "log"
                until(lambda: log.exists() and "ready marker" in log.read_text())
                before = request("/jobs/" + jid)
                worker = before["pid"]
                self.assertEqual(before["phase"], "running")
                servers[-1].kill()  # Actual service process crash, not a new Jobs object.
                servers[-1].wait(timeout=5)
                port = start()
                after = request("/jobs/" + jid)
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
