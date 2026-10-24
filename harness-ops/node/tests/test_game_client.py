import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLI = Path(__file__).resolve().parents[1] / "pf-node"
TOKEN = "fixture-game-token"
PREPARE = "/games/fixture/versions/v1/prepare"


class GameClient(unittest.TestCase):
    def server(self, route):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append({"method": self.command, "path": self.path, "body": body,
                              "request_id": self.headers.get("X-Request-ID"),
                              "authorization": self.headers.get("Authorization")})
                response = route(calls[-1])
                if response is None:
                    self.close_connection = True
                    return
                status, headers, value = response
                data = json.dumps(value).encode()
                self.send_response(status)
                for key, val in headers.items():
                    self.send_header(key, val)
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

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}", calls

    def cli(self, url, *args):
        env = dict(os.environ, PF_NODE_URL=url, PF_NODE_TOKEN=TOKEN,
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return subprocess.run([sys.executable, str(CLI), "games", *args], env=env,
                              capture_output=True, text=True, timeout=5)

    def test_lost_prepare_response_queries_same_request_without_reposting(self):
        job = {"id": "0123456789ab", "request_id": "prepare-001", "state": "running",
               "phase": "queued", "producer_evidence": {"kept": True}}

        def route(call):
            if call["method"] == "POST" and call["path"] == PREPARE:
                return None  # Accepted, but no HTTP response reaches the caller.
            return 200, {}, job

        url, calls = self.server(route)
        result = self.cli(url, "prepare", "fixture", "v1", "--request-id", job["request_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), job)
        self.assertIn("request_id=prepare-001", result.stderr)
        self.assertEqual([(c["method"], c["path"]) for c in calls],
                         [("POST", PREPARE), ("GET", "/requests/prepare-001")])
        self.assertEqual(calls[0]["request_id"], "prepare-001")
        self.assertEqual(calls[0]["body"], b"{}")

    def test_post_and_uncertain_request_lookup_never_follow_redirects(self):
        other, leaked = self.server(lambda _: (200, {}, {"unexpected": True}))
        # A distinct hostname as well as port makes credential forwarding observable.
        destination = other.replace("127.0.0.1", "localhost") + "/credential-sink"

        def route(call):
            if call["method"] == "POST" and call["request_id"] == "lost-lookup":
                return None
            return 307, {"Location": destination}, {"error": TOKEN}

        url, calls = self.server(route)
        redirected = self.cli(url, "prepare", "fixture", "v1", "--request-id", "redirected")
        uncertain = self.cli(url, "prepare", "fixture", "v1", "--request-id", "lost-lookup")
        self.assertEqual(redirected.returncode, 2, redirected.stderr)
        self.assertEqual(uncertain.returncode, 2, uncertain.stderr)
        self.assertIn("重定向", redirected.stderr)
        self.assertIn("games request lost-lookup", uncertain.stderr)
        self.assertEqual([(c["method"], c["path"]) for c in calls],
                         [("POST", PREPARE), ("POST", PREPARE), ("GET", "/requests/lost-lookup")])
        self.assertEqual(leaked, [])
        self.assertNotIn(TOKEN, redirected.stdout + redirected.stderr + uncertain.stdout + uncertain.stderr)


if __name__ == "__main__":
    unittest.main()
