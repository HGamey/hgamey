"""A person who can only look at a task sees only what they can do above it: 刷新 and the views of its directions and
requests. The changes (编辑此版本, 新增方向, 发出协助请求) are not offered until the page is unlocked; 解锁操作 stands
in for them and opens the unlock form, and once unlocked they are there."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

SHOWN = "() => [...document.querySelectorAll('#detail-actions button')].filter(b => !b.hidden).map(b => b.textContent)"


class TaskReadonlyActionTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_changes_are_offered_only_once_unlocked(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-look", self.write(
            "create-sr-look", {"title": "只读看任务", "objective": "看"}))[0], 201)
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            page.goto(base + "/dash/research?kind=research&id=sr-look")
            page.wait_for_function(SHOWN + ".includes('解锁操作')")
            self.assertEqual(page.evaluate(SHOWN), ["刷新", "查看研究方向", "查看研究请求", "解锁操作"])
            page.click("#unlock-actions")
            self.assertTrue(page.is_visible("#unlock-form"))
            self.assertTrue(page.evaluate("() => document.activeElement?.id === 'steer-password'"))

            name, value = self.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": name, "value": value, "url": base}])
            page.reload()
            # 编辑此版本 is in the page before the record is: wait for one only the record's repaint shows.
            page.wait_for_function(SHOWN + ".includes('新增方向')")
            self.assertEqual(page.evaluate(SHOWN), ["刷新", "编辑此版本", "新增方向", "发出协助请求", "查看研究方向", "查看研究请求"])


if __name__ == "__main__":
    unittest.main()
