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
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


NODE = Path(__file__).resolve().parents[1]
ID = "a" * 32
OTHER_ID = "b" * 32


class TargetClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credential = self.root / "credential"
        self.credential.write_text("fixture-explicit-token")

    def server(self, route):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                call = {"method": self.command, "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "node_id": self.headers.get("X-Expected-Node-ID"),
                        "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))}
                calls.append(call)
                status, headers, value = route(call)
                data = json.dumps(value).encode()
                self.send_response(status)
                for key, val in headers.items():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = handle_request

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}", calls

    @staticmethod
    def identity(identity=ID, capabilities=None):
        return {"node_id": identity, "capabilities": capabilities if capabilities is not None else
                ["node.target-v1", "jobs.idempotency-v1", "games.prepare-v1"],
                "host": {"hostname": "fixture-host"}, "storage": {"data_root": "/fixture/data"}}

    def cli(self, program, args, env=None):
        # No workstation URL, registry, credential or proxy configuration is inherited.
        environment = {"PATH": os.defpath, "NO_PROXY": "*", "no_proxy": "*", **(env or {})}
        return subprocess.run([sys.executable, str(NODE / program), *args], env=environment,
                              capture_output=True, text=True, timeout=5)

    def test_help_and_explicit_credentials_never_fall_back(self):
        for program, args in [("pf-node", ["--help"]), ("pf-node", ["games", "--help"]),
                              ("pf-fleet", ["--help"]), ("pf-remote", ["--help"])]:
            result = self.cli(program, args)
            self.assertEqual(result.returncode, 0, result.stderr)
        missing = self.cli("pf-node", ["node"], {"PF_NODE_TOKEN": "ambient-fixture-token"})
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("显式", missing.stderr)
        endpoint, calls = self.server(lambda _: (200, {}, self.identity()))
        env = {"PF_NODE_URL": endpoint, "PF_NODE_TOKEN": "ambient-fixture-token",
               "PF_NODE_TOKEN_FILE": str(self.root / "missing")}
        conflict = self.cli("pf-node", ["node"], env)
        missing_file = self.cli("pf-node", ["--token-file", str(self.root / "missing"), "node"], env)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertNotEqual(missing_file.returncode, 0)
        self.assertEqual(calls, [])
        explicit = self.cli("pf-node", ["--token-file", str(self.credential), "node"], env)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(calls[0]["authorization"], "Bearer fixture-explicit-token")
        self.assertNotIn("ambient-fixture-token", conflict.stderr + missing_file.stderr + explicit.stderr)

    def test_bound_writes_and_original_request_queries_reject_changed_identity_and_redirects(self):
        sink, leaked = self.server(lambda _: (200, {}, {"unexpected": True}))
        state = {"identity": self.identity(OTHER_ID)}

        def route(call):
            if call["path"] == "/node":
                return 200, {}, state["identity"]
            if call["path"] == "/requests/original":
                return 200, {}, {"request_id": "original", "node_id": ID}
            return 307, {"Location": sink + "/sink"}, {"error": "fixture-explicit-token"}

        endpoint, calls = self.server(route)
        env = {"PF_NODE_URL": endpoint, "PF_NODE_ID": ID, "PF_NODE_TOKEN_FILE": str(self.credential)}
        args = ["--detach", "--request-id", "original", "phonefarm", "fixture"]
        mismatched = self.cli("pf-node", args, env)
        lookup = self.cli("pf-node", ["request", "original"], env)
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertNotEqual(lookup.returncode, 0)
        self.assertEqual([c["path"] for c in calls], ["/node", "/node"])
        state["identity"] = self.identity(capabilities=["jobs.idempotency-v1"])
        unsupported = self.cli("pf-node", args, env)
        self.assertIn("node.target-v1", unsupported.stderr)
        self.assertTrue(all(c["method"] == "GET" for c in calls))
        state["identity"] = self.identity()
        redirected = self.cli("pf-node", args, env)
        self.assertNotEqual(redirected.returncode, 0)
        self.assertEqual(leaked, [])
        self.assertEqual(sum(c["method"] == "POST" for c in calls), 1)
        self.assertTrue(all(c["node_id"] == ID for c in calls))
        self.assertIn("fixture-host", redirected.stderr)
        self.assertIn("/fixture/data", redirected.stderr)
        self.assertIn("--url " + endpoint, redirected.stderr)
        self.assertIn("--node-id " + ID, redirected.stderr)
        self.assertNotIn("fixture-explicit-token", redirected.stderr)
        found = self.cli("pf-node", ["request", "original"], env)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(calls[-1]["path"], "/requests/original")
        self.assertEqual(calls[-1]["node_id"], ID)

    def test_game_and_operator_actions_require_the_same_binding(self):
        state = {"identity": self.identity(OTHER_ID)}
        endpoint, calls = self.server(lambda _: (200, {}, state["identity"]))
        env = {"PF_NODE_URL": endpoint, "PF_NODE_ID": ID, "PF_NODE_TOKEN_FILE": str(self.credential)}
        game = self.cli("pf-node", ["games", "register", str(self.root / "not-opened")], env)
        self.assertIn("身份", game.stderr)
        self.assertEqual([c["path"] for c in calls], ["/node"])
        self.credential.write_text("hgop_fixture-operator")
        discovery = self.cli("pf-remote", ["--url", endpoint, "--token-file", str(self.credential), "node"])
        self.assertEqual(discovery.returncode, 0, discovery.stderr)
        self.assertEqual(calls[-1]["path"], "/remote/node")
        action = self.cli("pf-remote", ["action", "fixture-device", "tap", "--session-id", "session",
                                       "--request-id", "tap-original", "--params", '{"x":1,"y":2}'], env)
        self.assertNotEqual(action.returncode, 0)
        self.assertEqual(calls[-1]["path"], "/remote/node")
        self.assertTrue(all(c["method"] == "GET" for c in calls))
        self.assertNotIn("hgop_fixture-operator", discovery.stderr + action.stderr)

    def test_legacy_fleet_freezes_probe_identity_and_clears_ambient_token(self):
        fleet = runpy.run_path(str(NODE / "pf-fleet"))
        snapshot = {"schema_version": 1, "node_id": ID, "lock": {"status": "free", "queue": [],
                    "stale": False, "error": None}, "devices": [], "sampled_at": time.time(),
                    "stale": False, "error": None, "deferred": None}
        endpoint, calls = self.server(lambda c: (200, {}, self.identity() if c["path"] == "/node" else snapshot))
        entry = {"name": "fixture", "url": endpoint, "token_file": str(self.credential), "tags": [], "node_id": ID}
        with mock.patch.dict(os.environ, {"PF_NODE_TOKEN": "wrong-ambient", "PF_NODE_ID": OTHER_ID,
                                          "PF_NODE_URL": "http://wrong.invalid"}, clear=True):
            with mock.patch.object(os, "execvpe", side_effect=RuntimeError("selected")) as execute:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, "selected"):
                    fleet["cmd_run"]([entry], ["--node", "fixture", "--", "node"])
            environment = execute.call_args.args[2]
            self.assertNotIn("PF_NODE_TOKEN", environment)
            self.assertEqual(environment["PF_NODE_TOKEN_FILE"], str(self.credential))
            self.assertEqual(environment["PF_NODE_URL"], endpoint)
            self.assertEqual(environment["PF_NODE_ID"], ID)
            with self.assertRaisesRegex(SystemExit, "不能覆盖"):
                fleet["cmd_run"]([entry], ["--node", "fixture", "--", "--url", "http://wrong.invalid", "node"])
        self.assertTrue(all(c["authorization"] == "Bearer fixture-explicit-token" for c in calls))
        self.assertTrue(all(c["node_id"] == ID for c in calls))


if __name__ == "__main__":
    unittest.main()
