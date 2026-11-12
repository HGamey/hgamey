"""GOL 目标 is an engineers' word: the home page no longer shows it, and its page is reached from
更多 → 工程师用 like the other engineers' tools."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

HOME = {"now": 0, "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "fleet_age_s": 0,
        "elsewhere": [], "agents": []}


class HomeGolInMoreTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_the_home_top_bar_has_no_gol_and_more_has_it_for_engineers(self):
        with self._browser_page() as page:
            page.route("**/dash/api/home", lambda route: route.fulfill(json={**HOME, "now": time.time()}))
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 844})
                page.goto("http://%s:%d/dash" % self.address)
                page.wait_for_selector(".home-intro")
                page.wait_for_selector("#shell-more-menu", state="attached")
                outside = page.evaluate("""() => [...document.querySelectorAll('a[href*="/dash/gol"]')]
                    .filter(a => !a.closest('#shell-more-menu')).map(a => a.textContent)""")
                self.assertEqual(outside, [], width)
                self.assertNotIn("GOL", page.inner_text("body"), width)
                engineers = page.evaluate("""() => { let group = null; const found = [];
                    for (const li of document.querySelectorAll('#shell-more-menu li')) {
                        if (li.classList.contains('shell-menu-label')) group = li.textContent;
                        else if (li.querySelector('a[href*="/dash/gol"]')) found.push(group); }
                    return found; }""")
                self.assertEqual(engineers, ["工程师用"], width)


if __name__ == "__main__":
    unittest.main()
