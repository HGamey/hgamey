"""On a phone the Agent directory is a block per agent, not a table run off the screen: who it is; its zone, running
jobs and last activity on one line; its status in two lines, a tap showing the rest. Five agents no longer take
three screens."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

STATUS = "11-06新页面试用完成：6页只读截图+10条UX发现已按严重度写入friction.md，" * 4


class AgentsPhoneTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_each_agent_is_a_short_block_on_a_phone(self):
        now = time.time()
        agents = [{"name": f"agent-{i}", "revision": 1, "enabled": True, "archived": None, "tags": ["zone:公网", "maker:glm"],
                   "note": "GLM 5.3（DSH 会话），外部研究员", "checkin": {"at": now - 3600}, "status": {"text": STATUS, "at": now - 3600},
                   "activity": {"at": now - 11 * 3600, "source": "research"}} for i in range(5)]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/agents(\?.*)?$"), lambda route: route.fulfill(json={"agents": agents}))
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            row = '#agents-list tr[data-agent-row="agent-0"]'
            page.wait_for_selector(row)
            self.assertLessEqual(page.evaluate("() => document.getElementById('agents-list').scrollWidth - document.getElementById('agents-list').clientWidth"), 0)
            heights = page.eval_on_selector_all("#agents-list tbody tr", "rows => rows.map(r => r.getBoundingClientRect().height)")
            self.assertLess(max(heights), 150, heights)
            line = page.evaluate("r => ['.agent-zone', '.agent-running', '.agent-activity'].map(s => document.querySelector(r + ' ' + s).innerText.replace(/\\s+/g, ' ').trim())", row)
            self.assertEqual(line, ["公网", "0", "11 小时前写研究记录"])
            marks = page.evaluate("r => { const at = s => document.querySelector(r + ' ' + s); return [getComputedStyle(at('.agent-running'), '::before').content, getComputedStyle(at('.agent-running'), '::after').content, getComputedStyle(at('.agent-activity'), '::before').content]; }", row)
            self.assertEqual(marks, ['"· "', '" 个作业"', '"· "'], "公网 · 0 个作业 · 11 小时前 写研究记录")
            tops = page.evaluate("r => ['.agent-zone', '.agent-running', '.agent-activity'].map(s => Math.round(document.querySelector(r + ' ' + s).getBoundingClientRect().top))", row)
            self.assertEqual(len(set(tops)), 1, "one line")
            status = page.locator(row + " .agent-status")
            folded = status.bounding_box()["height"]
            status.click()
            self.assertGreater(status.bounding_box()["height"], folded * 1.5, "a tap shows the rest")


if __name__ == "__main__":
    unittest.main()
