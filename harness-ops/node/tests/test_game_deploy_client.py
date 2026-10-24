import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLI = Path(__file__).resolve().parents[1] / "pf-node"


class GameDeployClient(unittest.TestCase):
    def test_lost_deployment_submission_checks_capability_and_recovers_original_request(self):
        calls = []
        job = {"id": "0123456789ab", "request_id": "deploy-001", "state": "running",
               "phase": "queued", "queue": {"resources": ["device:fixture-device"]},
               "recovery": {"status": "not_reported"}, "producer_evidence": {"kept": True}}

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append({"method": self.command, "path": self.path, "body": body,
                              "request_id": self.headers.get("X-Request-ID")})
                if self.command == "GET" and self.path == "/node":
                    value = {"schema_version": 1, "node_id": "fixture-node", "capabilities": ["games.deploy-v1"]}
                elif self.command == "POST" and self.path == "/games/fixture/versions/v1/deploy":
                    self.close_connection = True  # Accepted; response lost before reaching the client.
                    return
                elif self.command == "GET" and self.path == "/requests/deploy-001":
                    value = job
                else:
                    self.send_error(404)
                    return
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_request

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ, PF_NODE_URL=f"http://127.0.0.1:{server.server_port}",
                       PF_NODE_TOKEN="fixture-deploy-token", NO_PROXY="127.0.0.1,localhost",
                       no_proxy="127.0.0.1,localhost")
            result = subprocess.run([sys.executable, str(CLI), "games", "deploy", "fixture", "v1",
                                     "--serial", "fixture-device", "--check", "--request-id", job["request_id"]],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), job)
            self.assertIn("request_id=deploy-001", result.stderr)
            self.assertEqual([(call["method"], call["path"]) for call in calls], [
                ("GET", "/node"), ("POST", "/games/fixture/versions/v1/deploy"),
                ("GET", "/requests/deploy-001")])
            self.assertEqual(calls[1]["request_id"], "deploy-001")
            self.assertEqual(json.loads(calls[1]["body"]),
                             {"node_id": "fixture-node", "serial": "fixture-device", "mode": "check"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
