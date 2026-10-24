import base64
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_service import Jobs, make_handler, readonly_token
from game_assets import AssetUploadError
import test_node_games as game_fixture


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()

    def tearDown(self):
        self.env.tearDown()

    def test_interrupted_http_restart_cli_resume_and_original_verification_job(self):
        env = self.env
        payload = bytes(range(256)) * 8193
        manifest = env.manifest()
        asset = manifest["assets"][0]
        asset.update(name="base/demo data.pk4", sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
        manifest["assets"].append({"name": "other", "role": "resource", "size": 7,
            "sha256": hashlib.sha256(b"missing").hexdigest(),
            "source": {"kind": "unavailable", "reason": "not part of this upload"}})
        self.assertEqual(env.call("POST", "/games", manifest)[0], 201)
        body = {"upload_id": "resume-fixture", "game_id": "demo", "version": "v1", "asset_name": asset["name"]}
        path = "/uploads/resume-fixture"
        self.assertEqual(env.call("GET", path)[1]["code"], "upload_not_found")
        with patch.object(env.jobs.games.cache, "upload_open", side_effect=AssetUploadError(
                503, "upload_storage_error", "fixture interruption after binding")):
            self.assertEqual(env.call("POST", "/uploads", body)[0], 503)
        incomplete = env.call("GET", path)[1]
        self.assertEqual((incomplete["phase"], incomplete["offset"]), ("unclaimed", None))
        code, initial = env.call("POST", "/uploads", body)
        self.assertEqual((code, initial["phase"], initial["offset"]), (201, "receiving", 0))
        self.assertEqual(initial["created_at"], incomplete["created_at"])
        abandoned = {**body, "upload_id": "abandoned", "asset_name": "other"}
        with patch.object(env.jobs.games.cache, "upload_open", side_effect=AssetUploadError(
                503, "upload_storage_error", "fixture interruption after binding")):
            self.assertEqual(env.call("POST", "/uploads", abandoned)[0], 503)
        self.assertEqual(env.call("POST", "/uploads/abandoned/cancel", {})[1]["phase"], "cancelled")
        self.assertEqual(env.call("POST", "/uploads", abandoned)[1]["phase"], "cancelled")
        self.assertEqual(env.call("POST", "/uploads", {**body, "asset_name": "other"})[0], 409)

        def put(data, token=env.admin, headers=None, short=False):
            connection = http.client.HTTPConnection("127.0.0.1", env.server.server_port, timeout=5)
            request_headers = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream",
                "Upload-Offset": "0", "Content-Length": str(512 << 10 if short else len(data)), **(headers or {})}
            try:
                connection.request("PUT", "/dash/api" + path if headers else path, data, request_headers)
                if short:
                    connection.sock.shutdown(socket.SHUT_WR)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        self.assertEqual(put(b"x", token=readonly_token(env.admin))[0], 401)
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1"}
        self.assertEqual(put(b"x", headers=view)[0], 403)
        steer = {**view, "Cookie": dash.STEER_COOKIE + "=" + env.board._cookie_val("test-steer", b"hgamey-steer-v1")}
        self.assertEqual(put(b"x", headers={**steer, "X-HG": "0"})[0], 400)
        # The body ends early. Its confirmed prefix survives a new server/manager;
        # subsequent CLI resume sends only the suffix, without a source URL.
        prefix = 123457
        self.assertEqual(put(payload[:prefix], headers=steer, short=True)[0], 400)
        self.assertEqual(env.call("GET", path)[1]["offset"], prefix)
        old = env.jobs
        env.server.shutdown()
        env.server.server_close()
        env.thread.join()
        if old.dispatch.thread:
            old.dispatch.thread.join(timeout=4)
        old.directory.close()
        old.games.close()
        old._read_root.close()
        env.jobs = Jobs(str(env.root), {}, data_dir=str(env.root))
        env.board.job_service = env.jobs
        env.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, env.jobs, env.admin, env.board))
        env.thread = threading.Thread(target=env.server.serve_forever, daemon=True)
        env.thread.start()
        env.url = "http://127.0.0.1:" + str(env.server.server_port)
        restored = env.call("GET", path)[1]
        self.assertEqual((restored["node_id"], restored["offset"], restored["created_at"]),
                         (initial["node_id"], prefix, initial["created_at"]))
        source = env.root / "synthetic-game.bin"
        source.write_bytes(payload)
        result = env.cli("upload", "demo", "v1", asset["name"], str(source), "--upload-id", "resume-fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        uploaded = json.loads(result.stdout)
        jid = uploaded["job"]["id"]
        self.assertEqual(env.call("POST", path + "/finalize", {})[1]["job"]["id"], jid)
        self.assertEqual(env.finish(jid)["exit"], 0)
        record = env.call("GET", "/dash/api" + path, headers=view)[1]
        self.assertEqual((record["phase"], record["observation"]["state"], record["offset"]),
                         ("verified", "verified", len(payload)))
        self.assertEqual(env.call("POST", path + "/finalize", {})[1]["job"]["id"], jid)
        self.assertEqual(len(env.jobs.dispatch.queue.rows()), 1)
        report = env.jobs._read_root.read_json(jid + "/out/preparation.json")
        self.assertEqual(report["scope"]["asset_name"], asset["name"])
        self.assertEqual([item["name"] for item in report["assets"]], [asset["name"]])
        game = env.call("GET", "/games/demo/versions/v1")[1]
        self.assertEqual(game["resource_state"], "incomplete")
        self.assertEqual(game["device_state"]["installed"], "unknown")
        self.assertEqual(env.jobs.games.cache.observe(asset)["state"], "verified")
        # A generic job may use this request namespace, but an upload must never
        # display that job as its verifier or accept its unrelated input body.
        self.assertEqual(env.call("POST", "/uploads", {**body, "upload_id": "conflict"})[0], 201)
        foreign = env.jobs.start({"bin": "bundle_prepare", "argv": ["-c", "pass"],
                                  "device_access": "none", "resources": []}, b"",
                                 request_id="upload-verify:conflict")
        self.assertEqual(env.finish(foreign["id"])["exit"], 0)
        self.assertEqual(env.call("GET", "/uploads/conflict"),
                         (409, {"code": "upload_verification_conflict",
                                "error": "verification request ID belongs to a different job; no job is adopted"}))
        self.assertEqual(env.board.last_view, 0)
        self.assertFalse(env.board.wake.is_set())


if __name__ == "__main__":
    unittest.main()
