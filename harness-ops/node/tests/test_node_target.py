"""Target pins reject a different node before body reads or observation."""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import job_runtime
import test_node_games as game_fixture


class NodeTargetTests(unittest.TestCase):
    def test_two_nodes_pin_before_upload_observation_and_request_lookup(self):
        nodes = []
        for _ in range(2):
            node = game_fixture.GameFlowTests()
            node.setUp()
            self.addCleanup(node.tearDown)
            node.jobs.bins["fixture"] = (sys.executable, str(node.root))
            nodes.append(node)
        a, b = nodes
        workers = []
        launch = job_runtime.launch

        def tracked_launch(*args, **kwargs):
            worker = launch(*args, **kwargs)
            workers.append(worker)
            return worker

        def finish_workers():
            for worker in workers:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.terminate()
                    worker.wait(timeout=5)

        self.addCleanup(finish_workers)
        tracker = patch.object(job_runtime, "launch", side_effect=tracked_launch)
        tracker.start()
        self.addCleanup(tracker.stop)
        identities = []
        for node in nodes:
            status, identity = node.call("GET", "/node")  # Discovery remains unpinned.
            self.assertEqual(status, 200, identity)
            self.assertRegex(identity["node_id"], r"^[0-9a-f]{32}$")
            self.assertIn("node.target-v1", identity["capabilities"])
            identities.append(identity["node_id"])
        aid, bid = identities
        self.assertNotEqual(aid, bid)

        def headers_only(method, path, target_headers, extra=None):
            # Do not send the declared body: a body-reading handler would block.
            connection = http.client.HTTPConnection(*b.server.server_address, timeout=2)
            try:
                connection.putrequest(method, path)
                for key, value in [("Authorization", "Bearer " + b.admin),
                                   ("Content-Length", "65536"),
                                   ("Content-Type", "application/octet-stream"),
                                   *target_headers, *(extra or {}).items()]:
                    connection.putheader(key, value)
                connection.endheaders()
                if (extra or {}).get("Expect") == "100-continue":
                    # HTTPResponse silently skips 100, so inspect the first reply.
                    with connection.sock.makefile("rb") as response:
                        line = response.readline().decode("ascii").strip()
                        self.assertEqual(line.split()[1], "409", line)
                        headers = http.client.parse_headers(response)
                        self.assertEqual(headers.get("Connection"), "close")
                        value = json.loads(response.read(int(headers["Content-Length"])))
                        return 409, value
                response = connection.getresponse()
                value = json.loads(response.read())
                self.assertTrue(response.will_close, "rejected unread body connection must close")
                return response.status, value
            finally:
                connection.close()

        with patch.object(b.jobs, "start", wraps=b.jobs.start) as start, \
             patch.object(b.jobs.uploads, "write", wraps=b.jobs.uploads.write) as upload, \
             patch.object(b.jobs.uploads, "chunk", wraps=b.jobs.uploads.chunk) as chunk, \
             patch.object(b.board.monitor, "request", side_effect=AssertionError("unexpected device observation")) as observe:
            for method, path in (("POST", "/jobs"), ("POST", "/uploads"), ("PUT", "/uploads/fixture")):
                status, value = headers_only(method, path, [("X-Expected-Node-ID", aid)],
                                             {"Expect": "100-continue"} if path == "/jobs" else None)
                self.assertEqual((status, value["code"]), (409, "target_mismatch"))
            status, value = b.call("GET", "/fleet?expected_node_id=" + aid)
            self.assertEqual((status, value["code"]), (409, "target_mismatch"))
            invalid = [
                ("/jobs", [("X-Expected-Node-ID", "not-an-id")]),
                ("/jobs", [("X-Expected-Node-ID", bid), ("X-Expected-Node-ID", bid)]),
                ("/jobs?expected_node_id=" + bid + "&expected_node_id=" + bid, []),
                ("/jobs?expected_node_id=" + bid, [("X-Expected-Node-ID", aid)]),
            ]
            for path, target_headers in invalid:
                with self.subTest(path=path, headers=target_headers):
                    status, value = headers_only("POST", path, target_headers)
                    self.assertEqual((status, value["code"]), (400, "invalid_target"))
            start.assert_not_called()
            upload.assert_not_called()
            chunk.assert_not_called()
            observe.assert_not_called()
        self.assertEqual(b.jobs.dispatch.queue.rows(), [])
        self.assertEqual(workers, [])

        code = ("import os; from pathlib import Path; "
                "p=Path(os.environ['PF_JOB_OUT'])/'executions.txt'; "
                "p.open('a').write('executed once\\n')")
        spec = {"bin": "fixture", "argv": ["-c", code], "device_access": "none", "resources": []}
        jobs = []
        for node, identity in zip(nodes, identities):
            status, job = node.call("POST", "/jobs?expected_node_id=" + identity, headers={
                "X-Expected-Node-ID": identity, "X-Request-ID": "shared-request-id", "X-Job": json.dumps(spec)})
            self.assertEqual(status, 200, job)
            self.assertEqual(node.finish(job["id"])["exit"], 0)
            jobs.append(job["id"])
        self.assertNotEqual(jobs[0], jobs[1])
        status, refused = b.call("GET", "/requests/shared-request-id", headers={"X-Expected-Node-ID": aid})
        self.assertEqual((status, refused["code"]), (409, "target_mismatch"))
        for node, identity, jid in zip(nodes, identities, jobs):
            status, found = node.call("GET", "/requests/shared-request-id?expected_node_id=" + identity)
            self.assertEqual((status, found["id"]), (200, jid))
            self.assertEqual(len(node.jobs.dispatch.queue.rows()), 1)
        # Pinning is a connection constraint, not part of the frozen job content.
        # Legacy raw API requests without a pin remain explicitly compatible.
        status, same = b.call("POST", "/jobs", headers={"X-Request-ID": "shared-request-id", "X-Job": json.dumps(spec)})
        self.assertEqual((status, same["id"]), (200, jobs[1]))
        self.assertEqual(b.call("GET", "/requests/shared-request-id")[1]["id"], jobs[1])
        for node, jid in zip(nodes, jobs):
            self.assertEqual(Path(node.jobs.path(jid, "out", "executions.txt")).read_text(), "executed once\n")
        self.assertEqual(len(workers), 2)


if __name__ == "__main__":
    unittest.main()
