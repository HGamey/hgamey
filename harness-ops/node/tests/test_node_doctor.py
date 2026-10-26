import base64
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_node_read
import node_doctor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_node_read.NodeReadTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.pin = {"X-Expected-Node-ID": self.fixture.node.node_id}

    def test_readonly_api_dashboard_and_cli_use_same_host_only_report(self):
        f = self.fixture
        with patch.object(f.jobs.programs, "refresh", side_effect=AssertionError("must not run configured programs")), \
             patch.object(f.board.monitor, "request", side_effect=AssertionError("must not wake device observer")):
            self.assertEqual(f.request("/doctor")[0], 400)
            self.assertEqual(f.request("/doctor", headers={"X-Expected-Node-ID": "0" * 32})[0], 409)
            value = f.get_json("/doctor", headers=self.pin)
            self.assertEqual(value["scope"], "host-only")
            self.assertEqual(sum(value["summary"].values()), len(value["checks"]))
            self.assertEqual(next(c for c in value["checks"] if c["id"] == "devices.qualification")["status"], "unknown")
            auth = {"Authorization": "Basic " + base64.b64encode((":" + f.pw).encode()).decode(), **self.pin}
            browser = f.get_json("/dash/api/doctor", headers=auth)
            self.assertEqual(browser["summary"], value["summary"])
            for path in ("/dash/doctor", "/dash/static/doctor.js", "/dash/static/doctor.css"):
                self.assertEqual(f.request(path, headers=auth)[0], 200)
            self.assertEqual(f.board.last_view, 0)
            self.assertFalse(f.board.wake.is_set())
            token = f.root / "readonly-token"
            token.write_text(f.ro)
            token.chmod(0o600)
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                       "--url", "http://127.0.0.1:" + str(f.server.server_port), "--node-id", f.node.node_id,
                       "--token-file", str(token), "doctor", "--json"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            cli = json.loads(result.stdout)
            self.assertEqual(cli["schema"], value["schema"])
            self.assertEqual(cli["node_id"], value["node_id"])
            self.assertEqual(cli["summary"], value["summary"])
            # A broken or incompatible server must not turn a known error into
            # CLI success just because its aggregate count says zero.
            value["checks"][0]["status"] = "error"
            with patch.object(node_doctor, "report", return_value=value):
                invalid = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(invalid.returncode, 1, invalid.stderr)
            self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_response")
            for name in ("doctor", "update-status"):
                bad_target = command.copy()
                bad_target[bad_target.index("--url") + 1] = "not-a-url"
                bad_target[-2] = name
                invalid = subprocess.run(bad_target, capture_output=True, text=True, timeout=15)
                self.assertEqual(invalid.returncode, 1, invalid.stderr)
                self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_url")

    def test_stale_binary_wrong_host_format_missing_storage_stay_explicit(self):
        f = self.fixture
        binary = f.root / "incompatible-binary"
        binary.write_bytes(b"\x7fELFfixture-not-executed")
        binary.chmod(0o700)
        f.jobs.bins["phonefarm"] = (str(binary), str(f.root))
        with patch.object(f.jobs.programs, "snapshot", return_value={"phonefarm": {"state": "stale", "observed_at": 123}}), \
             patch.object(node_doctor.platform, "system", return_value="Darwin"), \
             patch.object(node_doctor.subprocess, "run", side_effect=AssertionError("no program or device execution")):
            old_root = f.node.data_root
            f.node.data_root = str(f.root / "not-mounted")
            try:
                value = f.get_json("/doctor", headers=self.pin)
            finally:
                f.node.data_root = old_root
        checks = {item["id"]: item for item in value["checks"]}
        self.assertEqual(checks["phonefarm.identity"]["status"], "warning")
        self.assertEqual(checks["phonefarm.format"]["status"], "error")
        self.assertEqual(checks["path.data"]["status"], "error")
        self.assertEqual(checks["node.connection"]["status"], "ok")
        self.assertEqual(checks["devices.qualification"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
