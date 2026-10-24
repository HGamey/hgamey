import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_fleet import FleetState
from node_games import NodeGames
from node_service import Jobs, make_handler, readonly_token


class GameFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(str(self.root), {}, data_dir=str(self.root))
        self.admin = "game-flow-test-only-admin-credential"
        self.board = dash.Dash.__new__(dash.Dash)
        self.board._pass = lambda: "test-view"
        self.board._steer_pass = lambda: "test-steer"
        self.board.job_service = self.jobs
        self.board.last_view = 0
        self.board.wake = threading.Event()
        self.board.monitor = FleetState(str(self.root), str(self.root / "locks"), str(self.root / "shots"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, self.jobs, self.admin, self.board))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.source = None

    def tearDown(self):
        if self.source:
            self.source.shutdown()
            self.source.server_close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.jobs.dispatch.thread:
            self.jobs.dispatch.thread.join(timeout=4)
        self.jobs.directory.close()
        self.jobs.games.close()
        self.board.monitor.close()
        self.temp.cleanup()

    def call(self, method, path, data=None, token=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        values = {"Authorization": "Bearer " + (self.admin if token is None else token),
                  "Content-Type": "application/json", **(headers or {})}
        try:
            connection.request(method, path, body=None if data is None else json.dumps(data), headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def manifest(self, **extra):
        return {"schema_version": 1, "game_id": "demo", "version": "v1", "title": "Fixture game",
                "assets": [{"name": "data", "role": "resource", "sha256": hashlib.sha256(b"fixture").hexdigest(),
                            "size": 7, "source": {"kind": "unavailable", "reason": "historical metadata only"}}], **extra}

    def cli(self, *args):
        env = {**os.environ, "PF_NODE_TOKEN": self.admin, "PF_NODE_URL": self.url, "HOME": str(self.root)}
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                               "games", *args], env=env, text=True, capture_output=True, timeout=15)

    def finish(self, jid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.jobs.status(jid)
            if job["state"] == "finished":
                return job
            time.sleep(.05)
        self.fail("preparation did not finish: " + json.dumps(job))

    def test_cli_http_worker_cache_and_immutable_history(self):
        payload, requests = b"fixture", []

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        threading.Thread(target=self.source.serve_forever, daemon=True).start()
        manifest = self.manifest()
        manifest["assets"][0]["source"] = {"kind": "url", "url": f"http://127.0.0.1:{self.source.server_port}/game"}
        source = self.root / "manifest.json"
        source.write_text(json.dumps(manifest))
        registered = self.cli("register", str(source))
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout)["resource_state"], "incomplete")
        prepared = self.cli("prepare", "demo", "v1", "--request-id", "games-fixture-prepare")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        job = json.loads(prepared.stdout)
        path = "/games/demo/versions/v1"
        status, repeat = self.call("POST", path + "/prepare", {}, headers={"X-Request-ID": "games-fixture-prepare"})
        self.assertEqual((status, repeat["id"]), (200, job["id"]))
        finished = self.finish(job["id"])
        self.assertEqual(finished["exit"], 0, finished)
        self.assertEqual(finished["device_access"], "none")
        status, record = self.call("GET", path, token=readonly_token(self.admin))
        self.assertEqual((status, record["resource_state"]), (200, "verified"))
        self.assertEqual(record["device_state"]["scene_ready"], "unknown")
        self.assertEqual(record["preparations"][0]["id"], job["id"])
        self.assertIsNotNone(record["preparations"][0]["report"])
        self.assertEqual(requests, ["/game"])
        # A new attempt reads the verified content object; no network or device call.
        _, second = self.call("POST", path + "/prepare", {}, headers={"X-Request-ID": "games-cache-reuse"})
        self.assertEqual(self.finish(second["id"])["exit"], 0)
        self.assertEqual(requests, ["/game"])
        saved_at = record["created_at"]
        reopened = NodeGames(self.jobs, record["node_id"])
        try:
            restored = reopened.get("demo", "v1")
        finally:
            reopened.close()
        self.assertEqual(restored["created_at"], saved_at)
        self.assertEqual(len(restored["preparations"]), 2)
        self.assertEqual(self.call("POST", "/games", {**manifest, "title": "changed"})[0], 409)
        self.assertEqual(self.call("POST", "/games", manifest)[1]["created_at"], saved_at)
        self.assertEqual(self.call("POST", "/games", {**manifest, "version": "v2"})[0], 201)
        self.assertEqual(self.call("POST", "/games/demo/versions/v2/prepare", {},
                                   headers={"X-Request-ID": "games-fixture-prepare"})[0], 409)
        self.assertEqual(self.cli("show", "demo", "v1").returncode, 0)
        blob = Path(self.jobs.games.cache.object_path(manifest["assets"][0]["sha256"]))
        blob.chmod(0o600)
        blob.write_bytes(b"changed")
        changed = self.jobs.games.get("demo", "v1")
        self.assertEqual(changed["resource_state"], "unknown")
        self.assertEqual(changed["assets"][0]["observation"]["state"], "unknown")
        self.assertEqual(changed["assets"][0]["progress"]["state"], "verified")

    def test_web_permissions_and_historical_missing_source(self):
        manifest = self.manifest()
        self.assertEqual(self.call("POST", "/games", manifest, token=readonly_token(self.admin))[0], 401)
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1"}
        self.assertEqual(self.call("POST", "/dash/api/games", manifest, headers=view)[0], 403)
        steer = {**view, "Cookie": dash.STEER_COOKIE + "=" + self.board._cookie_val("test-steer", b"hgamey-steer-v1")}
        status, record = self.call("POST", "/dash/api/games", manifest, headers=steer)
        self.assertEqual((status, record["resource_state"]), (201, "incomplete"))
        path = "/dash/api/games/demo/versions/v1"
        self.assertEqual(self.call("GET", path, headers=view)[1]["manifest"], manifest)
        self.assertEqual(self.call("POST", path + "/prepare", {}, headers=steer)[0], 400)
        _, job = self.call("POST", path + "/prepare", {}, headers={**steer, "X-Request-ID": "missing-resource"})
        self.assertNotEqual(self.finish(job["id"])["exit"], 0)
        result = self.call("GET", path, headers=view)[1]
        self.assertEqual(result["resource_state"], "incomplete")
        self.assertEqual(result["assets"][0]["observation"]["state"], "unavailable")
        self.assertEqual(result["device_state"]["installed"], "unknown")
        self.assertEqual(self.board.last_view, 0)
        self.assertFalse(self.board.wake.is_set())


if __name__ == "__main__":
    unittest.main()
