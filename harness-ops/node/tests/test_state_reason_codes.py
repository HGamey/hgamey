"""Codes in a task's 状态说明 read as grey tags, as in the notes: 按用户意见 n-461699834d92 重写交付报告 reads as
按用户意见 留言 重写交付报告, and 显示技术细节 shows the note's ID."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

REASON = "按用户意见 n-461699834d92 重写交付报告"


class StateReasonCodeTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_codes_in_the_state_reason_are_tags(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-reason", self.write(
            "create-sr-reason", {"title": "重写报告", "objective": "按意见重写", "state": "active", "state_reason": REASON}))[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-reason" % self.address)
            reason = "#object-body h4:text-is('状态说明') + p"
            page.wait_for_selector(reason)
            self.assertEqual(page.inner_text(reason), "按用户意见 留言 重写交付报告")
            self.assertEqual(page.get_attribute(reason + " .code-chip", "title"), "n-461699834d92")
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.inner_text(reason), REASON)


if __name__ == "__main__":
    unittest.main()
