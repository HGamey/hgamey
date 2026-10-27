"""Local SQLite concurrency and scheduling contracts; no service or devices."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job_queue import JobQueue, RequestConflict


def claim_in_process(path, gate, results):
    queue = JobQueue(path)
    gate.wait(5)
    results.put(queue.claim_next(2))


class JobQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "_node" / "job-queue.sqlite3"
        self.queue = JobQueue(self.path)

    def submit(self, key, resources, **spec):
        row, created = self.queue.reserve(key, "content:" + key, {"command": key, **spec}, resources)
        self.assertTrue(created)
        self.queue.ready(row["job_id"])
        return row["job_id"]

    def settle_fixture(self, jid, state="blocked"):
        # These SQL-only fixtures never start a worker. Record that fact rather
        # than teaching the queue that every blocked row has stopped executing.
        self.queue.record_unstarted(jid, "failed", "SQL fixture never executed a command")
        self.queue.set_state(jid, state)

    def test_priority_ranks_eligible_jobs_without_rewriting_requests(self):
        from node_service import Jobs

        root = self.path.parent / "priority-node"
        jobs = Jobs(str(root), {"fixture": (sys.executable, str(root))}, data_dir=str(root))
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None  # Exercise preparation, never launch a worker.
        jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        queue = jobs.dispatch.queue
        base = {"bin": "fixture", "argv": ["-c", "pass"], "resources": []}

        owner = jobs.start({**base, "resources": ["busy"]}, b"", "priority-owner")
        self.assertEqual(queue.claim_next(2)["job_id"], owner["id"])
        legacy = jobs.start(base, b"", "priority-legacy")
        blocked = jobs.start({**base, "resources": ["busy"], "priority": 10}, b"", "priority-blocked")
        first = jobs.start({**base, "priority": 5}, b"", "priority-first")
        second = jobs.start({**base, "priority": 5}, b"", "priority-second")
        self.assertNotIn("priority", queue.get(legacy["id"])["spec"])
        self.assertNotIn("priority", legacy)
        self.assertEqual(legacy["queue"]["priority"], 0)
        self.assertEqual((first["priority"], first["queue"]["priority"]), (5, 5))
        self.assertEqual({row["job_id"]: row["priority"] for row in jobs.dispatch.snapshot()["items"]},
                         {owner["id"]: 0, legacy["id"]: 0, blocked["id"]: 10,
                          first["id"]: 5, second["id"]: 5})
        with patch.object(jobs, "_prepare", wraps=jobs._prepare) as prepare:
            self.assertEqual(jobs.start({**base, "priority": 5}, b"", "priority-first")["id"], first["id"])
            self.assertEqual(jobs.start(base, b"", "priority-legacy")["id"], legacy["id"])
            for request_id, spec in (("priority-first", {**base, "priority": 6}),
                                     ("priority-legacy", {**base, "priority": 0})):
                with self.assertRaises(RequestConflict):
                    jobs.start(spec, b"", request_id)
            for invalid in (True, None, 11):
                with self.assertRaisesRegex(ValueError, "priority"):
                    jobs.start({**base, "priority": invalid}, b"", "priority-invalid")
            prepare.assert_not_called()
        self.assertEqual(len(queue.rows()), 5)

        # Reopening preserves priorities and submission order; the occupied
        # resource beats priority, and the existing owner is never preempted.
        queue = JobQueue(queue.path)
        self.assertEqual(queue.claim_next(2)["job_id"], first["id"])
        self.assertIsNone(queue.claim_next(2))
        self.assertEqual(queue.get(owner["id"])["state"], "claimed")
        for finished, expected in ((first, second), (second, legacy), (owner, blocked)):
            queue.record_unstarted(finished["id"], "cancelled", "SQL fixture never launched")
            queue.set_state(finished["id"], "done")
            self.assertEqual(queue.claim_next(2)["job_id"], expected["id"])

    def test_concurrent_same_request_is_one_job_and_changed_content_conflicts(self):
        def reserve(_):
            return self.queue.reserve("request-1", "content-1", {"command": "example"}, ["phone:a"])
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(reserve, range(8)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({row["job_id"] for row, _ in results}), 1)
        row = results[0][0]
        self.assertRegex(row["job_id"], r"^[0-9a-f]{12}$")
        self.assertEqual(row["state"], "preparing")
        with self.assertRaises(RequestConflict):
            self.queue.reserve("request-1", "changed-content", {"command": "different"}, [])
        self.assertEqual(self.queue.by_request("request-1"), row)
        self.assertEqual(self.queue.rows(), [row])
        self.assertEqual(row["status_revision"], 1)
        with self.queue._transaction(write=True) as db:
            events = db.execute("SELECT * FROM job_events").fetchall()
            self.assertEqual(len(events), 1, "concurrent retries must publish only one admission")
            self.assertEqual(json.loads(events[0]["committed_status"]), row["status_projection"])
            db.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON job_events
                          BEGIN SELECT RAISE(ABORT,'injected event storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.ready(row["job_id"])
        self.assertEqual(self.queue.get(row["job_id"]), row, "event failure rolls back the queue transition too")
        with self.queue._transaction(write=True) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 1)
            db.execute("DROP TRIGGER fail_event")
        ready = self.queue.ready(row["job_id"])
        self.assertEqual(ready["status_revision"], 2)
        self.assertEqual(self.queue.ready(row["job_id"]), ready, "unchanged ready calls do not publish another event")

    def test_reopen_and_two_process_claim_has_one_owner(self):
        jid = self.submit("first", ["phone:a"])
        reopened = JobQueue(self.path)
        self.assertEqual(reopened.get(jid)["state"], "queued")
        context = multiprocessing.get_context("spawn")
        gate, results = context.Barrier(2), context.Queue()
        processes = [context.Process(target=claim_in_process, args=(str(self.path), gate, results)) for _ in range(2)]
        for process in processes:
            process.start()
        try:
            claims = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()
            results.join_thread()
        self.assertEqual([row["job_id"] for row in claims if row], [jid])
        claimed = JobQueue(self.path).ready(jid)
        self.assertEqual((claimed["state"], claimed["status_revision"]), ("claimed", 3))
        with self.queue._transaction() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT revision FROM job_events WHERE job_id=? ORDER BY sequence", (jid,))], [1, 2, 3])

    def test_recovery_borrows_only_its_blocked_ancestor_chain(self):
        parent = self.submit("parent", ["phone:a"])
        self.settle_fixture(parent)
        failed = self.submit("failed-recovery", ["phone:a"], recovery_for=parent)
        self.assertEqual(self.queue.claim_next(1)["job_id"], failed)
        sibling = self.submit("sibling-recovery", ["phone:a"], recovery_for=parent)
        self.settle_fixture(failed)
        self.assertIsNone(self.queue.claim_next(1))
        nested = self.submit("recover-failed", ["phone:a"], recovery_for=failed)
        self.assertEqual(self.queue.claim_next(1)["job_id"], nested)
        self.assertIsNone(self.queue.claim_next(2))
        self.settle_fixture(nested, "done")
        self.assertIsNone(self.queue.claim_next(1))
        self.settle_fixture(failed, "done")
        self.assertEqual(self.queue.claim_next(1)["job_id"], sibling)

    def test_blocked_jobs_hold_resources_without_consuming_execution_slots(self):
        first = self.submit("first", ["phone:a"])
        same = self.submit("same", ["phone:a"])
        other = self.submit("other", ["phone:b"])
        free = self.submit("free", [])
        self.assertEqual(self.queue.claim_next(2)["job_id"], first)
        self.assertEqual(self.queue.claim_next(2)["job_id"], other)
        self.assertIsNone(self.queue.claim_next(2))
        self.queue.set_state(first, "blocked")
        self.assertIsNone(self.queue.claim_next(2), "unknown host execution still reserves its CPU slot")
        self.settle_fixture(first)
        self.assertEqual(self.queue.claim_next(2)["job_id"], free)
        self.settle_fixture(other, "done")
        self.settle_fixture(free, "done")
        self.assertIsNone(self.queue.claim_next(2))
        self.settle_fixture(first, "done")
        self.assertEqual(self.queue.claim_next(2)["job_id"], same)
        self.assertEqual([row["job_id"] for row in self.queue.rows(["done"])], [first, other, free])
        self.assertFalse(self.queue.cancel_pending(same))
        pending = self.submit("cancel-queued", ["phone:c"])
        self.assertTrue(self.queue.cancel_pending(pending))
        self.assertFalse(self.queue.cancel_pending(pending))
        preparing, _ = self.queue.reserve("cancel-preparing", "content", {}, [])
        self.assertTrue(self.queue.cancel_pending(preparing["job_id"]))
        self.assertEqual(self.queue.ready(preparing["job_id"])["state"], "done")
        self.assertIsNone(self.queue.claim_next(2))

    def test_shared_multidimensional_capacity_survives_restart_and_configuration_changes(self):
        self.queue = JobQueue(self.path, host_capacity={"cpu": 4, "gpu": 1, "io": 2})
        gpu = self.submit("gpu", [], host_slots={"cpu": 2, "gpu": 1, "io": 0})
        wait_gpu = self.submit("wait-gpu", [], host_slots={"cpu": 1, "gpu": 1, "io": 0})
        io = self.submit("io", [], host_slots={"cpu": 1, "gpu": 0, "io": 2})
        cpu = self.submit("cpu", [], host_slots={"cpu": 2, "gpu": 0, "io": 0})
        self.assertEqual(self.queue.claim_next(4)["job_id"], gpu)
        # The earlier GPU waiter must not block a feasible IO candidate.
        self.assertEqual(self.queue.claim_next(4)["job_id"], io)
        self.assertIsNone(self.queue.claim_next(4))
        self.assertEqual(self.queue.capacity_snapshot()["reserved"], {"cpu": 3, "gpu": 1, "io": 2})
        self.settle_fixture(gpu)
        self.assertEqual(self.queue.claim_next(4)["job_id"], wait_gpu)
        self.assertEqual(self.queue.claim_next(4)["job_id"], cpu)
        self.assertEqual(self.queue.capacity_snapshot()["reserved"], {"cpu": 4, "gpu": 1, "io": 2})

        pending = self.submit("needs-gpu-after-shrink", [], host_slots={"cpu": 1, "gpu": 1, "io": 0})
        smaller = JobQueue(self.path, host_capacity={"cpu": 2, "gpu": 0, "io": 1})
        # The older queue object reads the shared configuration on every claim.
        self.assertEqual(self.queue.capacity(), smaller.capacity())
        self.assertEqual(self.queue.capacity_snapshot()["overcommitted"], {"cpu": 2, "gpu": 1, "io": 1})
        self.assertIsNone(self.queue.claim_next(4))
        for jid in (wait_gpu, io, cpu):
            self.settle_fixture(jid)
        self.assertEqual(self.queue.capacity_snapshot()["reserved"], {"cpu": 0, "gpu": 0, "io": 0})
        self.assertIsNone(smaller.claim_next(4), "request exceeds current total even with no occupied slots")
        enlarged = JobQueue(self.path, host_capacity={"cpu": 2, "gpu": 1, "io": 1})
        self.assertEqual(smaller.claim_next(4)["job_id"], pending)
        self.assertEqual(JobQueue(self.path).capacity(), enlarged.capacity())

    def test_paused_queued_jobs_skip_claim_until_resumed_and_started_refuse_pause(self):
        from node_service import Jobs

        root = self.path.parent / "pause-node"
        jobs = Jobs(str(root), {"fixture": (sys.executable, str(root))}, data_dir=str(root))
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None  # SQL-level pause contract; never launch a worker.
        jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        queue = jobs.dispatch.queue
        base = {"bin": "fixture", "argv": ["-c", "pass"], "resources": []}

        target = jobs.start(base, b"", "pause-target")["id"]
        self.assertFalse(queue.get(target)["paused"])
        row = queue.set_paused(target, True)
        self.assertTrue(row["paused"])
        self.assertIsNone(queue.claim_next(2), "a paused queued job must not be claimed")
        self.assertEqual(queue.set_paused(target, True)["paused"], True, "pause is idempotent")
        row = queue.set_paused(target, False)
        self.assertFalse(row["paused"])
        self.assertEqual(queue.claim_next(2)["job_id"], target)

        # Claimed (started) and terminal jobs refuse pause instead of pretending.
        with self.assertRaises(ValueError):
            queue.set_paused(target, True)
        finished = jobs.start(base, b"", "pause-finished")["id"]
        queue.set_paused(finished, True)
        queue.cancel_pending(finished)
        with self.assertRaises(ValueError):
            queue.set_paused(finished, False)
        status = jobs.status(finished)
        self.assertEqual(status["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()

class ActorIdTests(unittest.TestCase):
    """actor_id: declared submitter identity, carried through spec and status."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "_node" / "job-queue.sqlite3"

    def test_actor_id_validation_and_status_output(self):
        from node_service import Jobs
        root = self.path.parent / "actor-id"
        jobs = Jobs(str(root), {"fixture": (sys.executable, str(root))}, data_dir=str(root))
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None
        jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}

        base = {"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": []}
        # 无 actor_id：正常
        job = jobs.dispatch.submit(base, b"", "no-actor")
        self.assertNotIn("actor_id", job)
        # 带 actor_id：保留
        job = jobs.dispatch.submit({**base, "actor_id": "test-agent-1"}, b"", "with-actor")
        self.assertEqual(job.get("actor_id"), "test-agent-1")
        # 空字符串/超长/非字符串：拒绝
        for bad in ["", "x" * 65, 123]:
            with self.assertRaises(ValueError, msg=str(bad)):
                jobs.start({**base, "actor_id": bad}, b"", f"bad-{type(bad).__name__}")
        # status 输出包含 actor_id
        status = jobs.status(job["id"])
        self.assertEqual(status.get("actor_id"), "test-agent-1")


class DiscussRouteTests(unittest.TestCase):
    """任务级 discuss：人工和 agent 的讨论；agent 有问题想问人，其他 agent 可参与。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "_node" / "job-queue.sqlite3"

    def test_discuss_read_write_and_errors(self):
        from http.server import ThreadingHTTPServer
        import threading
        from node_service import Jobs, make_handler

        root = self.path.parent / "discuss"
        jobs = Jobs(str(root), {"fixture": (sys.executable, str(root))}, data_dir=str(root))
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None
        jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, jobs, "discuss-fixture-admin"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server():
            server.shutdown(); server.server_close(); thread.join(5)
        self.addCleanup(close_server)

        import http.client

        def call(method, path, body=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            headers = {"Authorization": "Bearer discuss-fixture-admin",
                       "X-Expected-Node-ID": jobs.games.node_id}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status, value

        submitted = jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                          "resources": []}, b"", "discuss-target")
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]

        # 读空列表
        status, value = call("GET", f"/jobs/{jid}/discuss")
        self.assertEqual((status, value["entries"]), (200, []))

        # 人工发言
        status, value = call("POST", f"/jobs/{jid}/discuss", json.dumps({"text": "这个方向可行吗？"}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(value["entry"]["from"], "human")

        # agent 发言
        status, value = call("POST", f"/jobs/{jid}/discuss", json.dumps({"text": "我测了，可行", "from": "test-agent"}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(value["entry"]["from"], "test-agent")

        # 读取包含两条
        status, value = call("GET", f"/jobs/{jid}/discuss")
        self.assertEqual(len(value["entries"]), 2)
        self.assertEqual(value["entries"][0]["from"], "human")
        self.assertEqual(value["entries"][1]["from"], "test-agent")

        # 不存在任务 404
        status, _ = call("GET", "/jobs/no-such-job/discuss")
        self.assertEqual(status, 404)

        # 空文本 400
        status, _ = call("POST", f"/jobs/{jid}/discuss", json.dumps({"text": ""}).encode())
        self.assertEqual(status, 400)


class SessionPauseRouteTests(unittest.TestCase):
    """session-pause/resume routes: running jobs only; flag file persists the request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "_node" / "job-queue.sqlite3"

    def test_routes_manage_flag_only_for_running_jobs(self):
        from http.server import ThreadingHTTPServer
        import threading
        from node_service import Jobs, make_handler

        root = self.path.parent / "pause-routes"
        jobs = Jobs(str(root), {"fixture": (sys.executable, str(root))}, data_dir=str(root))
        self.addCleanup(jobs._read_root.close)
        jobs.dispatch.start = lambda: None
        jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, jobs, "pause-fixture-admin-not-production"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server():
            server.shutdown(); server.server_close(); thread.join(5)
        self.addCleanup(close_server)

        import http.client

        def call(method, path):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(method, path, body=b"", headers={
                "Authorization": "Bearer pause-fixture-admin-not-production",
                "X-Expected-Node-ID": jobs.games.node_id})
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status, value

        submitted = jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                          "resources": []}, b"", "session-pause-target")
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
        # 排队态：session 暂停不适用（409），队列级暂停仍可用。
        status, value = call("POST", f"/jobs/{jid}/session-pause")
        self.assertEqual((status, value["code"]), (409, "session_pause_unavailable"))
        # 领取（模拟运行中）后：写标志文件。
        queue = jobs.dispatch.queue
        row = queue.claim_next(2)
        self.assertEqual(row["job_id"], jid)
        status, value = call("POST", f"/jobs/{jid}/session-pause")
        self.assertEqual((status, value["session_pause_requested"]), (200, True))
        self.assertTrue((jobs._read_root.open_file(jid + "/pause.requested").read() == b""))
        # 幂等：重复暂停不报错。
        status, value = call("POST", f"/jobs/{jid}/session-pause")
        self.assertEqual(status, 200)
        # 恢复：删除标志。
        status, value = call("POST", f"/jobs/{jid}/session-resume")
        self.assertEqual((status, value["session_pause_requested"]), (200, False))
        import os
        with self.assertRaises(FileNotFoundError):
            jobs._read_root.open_file(jid + "/pause.requested")
