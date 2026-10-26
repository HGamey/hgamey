"""Real HTTP/CLI admission with synthetic reservations; no worker or device IO."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_node_games as fixture


class HostResourceTests(unittest.TestCase):
    def test_host_defaults_and_original_requests_through_http_and_cli(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins["phonefarm"] = (sys.executable, str(env.root))
        queue = env.jobs.dispatch.queue
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        host = {"bin": "phonefarm", "argv": ["eval"], "device_access": "none", "label": None}
        requests, cli_calls = [], []

        def submit(key, spec, data=None):
            code, result = env.call("POST", "/jobs", data, headers={**pin,
                "X-Request-ID": key, "X-Job": json.dumps(spec)})
            requests.append({"request_id": key, "status": code})
            return code, result

        def accepted(key, spec):
            code, result = submit(key, spec)
            self.assertEqual(code, 200, result)
            return result["id"]

        def cli(key):
            process_env = {**os.environ, "PF_NODE_TOKEN": env.admin,
                           "PF_NODE_URL": env.url, "PF_NODE_ID": env.jobs.node_id}
            process_env.pop("PF_NODE_TOKEN_FILE", None)
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "pf-node"),
                "--request-id", key, "--device-access", "none", "--detach", "phonefarm", "eval"],
                cwd=env.root, env=process_env, capture_output=True, text=True, timeout=10)
            cli_calls.append({"request_id": key, "exit": result.returncode})
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        # Explicit legacy resources create exactly the normalized old-default
        # record through real HTTP, without mirroring its fingerprint algorithm.
        old_id = accepted("before-host-default-fix", {**host, "resources": ["legacy-device"]})
        original = queue.get(old_id)
        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare, \
                patch.object(env.jobs.programs, "require_session",
                             side_effect=AssertionError("existing ID must skip new admission")):
            self.assertEqual(cli("before-host-default-fix"), old_id)
            self.assertEqual(submit("before-host-default-fix", {**host, "argv": ["different"]})[0], 409)
            self.assertEqual(submit("before-host-default-fix", host, {"different": "body"})[0], 409)
            self.assertEqual(submit("before-host-default-fix", {**host, "resources": []})[0], 409)
            prepare.assert_not_called()
        self.assertEqual(queue.get(old_id), original, "retry must not rewrite the original reservation")

        first = accepted("host-http", host)
        second = cli("host-cli")
        self.assertEqual(queue.get(first)["resources"], [], "explicit none must not inherit legacy-device")
        self.assertEqual(queue.get(second)["resources"], [])
        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare:
            self.assertEqual(cli("host-cli"), second)
            self.assertEqual(submit("host-http", {**host, "resources": ["legacy-device"]})[0], 409)
            prepare.assert_not_called()

        device = accepted("device-blocker", {"bin": "phonefarm", "argv": ["eval"]})
        self.assertEqual(queue.get(device)["resources"], ["legacy-device"])
        self.assertNotIn("device_access", queue.get(device)["spec"])
        # Scheduling premise only: a retained device obligation whose host
        # capacity has already been released. This does not prove recovery.
        queue.set_state(device, "blocked", {"kind": "fixture_device_recovery"},
                        host_capacity_release={"at": 1, "basis": "fixture_host_completed"})
        third = accepted("host-awaits-capacity", host)

        def cached_lease(**options):
            self.assertFalse(options.get("refresh"), "host-only dispatch must not refresh device leases")
            return {"status": "blocked", "entries": [], "error": None}

        with patch.object(env.jobs.leases, "snapshot", side_effect=cached_lease), \
                patch.object(env.jobs.dispatch, "launch") as launch:
            env.jobs.dispatch.tick()
        self.assertEqual([call.args[0]["job_id"] for call in launch.call_args_list], [first, second])
        self.assertEqual(queue.get(third)["state"], "queued")
        self.assertEqual(queue.capacity_snapshot()["reserved"], {"cpu": 2, "gpu": 0, "io": 0})
        self.assertEqual(queue.get(old_id), original)
        self.assertEqual(env.jobs.status(old_id)["queue"]["waiting"]["kind"], "resource")
        self.assertEqual(queue.get(device)["state"], "blocked")
        self.assertEqual(queue.get(device)["resources"], ["legacy-device"])

        write = accepted("explicit-write", {**host, "device_access": "write"})
        self.assertEqual(queue.get(write)["resources"], ["legacy-device"])
        named = accepted("host-explicit-resource", {**host, "resources": ["legacy-device"]})
        self.assertEqual(env.jobs.status(named)["queue"]["waiting"]["kind"], "resource")
        recovery = accepted("recover-device", {**host, "device_access": "recovery",
                            "recovery_for": device, "resources": ["ignored-recovery-override"]})
        self.assertEqual(queue.get(recovery)["resources"], queue.get(device)["resources"])
        self.assertEqual(queue.get(recovery)["spec"]["recovery_for"], device)
        self.assertEqual(len(queue.rows()), 8, "all retries must preserve the original job count")
        print(json.dumps({"scope": "HTTP/CLI admission and synthetic scheduler only; no workers/devices",
                          "http": requests, "cli": cli_calls,
                          "original_job": old_id, "host_claimed": [first, second],
                          "device_still_blocked": device, "jobs": len(queue.rows())}, sort_keys=True))
