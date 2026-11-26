import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import approval_bridge as ab  # noqa: E402


class Fake:
    """Center + gateway in one: records calls, serves canned answers."""
    def __init__(self):
        self.pending = [{"id": "ap-run-4-c75f", "job_id": "abcdef012345", "category": "delete", "category_zh": "删除",
                         "critical": True, "action": "tap 删除", "reason": "r", "expires_at": time.time() + 300,
                         "has_screenshot": True}]
        self.tg = [{"id": 100, "text": "old"}]
        self.sent, self.decisions = [], []

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        if url.endswith("/approvals") and method == "GET":
            return 200, json.dumps({"items": self.pending}).encode()
        if url.endswith("/screenshot"):
            return 200, b"\xff\xd8jpeg"
        if "/api/send" in url:
            self.sent.append(body)
            return 200, json.dumps({"status": "sent", "message_id": len(self.sent)}).encode()
        if "/api/messages" in url:
            return 200, json.dumps(self.tg[-1:]).encode()
        if "/api/wait" in url:
            after = int(url.split("after_id=")[1].split("&")[0])
            return 200, json.dumps([m for m in self.tg if m["id"] > after]).encode()
        if method == "POST" and "/approvals/" in url:
            assert headers["X-Approval-Via"] == "telegram"
            self.decisions.append((url, body))
            self.pending = []
            return 200, b"{}"
        raise AssertionError(url)


class BridgeTests(unittest.TestCase):
    def test_parse_reply(self):
        self.assertEqual(ab.parse_reply("允许 c75f"), ("allow", "c75f"))
        self.assertEqual(ab.parse_reply("拒绝：C75F"), ("deny", "c75f"))
        self.assertEqual(ab.parse_reply("y c75f"), ("allow", "c75f"))
        self.assertIsNone(ab.parse_reply("回复「允许 c75f」或「拒绝 c75f」"))
        self.assertIsNone(ab.parse_reply("已允许：tap 删除"))
        self.assertIsNone(ab.parse_reply("maybe c75f"))
        self.assertEqual(ab.parse_reply("完成 ab12"), ("done", "ab12"))
        self.assertEqual(ab.parse_reply("放弃 ab12"), ("give_up", "ab12"))
        # 动词按类别: 「完成」批不了付款, 「允许」结束不了接手
        self.assertIsNone(ab.decision_for("done", "payment"))
        self.assertIsNone(ab.decision_for("allow", "handoff"))
        self.assertEqual(ab.decision_for("done", "handoff"), "allow")
        self.assertEqual(ab.decision_for("deny", "delete"), "deny")
        self.assertIsNone(ab.parse_reply(ab.caption({"id": "ap-x-1-ab12", "job_id": "j", "category": "handoff"})))
        self.assertIsNone(ab.parse_reply(ab.caption({"id": "ap-x-1-c75f", "job_id": "j"})))

    def test_round_trip(self):
        fake = Fake()
        ab.http = fake
        b = ab.Bridge("https://c", "tok", "https://g", "k", "tg:me", log=lambda s: None)
        b.tick(0)  # sends the approval, primes the reply cursor
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["name"], "ap-run-4-c75f.jpg")
        self.assertIn("允许 c75f", fake.sent[0]["text"])
        b.tick(0)  # nothing new: no duplicate send
        self.assertEqual(len(fake.sent), 1)
        fake.tg.append({"id": 101, "text": fake.sent[0]["text"], "sender": "我"})  # the bridge's own message echoed back
        fake.tg.append({"id": 103, "text": "完成 c75f", "sender": "我"})  # wrong verb for a delete approval
        fake.tg.append({"id": 102, "text": "允许 c75f", "sender": "别人"})  # not from the owner: ignored
        b.tick(0)
        self.assertEqual(len(fake.decisions), 0)
        fake.tg.append({"id": 104, "text": "允许 c75f", "sender": "我"})
        b.tick(0)
        self.assertEqual(len(fake.decisions), 1)
        url, body = fake.decisions[0]
        self.assertTrue(url.endswith("/jobs/abcdef012345/approvals/ap-run-4-c75f"))
        self.assertEqual(body["decision"], "allow")
        self.assertTrue(fake.sent[-1]["text"].startswith("已允许"))
        fake.tg.append({"id": 106, "text": "允许 c75f", "sender": "我"})  # repeated reply: nothing pending any more
        b.tick(0)
        self.assertEqual(len(fake.decisions), 1)


class AuthTests(unittest.TestCase):
    def test_exits_after_repeated_auth_failure(self):
        ab.http = lambda *a, **k: (401, b"{}")
        b = ab.Bridge("https://c", "bad", "https://g", "k", "tg:me", log=lambda s: None)
        b.notify_pending()
        b.notify_pending()
        with self.assertRaises(SystemExit):
            b.notify_pending()


if __name__ == "__main__":
    unittest.main()
