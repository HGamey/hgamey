"""回放: a step's number is its place in the whole task — the same in the address, on the card, in the position line
and in the list, whether or not only the key steps are shown — so a link names the step a person sees. Taking an
agent's view starts where that agent came in."""
import json
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

RID = "sr-step-number"
WHERE = "() => document.getElementById('replay-where').textContent"
BADGE = "() => document.querySelector('#replay-step .tour-num').textContent"


class ReplayStepNumberTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, agent, pf = (core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web,
                                    core.CoreFlowTests.agent, core.CoreFlowTests.pf)
    write = staticmethod(core.CoreFlowTests.write)

    def test_the_address_names_the_step_shown(self):
        # 1 新建 (人) · 2 人留言 · 3 指派给 agent-a (管理员) · 4 agent-a 留言 (细节) · 5 agent-a 提问
        self.assertEqual(self.web("POST", f"/research-state/research/{RID}", self.write(
            "create-step-number", {"title": "步号", "objective": "对上"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/note/n-first", self.write(
            "n-first", {"research_id": RID, "text": "先看一眼"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        code, out, err = self.pf(core.ADMIN, "research", "assign", RID, "agent-a")
        self.assertEqual(code, 0, err)
        for args in (("在看",), ("要不要换手机？", "--ask")):
            code, out, err = self.pf(token, "research", "note", RID, *args)
            self.assertEqual(code, 0, err)
        base = "http://%s:%d/dash/tour/replay?id=%s" % (*self.address, RID)
        with self._browser_page() as page:
            page.goto(base + "&step=4")
            page.wait_for_function(WHERE + ".startsWith('第 4 / 5 步')")
            self.assertEqual(page.evaluate(BADGE), "4")
            self.assertFalse(page.is_checked("#replay-key"), "a detail step asked for by its number shows all steps")
            page.check("#replay-key")
            page.wait_for_function(WHERE + ".startsWith('第 5 / 5 步')")
            self.assertTrue(page.url.endswith("step=5"), page.url)
            self.assertEqual(page.evaluate("() => [...document.querySelectorAll('#replay-list .replay-n')].map(n => n.textContent)"),
                             ["1", "2", "3", "5"])
            self.assertEqual(page.text_content("#replay-count"), "列出的 4 步（共 5 步）")

            page.goto(base + "&step=1")
            page.wait_for_function(WHERE + ".startsWith('第 1 / 5 步')")
            page.select_option("#replay-as", "agent-a")
            page.wait_for_function(WHERE + ".startsWith('第 3 / 5 步')")
            self.assertIn("把任务指派给 agent-a", page.text_content("#replay-step .replay-say"))


if __name__ == "__main__":
    unittest.main()
