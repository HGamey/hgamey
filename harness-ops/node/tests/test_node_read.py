import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
from node_service import Jobs, make_handler, readonly_token
from node_read import NodeRead
from node_fleet import FleetState


class NoMcp:
    def handle(self, msg, allow):
        raise AssertionError("host-only reads must not invoke phonefarm")


class NodeReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = Jobs(str(self.root), {})
        self.node = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.admin = "unit-test-admin-credential-not-production"
        self.ro = readonly_token(self.admin)
        self.pw = "unit-test-dash-password"
        # Use actual dashboard authentication and routing, without starting samplers.
        self.board = dash.Dash.__new__(dash.Dash)
        self.board._pass = lambda: self.pw
        self.board.last_view = 0
        self.board.wake = threading.Event()
        self.board.monitor = FleetState(str(self.root), str(self.root / "locks"), str(self.root / "shots"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            NoMcp(), self.jobs, self.admin, self.board, self.node))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.board.monitor.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def fixture(self, n, **extra):
        jid = f"{n:012x}"
        p = Path(self.jobs.path(jid))
        (p / "out" / "nested").mkdir(parents=True)
        (p / "log").write_bytes(b"example log\n" * 10000)
        (p / "out" / "nested" / "result.json").write_text('{"metric": 2.413144}\n')
        (p / "out" / "image.bin").write_bytes(bytes(range(256)))
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["custom-analysis"],
                                   "label": "Doom study", "started": n, "exit": 0, **extra})
        return jid

    def request(self, path, *, method="GET", token=None, headers=None, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        h = {"Authorization": "Bearer " + (token or self.ro), **(headers or {})}
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        status, response_headers, content = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return status, response_headers, content

    def get_json(self, path, **kw):
        status, _, body = self.request(path, **kw)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_identity_survives_restart_and_data_root_is_explicit(self):
        again = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.assertEqual(again.node_id, self.node.node_id)
        identity = self.get_json("/node")
        self.assertEqual(identity["node_id"], self.node.node_id)
        self.assertEqual(identity["storage"]["jobs_root"], self.jobs.dir)
        self.assertEqual(identity["devices"]["status"], "not_probed")
        self.assertIsNone(identity["service"]["revision"])
        separate = Jobs(str(self.root), {}, str(self.root / "runtime"))
        self.assertEqual(separate.dir, str(self.root / "runtime" / "jobs"))
        self.assertTrue((self.root / "_jobs").is_dir())

    def test_arbitrary_old_failed_jobs_are_paginated_and_searchable(self):
        for i in range(1, 206):
            self.fixture(i, exit=7 if i == 1 else 0)
        seen, cursor = [], None
        while True:
            query = {"limit": 50, "query": "custom-analysis"}
            if cursor:
                query["cursor"] = cursor
            page = self.get_json("/jobs?" + urlencode(query))
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(seen), 205)
        self.assertEqual(len(set(seen)), 205)
        oldest = self.get_json("/jobs?query=000000000001")["items"]
        self.assertEqual(oldest[0]["exit"], 7)
        self.assertEqual(oldest[0]["state"], "finished")

    def test_cursor_stays_on_query_and_missing_metadata_is_visible(self):
        for i in range(1, 4):
            self.fixture(i)
        (Path(self.jobs.dir) / "eeeeeeeeeeee").mkdir()
        (Path(self.jobs.dir) / "eeeeeeeeeeee" / "meta.json").write_text("{")
        page = self.get_json("/jobs?limit=1")
        self.assertEqual(page["warning_count"], 1)
        self.assertEqual(page["warnings"][0]["job"], "eeeeeeeeeeee")
        cursor = page["next_cursor"]
        self.fixture(4)  # Newer insertion must not duplicate the previous page.
        following = self.get_json("/jobs?" + urlencode({"cursor": cursor, "limit": 50}))
        self.assertEqual([m["started"] for m in following["items"]], [2, 1])
        for q in ({"cursor": cursor, "query": "changed"}, {"limit": 201}, {"cursor": "!bad"}, {"state": "success"}):
            self.assertEqual(self.request("/jobs?" + urlencode(q))[0], 400)

    def test_readonly_can_read_bounded_log_artifacts_and_ranged_output(self):
        jid = self.fixture(1)
        self.assertEqual(self.get_json(f"/jobs/{jid}")["id"], jid)
        entries = self.get_json(f"/jobs/{jid}/artifacts")["items"]
        self.assertEqual([e["name"] for e in entries], ["image.bin", "nested"])
        meta = self.get_json(f"/jobs/{jid}/out?path=image.bin&meta=1")
        self.assertEqual(meta["sha256"], hashlib.sha256(bytes(range(256))).hexdigest())
        status, h, body = self.request(f"/jobs/{jid}/out?path=image.bin", headers={"Range": "bytes=10-19"})
        self.assertEqual((status, body), (206, bytes(range(10, 20))))
        self.assertEqual(h["Content-Disposition"], "attachment")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(len(self.request(f"/jobs/{jid}/log?offset=10&limit=65536")[2]), 65536)
        self.assertEqual(self.request(f"/jobs/{jid}/log?offset=-1")[0], 400)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin&length=-1&offset=0")[0], 416)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin", headers={"Range": "bytes=20-10"})[0], 416)
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin&etag=old")[0], 409)

    def test_readonly_and_dash_password_cannot_mutate(self):
        jid = self.fixture(1)
        for path in ("/jobs", f"/jobs/{jid}/kill", "/admin/restart"):
            self.assertEqual(self.request(path, method="POST", body=b"")[0], 401)
        self.assertEqual(self.request("/node", token="wrong")[0], 401)
        self.assertEqual(self.request("/jobs", method="DELETE")[0], 401)
        self.assertEqual(self.request(f"/jobs/{jid}", headers={"Authorization": "Basic " +
            base64.b64encode(("user:" + self.pw).encode()).decode()})[0], 401)

    def test_dashboard_uses_same_catalog_without_waking_phone_sampler(self):
        jid = self.fixture(1)
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        self.assertEqual(self.get_json("/dash/api/node", headers=auth)["node_id"], self.node.node_id)
        for path in ("/jobs", f"/jobs/{jid}", f"/jobs/{jid}/artifacts"):
            self.assertEqual(self.get_json(path), self.get_json("/dash/api" + path, headers=auth))
        self.assertEqual(self.request(f"/dash/api/jobs/{jid}/out?path=image.bin", headers=auth)[2], bytes(range(256)))
        self.assertEqual(self.request("/dash/api/jobs")[0], 401)
        # Static files may be absent during development, but route must never wake samplers.
        self.request("/dash/jobs", headers=auth)
        self.request("/dash/static/jobs.js", headers=auth)
        self.assertEqual(self.board.last_view, 0)
        self.assertFalse(self.board.wake.is_set())

    def test_fleet_readers_share_one_sampler_without_jobs_or_screenshots(self):
        monitor = self.board.monitor
        self.get_json("/node")
        self.get_json("/jobs")
        self.assertIsNone(monitor._thread)
        self.assertEqual(self.request("/fleet", token="wrong")[0], 401)
        self.assertIsNone(monitor._thread)
        auth = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        sample = {"devices": [{"serial": "fixture-only", "online": True,
                               "thermal": {"soc_max_c": 42}}], "notes": []}
        with patch("node_fleet.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(sample).encode(), b"")) as run:
            first = self.get_json("/fleet?wait=2")
            second = self.get_json("/dash/api/fleet?wait=2", headers=auth)
            self.assertEqual(first["devices"], sample["devices"])
            self.assertEqual(first["sampled_at"], second["sampled_at"])
            self.assertEqual(first["node_id"], self.node.node_id)
            self.assertFalse(first["stale"])
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("--screenshot-dir", run.call_args.args[0])
            self.assertNotIn("DEVLOCK_LABEL", run.call_args.kwargs["env"])
            self.assertEqual(list(Path(self.jobs.dir).iterdir()), [])
            # A failed refresh preserves last-good data and its original timestamp.
            with monitor._condition:
                monitor._sampled_at -= 100
                previous_at = monitor._sampled_at
                monitor._next_at = 0
            run.side_effect = subprocess.CalledProcessError(1, "fixture-only")
            failed = self.get_json("/fleet?wait=2")
            self.assertEqual(failed["sampled_at"], previous_at)
            self.assertEqual(failed["devices"], sample["devices"])
            self.assertTrue(failed["stale"])
            self.assertIn("CalledProcessError", failed["error"])
            # An incompatible sample is a failed attempt, not a dead sampler thread.
            with monitor._condition:
                monitor._next_at = 0
            run.side_effect = None
            run.return_value.stdout = b'{"devices":[{"online":true,"battery":"unavailable"}]}'
            incompatible = self.get_json("/fleet?wait=2")
            self.assertIn("battery must be an object", incompatible["error"])
            self.assertFalse(incompatible["refreshing"])
            self.assertEqual(incompatible["devices"], sample["devices"])
            with monitor._condition:
                monitor._next_at = 0
            run.return_value.stdout = json.dumps(sample).encode()
            recovered = self.get_json("/fleet?wait=2")
            self.assertIsNone(recovered["error"])
            self.assertFalse(recovered["stale"])
        for wait in ("nan", "6", "-1"):
            self.assertEqual(self.request("/fleet?wait=" + wait)[0], 400)

    def test_artifact_pagination_and_cross_directory_cursor_rejected(self):
        jid = self.fixture(1)
        page = self.get_json(f"/jobs/{jid}/artifacts?limit=1")
        next_page = self.get_json(f"/jobs/{jid}/artifacts?" + urlencode({"cursor": page["next_cursor"]}))
        self.assertEqual(next_page["items"][0]["name"], "nested")
        self.assertEqual(self.request(f"/jobs/{jid}/artifacts?" + urlencode(
            {"cursor": page["next_cursor"], "path": "nested"}))[0], 400)

    def test_paths_and_symlinks_cannot_expose_host_files(self):
        jid = self.fixture(1)
        secret = self.root / "private.config"
        secret.write_text("must-not-read")
        out = Path(self.jobs.path(jid, "out"))
        (out / "external").symlink_to(secret)
        (out / "linked-directory").symlink_to(self.root, target_is_directory=True)
        entries = self.get_json(f"/jobs/{jid}/artifacts")["items"]
        self.assertEqual(next(e for e in entries if e["name"] == "external")["kind"], "unavailable")
        for rel in ("../meta.json", str(secret), "external", "linked-directory/private.config"):
            for endpoint in ("out", "artifacts"):
                status, _, body = self.request(f"/jobs/{jid}/{endpoint}?" + urlencode({"path": rel}))
                self.assertEqual(status, 400)
                self.assertNotIn(b"must-not-read", body)
        self.assertEqual(self.request(f"/jobs/{jid}/out")[0], 400)  # Whole archive cannot carry links either.
        (Path(self.jobs.dir) / "ffffffffffff").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.request("/jobs/ffffffffffff")[0], 400)
        self.assertEqual(self.request("/jobs/../../private.config")[0], 404)

    def test_cli_queries_use_the_same_read_contract(self):
        jid = self.fixture(1)
        cli = Path(__file__).resolve().parents[1] / "pf-node"
        env = dict(os.environ, PF_NODE_URL="http://%s:%s" % self.server.server_address, PF_NODE_TOKEN=self.ro)
        for args in (["node"], ["jobs", "--query", "Doom"], ["artifacts", jid, "--path", "nested"]):
            result = subprocess.run([sys.executable, str(cli), *args], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
