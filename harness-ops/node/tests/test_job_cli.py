import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
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
                if self.path.endswith("/queue-release"):
                    value["released"] = allowed
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
        released = run("release", JOB, "--note", "checked device state")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertTrue(json.loads(released.stdout)["released"])
        self.assertIn("不证明设备已恢复", released.stderr)
        self.assertEqual(requests, [(f"/jobs/{JOB}/cancel", {"force": False}),
                                    (f"/jobs/{JOB}/cancel", {"force": True}),
                                    (f"/jobs/{JOB}/cancel", {"force": False}),
                                    (f"/jobs/{JOB}/kill", None),
                                    (f"/jobs/{JOB}/queue-release", {"note": "checked device state"})])

    def test_submission_retries_same_request_and_recovers_without_another_job(self):
        posts, gets = [], []
        meta = {"id": JOB, "state": "running", "phase": "queued", "request_id": "study-001"}

        class Handler(BaseHTTPRequestHandler):
            def reply(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                gets.append(self.path)
                if self.path == "/node":
                    self.reply({"capabilities": ["jobs.idempotency-v1", "jobs.queue-v1"]})
                elif self.path == "/requests/study-001":
                    self.reply(meta)
                elif self.path == "/queue":
                    self.reply({"items": [meta]})
                elif self.path == "/leases":
                    self.reply({"status": "blocked", "entries": []})
                elif self.path == f"/jobs/{JOB}/leases":
                    self.reply({"job_id": JOB, "status": "not_reported", "entries": [], "errors": []})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                posts.append((self.path, self.headers.get("X-Request-ID"), self.headers.get("X-Job"), body))
                if len(posts) == 1:
                    self.close_connection = True  # Accepted on server; response lost in transit.
                    return
                self.reply(meta)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{server.server_port}", PF_NODE_TOKEN="fixture-admin")

        def run(*args):
            return subprocess.run([sys.executable, str(CLI), *args], env=env,
                                  capture_output=True, text=True, timeout=10)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            source.write_text('{"scene":"fixture"}')
            cli = self.load_cli()
            with mock.patch("time.time", return_value=100):
                first_pack = cli["pack"]([str(source)])
            with mock.patch("time.time", return_value=200):
                self.assertEqual(cli["pack"]([str(source)]), first_pack)
            submitted = run("--request-id", "study-001", "--resource", "device:fixture",
                            "--resource", "capture:fixture", "--detach", "phonefarm", "eval", str(source))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(submitted.stdout.strip(), JOB)
        self.assertIn("phase=queued request_id=study-001", submitted.stderr)
        self.assertNotIn("已在节点启动", submitted.stderr)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0][:2], ("/jobs", "study-001"))
        self.assertEqual(json.loads(posts[0][2])["resources"], ["device:fixture", "capture:fixture"])
        self.assertEqual(posts[0][3], first_pack[1])
        recovered = run("request", "study-001")
        queued = run("queue")
        leases = run("leases")
        job_leases = run("leases", JOB)
        missing_leases = run("leases", "ffffffffffff")
        bad_leases = run("leases", "not-a-job")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout), meta)
        self.assertEqual(queued.returncode, 0, queued.stderr)
        self.assertEqual(json.loads(queued.stdout), {"items": [meta]})
        self.assertEqual(leases.returncode, 0, leases.stderr)
        self.assertEqual(json.loads(leases.stdout), {"status": "blocked", "entries": []})
        self.assertEqual(job_leases.returncode, 0, job_leases.stderr)
        self.assertEqual(json.loads(job_leases.stdout), {"job_id": JOB, "status": "not_reported", "entries": [], "errors": []})
        self.assertNotEqual(missing_leases.returncode, 0)
        self.assertIn("节点未提供该 lease 证据入口", missing_leases.stderr)
        self.assertNotEqual(bad_leases.returncode, 0)
        self.assertEqual(gets, ["/node", "/requests/study-001", "/queue", "/leases", f"/jobs/{JOB}/leases", "/jobs/ffffffffffff/leases"])
        self.assertEqual(len(posts), 2)

    def test_submission_refuses_old_nodes_and_reports_conflict_or_unknown_result(self):
        mode, posts = ["old"], []

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(404 if mode[0] == "404" else 200,
                           {"capabilities": [] if mode[0] == "old" else ["jobs.idempotency-v1"] +
                            (["jobs.recovery-v1"] if mode[0] in ("modern", "parent-conflict") else [])})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                posts.append((self.headers.get("X-Request-ID"), self.headers.get("X-Job"), body))
                if mode[0] == "unknown":
                    self.close_connection = True
                elif mode[0] == "conflict":
                    self.reply(409, {"error": "request content differs"})
                elif mode[0] == "parent-conflict":
                    self.reply(409, {"error": "recovery_for must name a blocked reservation"})
                else:
                    self.reply(200, {"id": JOB, "phase": "preparing", "state": "running"})

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cli = self.load_cli()

        def run(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(cli["main"].__globals__, URL=f"http://127.0.0.1:{server.server_port}"), \
                    mock.patch.object(sys, "argv", ["pf-node", *args]), mock.patch("time.sleep"), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    cli["main"]()
                    code = 0
                except SystemExit as e:
                    code = e.code
            return code, stdout.getvalue(), stderr.getvalue()

        for old in ("old", "404"):
            mode[0] = old
            code, _, _ = run("--detach", "phonefarm", "eval")
            self.assertIn("升级", str(code))
            self.assertEqual(posts, [])
        code, _, _ = run("--request-id", "bad/id", "--detach", "phonefarm", "eval")
        self.assertIn("request_id", str(code))
        self.assertEqual(posts, [])
        mode[0] = "idempotency-only"
        code, _, _ = run("--device-access", "none", "--detach", "phonefarm", "eval")
        self.assertIn("jobs.recovery-v1", str(code))
        self.assertEqual(posts, [], "explicit access must not POST to an older node")
        for options, explanation in [
            (["--device-access", "recovery"], "--recovery-for"),
            (["--recovery-for", JOB], "--device-access recovery"),
            (["--device-access", "write", "--recovery-for", JOB], "--device-access recovery"),
            (["--device-access", "recovery", "--recovery-for", JOB, "--resource", "override"], "继承"),
            (["--device-access", "none", "--device-access", "write"], "矛盾"),
        ]:
            code, _, _ = run(*options, "--detach", "phonefarm", "eval")
            self.assertIn(explanation, str(code))
        self.assertEqual(posts, [])
        mode[0] = "modern"
        code, _, stderr = run("--device-access", "recovery", "--recovery-for", JOB,
                             "--detach", "phonefarm", "fixture-recover")
        self.assertEqual(code, 0, stderr)
        spec = json.loads(posts.pop()[1])
        self.assertEqual(spec["device_access"], "recovery")
        self.assertEqual(spec["recovery_for"], JOB)
        self.assertNotIn("resources", spec)
        code, _, stderr = run("--device-access", "none", "--detach", "phonefarm", "fixture-host-only")
        self.assertEqual(code, 0, stderr)
        spec = json.loads(posts.pop()[1])
        self.assertEqual(spec["device_access"], "none")
        self.assertNotIn("recovery_for", spec)
        mode[0] = "parent-conflict"
        code, _, _ = run("--device-access", "recovery", "--recovery-for", JOB,
                         "--request-id", "recover-001", "--detach", "phonefarm", "fixture-recover")
        self.assertIn("recovery_for must name a blocked reservation", str(code))
        self.assertNotIn("与已有提交内容冲突", str(code))
        posts.clear()
        mode[0] = "conflict"
        code, _, _ = run("--request-id", "same-id", "--detach", "phonefarm", "eval")
        self.assertIn("冲突", str(code))
        self.assertIn("request content differs", str(code))
        self.assertEqual(len(posts), 1)
        mode[0] = "unknown"
        posts.clear()
        code, _, stderr = run("--request-id", "same-id", "--detach", "phonefarm", "eval")
        self.assertEqual(code, 1)
        self.assertIn("提交结果未知", stderr)
        self.assertIn("pf-node request same-id", stderr)
        self.assertEqual(len(posts), 5)
        self.assertTrue(all(post == posts[0] for post in posts))
        mode[0] = "ok"
        posts.clear()
        code, _, stderr = run("--detach", "phonefarm", "eval")
        self.assertEqual(code, 0, stderr)
        request_id, spec, body = posts[0]
        self.assertEqual(str(uuid.UUID(request_id)), request_id)
        self.assertNotIn("resources", json.loads(spec))
        self.assertNotIn("device_access", json.loads(spec))
        self.assertNotIn("recovery_for", json.loads(spec))
        self.assertEqual(body, b"")
        self.assertIn("phase=preparing", stderr)

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

    def test_session_actions_use_original_json_once_and_status_only_reads(self):
        cli = self.load_cli()
        action = {"schema": "phonefarm/gray-control-action@1", "request_id": "capture-1", "session_id": "session-1",
                  "actor_id": "agent", "control_epoch": 2, "action": "sample", "params": {"seconds": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "action.json"
            source.write_text(json.dumps(action))
            response = {"schema": "phonefarm/gray-control-action-result@1", "request_id": "capture-1", "status": "accepted"}
            call = mock.Mock(side_effect=[json.dumps({"capabilities": ["jobs.session-control-v1"]}).encode(),
                                          json.dumps(response).encode()])
            with mock.patch.dict(cli["session_command"].__globals__, call=call), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                cli["session_command"](["session-action", JOB, "--request", str(source)])
            posted = call.call_args_list[1]
            self.assertEqual(posted.args[:2], ("POST", f"/jobs/{JOB}/session/actions"))
            self.assertEqual(json.loads(posted.args[2]), action)
            self.assertEqual(posted.kwargs["attempts"], 1)
            call = mock.Mock(return_value=b'{"capabilities":[]}')
            with mock.patch.dict(cli["session_command"].__globals__, call=call), self.assertRaises(SystemExit):
                cli["session_command"](["session-action", JOB, "--request", str(source)])
            self.assertEqual(call.call_count, 1)
            call = mock.Mock(side_effect=[b'{"capabilities":["jobs.session-control-v1"]}', SystemExit("timeout")])
            stderr = io.StringIO()
            with mock.patch.dict(cli["session_command"].__globals__, call=call), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                cli["session_command"](["session-action", JOB, "--request", str(source)])
            self.assertEqual(call.call_count, 2)
            self.assertIn(f"session-status {JOB} --request-id capture-1", stderr.getvalue())
        call = mock.Mock(return_value=json.dumps(response).encode())
        with mock.patch.dict(cli["session_command"].__globals__, call=call), contextlib.redirect_stdout(io.StringIO()):
            cli["session_command"](["session-status", JOB, "--request-id", "capture-1"])
        self.assertEqual(call.call_args.args, ("GET", f"/jobs/{JOB}/session/actions/capture-1"))

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
