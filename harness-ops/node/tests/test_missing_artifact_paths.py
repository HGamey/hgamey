"""A missing output path must not claim that its existing job is absent."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import TestCase
from urllib.parse import urlencode

import test_node_read as read_fixture
import dash


class MissingArtifactPathTests(TestCase):
    setUp = read_fixture.NodeReadTests.setUp
    tearDown = read_fixture.NodeReadTests.tearDown
    fixture = read_fixture.NodeReadTests.fixture
    request = read_fixture.NodeReadTests.request
    get_json = read_fixture.NodeReadTests.get_json

    def test_missing_output_paths_are_distinct_in_token_and_dashboard_reads(self):
        jid = self.fixture(1)
        path = "nested/missing file.json"
        self.assertEqual(self.get_json(f"/jobs/{jid}")["id"], jid)
        view = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"}
        for prefix, headers in (("", {}), ("/dash/api", view)):
            for route, extra in (("out", {}), ("out", {"meta": "1"}), ("artifacts", {})):
                with self.subTest(prefix=prefix, route=route, extra=extra):
                    status, _, raw = self.request(f"{prefix}/jobs/{jid}/{route}?" +
                                                  urlencode({"path": path, **extra}), headers=headers)
                    value = json.loads(raw)
                    self.assertEqual(status, 404, value)
                    self.assertEqual(value["code"], "artifact_path_not_found", value)
                    self.assertEqual(value["path"], path)
                    self.assertIn(path, value["error"])
                    self.assertNotIn("no such job", value["error"])
        self.assertEqual(self.request(f"/jobs/{jid}/out?path=image.bin")[2], bytes(range(256)))

    def test_missing_jobs_still_report_job_not_found(self):
        for suffix in ("", "/out?path=missing", "/out?path=missing&meta=1", "/artifacts?path=missing"):
            with self.subTest(suffix=suffix):
                status, _, raw = self.request("/jobs/ffffffffffff" + suffix)
                self.assertEqual(status, 404, raw)
                self.assertEqual(json.loads(raw), {"code": "job_not_found", "error": "no such job"})

    def test_cli_prints_the_missing_path_error_without_a_client_change(self):
        jid = self.fixture(1)
        cli = Path(__file__).resolve().parents[1] / "pf-node"
        env = dict(os.environ, PF_NODE_URL="http://%s:%s" % self.server.server_address,
                   PF_NODE_TOKEN=self.ro, PF_NODE_ID=self.node.node_id)
        env.pop("PF_NODE_TOKEN_FILE", None)
        result = subprocess.run([sys.executable, str(cli), "out", jid, "nested/missing"],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact_path_not_found", result.stderr)
        self.assertIn("nested/missing", result.stderr)
        self.assertNotIn("job_not_found", result.stderr)
