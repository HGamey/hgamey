"""A chain of tasks is in 进行中 on the home page while any of its steps is; it reads by its first task (the user's
roots-only list). When that first task is itself delivered or closed and a follow-on is what moves, the row says so:
已交付 · 接续「<follow-on>」进行中, the follow-on a link to its own page, instead of a delivered task under 进行中
with nothing to explain it. A chain whose first task is still active keeps 含 n 个接续任务."""
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_cleanup_shown import Monitor


class HomeChainNowTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, title, follows=None):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-" + rid, {"title": title, "objective": "接续", "follows": follows}))[0], 201)

    def test_a_chain_moving_on_a_follow_on_says_which(self):
        self.task("sr-p1", "P1 framedump 上机验证")
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-p1", "--summary", "## 结论\n工具可用。", "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        self.task("sr-gi", "原神连续段数据集 gi-sim v1", follows="sr-p1")
        self.task("sr-a", "仍在做的第一步")
        self.task("sr-b", "它的接续", follows="sr-a")

        rows = {t["id"]: t for t in self.board._tasks()}
        self.assertEqual((rows["sr-p1"]["state"], rows["sr-p1"]["now"]),
                         ("active", {"id": "sr-gi", "title": "原神连续段数据集 gi-sim v1", "first": "delivered"}))
        self.assertNotIn("now", rows["sr-a"], "the first task is the one moving")

        self.board.monitor, self.board.shot_dir = Monitor([]), str(self.tmp)
        self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
        with self._browser_page() as page, mock.patch.object(self.board, "_elsewhere", return_value=[]):
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('#tasks [data-task="sr-p1"]')
            meta = '#tasks [data-task="%s"] .task-meta'
            self.assertIn("已交付 · 接续「原神连续段数据集 gi-sim v1」进行中", page.inner_text(meta % "sr-p1"))
            self.assertNotIn("含 1 个接续任务", page.inner_text(meta % "sr-p1"))
            link = page.get_attribute(f'{meta % "sr-p1"} a.chain-now', "href")
            self.assertIn("id=sr-gi", link)
            self.assertIn("id=sr-p1", page.get_attribute('#tasks [data-task="sr-p1"] a.task-title', "href"), "the row still opens the first task")
            self.assertIn("含 1 个接续任务", page.inner_text(meta % "sr-a"))
            page.click(f'{meta % "sr-p1"} a.chain-now')
            page.wait_for_url("**id=sr-gi**")


if __name__ == "__main__":
    unittest.main()
