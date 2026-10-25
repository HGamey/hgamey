"""Local SQLite concurrency and scheduling contracts; no service or devices."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest

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
        self.assertEqual(JobQueue(self.path).ready(jid)["state"], "claimed")

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


if __name__ == "__main__":
    unittest.main()
