import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "pf-node"
JOB = "0123456789ab"


class JobCLI(unittest.TestCase):
    def load_cli(self):
        with mock.patch.dict(os.environ, PF_NODE_TOKEN="fixture-token"):
            return runpy.run_path(str(CLI))

    def test_cancel_http_modes_permissions_and_legacy_kill(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                requests.append((self.path, json.loads(body) if body else None))
                allowed = self.headers.get("Authorization") == "Bearer fixture-admin"
                value = {"accepted": allowed, "reason": None if allowed else "management token required",
                         "job": {"id": JOB, "state": "running", "phase": "cancelling",
                                 "outcome": None, "recovery": "unknown", "exit": None}}
                data = json.dumps(value).encode()
                self.send_response(200 if allowed else 403)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{server.server_port}", PF_NODE_TOKEN="fixture-admin")

        def run(*args, token="fixture-admin"):
            return subprocess.run([sys.executable, str(CLI), *args],
                                  env=dict(env, PF_NODE_TOKEN=token), capture_output=True, text=True, timeout=5)

        soft = run("cancel", JOB)
        self.assertEqual(soft.returncode, 0, soft.stderr)
        self.assertTrue(json.loads(soft.stdout)["accepted"])
        self.assertIn("请求已记录", soft.stderr)
        self.assertNotIn("已杀死", soft.stderr)
        force = run("cancel", JOB, "--force")
        self.assertEqual(force.returncode, 0, force.stderr)
        self.assertIn("可能打断恢复", force.stderr)
        denied = run("cancel", JOB, token="fixture-readonly")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("请求被拒绝", denied.stderr)
        self.assertIn("管理 token", denied.stderr)
        legacy = run("kill", JOB)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(requests, [(f"/jobs/{JOB}/cancel", {"force": False}),
                                    (f"/jobs/{JOB}/cancel", {"force": True}),
                                    (f"/jobs/{JOB}/cancel", {"force": False}),
                                    (f"/jobs/{JOB}/kill", None)])

    def test_ctrl_c_requests_soft_cancel_without_fetching_unfinished_output(self):
        cli = self.load_cli()
        requests = []

        def call(method, path, data=None, headers=None, **kwargs):
            requests.append((method, path, data))
            if method == "POST":
                return json.dumps({"accepted": True, "job": {"state": "running", "phase": "cancelling"}}).encode()
            if "/log?" in path:
                return b""
            raise KeyboardInterrupt

        stderr = io.StringIO()
        fetch = mock.Mock()
        with mock.patch.dict(cli["main"].__globals__, call=call, fetch_out=fetch), \
                mock.patch.object(sys, "argv", ["pf-node", "wait", JOB]), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                cli["main"]()
        self.assertEqual(stopped.exception.code, 130)
        fetch.assert_not_called()
        posts = [(path, json.loads(data)) for method, path, data in requests if method == "POST"]
        self.assertEqual(posts, [(f"/jobs/{JOB}/cancel", {"force": False})])
        self.assertIn("独立 worker", stderr.getvalue())
        self.assertIn("不是任务终态", stderr.getvalue())

    def test_terminal_unknown_exit_is_not_success_and_legacy_exit_is_preserved(self):
        cli = self.load_cli()
        cases = [({"state": "finished", "phase": "interrupted", "exit": -1,
                   "outcome": "interrupted", "recovery": "unknown"}, 125),
                 ({"state": "finished", "exit": None}, 125),
                 ({"state": "finished", "exit": 0}, 0)]
        for status, expected in cases:
            with self.subTest(status=status):
                stderr = io.StringIO()
                stdout = mock.Mock(buffer=io.BytesIO())
                call = mock.Mock(side_effect=[b"", json.dumps(status).encode(), b""])
                with mock.patch.dict(cli["follow"].__globals__, call=call), \
                        mock.patch.object(sys, "stdout", stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli["follow"](JOB), expected)
                if expected == 125:
                    self.assertIn("没有可信退出码", stderr.getvalue())
                self.assertIn("recovery=", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
