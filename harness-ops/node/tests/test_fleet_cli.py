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


if __name__ == "__main__":
    unittest.main()
