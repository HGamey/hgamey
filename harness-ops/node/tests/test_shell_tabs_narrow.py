"""On a phone the six tabs fit beside 更多 at 390 px. On a narrower screen they scroll a whole tab at a time, so no
tab is cut through a word at the left edge, and the side that has more tabs fades out."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

STATE = """() => { const t = document.querySelector('.shell-tabs'), r = t.getBoundingClientRect();
    return {over: t.scrollWidth - t.clientWidth, left: t.classList.contains('more-left'), right: t.classList.contains('more-right'),
            cut: [...t.children].some(a => { const b = a.getBoundingClientRect(); return b.left < r.left - 1 && b.right > r.left + 1; })}; }"""


class ShellTabsNarrowTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_tabs_fit_a_phone_and_scroll_whole_tabs_when_narrower(self):
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            page.set_viewport_size({"width": 390, "height": 844})
            for path in ("/dash", "/dash/research", "/dash/structure"):
                page.goto(base + path)
                page.wait_for_selector(".shell-tabs .shell-tab")
                self.assertEqual(page.evaluate(STATE), {"over": 0, "left": False, "right": False, "cut": False}, path)

            page.set_viewport_size({"width": 340, "height": 700})
            page.goto(base + "/dash/fleet")
            page.wait_for_selector(".shell-tabs .shell-tab")
            page.wait_for_timeout(300)
            state = page.evaluate(STATE)
            self.assertGreater(state["over"], 0)
            self.assertFalse(state["cut"], "the current tab is brought in view on a whole-tab stop")
            self.assertTrue(state["left"] or state["right"])
            page.hover(".shell-tabs")
            page.mouse.wheel(37, 0)
            page.wait_for_timeout(600)
            self.assertFalse(page.evaluate(STATE)["cut"], "a scroll stops on a whole tab")
            page.evaluate("() => document.querySelector('.shell-tabs').scrollLeft = 0")
            page.wait_for_timeout(300)
            self.assertEqual({k: v for k, v in page.evaluate(STATE).items() if k in ("left", "right")}, {"left": False, "right": True})


if __name__ == "__main__":
    unittest.main()
