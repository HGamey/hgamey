"""A task handed to another agent keeps what its earlier owner did: human notes the earlier owner answered do not
hold back the new owner's delivery, the earlier owner's own notes are not the new owner's to answer unless addressed
to it, and a refused delivery names the notes that still hold it back."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class OwnerHandoverTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    request, cli, web, pf, agent, duties = (core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web,
                                            core.CoreFlowTests.pf, core.CoreFlowTests.agent, core.CoreFlowTests.duties)
    write = staticmethod(core.CoreFlowTests.write)

    def token(self, name):
        self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        return self.cli("POST", f"/agents/{name}/token", {})[1]["token"]

    def human(self, nid, text):
        self.assertEqual(self.web("POST", f"/research-state/note/{nid}", self.write(
            "web-" + nid, {"research_id": "sr-hand", "text": text, "reply_to": None, "decision": None}))[0], 201)

    def deliver(self, token):
        return self.pf(token, "research", "deliver", "sr-hand", "--summary", "完成", "--artifact", f"job:{self.jid}")

    def test_a_new_owner_answers_only_what_nobody_who_owned_the_task_has(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-hand", self.write(
            "web-task", {"title": "交接的任务", "objective": "换人接着做"}))[0], 201)
        first, second = self.token("agent-a"), self.token("agent-b")
        self.assertEqual(self.pf(first, "research", "claim", "sr-hand")[0], 0)
        self.human("n-early", "先跑基线")
        self.assertEqual(self.pf(first, "research", "note", "sr-hand", "采纳，基线在跑", "--reply-to", "n-early",
                                 "--decision", "adopt")[0], 0)
        self.assertEqual(self.pf(first, "research", "note", "sr-hand", "基线跑完了，记一下数")[0], 0)

        # A human hands the task to agent-b, then writes one more note.
        current = self.cli("GET", "/research-state/research/sr-hand")[1]
        self.assertEqual(self.web("POST", "/research-state/research/sr-hand", {
            **self.write("web-hand", {**current["data"], "owner": "agent-b"}),
            "base_revision": current["revision"], "reason": "换人"})[0], 201)
        self.human("n-late", "交付前补一张对比图")
        # A note the earlier owner addresses to the new one is still the new one's to answer.
        self.assertEqual(self.pf(first, "research", "note", "sr-hand", "交给你了，基线数据在 out/", "--to", "agent-b")[0], 0)

        owed = [duty for duty in self.duties(second)["duties"] if duty["kind"] in ("answer_note", "urgent_note")]
        self.assertEqual([duty["target"] for duty in owed][:1], ["n-late"])
        self.assertEqual(len(owed), 2)
        self.assertIn("agent-a 在 sr-hand 留言：交给你了", owed[1]["text"])
        code, _, err = self.deliver(second)
        self.assertNotEqual(code, 0)
        self.assertIn("code=research_guidance_unanswered", err)
        self.assertIn("挡住的留言：n-late", err)
        self.assertNotIn("n-early", err)

        self.assertEqual(self.pf(second, "research", "note", "sr-hand", "补上了", "--reply-to", "n-late",
                                 "--decision", "adopt")[0], 0)
        code, _, err = self.deliver(second)
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
