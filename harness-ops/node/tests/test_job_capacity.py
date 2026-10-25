"""HTTP admission and shared SQL configuration; no worker is launched."""
import http.client
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_capacity import CapacityExceeded
from node_service import Jobs, read_host_capacity, readonly_token
import test_node_games as fixture


class JobCapacityTests(unittest.TestCase):
    def test_http_frozen_requests_and_persistent_capacity_changes(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins["fixture"] = (sys.executable, str(env.root))
        queue = env.jobs.dispatch.queue
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        base = {"bin": "fixture", "argv": ["-c", "pass"], "resources": []}
        slots = {"cpu": 2, "gpu": 0, "io": 0}
        spec = {**base, "host_slots": slots}

        def submit(key, value, data=None):
            return env.call("POST", "/jobs", data, headers={**pin, "X-Request-ID": key,
                                                            "X-Job": json.dumps(value)})

        def configure(value=None, max_running=2):
            # A second Jobs instance has no HTTP server/dispatcher; it represents
            # an explicit restart config or another reader of the same SQL root.
            return Jobs(str(env.root), {}, data_dir=str(env.root), max_running=max_running,
                        host_capacity=value).dispatch.queue.capacity()

        code, identity = env.call("GET", "/node", token=readonly_token(env.admin), headers=pin)
        self.assertEqual(code, 200, identity)
        self.assertIn("jobs.host-capacity-v1", identity["capabilities"])
        initial = identity["execution"]["host_capacity"]
        self.assertEqual(initial["slots"], slots)
        self.assertEqual(initial["source"], "initial_default")
        self.assertEqual(initial["basis"], "declared_scheduler_slots")

        oversized = {**base, "host_slots": {**slots, "cpu": 3}}
        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare:
            for declaration, expected in ((oversized, 422), ({**base, "host_slots": None}, 400),
                    ({**spec, "device_access": "recovery", "recovery_for": "a" * 12}, 400)):
                # No upload body is sent. Early rejection must not wait for it.
                connection = http.client.HTTPConnection(*env.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/jobs")
                    for key, value in {**pin, "Authorization": "Bearer " + env.admin,
                            "X-Request-ID": "refused-capacity", "X-Job": json.dumps(declaration),
                            "Content-Length": "65536"}.items():
                        connection.putheader(key, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, expected, result)
                    if expected == 422:
                        self.assertEqual(result["code"], "host_capacity_exceeded")
                        self.assertEqual(result["requested"]["cpu"], 3)
                        self.assertEqual(result["total"]["cpu"], 2)
                    else:
                        self.assertIn("host_slots", result["error"])
                finally:
                    connection.close()
            # Direct callers must still pass the same intake admission.
            with self.assertRaises(CapacityExceeded):
                env.jobs.start(oversized, b"", request_id="direct-over-capacity")
            prepare.assert_not_called()
        self.assertEqual(queue.rows(), [])

        code, declared = submit("capacity-original", spec)
        self.assertEqual(code, 200, declared)
        original = queue.get(declared["id"])
        self.assertEqual(original["spec"], {**spec, "label": None})
        self.assertEqual(declared["queue"]["host_capacity"]["source"], "explicit")
        code, legacy = submit("capacity-legacy", base)
        self.assertEqual(code, 200, legacy)
        self.assertEqual(queue.get(legacy["id"])["spec"], {**base, "label": None})
        self.assertEqual(legacy["queue"]["host_capacity"]["slots"], {"cpu": 1, "gpu": 0, "io": 0})
        self.assertEqual(legacy["queue"]["host_capacity"]["source"], "legacy_default")

        policy_file = env.root / "capacity.json"
        small = {"cpu": 1, "gpu": 0, "io": 0}
        policy_file.write_text(json.dumps({"schema": "phonefarm/host-capacity@1", "slots": small}))
        self.assertEqual(read_host_capacity(policy_file), small)
        shrunk = configure(read_host_capacity(policy_file))
        self.assertEqual(shrunk["source"], "file")
        # Omission and a same-valued file preserve the original policy timestamp.
        self.assertEqual(configure(max_running=8), shrunk)
        self.assertEqual(configure(small), shrunk)
        self.assertEqual(queue.capacity(), shrunk)
        current = env.call("GET", "/node", headers=pin)[1]["execution"]["host_capacity"]
        self.assertEqual(current, shrunk, "an already-running reader must not cache old totals")
        code, waiting = env.call("GET", "/jobs/" + declared["id"], headers=pin)
        self.assertEqual((code, waiting["phase"]), (200, "queued"))
        self.assertEqual(waiting["queue"]["waiting"]["reason"], "request_exceeds_current_total")

        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare:
            code, repeated = submit("capacity-original", spec)
            self.assertEqual((code, repeated["id"]), (200, declared["id"]))
            self.assertEqual(submit("capacity-original", {**base, "host_slots": small})[0], 409)
            self.assertEqual(submit("capacity-original", spec, {"different": "body"})[0], 409)
            self.assertEqual(submit("capacity-new-oversized", spec)[0], 422)
            prepare.assert_not_called()
        self.assertEqual(queue.get(declared["id"])["fingerprint"], original["fingerprint"])
        self.assertEqual(len(queue.rows()), 2)

        larger = {"cpu": 3, "gpu": 1, "io": 1}
        self.assertEqual(configure(larger)["slots"], larger)
        code, waiting = env.call("GET", "/jobs/" + declared["id"], headers=pin)
        self.assertEqual(code, 200, waiting)
        self.assertIsNone(waiting["queue"]["waiting"]["kind"])
        code, snapshot = env.call("GET", "/queue", headers=pin)
        self.assertEqual(code, 200, snapshot)
        self.assertEqual(snapshot["host_capacity"]["reserved"], {"cpu": 0, "gpu": 0, "io": 0})
        self.assertEqual(snapshot["host_capacity"]["available"], larger)
        self.assertIsNone(env.jobs.dispatch.thread)
        for job in (declared, legacy):
            self.assertFalse((Path(env.jobs.path(job["id"])) / "launch.json").exists())
            code, receipt = env.call("POST", "/jobs/" + job["id"] + "/cancel", {}, headers=pin)
            self.assertEqual((code, receipt["accepted"], receipt["job"]["outcome"]), (200, True, "cancelled"))

        policy_file.write_bytes(b" " * 4097)
        with self.assertRaisesRegex(ValueError, "4 KiB"):
            read_host_capacity(policy_file)


if __name__ == "__main__":
    unittest.main()
