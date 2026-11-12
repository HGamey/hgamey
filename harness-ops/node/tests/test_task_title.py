"""A task's title is read by everyone who opens it: agents creating a task (a follow-on one too) are told to title it in
plain words, in the same text their check-in, the research help and the skill carry."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import agent_duties

RULE = "新建任务（create research，接续任务也一样）时，标题写成人能看懂的话，如「原神超分：画质不降时帧率能提高多少」，不用「A 轨」「B 轨」这类只有自己懂的代号。"


class TaskTitleTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    request, cli, pf, duties = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.pf, core.CoreFlowTests.duties

    def test_task_titles_are_asked_for_in_plain_words_everywhere_an_agent_reads(self):
        self.assertEqual(agent_duties.TASK_TITLE_HELP, RULE)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertIn(RULE, self.duties(token, rules=True)["loop"])
        self.assertIn(RULE, self.pf(token, "research", "--help")[1])
        self.assertIn(RULE, (core.NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
