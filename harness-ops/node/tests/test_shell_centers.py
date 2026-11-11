"""中心节点 in the top bar: a select beside the node button on a wide screen; on a narrow one, where the bar has no room
for it, the same entries are in 更多. Either way choosing an entry opens the current page there."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

ENTRY = "https://gameharness.liyao.space"


class ShellCentersTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_the_center_switcher_is_in_the_bar_on_a_wide_screen_and_in_more_on_a_phone(self):
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            page.route(ENTRY + "/**", lambda route, request: route.fulfill(body="<p>entry</p>", content_type="text/html"))
            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(base + "/dash/jobs?view=1")
            page.wait_for_selector(".shell-top")
            self.assertTrue(page.is_visible(".shell-center"))
            page.click(".shell-more-btn")
            self.assertTrue(page.is_hidden(".shell-centers"))
            page.keyboard.press("Escape")

            page.set_viewport_size({"width": 390, "height": 844})
            self.assertTrue(page.is_hidden(".shell-center"))
            page.click(".shell-more-btn")
            self.assertEqual(page.eval_on_selector_all(".shell-centers a", "e => e.map(a => a.textContent)"),
                             ["gameharness · Cloudflare", "office · frp"])
            self.assertEqual(page.eval_on_selector_all(".shell-centers a[aria-current]", "e => e.length"), 0,
                             "this page is on neither entry")
            # The page as it is when the entry is chosen, the place in it included.
            page.evaluate("history.replaceState(null, '', location.pathname + location.search + '#job=abc')")
            page.click('.shell-centers a:has-text("gameharness")')
            page.wait_for_url(ENTRY + "/**")
            self.assertEqual(page.url, ENTRY + "/dash/jobs?view=1#job=abc")


if __name__ == "__main__":
    unittest.main()
