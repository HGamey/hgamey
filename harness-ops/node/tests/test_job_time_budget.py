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
import test_node_games as fixture


class JobTimeBudgetTests(unittest.TestCase):
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
