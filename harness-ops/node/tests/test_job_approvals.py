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
        self.jobs = Jobs(os.path.join(self.tmp.name, "jobs"))  # 签名模式的决定目录在 jobs 的上一级
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
        with open(os.path.join(job_approvals.decision_dir(self.jobs, "abcdef012345"), "ap-r-1-0001.decision.json")) as f:
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
        os.makedirs(self.jobs.path("not-a-job", "approvals"))
        items = job_approvals.pending_all(self.jobs)
        self.assertEqual([(i["job_id"], i["id"]) for i in items],
                         [("0123456789ab", "ap-x-1-0002"), ("abcdef012345", "ap-r-1-0001")])
        job_approvals.decide(self.jobs, "0123456789ab", "ap-x-1-0002", b'{"decision":"deny"}', "dash")
        self.assertEqual([i["id"] for i in job_approvals.pending_all(self.jobs)], ["ap-r-1-0001"])

    def test_signed_decisions(self):
        try:
            import approval_sign
            signer = approval_sign.Signer()
        except ImportError:
            self.skipTest("cryptography not installed")
        job_approvals.SIGNER = signer
        try:
            self.write("ap-r-1-0001")
            # 作业自己往决定目录/审批目录写一个没签名的「允许」: 不算
            ddir = job_approvals.decision_dir(self.jobs, "abcdef012345")
            self.assertFalse(ddir.startswith(self.jobs.path("abcdef012345")), "决定不在作业目录里")
            os.makedirs(ddir, exist_ok=True)
            with open(os.path.join(ddir, "ap-r-1-0001.decision.json"), "w") as f:
                json.dump({"decision": "allow", "by": "job", "sig": "00" * 64}, f)
            self.assertEqual(job_approvals.list_requests(self.jobs, "abcdef012345")[0]["state"], "pending")
            os.remove(os.path.join(ddir, "ap-r-1-0001.decision.json"))
            code, out = job_approvals.decide(self.jobs, "abcdef012345", "ap-r-1-0001", b'{"decision":"allow"}', "dash")
            self.assertEqual(code, 200)
            self.assertTrue(signer.valid("abcdef012345", "ap-r-1-0001", "allow", out["content"], out["sig"]))
            self.assertFalse(signer.valid("abcdef012345", "ap-r-1-0001", "deny", out["content"], out["sig"]))
            self.assertEqual(job_approvals.list_requests(self.jobs, "abcdef012345")[0]["state"], "allowed")
            # 决定之后有人改了请求(人看到的内容): 签名对不上, 这条决定不再算数
            with open(os.path.join(self.dir, "ap-r-1-0001.json")) as f:
                req = json.load(f)
            req["reason"] = "看起来无害"
            with open(os.path.join(self.dir, "ap-r-1-0001.json"), "w") as f:
                json.dump(req, f)
            self.assertEqual(job_approvals.list_requests(self.jobs, "abcdef012345")[0]["state"], "pending")
        finally:
            job_approvals.SIGNER = None

    def test_signature_vector_matches_phonefarm(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            import approval_sign
        except ImportError:
            self.skipTest("cryptography not installed")
        k = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        c = approval_sign.content_hash(b'{"x":1}', b"jpg")
        self.assertEqual(c, "ae7125ed5d039139cb21bd22403f2acd55e51023afa478bb36bc06b6323b45b7")
        sig = k.sign(approval_sign.message("abcdef012345", "ap-skill-x-2-dbf2554a16c65c35", "allow", c)).hex()
        # 与 phonefarm/src/approval.rs a_signature_made_by_the_center_python_code_verifies 同一组数
        self.assertEqual(sig, "3e558ca3ca8f4c3bbd932978e705439eb8d4ba53bed33a07da6ddc499c2373a7c8357de7205b3a58897dffbc5482239be95312ecb98824870623c653b356ea08")

    def test_no_dir(self):
        self.assertEqual(job_approvals.list_requests(self.jobs, "000000000000"), [])


if __name__ == "__main__":
    unittest.main()
