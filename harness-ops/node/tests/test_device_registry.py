"""Node forwards one PF registry operation; it never owns another registry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
import test_node_games as fixture
from node_service import readonly_token


PROGRAM = r'''
import json, os, sys
from pathlib import Path
root = Path(__file__).parent
with (root / "pf-calls.jsonl").open("a") as output:
    output.write(json.dumps({"argv": sys.argv[1:], "body": sys.stdin.read(),
        "cwd": os.getcwd(), "state_dir": os.environ.get("PHONEFARM_STATE_DIR"),
        "job_env": {k:v for k,v in os.environ.items() if k.startswith("PF_JOB_") or k == "PF_RUNTIME_REPORT"}}) + "\n")
response = json.loads((root / "pf-response.json").read_text())
print(json.dumps(response["body"]))
if "stderr" in response:
    print(response["stderr"], file=sys.stderr)
raise SystemExit(response["exit"])
'''


class DeviceRegistryTests(unittest.TestCase):
    def test_http_cli_authority_target_and_single_pf_protocol(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        calls = env.root / "pf-calls.jsonl"
        program = env.root / "configured-pf"
        program.write_text("#!" + sys.executable + "\n" + PROGRAM)
        program.chmod(0o755)
        # Add after NodeRead startup, so this fixture needs only the registry
        # protocol and cannot accidentally accept a runtime/device command.
        env.jobs.bins["phonefarm"] = (str(program), str(env.root))
        env.jobs.pf_env = {"PATH": os.defpath, "PHONEFARM_STATE_DIR": str(env.root / "isolated-state"),
                           "PF_JOB_ID": "must-not-leak", "PF_JOB_OUT": "/not-a-job",
                           "PF_RUNTIME_REPORT": str(env.root / "must-not-exist.json")}
        node_id = env.jobs.games.node_id
        pinned = {"X-Expected-Node-ID": node_id}
        record = {"schema": "phonefarm/device-registry@1", "revision": 0, "devices": [],
                  "updated_at": None, "identity_basis": "operator_declared",
                  "physical_identity_verified": False, "execution_scope": "global"}

        def reply(body, code=0, **extra):
            (env.root / "pf-response.json").write_text(json.dumps({"body": body, "exit": code, **extra}))

        def observed():
            return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

        def cli(*args):
            process_env = {**os.environ, "PF_NODE_URL": env.url, "PF_NODE_ID": node_id,
                           "PF_NODE_TOKEN": env.admin, "HOME": str(env.root)}
            process_env.pop("PF_NODE_TOKEN_FILE", None)
            return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                                   *args], env=process_env, capture_output=True, text=True, timeout=10)

        view = {"Cookie": f"{dash.COOKIE}={env.board._cookie_val(env.board._pass())}", **pinned}
        steer = {**view, "X-HG": "1", "Cookie": view["Cookie"] + "; " + dash.STEER_COOKIE + "=" +
                 env.board._cookie_val(env.board._steer_pass(), b"hgamey-steer-v1")}
        request = {"expected_revision": 0, "device": {"device_id": "desk-a", "label": "Desk A",
                    "aliases": ["usb-fixture", "192.0.2.1:5555"], "enabled": True}}
        reply(record)
        with patch.object(env.jobs, "start", side_effect=AssertionError("registry must not create jobs")), \
             patch.object(env.board.monitor, "request", side_effect=AssertionError("registry must not sample devices")):
            status, identity = env.call("GET", "/node")
            self.assertEqual(status, 200)
            self.assertIn("devices.registry-v1", identity["capabilities"])
            self.assertEqual(observed(), [])
            for path, token, headers in (("/node/devices", readonly_token(env.admin), {}),
                                         ("/dash/api/node/devices", "invalid", view)):
                status, value = env.call("GET", path, token=token, headers=headers)
                self.assertEqual((status, value), (200, {**record, "node_id": node_id}))

            before = len(observed())
            # Every refusal occurs before dispatching the PF helper.
            self.assertEqual(env.call("POST", "/node/devices", request,
                                      token=readonly_token(env.admin), headers=pinned)[0], 401)
            self.assertEqual(env.call("POST", "/node/devices", request)[0], 400)
            self.assertEqual(env.call("POST", "/node/devices", request,
                                      headers={"X-Expected-Node-ID": "f" * 32})[0], 409)
            self.assertEqual(env.call("POST", "/dash/api/node/devices", request,
                                      token="invalid", headers={**view, "X-HG": "1"})[0], 403)
            self.assertEqual(env.call("POST", "/dash/api/node/devices", request,
                                      token="invalid", headers={k:v for k,v in steer.items() if k != "X-HG"})[0], 400)
            self.assertEqual(len(observed()), before)

            record = {**record, "revision": 1, "devices": [request["device"]],
                      "updated_at": "2026-10-25T00:00:00Z"}
            reply(record)
            request_file = env.root / "register.json"
            request_file.write_text(json.dumps(request))
            saved = cli("devices-set", str(request_file))
            self.assertEqual(saved.returncode, 0, saved.stderr)
            self.assertEqual(json.loads(saved.stdout), {**record, "node_id": node_id})
            self.assertEqual(len(observed()), before + 1)
            self.assertEqual(json.loads(observed()[-1]["body"]), request)
            self.assertEqual(observed()[-1]["argv"], ["lease", "devices-set", "--request", "-", "--json"])
            readback = cli("devices")
            self.assertEqual(readback.returncode, 0, readback.stderr)
            self.assertEqual(json.loads(readback.stdout)["revision"], 1)
            self.assertEqual(observed()[-1]["argv"], ["lease", "devices", "--json"])

            reply({"schema": "phonefarm/device-registry-error@1", "code": "conflict", "error": "revision changed"}, 2)
            before = len(observed())
            rejected = cli("devices-set", str(request_file))
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(len(observed()), before + 1, "a conflict must not retry or rediscover")
            status, failed = env.call("POST", "/dash/api/node/devices", request, token="invalid", headers=steer)
            self.assertEqual((status, failed["code"]), (409, "conflict"))
            # A supported Node API does not pretend an older PF returned an
            # empty directory, nor expose arbitrary helper diagnostics.
            reply({"legacy": True}, 0, stderr="fixture-private-diagnostic")
            status, unavailable = env.call("GET", "/node/devices")
            self.assertEqual((status, unavailable["code"]), (503, "unavailable"))
            self.assertNotIn("fixture-private-diagnostic", json.dumps(unavailable))
        for call in observed():
            self.assertEqual(call["cwd"], str(env.root.resolve()))
            self.assertEqual(call["state_dir"], str(env.root / "isolated-state"))
            self.assertEqual(call["job_env"], {})
        self.assertFalse((env.root / "must-not-exist.json").exists())
        self.assertEqual(env.jobs.dispatch.queue.rows(), [])


if __name__ == "__main__":
    unittest.main()
