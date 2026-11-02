"""Browser visits to a legacy public hostname move to the canonical one; API clients do not."""
import http.client
from http.server import ThreadingHTTPServer
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import node_service
from node_service import Jobs, make_handler

ADMIN = "canonical-host-fixture-not-production"
CANONICAL = "new.example.test"


class CanonicalHostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.jobs = Jobs(str(root / "node"), {"fixture": (sys.executable, str(root))})
        self.addCleanup(self.jobs._read_root.close)
        handler = make_handler(None, self.jobs, ADMIN, canonical_host=CANONICAL)
        self.addCleanup(self.jobs.directory.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, host, headers=None, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True)
            for key, value in {"Host": host, **(headers or {})}.items():
                connection.putheader(key, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("Location")
        finally:
            connection.close()

    def test_browser_pages_on_legacy_host_redirect_with_path_and_query(self):
        for method in ("GET", "HEAD"):
            for path in ("/", "/dash", "/dash/", "/dash/jobs?x=1", "/dash/login"):
                with self.subTest(method=method, path=path):
                    status, location = self.request(method, path, "old.example.test")
                    self.assertEqual(status, 308)
                    self.assertEqual(location, "https://" + CANONICAL + path)

    def test_api_post_and_authorized_requests_are_not_redirected(self):
        old = "old.example.test:443"
        self.assertEqual(self.request("GET", "/health", old)[0], 200)
        self.assertEqual(self.request("GET", "/node", old,
                                      {"Authorization": "Bearer " + ADMIN})[0], 200)
        self.assertNotEqual(self.request("GET", "/dash/api/home", old)[0], 308)
        self.assertNotEqual(self.request("GET", "/dash/", old,
                                         {"Authorization": "Bearer " + ADMIN})[0], 308)
        self.assertNotEqual(self.request("POST", "/dash/login", old,
                                         {"Content-Type": "application/x-www-form-urlencoded"}, b"pw=x")[0], 308)

    def test_canonical_local_and_lan_hosts_are_served_directly(self):
        for host in (CANONICAL, CANONICAL.upper() + ":443", "127.0.0.1:8765", "localhost:8765",
                     "192.168.3.91:8765", "[::1]:8765", "macmini3.local", "macmini3"):
            with self.subTest(host=host):
                self.assertNotEqual(self.request("GET", "/dash/", host)[0], 308)

    def test_service_accepts_canonical_host_argument(self):
        args = node_service.argument_parser().parse_args(["--canonical-host", CANONICAL])
        self.assertEqual(args.canonical_host, CANONICAL)


if __name__ == "__main__":
    unittest.main()
