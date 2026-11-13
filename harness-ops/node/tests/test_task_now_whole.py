"""The 现在 line keeps an agent's last action short (留言「…」 cut at its first sentence or 40 characters, a job's label at
30), and says it in full on hover, so a cut-off action can be read without opening anything."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_task_now

NOTE = "先把风扇还原为任务开始时的 1 档，再跑一次 720p 基线和 1080p 对照，记录每一档的帧率和整机功耗，最后把三张截图贴到留言里。然后看 1440p。"
LABEL = "恢复 ef26942414f4: 风扇还原为任务开始时的 1 档并核对温度曲线是否回到基线"


class TaskNowWholeTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)
    task, line = test_task_now.TaskNowTests.task, test_task_now.TaskNowTests.line

    def test_a_cut_off_last_action_reads_in_full_on_hover(self):
        self.task("sr-long")
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-long")[0], 0)
        self.assertEqual(self.pf(token, "research", "note", "sr-long", NOTE)[0], 0)
        job = {"id": "a" * 12, "bin": "fixture", "argv": ["run"], "label": LABEL, "actor_id": "agent-a",
               "state": "finished", "outcome": "succeeded", "started": time.time() + 5}
        with self._browser_page() as page:
            shown = self.line(page, "sr-long")
            self.assertTrue(shown.endswith("…」"), shown)
            self.assertEqual(page.get_attribute(".task-now span", "title"), f"最后一次动作：留言「{NOTE}」")
            shown = self.line(page, "sr-long", [job])
            self.assertTrue(shown.endswith("…」"), shown)
            self.assertEqual(page.get_attribute(".task-now span", "title"), f"最后一次动作：启动作业「{LABEL}」")


if __name__ == "__main__":
    unittest.main()
