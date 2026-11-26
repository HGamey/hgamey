import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import job_approvals  # noqa: E402


class Jobs:
    def __init__(self, root):
        self.root = root
        self.dir = root

    def path(self, jid, *parts):
        return os.path.join(self.root, jid, *parts)


def request(rid, **over):
    value = {"schema": "phonefarm/approval@1", "id": rid, "state": "pending", "category": "payment",
             "reason": "目标「立即支付」", "action": "tap", "created_at": time.time(),
             "expires_at": time.time() + 300, "screenshot": "/x.jpg"}
    value.update(over)
    return value


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Jobs(self.tmp.name)
        self.dir = self.jobs.path("abcdef012345", "approvals")
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rid, **over):
        with open(os.path.join(self.dir, rid + ".json"), "w") as f:
            json.dump(request(rid, **over), f)

    def test_list_and_decide_once(self):
        self.write("ap-r-1-0001")
        self.write("ap-r-2-0002", expires_at=time.time() - 60)
        items = job_approvals.list_requests(self.jobs, "abcdef012345")
        self.assertEqual([i["state"] for i in items], ["pending", "expired"])
        self.assertNotIn("screenshot", items[0])
        self.assertEqual(len(job_approvals.list_requests(self.jobs, "abcdef012345", pending_only=True)), 1)
        code, out = job_approvals.decide(self.jobs, "abcdef012345", "ap-r-1-0001", b'{"decision":"allow"}', "admin")
        self.assertEqual((code, out["decision"]), (200, "allow"))
        code, _ = job_approvals.decide(self.jobs, "abcdef012345", "ap-r-1-0001", b'{"decision":"deny"}', "admin")
        self.assertEqual(code, 409)
        with open(os.path.join(self.dir, "ap-r-1-0001.decision.json")) as f:
            self.assertEqual(json.load(f)["decision"], "allow")
        self.assertEqual(job_approvals.list_requests(self.jobs, "abcdef012345")[0]["state"], "allowed")

    def test_expired_or_decided_requests_refuse_new_decisions(self):
        self.write("ap-r-9-0009", expires_at=time.time() - 1)
        self.assertEqual(job_approvals.decide(self.jobs, "abcdef012345", "ap-r-9-0009", b'{"decision":"allow"}', "a")[0], 409)
        self.write("ap-r-8-0008", state="denied")
        self.assertEqual(job_approvals.decide(self.jobs, "abcdef012345", "ap-r-8-0008", b'{"decision":"allow"}', "a")[0], 409)

    def test_rejects_bad_input(self):
        self.write("ap-r-1-0001")
        self.assertEqual(job_approvals.decide(self.jobs, "abcdef012345", "ap-r-1-0001", b'{"decision":"maybe"}', "a")[0], 400)
        self.assertEqual(job_approvals.decide(self.jobs, "abcdef012345", "../x", b'{"decision":"allow"}', "a")[0], 404)
        self.assertEqual(job_approvals.decide(self.jobs, "abcdef012345", "ap-missing", b'{"decision":"allow"}', "a")[0], 404)
        with self.assertRaises(KeyError):
            job_approvals.screenshot_path(self.jobs, "abcdef012345", "ap-r-1-0001")

    def test_pending_all_across_jobs(self):
        self.write("ap-r-1-0001", created_at=time.time() - 10)
        other = self.jobs.path("0123456789ab", "approvals")
        os.makedirs(other)
        with open(os.path.join(other, "ap-x-1-0002.json"), "w") as f:
            json.dump(request("ap-x-1-0002"), f)
        os.makedirs(os.path.join(self.tmp.name, "not-a-job", "approvals"))
        items = job_approvals.pending_all(self.jobs)
        self.assertEqual([(i["job_id"], i["id"]) for i in items],
                         [("0123456789ab", "ap-x-1-0002"), ("abcdef012345", "ap-r-1-0001")])
        job_approvals.decide(self.jobs, "0123456789ab", "ap-x-1-0002", b'{"decision":"deny"}', "dash")
        self.assertEqual([i["id"] for i in job_approvals.pending_all(self.jobs)], ["ap-r-1-0001"])

    def test_no_dir(self):
        self.assertEqual(job_approvals.list_requests(self.jobs, "000000000000"), [])


if __name__ == "__main__":
    unittest.main()
