"""Deadline admission and queue facts; worker launch is mocked, no child runs."""
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime
import job_events
import worker_config
import test_node_games as fixture


class JobTimeBudgetTests(unittest.TestCase):
    def test_disk_guard_admission_frozen_launch_and_optional_event_fields(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins["fixture"] = (sys.executable, str(env.root))
        queue = env.jobs.dispatch.queue
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        spec = {"bin": "fixture", "argv": ["-c", "pass"], "resources": []}
        guard = {"max_log_bytes": 4096, "min_free_bytes": 1024, "stop_action": "escalate"}
        declared = {**spec, "disk_guard": guard}

        def submit(rid, value):
            return env.call("POST", "/jobs", headers={**pin, "X-Request-ID": rid,
                                                        "X-Job": json.dumps(value)})

        self.assertIn("jobs.disk-guard-v1", env.call("GET", "/node")[1]["capabilities"])
        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare:
            for invalid in ({**spec, "disk_guard": None},
                            {**declared, "disk_guard": {**guard, "max_log_bytes": True}},
                            {**declared, "device_access": "recovery", "recovery_for": "a" * 12}):
                connection = http.client.HTTPConnection(*env.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/jobs")
                    for key, value in {**pin, "Authorization": "Bearer " + env.admin,
                                       "X-Job": json.dumps(invalid), "Content-Length": "65536"}.items():
                        connection.putheader(key, value)
                    connection.endheaders()  # Deliberately never send an upload body.
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 400, payload)
                    self.assertIn("disk_guard", payload["error"])
                finally:
                    connection.close()
                with self.assertRaisesRegex(ValueError, "disk_guard"):
                    env.jobs.start(invalid, b"", request_id="invalid-guard")
            prepare.assert_not_called()
        self.assertEqual(queue.rows(), [])

        code, legacy = submit("guard-omitted", spec)
        self.assertEqual(code, 200, legacy)
        legacy_row = queue.get(legacy["id"])
        self.assertEqual(legacy_row["spec"], {**spec, "label": None})
        self.assertNotIn("disk_guard", legacy)
        self.assertNotIn("disk_guard", legacy["execution"])
        self.assertTrue(set(job_events.DISK_FIELDS).isdisjoint(legacy["committed_status"]))
        code, job = submit("guard-frozen", declared)
        self.assertEqual(code, 200, job)
        row = queue.get(job["id"])
        self.assertEqual(row["spec"]["disk_guard"], guard)
        self.assertEqual(job["disk_guard"], guard)
        observed = job["execution"]["disk_guard"]
        self.assertEqual((observed["schema"], observed["status"], observed["enforcement"]),
                         ("phonefarm/job-disk-guard@1", "not_started", "observed_stop_request"))
        self.assertIsNone(observed["observation"])
        self.assertIsNone(observed["triggered_at"])
        code, same = submit("guard-frozen", declared)
        self.assertEqual((code, same["id"]), (200, job["id"]))
        self.assertEqual(submit("guard-frozen", {**declared, "disk_guard": {
            **guard, "max_log_bytes": 8192}})[0], 409)
        self.assertEqual(len(queue.rows()), 2)
        self.assertEqual(queue.get(job["id"])["fingerprint"], row["fingerprint"])

        # Sampling bytes are intentionally outside the committed event projection.
        meta = env.jobs._read_meta(job["id"])
        queue.observe(job["id"], meta)
        revision = queue.get(job["id"])["status_revision"]
        meta["execution"]["disk_guard"]["observation"] = {
            "status": "known", "log_bytes": 123, "filesystems": [], "error": None}
        queue.observe(job["id"], meta)
        current = queue.get(job["id"])
        self.assertEqual(current["status_revision"], revision)
        self.assertEqual({key: current["status_projection"][key] for key in job_events.DISK_FIELDS},
                         {"disk_guard_status": "not_started", "disk_guard_trigger_reason": None,
                          "disk_guard_triggered_at": None})

        for expected in (legacy, job):
            frozen = queue.claim_next(2)
            self.assertEqual(frozen["job_id"], expected["id"])
            reaped = threading.Event()
            process = Mock()
            process.wait.side_effect = reaped.set
            with patch.object(job_runtime, "launch", return_value=process) as launch:
                env.jobs.dispatch.launch(frozen)
                launch.assert_called_once()
                kwargs = launch.call_args.kwargs
                if expected["id"] == legacy["id"]:
                    self.assertNotIn("disk_guard", kwargs)
                else:
                    self.assertEqual(kwargs["disk_guard"], guard)
                self.assertTrue(reaped.wait(1))

        # Fixed installation capability is explicit; no module addition or old
        # prefix mutation is necessary to continue jobs that omit the guard.
        entry = env.root / "fixed" / "phonefarm-worker"
        with patch.object(worker_config, "configuration", return_value={"capabilities": []}):
            with self.assertRaisesRegex(ValueError, "jobs.disk-guard-v1"):
                worker_config.require_capability(entry, "jobs.disk-guard-v1")
        config = {"capabilities": ["jobs.disk-guard-v1"]}
        with patch.object(worker_config, "configuration", return_value=config):
            self.assertIs(worker_config.require_capability(entry, "jobs.disk-guard-v1"), config)

    def test_http_frozen_budget_queue_wait_and_unstarted_cancel(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        # Keep real HTTP, input preparation and SQL, but never dispatch a child.
        env.jobs.dispatch.start = lambda: None
        env.jobs.bins["fixture"] = (sys.executable, str(env.root))
        queue = env.jobs.dispatch.queue
        pin = {"X-Expected-Node-ID": env.jobs.node_id}
        spec = {"bin": "fixture", "argv": ["-c", "pass"], "resources": ["exclusive"]}
        budget = {"wall_seconds": 1, "timeout_action": "graceful"}
        timed = {**spec, "time_budget": budget}

        def submit(request_id, declaration):
            return env.call("POST", "/jobs", headers={**pin, "X-Request-ID": request_id,
                                                       "X-Job": json.dumps(declaration)})

        def unstarted(job):
            self.assertEqual(job["time_budget"], budget)
            observation = job["execution"]["time_budget"]
            self.assertEqual(observation["schema"], "phonefarm/job-time-budget@1")
            self.assertEqual(observation["status"], "not_started")
            for field in ("started_at", "deadline_at", "observed_at", "elapsed_seconds",
                          "exceeded_at", "finished_at"):
                self.assertIsNone(observation[field], field)

        self.assertIn("jobs.time-budget-v1", env.call("GET", "/node")[1]["capabilities"])
        with patch.object(env.jobs, "_prepare", wraps=env.jobs._prepare) as prepare:
            for invalid in ({**spec, "time_budget": None},
                            {**timed, "device_access": "recovery", "recovery_for": "a" * 12}):
                # A handler that reads this unprovided body would time out.
                connection = http.client.HTTPConnection(*env.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/jobs")
                    for key, value in {**pin, "Authorization": "Bearer " + env.admin,
                                       "X-Job": json.dumps(invalid), "Content-Length": "65536"}.items():
                        connection.putheader(key, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 400, payload)
                    self.assertIn("time_budget", payload["error"])
                finally:
                    connection.close()
                with self.assertRaisesRegex(ValueError, "time_budget"):
                    env.jobs.start(invalid, b"", request_id="invalid-direct")
            prepare.assert_not_called()
        self.assertEqual(queue.rows(), [])

        code, legacy = submit("legacy-budget-omitted", spec)
        self.assertEqual(code, 200, legacy)
        legacy_row = queue.get(legacy["id"])
        self.assertEqual(legacy_row["spec"], {**spec, "label": None})
        self.assertNotIn("time_budget", legacy)
        self.assertNotIn("time_budget", legacy["execution"])
        code, queued = submit("frozen-time-budget", timed)
        self.assertEqual(code, 200, queued)
        unstarted(queued)
        row = queue.get(queued["id"])
        self.assertEqual(row["spec"]["time_budget"], budget)
        unstarted(env.jobs._read_meta(queued["id"]))
        self.assertEqual(queue.claim_next(2)["job_id"], legacy["id"])
        self.assertIsNone(queue.claim_next(2), "the declared resource is still reserved")
        with patch("job_dispatch.time.time", return_value=row["submitted_at"] + 86400):
            code, waiting = env.call("GET", "/jobs/" + queued["id"], headers=pin)
        self.assertEqual((code, waiting["phase"], waiting["queue"]["waiting"]["kind"]),
                         (200, "queued", "resource"))
        unstarted(waiting)
        code, repeat = submit("frozen-time-budget", timed)
        self.assertEqual((code, repeat["id"]), (200, queued["id"]))
        self.assertEqual(submit("frozen-time-budget", {
            **timed, "time_budget": {**budget, "wall_seconds": 2}})[0], 409)
        self.assertEqual(len(queue.rows()), 2)
        self.assertEqual(queue.get(queued["id"])["fingerprint"], row["fingerprint"])

        code, cancelled = env.call("POST", "/jobs/" + queued["id"] + "/cancel", {}, headers=pin)
        self.assertEqual((code, cancelled["accepted"]), (200, True))
        terminal = cancelled["job"]
        self.assertEqual((terminal["state"], terminal["outcome"], terminal["recovery"]["status"]),
                         ("finished", "cancelled", "not_needed"))
        unstarted(terminal)

        # Check the worker boundary without pretending that a process started.
        code, launchable = submit("budget-launch-plumbing", {**timed, "resources": []})
        self.assertEqual(code, 200, launchable)
        launch_row = queue.claim_next(2)
        self.assertEqual(launch_row["job_id"], launchable["id"])
        for frozen in (legacy_row, launch_row):
            reaped = threading.Event()
            process = Mock()
            process.wait.side_effect = reaped.set
            with patch.object(job_runtime, "launch", return_value=process) as launch:
                env.jobs.dispatch.launch(frozen)
                launch.assert_called_once()
                kwargs = launch.call_args.kwargs
                if frozen["job_id"] == legacy["id"]:
                    self.assertNotIn("time_budget", kwargs)
                else:
                    self.assertEqual(kwargs["time_budget"], budget)
                self.assertTrue(reaped.wait(1), "mock worker waiter did not finish")
        unstarted(env.jobs._read_meta(launchable["id"]))


if __name__ == "__main__":
    unittest.main()
