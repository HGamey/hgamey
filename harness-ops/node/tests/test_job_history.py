"""Explicit historical sources remain distinct and never become current jobs."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode


NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
from node_fleet import FleetState
from node_history import load_history_config
from node_read import NodeRead
from node_service import Jobs, make_handler, readonly_token

JID = "a0123456789b"
OTHER = "b0123456789b"
BAD = "c0123456789b"


class NoMcp:
    def handle(self, *_):
        raise AssertionError("history must not call MCP or a device")


class HistoryFixture:
    """Also usable by the one-off browser fixture; no worker or PF executable."""
    def __init__(self, root=None, port=0, *, allow_current_queue=False):
        self.temp = tempfile.TemporaryDirectory(prefix="job-history-") if root is None else None
        self.root = Path(self.temp.name).resolve() if self.temp else Path(root).resolve()
        if not self.temp:
            self.root.mkdir(parents=True)
        self.source_roots = {"old-a": self.root / "old A" / "_jobs",
                             "old-b": self.root / "old B" / "_jobs"}
        self.revisions = {"old-a": 1, "old-b": 3, "missing": 2}
        self.payloads = {"old-a": b"alpha historical evidence\n", "old-b": b"beta historical evidence\n"}
        self.records = {}
        self.log = b"zero\none\ntwo\n"
        for source, directory in self.source_roots.items():
            (directory / JID / "out").mkdir(parents=True)
            record = {"id": JID, "label": source + " primary", "bin": "legacy-analysis",
                      "argv": ["historical-only"], "started": 2, "state": "running", "pid": os.getpid()}
            self.records[source] = record
            (directory / JID / "meta.json").write_text(json.dumps(record))
            (directory / JID / "log").write_bytes(self.log)
            (directory / JID / "out" / "result.bin").write_bytes(self.payloads[source])
        a = self.source_roots["old-a"]
        (a / JID / "exitcode").write_bytes(b"7\n")
        (a / OTHER).mkdir()
        (a / OTHER / "meta.json").write_text(json.dumps({"id": OTHER, "label": "older record",
                                                        "started": 1, "exit": 0}))
        (a / BAD).mkdir()
        (a / BAD / "meta.json").write_bytes(b"{broken metadata")
        (self.root / "outside.bin").write_bytes(b"not a historical artifact")
        (a / JID / "out" / "link.bin").symlink_to(self.root / "outside.bin")
        self.config = self.root / "history.json"
        self.config.write_text(json.dumps({"schema": "hgamey/job-history@1", "sources": [
            {"source_id": source, "revision": self.revisions[source], "label": source,
             "jobs_root": str(directory)} for source, directory in {
                 **self.source_roots, "missing": self.root / "missing" / "_jobs"}.items()]}))
        for directory in self.source_roots.values():
            self.permissions(directory, readonly=True)
        self.before = self.snapshot()
        current = self.root / "current"
        self.jobs = Jobs(str(current), {}, history_sources=load_history_config(str(self.config)))
        # Same job ID in the current namespace must never receive a historical mutation.
        current_job = Path(self.jobs.path(JID))
        (current_job / "out").mkdir(parents=True)
        (current_job / "meta.json").write_text(json.dumps({"id": JID, "label": "CURRENT",
            "bin": "current-fixture", "argv": [], "started": 3, "exit": 9}))
        self.current_original = (current_job / "meta.json").read_bytes()
        self.node = NodeRead(self.jobs, self.jobs.data_root, str(NODE))
        self.admin = "history-fixture-admin-not-production"
        self.readonly = readonly_token(self.admin)
        self.password = "history-fixture-view-password"
        self.board = dash.Dash.__new__(dash.Dash)
        self.board._pass = lambda: self.password
        self.board._steer_pass = lambda: None
        self.board.job_service = self.jobs
        self.board.last_view, self.board.wake = 0, threading.Event()
        self.fleet = FleetState(str(current), str(self.root / "unused-locks"), str(self.root / "unused-shots"))
        self.board.monitor = self.fleet
        self.guards = [patch("node_service._pid_alive", side_effect=AssertionError("history inspected saved PID")),
                       patch.object(self.jobs, "start", side_effect=AssertionError("history started a current job")),
                       patch.object(self.jobs, "cancel", side_effect=AssertionError("history cancelled a current job"))]
        if not allow_current_queue:
            self.guards.append(patch.object(self.jobs.leases, "snapshot", side_effect=AssertionError("history queried current lease")))
        self.guard_mocks = [guard.start() for guard in self.guards]
        self.requests = []
        requests = self.requests
        base = make_handler(NoMcp(), self.jobs, self.admin, self.board, self.node, self.fleet)

        class Handler(base):
            def do_GET(self):
                requests.append(("GET", self.path, self.headers.get("X-Expected-Node-ID")))
                return super().do_GET()

            def do_POST(self):
                requests.append(("POST", self.path, self.headers.get("X-Expected-Node-ID")))
                return super().do_POST()

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = f"{dash.COOKIE}={self.board._cookie_val(self.password)}"

    @staticmethod
    def permissions(root, readonly):
        for directory, dirs, files in os.walk(root, followlinks=False):
            os.chmod(directory, 0o555 if readonly else 0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o444 if readonly else 0o600)

    def snapshot(self):
        result = {}
        for source, root in self.source_roots.items():
            for directory, dirs, files in os.walk(root, followlinks=False):
                for path in [Path(directory), *(Path(directory) / name for name in files)]:
                    st = path.lstat()
                    value = os.readlink(path) if stat.S_ISLNK(st.st_mode) else (
                        path.read_bytes() if stat.S_ISREG(st.st_mode) else None)
                    result[(source, str(path.relative_to(root)))] = (st.st_mode, st.st_mtime_ns, value)
        return result

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.jobs.history.close()
            self.jobs.directory.close()
            self.jobs.games.close()
            self.fleet.close()
            self.jobs._read_root.close()
            if self.thread.is_alive():
                raise RuntimeError("historical HTTP fixture thread did not exit")
        finally:
            for guard in reversed(self.guards):
                guard.stop()
            for directory in self.source_roots.values():
                self.permissions(directory, readonly=False)
            if self.temp:
                self.temp.cleanup()

    def request(self, path, *, method="GET", headers=None, auth=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        values = {"X-Expected-Node-ID": self.node.node_id,
                  **({"Authorization": "Bearer " + self.readonly} if auth else {}), **(headers or {})}
        try:
            connection.request(method, path, body=b"{}" if method == "POST" else None, headers=values)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def path(self, source="old-a", suffix="", **query):
        return f"/job-sources/{source}/jobs{suffix}?" + urlencode({
            "source_revision": self.revisions[source], **query})

    def cli(self, *args):
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "--url", self.url,
                               "--node-id", self.node.node_id, *args], cwd=self.root,
                              env={"PATH": os.defpath, "HOME": str(self.root), "PF_NODE_TOKEN": self.readonly,
                                   "NO_PROXY": "*", "no_proxy": "*"},
                              capture_output=True, text=True, timeout=10)


class JobHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HistoryFixture()
        self.addCleanup(self.fixture.close)

    def get(self, path, **kwargs):
        status, _, raw = self.fixture.request(path, **kwargs)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def unchanged(self):
        f = self.fixture
        self.assertEqual(f.snapshot(), f.before, "history read changed bytes, metadata or namespace")
        self.assertEqual(Path(f.jobs.path(JID, "meta.json")).read_bytes(), f.current_original)
        self.assertEqual(f.jobs.dispatch.queue.rows(), [])
        for guard in f.guard_mocks:
            guard.assert_not_called()

    def test_readonly_http_cli_keeps_same_job_ids_and_artifacts_separate(self):
        f = self.fixture
        sources = self.get("/job-sources")
        self.assertEqual((sources["schema"], sources["node_id"]), ("hgamey/job-sources@1", f.node.node_id))
        self.assertEqual({item["source_id"] for item in sources["items"]}, {"old-a", "old-b", "missing"})
        self.assertTrue(all(item["read_only"] for item in sources["items"]))
        self.assertIn("jobs.history-read-v1", self.get("/node")["capabilities"])
        first = self.get(f.path(limit=1))
        self.assertEqual([item["id"] for item in first["items"]], [JID])
        self.assertIsNotNone(first["next_cursor"])
        next_page = self.get(f.path(limit=1, cursor=first["next_cursor"]))
        self.assertEqual([item["id"] for item in next_page["items"]], [OTHER])
        self.assertIsNone(next_page["next_cursor"])
        for source in ("old-a", "old-b"):
            detail = self.get(f.path(source, "/" + JID))
            self.assertEqual((detail["node_id"], detail["source_id"], detail["source_revision"]),
                             (f.node.node_id, source, f.revisions[source]))
            self.assertEqual(detail["record"], f.records[source])
            self.assertEqual(detail["observation"], "historical_readonly")
            self.assertEqual(detail["recorded_state"], "running")
            self.assertEqual(detail["recorded_exit"], 7 if source == "old-a" else None)
            self.assertEqual(detail["recorded_exit_source"], "exitcode" if source == "old-a" else None)
            status, _, log = f.request(f.path(source, f"/{JID}/log", offset=5, limit=4))
            self.assertEqual((status, log), (200, b"one\n"))
            artifacts = self.get(f.path(source, f"/{JID}/artifacts"))
            self.assertIn("result.bin", [item["path"] for item in artifacts["items"]])
            status, headers, raw = f.request(f.path(source, f"/{JID}/out", path="result.bin"),
                                             headers={"Range": "bytes=1-4"})
            self.assertEqual((status, raw), (206, f.payloads[source][1:5]))
            self.assertEqual(headers["Content-Range"], f"bytes 1-4/{len(f.payloads[source])}")
            cli = f.cli("out", JID, "result.bin", "--source", source, "--source-revision", str(f.revisions[source]))
            self.assertEqual(cli.returncode, 0, cli.stderr)
            downloaded = f.root / "pf-node-out" / source / f"revision-{f.revisions[source]}" / JID / "result.bin"
            self.assertEqual(downloaded.read_bytes(), f.payloads[source])
        self.assertTrue(all(method == "GET" for method, _, _ in f.requests))
        self.assertTrue(all(pin == f.node.node_id for _, _, pin in f.requests))
        self.unchanged()

    def test_failed_source_bindings_never_fall_back_or_authorize_mutation(self):
        f = self.fixture
        page = self.get(f.path(limit=1))
        self.assertIn({"job": BAD, "error": "metadata_unavailable"}, page["warnings"])
        self.assertGreaterEqual(page["warning_count"], 1)
        cases = [(f.path(source_revision=2), 409, "history_source_changed"),
                 (f.path("old-b", cursor=page["next_cursor"]), 400, "history_invalid_request"),
                 (f.path("missing"), 503, "history_source_unavailable"),
                 (f.path(suffix=f"/{JID}/out", path="link.bin"), 400, "history_artifact_unavailable"),
                 (f.path(suffix=f"/{JID}/out"), 409, "history_directory_export_unsupported")]
        for path, expected, code in cases:
            with self.subTest(path=path):
                status, _, raw = f.request(path)
                self.assertEqual((status, json.loads(raw)["code"]), (expected, code))
        status, _, raw = f.request(f.path(), headers={"X-Expected-Node-ID": "0" * 32})
        self.assertEqual((status, json.loads(raw)["code"]), (409, "target_mismatch"))
        dashboard = "/dash/api" + f.path(suffix="/" + JID)
        self.assertEqual(f.request(dashboard, auth=False)[0], 401)
        self.assertEqual(self.get(dashboard, auth=False, headers={"Cookie": f.cookie})["record"], f.records["old-a"])
        for prefix, headers in (("", {}), ("/dash/api", {"Cookie": f.cookie})):
            status, _, raw = f.request(prefix + f.path(suffix=f"/{JID}/cancel"), method="POST", headers=headers)
            self.assertEqual((status, json.loads(raw)["code"]), (405, "history_read_only"))
        before_cli = len(f.requests)
        cli = f.cli("status", JID, "--source", "old-a", "--source-revision", "2")
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("history_source_changed", cli.stderr + cli.stdout)
        self.assertFalse(any(path.startswith("/jobs/") for _, path, _ in f.requests[before_cli:]))
        self.unchanged()


if __name__ == "__main__":
    unittest.main()
