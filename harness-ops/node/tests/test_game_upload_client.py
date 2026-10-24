import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLI = Path(__file__).resolve().parents[1] / "pf-node"


class GameUploadClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "local-private-name.bin"
        self.payload = b"abcdefgh"
        self.file.write_bytes(self.payload)
        self.asset = {"name": "data/a b.pk4", "role": "resource", "size": len(self.payload),
                      "sha256": hashlib.sha256(self.payload).hexdigest(),
                      "source": {"kind": "unavailable", "reason": "fixture upload"}}
        self.manifest = {"schema_version": 1, "game_id": "fixture", "version": "v1",
                         "title": "Fixture", "assets": [self.asset]}
        self.record = None
        self.stored = bytearray()
        self.calls = []
        self.fault = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.calls.append({"method": self.command, "path": self.path, "body": body,
                                    "offset": self.headers.get("Upload-Offset"),
                                    "content_type": self.headers.get("Content-Type")})
                if self.command == "GET" and self.path == "/node":
                    return self.reply({"schema_version": 1, "node_id": "fixture-node",
                                       "capabilities": ["games.upload-v1"]})
                if self.command == "GET" and self.path == "/games/fixture/versions/v1":
                    return self.reply({"schema_version": 1, "node_id": "fixture-node", "game_id": "fixture",
                                       "version": "v1", "manifest": owner.manifest})
                if self.command == "POST" and self.path == "/uploads":
                    identity = json.loads(body)
                    owner.record = {"schema_version": 1, "node_id": "fixture-node", **identity,
                                    "sha256": owner.asset["sha256"], "size": owner.asset["size"],
                                    "offset": 0, "phase": "receiving", "error": None,
                                    "chunk_max_bytes": 4, "created_at": 1,
                                    "observation": {"state": "missing"}, "job": None}
                    return self.reply(owner.record)
                if owner.record is not None and self.path == "/uploads/" + owner.record["upload_id"]:
                    if self.command == "GET":
                        return self.reply(owner.record)
                    if self.command == "PUT":
                        offset = int(self.headers["Upload-Offset"])
                        if offset != len(owner.stored):
                            return self.reply({"code": "upload_offset_mismatch"}, 409)
                        fault, owner.fault = owner.fault, None
                        owner.stored.extend(body[:2] if fault == "disconnect-prefix" else body)
                        owner.record["offset"] = len(owner.stored)
                        owner.record["phase"] = "received" if len(owner.stored) == owner.asset["size"] else "receiving"
                        if fault == "disconnect-prefix":
                            self.close_connection = True
                            return
                        value = dict(owner.record)
                        if fault == "wrong-offset":
                            value["offset"] = offset  # Stored bytes exist, but this acknowledgement is invalid.
                        return self.reply(value)
                if (owner.record is not None and self.command == "POST"
                        and self.path == "/uploads/" + owner.record["upload_id"] + "/finalize"):
                    owner.record.update(phase="verifying", job={"id": "0123456789ab", "phase": "queued",
                                        "request_id": "upload-verify:" + owner.record["upload_id"],
                                        "producer_evidence": {"keep": True}})
                    return self.reply(owner.record)
                return self.reply({"code": "upload_not_found"}, 404)

            do_GET = do_POST = do_PUT = handle_request

            def reply(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def upload(self, upload_id):
        env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{self.server.server_port}",
                   PF_NODE_TOKEN="upload-fixture-token", NO_PROXY="127.0.0.1,localhost",
                   no_proxy="127.0.0.1,localhost")
        return subprocess.run([sys.executable, str(CLI), "games", "upload", "fixture", "v1",
                               self.asset["name"], str(self.file), "--upload-id", upload_id],
                              env=env, capture_output=True, text=True, timeout=8)

    def test_interrupted_prefix_stops_then_explicit_same_id_resumes_at_confirmed_offset(self):
        self.fault = "disconnect-prefix"
        first = self.upload("prefix-001")
        self.assertEqual(first.returncode, 2, first.stderr)
        found = json.loads(first.stdout)
        self.assertEqual((found["upload_id"], found["offset"], found["job"]), ("prefix-001", 2, None))
        self.assertEqual(len([c for c in self.calls if c["method"] == "PUT"]), 1)
        self.assertFalse(any(c["path"].endswith("/finalize") for c in self.calls))
        self.assertEqual(self.calls[-1]["path"], "/uploads/prefix-001")
        resumed = self.upload("prefix-001")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout), self.record)
        self.assertEqual(self.record["phase"], "verifying")
        self.assertEqual(self.record["observation"]["state"], "missing")
        self.assertEqual(bytes(self.stored), self.payload)
        puts = [c for c in self.calls if c["method"] == "PUT"]
        self.assertEqual([(c["offset"], c["body"]) for c in puts], [("0", b"abcd"), ("2", b"cdef"), ("6", b"gh")])
        self.assertTrue(all(c["content_type"] == "application/octet-stream" for c in puts))
        self.assertEqual(len([c for c in self.calls if c["method"] == "POST" and c["path"] == "/uploads"]), 1)
        self.assertEqual(len([c for c in self.calls if c["path"].endswith("/finalize")]), 1)
        self.assertFalse(any(str(self.file).encode() in c["body"] for c in self.calls))

    def test_unconfirmed_put_offset_only_queries_without_sending_next_chunk(self):
        # Registration persisted its binding, but stopped before claiming bytes.
        self.record = {"schema_version": 1, "node_id": "fixture-node", "upload_id": "offset-001",
                       "game_id": "fixture", "version": "v1", "asset_name": self.asset["name"],
                       "sha256": self.asset["sha256"], "size": self.asset["size"],
                       "offset": None, "phase": "unclaimed", "error": None,
                       "chunk_max_bytes": 4, "created_at": 1,
                       "observation": {"state": "missing"}, "job": None}
        self.fault = "wrong-offset"
        result = self.upload("offset-001")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["offset"], 4)
        self.assertIn("精确持久 offset", result.stderr)
        self.assertEqual(bytes(self.stored), b"abcd")
        self.assertEqual([(c["method"], c["path"]) for c in self.calls], [
            ("GET", "/node"), ("GET", "/games/fixture/versions/v1"),
            ("GET", "/uploads/offset-001"), ("POST", "/uploads"),
            ("PUT", "/uploads/offset-001"), ("GET", "/uploads/offset-001")])
        self.assertEqual(json.loads(self.calls[3]["body"]), {"upload_id": "offset-001", "game_id": "fixture",
                                                          "version": "v1", "asset_name": self.asset["name"]})
        self.assertFalse(any(c["path"].endswith("/finalize") for c in self.calls))


if __name__ == "__main__":
    unittest.main()
