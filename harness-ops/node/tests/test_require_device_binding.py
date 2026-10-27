"""Node-level require-device-binding admission: opt-in multi-device discipline."""
import http.client
import json
from http.server import ThreadingHTTPServer
import tempfile
import threading
import unittest
from pathlib import Path

NODE = Path(__file__).resolve().parents[1]
sys_path = str(NODE)
import sys
sys.path.insert(0, sys_path)

from node_service import Jobs, make_handler

ADMIN = "binding-required-fixture-not-production"


class BindingRequiredTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jobs = Jobs(str(self.root / "node"), {"fixture": (sys.executable, str(self.root))},
                         require_device_binding=True)
        self.addCleanup(self.jobs._read_root.close)
        handler = make_handler(None, self.jobs, ADMIN)
        self.addCleanup(self.jobs.directory.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def post_job(self, spec, request_id):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        body = json.dumps(spec).encode()
        try:
            connection.request("POST", "/jobs", body=body,
                               headers={"Authorization": "Bearer " + ADMIN,
                                        "X-Expected-Node-ID": self.jobs.games.node_id,
                                        "X-Job": json.dumps(spec), "X-Request-ID": request_id,
                                        "Content-Type": "application/gzip"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("GET", path, headers={
                "Authorization": "Bearer " + ADMIN, "X-Expected-Node-ID": self.jobs.games.node_id})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_capability_reported_and_host_jobs_unaffected(self):
        identity = self.get("/node")[1]
        self.assertIn("jobs.require-device-binding-v1", identity["capabilities"])
        self.assertEqual(identity["execution"].get("require_device_binding"), True)
        status, job = self.post_job({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none"},
                                    "host-ok")
        self.assertEqual(status, 200, job)

    def test_write_without_binding_refused_and_none_admitted(self):
        status, refused = self.post_job({"bin": "fixture", "argv": ["-c", "pass"],
                                         "device_access": "write"}, "no-binding")
        self.assertEqual(status, 400, refused)
        self.assertIn("device_binding", json.dumps(refused))
        # device_access=none 的宿主任务不受影响。
        status, admitted = self.post_job({"bin": "fixture", "argv": ["-c", "pass"],
                                          "device_access": "none"}, "none-ok")
        self.assertEqual(status, 200, admitted)


if __name__ == "__main__":
    unittest.main()
