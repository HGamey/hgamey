"""The top bar keeps six tabs a leader reads: 总览、指挥中心、任务、Agent、设备、结构. 作业 is the first of the engineers'
tools under 更多, and 数据集 sits after 研究结果 among the results; on a phone it is the same. Their pages keep their
addresses, and the 更多 button names the page when it is one of them."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

TABS = ["总览", "指挥中心", "任务", "Agent", "设备", "结构"]


class ShellNavTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def menu(self, page):
        return page.eval_on_selector_all("#shell-more-menu li", """items => {
            const groups = {}; let group = null;
            for (const item of items) { if (item.classList.contains('shell-menu-label')) groups[group = item.textContent] = []; else groups[group].push(item.textContent); }
            return groups; }""")

    def test_six_tabs_and_the_rest_under_more(self):
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 800})
                page.goto(base + "/dash/fleet")
                page.wait_for_selector(".shell-tabs .shell-tab")
                self.assertEqual(page.eval_on_selector_all(".shell-tabs .shell-tab", "e => e.map(a => a.textContent)"), TABS, width)
                groups = self.menu(page)
                self.assertEqual(groups["看结果"][:2], ["研究结果", "数据集"], width)
                self.assertEqual(groups["工程师用"][0], "作业", width)
            for path, name in (("/dash/jobs", "作业"), ("/dash/datasets", "数据集")):
                self.assertEqual(page.goto(base + path).status, 200)
                page.wait_for_selector(".shell-more-btn")
                self.assertEqual((page.text_content(".shell-more-btn"), page.get_attribute(".shell-more-btn", "aria-current")), (name, "page"))
                self.assertEqual(page.get_attribute(f'#shell-more-menu a:text-is("{name}")', "aria-current"), "page")
                self.assertEqual(page.eval_on_selector_all(".shell-tabs [aria-current]", "e => e.length"), 0)


if __name__ == "__main__":
    unittest.main()
