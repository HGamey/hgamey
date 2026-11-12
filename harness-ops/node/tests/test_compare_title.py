"""A comparison's title is read by whoever answers it: agents are told to write it in plain words, in the same text
their check-in, the help and the skill carry (test_compare_help_is_shared_by_checkin_help_and_skill)."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import agent_duties


class CompareTitleTests(unittest.TestCase):
    def test_comparison_titles_are_asked_for_in_plain_words(self):
        rule = "题目写成人能看懂的话，如「两张游戏文字画面的清晰度对比」，不写「2× 文字区域画质盲评」这类术语。"
        self.assertIn(rule, agent_duties.COMPARE_DUTY)
        self.assertIn(agent_duties.COMPARE_DUTY, agent_duties.LOOP)
        self.assertIn(rule, (core.NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
