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
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


CLI = Path(__file__).resolve().parents[1] / "pf-node"
JOB = "0123456789ab"
NODE_ID = "1" * 32


def identity(**fields):
    return {"node_id": NODE_ID, "capabilities": ["node.target-v1"],
            "host": {"hostname": "fixture"}, "storage": {"data_root": "/fixture/data"}, **fields}


class JobCLI(unittest.TestCase):
    def load_cli(self, *, mock_connection=True):
        cli = runpy.run_path(str(CLI))
        if mock_connection:
            # These unit cases replace call(); target-handshake HTTP is covered separately.
            target = cli["Target"]("http://fixture.invalid", "fixture-token", NODE_ID)
            target._identity = identity()
            cli["main"].__globals__.update(TARGET=target, Target=mock.Mock(resolve=mock.Mock(return_value=target)))
        return cli

    def test_cancel_http_modes_permissions_and_legacy_kill(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/node":
                    self.send_error(404)
                    return
                data = json.dumps(identity()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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
        env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{server.server_port}", PF_NODE_TOKEN="fixture-admin", PF_NODE_ID=NODE_ID)
        env.pop("PF_NODE_TOKEN_FILE", None)

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
                    self.reply(identity(capabilities=["node.target-v1", "jobs.idempotency-v1", "jobs.queue-v1", "jobs.execution-policy-v1"],
                                        execution={"process_policies": {"process_group": {"available": True}}}))
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
        env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{server.server_port}", PF_NODE_TOKEN="fixture-admin", PF_NODE_ID=NODE_ID)
        env.pop("PF_NODE_TOKEN_FILE", None)

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
                            "--resource", "capture:fixture", "--execution-policy", "process_group",
                            "--detach", "phonefarm", "eval", str(source))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(submitted.stdout.strip(), JOB)
        self.assertIn("phase=queued request_id=study-001", submitted.stderr)
        self.assertNotIn("已在节点启动", submitted.stderr)
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0][:2], ("/jobs", "study-001"))
        self.assertEqual(json.loads(posts[0][2])["resources"], ["device:fixture", "capture:fixture"])
        self.assertEqual(json.loads(posts[0][2])["execution_policy"], "process_group")
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
        self.assertEqual(gets, ["/node", "/node", "/requests/study-001", "/node", "/queue",
                               "/node", "/leases", "/node", f"/jobs/{JOB}/leases",
                               "/node", "/jobs/ffffffffffff/leases"])
        self.assertEqual(len(posts), 2)

    def test_execution_policy_refusal_precedes_packing_or_post(self):
        main = self.load_cli()["main"]
        for policy_capability in (False, True):
            node = {"capabilities": ["jobs.idempotency-v1"]}
            if policy_capability:
                node["capabilities"].append("jobs.execution-policy-v1")
                node["execution"] = {"process_policies": {"contained": {"available": False, "reason": "no contained backend"}}}
            call = mock.Mock(return_value=json.dumps(node).encode())
            pack = mock.Mock(side_effect=AssertionError("must not pack an unsupported job"))
            with mock.patch.dict(main.__globals__, call=call, pack=pack), \
                    mock.patch.object(sys, "argv", [str(CLI), "--execution-policy", "contained", "--detach", "phonefarm"]), \
                    self.assertRaises(SystemExit) as error:
                main()
            self.assertIn("未打包或提交", str(error.exception))
            call.assert_called_once_with("GET", "/node", attempts=1, raise_4xx=True)
            pack.assert_not_called()

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
                           identity(capabilities=["node.target-v1"] + ([] if mode[0] == "old" else ["jobs.idempotency-v1"] +
                            (["jobs.recovery-v1"] if mode[0] in ("modern", "parent-conflict") else []))))

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
        cli = self.load_cli(mock_connection=False)

        def run(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"PF_NODE_URL": f"http://127.0.0.1:{server.server_port}",
                                             "PF_NODE_TOKEN": "fixture-admin", "PF_NODE_ID": NODE_ID}, clear=True), \
                    mock.patch.object(sys, "argv", ["pf-node", *args]), mock.patch("time.sleep"), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    cli["main"]()
                    code = 0
                except (SystemExit, cli["TargetError"]) as e:
                    code = e.code if isinstance(e, SystemExit) else str(e)
            return code, stdout.getvalue(), stderr.getvalue()

        for old in ("old", "404"):
            mode[0] = old
            code, _, _ = run("--detach", "phonefarm", "eval")
            self.assertIn("升级" if old == "old" else "HTTP 404", str(code))
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
        self.assertIn("HTTP 409", str(code))
        self.assertIn("恢复目标条件", str(code))
        self.assertIn("request_id=recover-001", str(code))
        self.assertNotIn("recovery_for must name a blocked reservation", str(code))
        self.assertNotIn("与已有提交内容冲突", str(code))
        posts.clear()
        mode[0] = "conflict"
        code, _, _ = run("--request-id", "same-id", "--detach", "phonefarm", "eval")
        self.assertIn("冲突", str(code))
        self.assertIn("request_id=same-id", str(code))
        self.assertNotIn("request content differs", str(code))
        self.assertEqual(len(posts), 1)
        mode[0] = "unknown"
        posts.clear()
        code, _, stderr = run("--request-id", "same-id", "--detach", "phonefarm", "eval")
        self.assertEqual(code, 1)
        self.assertIn("提交结果未知", stderr)
        self.assertIn("request same-id", stderr)
        self.assertIn(f"--node-id {NODE_ID}", stderr)
        self.assertEqual(len(posts), 5)
        self.assertTrue(all(post == posts[0] for post in posts))
        mode[0] = "ok"
        posts.clear()
        session_argv = ["gray-layer", "session", "--serial", "FIXTURE", "--package", "com.example.game",
                        "--mode", "frameaudit", "--out", "@out", "--max-seconds", "900", "--power-policy", "battery"]
        code, _, stderr = run("--detach", "phonefarm", *session_argv)
        self.assertEqual(code, 0, stderr)
        request_id, spec, body = posts[0]
        self.assertEqual(str(uuid.UUID(request_id)), request_id)
        self.assertNotIn("resources", json.loads(spec))
        self.assertNotIn("device_access", json.loads(spec))
        self.assertNotIn("recovery_for", json.loads(spec))
        self.assertEqual(json.loads(spec)["argv"], session_argv)
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

    def test_execution_reconcile_is_explicit_single_post_without_follow(self):
        cli = self.load_cli()
        node = b'{"capabilities":["jobs.execution-reconcile-v1"]}'
        for force in (False, True):
            result = {"accepted": True, "job": {"id": JOB, "state": "finished", "phase": "interrupted",
                      "outcome": "interrupted", "lost": True, "queue": {"state": "blocked"},
                      "execution": {"reconciliation": {"state": "requested", "force": force},
                                    "containment": {"handoff": {"status": "pending"}}}}}
            call = mock.Mock(side_effect=[node, json.dumps(result).encode()])
            stdout, stderr, follow = io.StringIO(), io.StringIO(), mock.Mock()
            with mock.patch.dict(cli["main"].__globals__, call=call, follow=follow), \
                    mock.patch.object(sys, "argv", [str(CLI), "execution-reconcile", JOB] + (["--force"] if force else [])), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                cli["main"]()
            self.assertEqual(json.loads(stdout.getvalue()), result)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args_list[0].args, ("GET", "/node"))
            self.assertEqual(call.call_args.args[:2], ("POST", f"/jobs/{JOB}/execution/reconcile"))
            self.assertEqual(json.loads(call.call_args.args[2]), {"force": force})
            self.assertNotIn("X-Request-ID", call.call_args.args[3])
            self.assertTrue(all(request.kwargs["attempts"] == 1 for request in call.call_args_list))
            self.assertIn("结束绑定域内残留进程" if force else "普通核查不杀进程", stderr.getvalue())
            self.assertIn("不代表设备恢复或队列资源释放", stderr.getvalue())
            follow.assert_not_called()
        for replies, expected_calls in (([b'{"capabilities":[]}'], 1), ([node, TimeoutError("fixture timeout")], 2)):
            call, stderr = mock.Mock(side_effect=replies), io.StringIO()
            with mock.patch.dict(cli["execution_reconcile_command"].__globals__, call=call), \
                    contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as failed:
                cli["execution_reconcile_command"](["execution-reconcile", JOB])
            self.assertNotEqual(failed.exception.code, 0)
            self.assertEqual(call.call_count, expected_calls)
            if expected_calls == 2:
                self.assertIn(f"status {JOB}", stderr.getvalue())
                self.assertIn(f"--node-id {NODE_ID}", stderr.getvalue())
                self.assertIn("不自动重发 POST", stderr.getvalue())
            else:
                self.assertIn("jobs.execution-reconcile-v1", str(failed.exception.code))

    def test_session_recover_submits_once_with_visible_id_and_bound_parent(self):
        cli = self.load_cli()
        recovery = {"id": "abcdef123456", "recovery_for": JOB, "state": "running", "phase": "queued"}
        for mode in ("success", "missing-capability", "timeout", "wrong-parent"):
            with self.subTest(mode=mode):
                requests, stdout, stderr = [], io.StringIO(), io.StringIO()

                def call(method, path, data=None, headers=None, **kwargs):
                    requests.append((method, path, headers))
                    self.assertEqual(kwargs["attempts"], 1)
                    self.assertTrue(kwargs["raise_errors"])
                    if method == "GET":
                        self.assertEqual(path, "/node")
                        return json.dumps({"capabilities": [] if mode == "missing-capability" else ["jobs.session-recovery-v1"]}).encode()
                    self.assertEqual((method, path, data), ("POST", f"/jobs/{JOB}/session/recover", b"{}"))
                    self.assertEqual(headers["Content-Type"], "application/json")
                    request_id = headers["X-Request-ID"]
                    self.assertIn(f"request_id={request_id}", stderr.getvalue(), "ID must be printed before POST")
                    self.assertEqual(stdout.getvalue(), "")
                    if mode == "success":
                        self.assertEqual(str(uuid.UUID(request_id)), request_id)
                    else:
                        self.assertEqual(request_id, "recover-fixture-1")
                    if mode == "timeout":
                        raise TimeoutError("fixture timeout")
                    return json.dumps({**recovery, "recovery_for": "bbbbbbbbbbbb"} if mode == "wrong-parent" else recovery).encode()

                args = [str(CLI), "session-recover", JOB]
                if mode != "success":
                    args += ["--request-id", "recover-fixture-1"]
                follow, fetch = mock.Mock(), mock.Mock()
                code = 0
                with mock.patch.dict(cli["main"].__globals__, call=call, follow=follow, fetch_out=fetch), \
                        mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    try:
                        cli["main"]()
                    except SystemExit as error:
                        code = error.code
                follow.assert_not_called()
                fetch.assert_not_called()
                self.assertEqual(sum(method == "POST" for method, _, _ in requests), 0 if mode == "missing-capability" else 1)
                self.assertEqual(len(requests), 1 if mode == "missing-capability" else 2)
                if mode == "success":
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(stdout.getvalue()), recovery)
                    self.assertIn("不代表设备恢复", stderr.getvalue())
                else:
                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout.getvalue(), "")
                if mode == "missing-capability":
                    self.assertIn("jobs.session-recovery-v1", str(code))
                if mode == "timeout":
                    self.assertIn("request recover-fixture-1", stderr.getvalue())
                    self.assertIn(f"--node-id {NODE_ID}", stderr.getvalue())
                    self.assertIn(f"session-recover {JOB} --request-id recover-fixture-1", stderr.getvalue())
                if mode == "wrong-parent":
                    self.assertIn("恢复任务回包未匹配原任务", stderr.getvalue())

    def test_session_actions_use_original_json_once_and_status_only_reads(self):
        cli = self.load_cli()
        action = {"schema": "phonefarm/gray-control-action@1", "request_id": "capture-1", "session_id": "session-1",
                  "actor_id": "agent", "control_epoch": 2, "action": "quality", "params": {"op": "checkpoint", "label": "scene-dusk-01"}}
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
        # Machine-readable reads retain server codes and never turn a network failure into missing.
        errors = [
            (404, {"schema": "phonefarm/gray-control-error@1", "code": "not_found", "error": "no receipt"}, "not_found", "no receipt"),
            (409, {"error": {"code": "conflict", "message": "changed session"}}, "conflict", "changed session"),
            (404, {"error": "no such job"}, "http_error", "no such job"),
            (503, {"code": "io_error", "error": "helper unavailable"}, "io_error", "helper unavailable"),
            (None, None, "network_error", "<urlopen error offline>"),
        ]
        for status, body, code, message in errors:
            error = (urllib.error.HTTPError("http://fixture", status, "fixture", {}, io.BytesIO(json.dumps(body).encode()))
                     if status else urllib.error.URLError("offline"))
            stdout = io.StringIO()
            with self.subTest(status=status, code=code), \
                    mock.patch.object(cli["call"].__globals__["TARGET"].opener, "open", side_effect=error) as opened, \
                    contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as failed:
                cli["session_command"](["session-status", JOB, "--request-id", "capture-1", "--json"])
            self.assertEqual(failed.exception.code, 1)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0].method, "GET")
            self.assertEqual(json.loads(stdout.getvalue()), {"error": {"code": code, "message": message}, "http_status": status})
        stdout = io.StringIO()
        call = mock.Mock(return_value=json.dumps(response).encode())
        with mock.patch.dict(cli["session_status_json"].__globals__, call=call), contextlib.redirect_stdout(stdout):
            cli["session_command"](["session-status", JOB, "--json", "--request-id", "capture-1"])
        self.assertEqual(json.loads(stdout.getvalue()), response)
        self.assertEqual(call.call_args.args, ("GET", f"/jobs/{JOB}/session/actions/capture-1"))
        self.assertEqual(call.call_args.kwargs["attempts"], 1)

    def test_terminal_unknown_exit_is_not_success_and_legacy_exit_is_preserved(self):
        cli = self.load_cli()
        cases = [({"state": "finished", "phase": "interrupted", "exit": -1,
                   "outcome": "interrupted", "recovery": "unknown"}, 125),
                 ({"state": "finished", "exit": None}, 125),
                 ({"state": "finished", "exit": 0}, 0)]
        for group in ("present", "unknown"):
            for rc, expected in ((0, 125), (7, 7)):
                cases.append(({"state": "finished", "phase": "interrupted", "outcome": "interrupted",
                               "exit": rc, "recovery": {"status": "required"},
                               "execution": {"owned_process_group": {"status": group}}}, expected))
        for status, expected in cases:
            with self.subTest(status=status):
                stderr = io.StringIO()
                stdout = mock.Mock(buffer=io.BytesIO())
                call = mock.Mock(side_effect=[json.dumps(status).encode(), b"", b""])
                with mock.patch.dict(cli["follow"].__globals__, call=call), \
                        mock.patch.object(sys, "stdout", stdout), contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli["follow"](JOB), expected)
                if status.get("execution") and status["exit"] == 0:
                    self.assertIn("直接命令 exit=0，但原进程组结束未确认", stderr.getvalue())
                    self.assertNotIn("没有可信退出码", stderr.getvalue())
                elif expected == 125:
                    self.assertIn("没有可信退出码", stderr.getvalue())
                if not status.get("execution") or status["exit"] != 0:
                    self.assertIn("recovery=", stderr.getvalue())

    def test_retry_finalize_checks_capability_and_posts_once_without_claiming_recovery(self):
        cli = self.load_cli()
        blocked = {"id": JOB, "state": "running", "phase": "storage_blocked", "exit": 0,
                   "queue": {"state": "blocked", "state_reason": {"kind": "durability", "message": "fsync failed"}}}
        still_recovery = {"id": JOB, "state": "finished", "exit": 0, "queue": {"state": "blocked"},
                          "recovery": {"status": "required"}}
        scenarios = [([], None, "未提供 jobs.durability-v1"),
                     (["jobs.durability-v1"], {"storage_committed": False, "job": blocked}, "fsync failed"),
                     (["jobs.durability-v1"], {"storage_committed": True, "job": still_recovery}, "不代表设备恢复"),
                     (["jobs.durability-v1"], cli["ClientError"](503, b"storage unavailable"), "不会自动重试"),
                     (["jobs.durability-v1"], urllib.error.URLError("offline"), "不会自动重试")]
        for caps, result, diagnostic in scenarios:
            with self.subTest(result=result):
                replies = [json.dumps({"capabilities": caps}).encode()]
                if result is not None:
                    replies.append(result if isinstance(result, Exception) else json.dumps(result).encode())
                call, stdout, stderr = mock.Mock(side_effect=replies), io.StringIO(), io.StringIO()
                with mock.patch.dict(cli["main"].__globals__, call=call), \
                        mock.patch.object(sys, "argv", [str(CLI), "retry-finalize", JOB]), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as stopped:
                    cli["main"]()
                self.assertEqual(call.call_args_list[0].args, ("GET", "/node"))
                for request in call.call_args_list:
                    self.assertEqual(request.kwargs["attempts"], 1)
                    self.assertTrue(request.kwargs["raise_errors"])
                if result is None:
                    self.assertEqual(call.call_count, 1)
                else:
                    self.assertEqual(call.call_count, 2)
                    self.assertEqual(call.call_args.args, ("POST", f"/jobs/{JOB}/retry-finalize", b"{}",
                                                         {"Content-Type": "application/json"}))
                if isinstance(result, dict):
                    self.assertEqual(json.loads(stdout.getvalue()), result)
                    self.assertEqual(stopped.exception.code, 0 if result["storage_committed"] else 1)
                else:
                    self.assertNotEqual(stopped.exception.code, 0)
                self.assertIn(diagnostic, stderr.getvalue() + str(stopped.exception.code))

    def test_wait_storage_failure_and_explicit_unstarted_terminal_need_no_log_or_output(self):
        cli = self.load_cli()
        blocked = {"state": "running", "phase": "storage_blocked", "exit": 7,
                   "execution": {"returncode": 7}, "recovery": {"status": "required"},
                   "queue": {"state": "blocked", "state_reason": {"kind": "durability", "message": "directory fsync failed"}}}
        unstarted = {"state": "finished", "phase": "finished", "outcome": "failed", "exit": None,
                     "execution": {"owned_process_group": {"status": "not_started"}}, "recovery": {"status": "not_needed"}}
        cancelled = {"state": "finished", "phase": "finished", "outcome": "cancelled", "exit": None,
                     "queue": {"state_reason": {"kind": "cancelled_before_launch"}}, "recovery": {"status": "not_needed"}}
        for states in ([blocked], [{**blocked, "phase": "finalizing", "queue": {"state": "claimed"}}, blocked],
                       [unstarted], [cancelled]):
            with self.subTest(states=states):
                pending, reads = list(states), []

                def call(method, path, *_args, **_kwargs):
                    reads.append((method, path))
                    if path == f"/jobs/{JOB}":
                        return json.dumps(pending.pop(0)).encode()
                    if len(states) == 2 and len(pending) == 1 and path.endswith("/log?offset=0"):
                        return b"partial log"
                    raise AssertionError("must not require absent log/output or perform a mutation")

                stderr, stdout, fetch = io.StringIO(), mock.Mock(buffer=io.BytesIO()), mock.Mock()
                with mock.patch.dict(cli["main"].__globals__, call=call, fetch_out=fetch), \
                        mock.patch.object(sys, "argv", [str(CLI), "wait", JOB]), \
                        mock.patch.object(sys, "stdout", stdout), contextlib.redirect_stderr(stderr), \
                        mock.patch.object(cli["time"], "sleep"), self.assertRaises(SystemExit) as stopped:
                    cli["main"]()
                self.assertEqual(stopped.exception.code, 125)
                fetch.assert_not_called()
                self.assertEqual(reads[0], ("GET", f"/jobs/{JOB}"))
                self.assertEqual(len(reads), 3 if len(states) == 2 else 1)
                if states[-1] is blocked:
                    for text in ("storage_blocked", "exit=7", "execution.returncode=7", "directory fsync failed", "retry-finalize"):
                        self.assertIn(text, stderr.getvalue())
                else:
                    self.assertIn(f"outcome={states[-1]['outcome']}", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
