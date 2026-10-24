import contextlib
import copy
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
fleet = runpy.run_path(str(NODE / "pf-fleet"))


def snapshot():
    return {"schema_version": 1, "node_id": "fixture-node", "now": time.time(),
            "lock": {"status": "free", "held_by": None, "since": None,
                     "heartbeat_age_s": None, "stale": False, "queue": [], "error": None},
            "devices": [{"serial": "fixture-phone", "thermal": {"status": "warm"}}],
            "sampled_at": time.time(), "age_s": 0, "stale": False,
            "refreshing": False, "error": None, "deferred": None, "policy": {}}


class FleetCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name) / "token"
        self.token.write_text("fixture-token")

    def node(self, name="fixture", url="http://unused.invalid"):
        return {"name": name, "url": url, "tags": [], "token_file": str(self.token)}

    def test_status_run_and_node_fleet_use_get_without_creating_jobs(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path))
                body = json.dumps(snapshot()).encode()
                self.send_response(404 if self.path.startswith("/old/") else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        node = self.node(url=url)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fleet["cmd_status"]([node], ["--json"])
        result = json.loads(output.getvalue())[0]
        self.assertTrue(result["available"])
        self.assertEqual(result["devices"][0]["thermal"], {"status": "warm"})
        with mock.patch.object(os, "execvpe", side_effect=RuntimeError("selected")) as launch:
            with self.assertRaisesRegex(RuntimeError, "selected"):
                fleet["cmd_run"]([node], ["--any-free", "--", "node"])
            self.assertEqual(launch.call_args.args[2]["PF_NODE_URL"], url)
        env = dict(os.environ, PF_NODE_URL=url, PF_NODE_TOKEN="fixture-token")
        result = subprocess.run([sys.executable, str(NODE / "pf-node"), "fleet", "--wait", "2"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["node_id"], "fixture-node")
        old = fleet["probe_node"](self.node(url=url + "/old"), 1)
        self.assertTrue(old["reachable"])
        self.assertFalse(old["ok"])
        self.assertIn("升级", old["error"])
        self.assertEqual(requests, [("GET", "/fleet?wait=5"), ("GET", "/fleet?wait=5"),
                                    ("GET", "/fleet?wait=2"), ("GET", "/old/fleet?wait=5")])

    def test_total_deadline_keeps_every_node_and_ignores_late_results(self):
        release = threading.Event()
        finished = threading.Event()
        nodes = [self.node(name) for name in ("fast", "slow1", "slow2", "slow3")]
        remaining = [3]
        lock = threading.Lock()

        def probe(node, timeout):
            if node["name"] != "fast":
                release.wait(2)
                with lock:
                    remaining[0] -= 1
                    if not remaining[0]:
                        finished.set()
            return {"name": node["name"], "ok": True}

        self.addCleanup(release.set)
        with mock.patch.dict(fleet["probe_nodes"].__globals__, probe_node=probe):
            start = time.monotonic()
            result = fleet["probe_nodes"](nodes, timeout=0.1)
            self.assertLess(time.monotonic() - start, 0.7)
            self.assertEqual([r["name"] for r in result], [n["name"] for n in nodes])
            self.assertTrue(result[0]["ok"])
            self.assertTrue(all(not r["ok"] and r["devices"] is None for r in result[1:]))
            before = copy.deepcopy(result)
            release.set()
            self.assertTrue(finished.wait(1))
            self.assertEqual(result, before)

    def test_auto_selection_excludes_unknown_stale_queued_and_sampling_errors(self):
        variants = {}
        for name in ("unknown", "stale", "queued", "error", "free"):
            value = snapshot()
            if name == "unknown":
                value["lock"]["status"] = "unknown"
            elif name == "stale":
                value["stale"] = True
            elif name == "queued":
                value["lock"]["queue"] = ["waiting-task"]
            elif name == "error":
                value.update(error="sampling failed", devices=None)
            variants[name] = value
        nodes = [self.node(name, name) for name in variants]
        globals_ = fleet["probe_node"].__globals__
        with mock.patch.dict(globals_, _get_fleet=lambda url, token, timeout: variants[url]):
            with mock.patch.object(os, "execvpe", side_effect=RuntimeError("selected")) as launch:
                with self.assertRaisesRegex(RuntimeError, "selected"):
                    fleet["cmd_run"](nodes, ["--any-free", "--", "node"])
                self.assertEqual(launch.call_args.args[2]["PF_NODE_URL"], "free")
                with self.assertRaisesRegex(SystemExit, "没有可用节点"):
                    fleet["cmd_run"](nodes[:-1], ["--any-free", "--", "node"])
                with self.assertRaisesRegex(RuntimeError, "selected"):
                    fleet["cmd_run"](nodes, ["--node", "error", "--", "node"])
                self.assertEqual(launch.call_args.args[2]["PF_NODE_URL"], "error")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                fleet["cmd_status"](nodes[:-1], [])
            self.assertIn("未知", output.getvalue())
            self.assertIn("采样未就绪: sampling failed", output.getvalue())


class ManagedFleetCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote_token = self.root / "remote.token"
        self.remote_token.write_text("remote-fixture-secret")
        self.calls = []
        self.entry = {"name": "fixture", "url": "http://remote.invalid", "tags": [], "note": "",
                      "ssh": "fixture-ssh", "goals_dir": "/fixture/goals", "enabled": True,
                      "revision": 3, "node_id": "remote-id", "credential_present": True,
                      "created_at": 1, "updated_at": 2, "stale": True, "refreshing": True,
                      "connection": {"status": "unavailable", "reachable": False, "attempted_at": 7,
                                     "last_seen_at": 4, "error": "unavailable"},
                      "snapshot": {"at": 4, "url": "http://remote.invalid", "identity": {"node_id": "remote-id",
                                       "host": {"hostname": "fixture-host", "platform": "Linux"},
                                       "service": {"revision": "fixture-revision", "dirty": False},
                                       "storage": {"data_root": "/fixture/data", "jobs_root": "/fixture/jobs"}},
                                   "fleet": {"devices": None, "lock": {"status": "unknown", "lease": {"status": "clear"}},
                                             "sampled_at": None, "thermal": {"original": True}},
                                   "queue": {"active_count": 2, "queued_count": 1, "blocked_count": 3,
                                             "max_running": 4, "lease": {"status": "blocked", "bridge_held": True,
                                                 "entries": [{"label": "recover-label", "job_id": "old-job",
                                                              "state": "recovery_required", "reason": "restore pending"}]},
                                             "items": [{"job_id": "old-job", "phase": "blocked"}]}}}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append(("GET", self.path, None, self.headers.get("Authorization")))
                if self.path.startswith("/redirect/"):
                    self.send_response(302)
                    self.send_header("Location", "/nodes")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.split("?")[0] == "/nodes":
                    self.reply(200, {"schema_version": 1, "now": 9, "local": {"node_id": "hub-id"},
                                     "items": [owner.entry]})
                else:
                    self.reply(200, owner.entry)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(("POST", self.path, body, self.headers.get("Authorization")))
                if body.get("name") == "clash":
                    self.reply(409, {"error": "remote-fixture-secret must never be echoed"})
                    return
                if body.get("name") == "uncertain":
                    self.close_connection = True
                    return
                value = copy.deepcopy(owner.entry)
                value.update({key: val for key, val in body.items() if key != "token"})
                if "token" in body:
                    value["credential_present"] = body["token"] is not None
                self.reply(201 if self.path == "/nodes" else 200, value)

            def reply(self, code, value):
                data = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def cli(self, *args, hub=None):
        # Never inherit a workstation's hub, registry or credentials.
        env = {"PATH": os.environ.get("PATH", ""), "PF_NODE_TOKEN": "hub-fixture-secret",
               "PF_FLEET_HUB": hub or self.url, "PF_FLEET_REGISTRY": str(self.root / "not-a-registry")}
        return subprocess.run([sys.executable, str(NODE / "pf-fleet"), *args],
                              env=env, capture_output=True, text=True, timeout=5)

    def test_directory_queries_and_revision_writes_preserve_shared_facts(self):
        listed = self.cli("--hub", self.url, "status", "--refresh", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["items"], [self.entry])
        shown = self.cli("show", "fixture", "--devices", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout), self.entry)
        created = self.cli("register", "fixture", "--url", "http://remote.invalid", "--token-file",
                           str(self.remote_token), "--tag", "android", "--ssh", "fixture-ssh", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        updated = self.cli("update", "fixture", "--revision", "3", "--clear-token", "--clear-tags", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        disabled = self.cli("disable", "fixture", "--revision", "4", "--json")
        enabled = self.cli("enable", "fixture", "--revision", "5", "--json")
        self.assertEqual((disabled.returncode, enabled.returncode), (0, 0))
        self.assertEqual([(c[0], c[1]) for c in self.calls],
                         [("GET", "/nodes?refresh=1"), ("GET", "/nodes/fixture?devices=1"),
                          ("POST", "/nodes"), ("POST", "/nodes/fixture"),
                          ("POST", "/nodes/fixture"), ("POST", "/nodes/fixture")])
        self.assertEqual(self.calls[2][2]["token"], "remote-fixture-secret")
        self.assertEqual(self.calls[3][2], {"revision": 3, "tags": [], "token": None})
        self.assertEqual(self.calls[4][2], {"revision": 4, "enabled": False})
        self.assertEqual(self.calls[5][2], {"revision": 5, "enabled": True})
        self.assertTrue(all(c[3] == "Bearer hub-fixture-secret" for c in self.calls))
        for result in (created, updated, disabled, enabled):
            self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        text = self.cli("show", "fixture")
        self.assertEqual(text.returncode, 0, text.stderr)
        for fact in ("host=fixture-host", "platform=Linux", "service_revision=fixture-revision",
                     "data_root=/fixture/data", "jobs_root=/fixture/jobs", "active=2 queued=1 blocked=3",
                     "lease=blocked", "state=recovery_required", "devices=未知"):
            self.assertIn(fact, text.stdout)
        self.assertNotIn("lease=clear", text.stdout)
        self.assertEqual(self.calls[-1][0:2], ("GET", "/nodes/fixture"))

    def test_import_resolves_relative_credentials_and_keeps_conflicts_per_entry(self):
        path = self.root / "nodes.json"
        path.write_text(json.dumps({"nodes": [
            {"name": "first", "url": "http://remote.invalid", "token_file": "remote.token",
             "ssh": "ssh-meta", "goals_dir": "/meta/goals"},
            {"name": "clash", "url": "http://remote.invalid", "token_file": "remote.token"},
            {"name": "missing", "url": "http://remote.invalid", "token_file": "missing-token"},
            {"name": "last", "url": "http://remote.invalid"}]}))
        before = path.read_bytes()
        result = self.cli("import", str(path), "--json")
        self.assertEqual(result.returncode, 2, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual((value["created"], value["failed"]), (2, 2))
        self.assertEqual([r["status"] for r in value["items"]], ["created", "failed", "failed", "created"])
        self.assertEqual(value["items"][1]["error"]["code"], "conflict")
        self.assertEqual(value["items"][2]["error"]["code"], "credential_unavailable")
        self.assertEqual([c[2]["name"] for c in self.calls], ["first", "clash", "last"])
        self.assertTrue(all(c[0:2] == ("POST", "/nodes") for c in self.calls))
        self.assertEqual(self.calls[0][2]["token"], "remote-fixture-secret")
        self.assertEqual(self.calls[0][2]["ssh"], "ssh-meta")
        self.assertEqual(self.calls[0][2]["goals_dir"], "/meta/goals")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        self.assertNotIn(str(self.remote_token), result.stdout + result.stderr)

    def test_redirect_unknown_write_and_unsupported_commands_never_retry_or_fall_back(self):
        result = self.cli("nodes", "--json", hub=self.url + "/redirect")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "redirect_refused")
        self.assertEqual(len(self.calls), 1)
        result = self.cli("register", "uncertain", "--url", "http://remote.invalid",
                          "--token-file", str(self.remote_token), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "write_result_unknown")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        for args in (("run", "--any-free", "--", "node"), ("migrate", "old", "--from", "a", "--to", "b"),
                     ("update", "fixture", "--note", "changed")):
            rejected = self.cli(*args)
            self.assertEqual(rejected.returncode, 2)
            self.assertNotIn("not-a-registry", rejected.stdout + rejected.stderr)
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main()
