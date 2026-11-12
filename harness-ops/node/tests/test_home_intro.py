"""The home page says in one line how the system runs (a person asks, an agent experiments on real phones while the
person may step in at any time, a report comes back, a person judges it) and leads from there to 我来当一次 Agent, on a computer and on a phone."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

HOME = {"now": 0, "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "fleet_age_s": 0,
        "elsewhere": [], "agents": []}


class HomeIntroTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_one_line_says_how_the_system_runs_and_leads_to_the_tour(self):
        with self._browser_page() as page:
            page.route("**/dash/api/home", lambda route: route.fulfill(json={**HOME, "now": time.time()}))
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 844})
                page.goto("http://%s:%d/dash" % self.address)
                page.wait_for_selector(".home-intro")
                self.assertEqual(page.locator("main.home .home-intro").count(), 1)
                self.assertEqual(page.text_content(".home-intro"), "人提任务 → Agent 在真手机上做实验，人随时插话 → 交付报告 → 人来评判我来当一次 Agent →")
                self.assertTrue(page.is_visible(".home-intro a"), width)
                self.assertEqual(page.get_attribute(".home-intro a", "href").split("?")[0], "/dash/tour")
            page.click(".home-intro a")
            page.wait_for_url(lambda url: url.split("?")[0].endswith("/dash/tour"))


if __name__ == "__main__":
    unittest.main()
