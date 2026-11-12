"""The Agent page says how long ago an agent checked in and reported its status (8 天前), as its 最近活动 already does;
the exact time is on hover."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class AgentTimeTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_check_in_and_status_times_read_as_how_long_ago(self):
        now = time.time()
        agents = [{"name": "old", "revision": 1, "enabled": True, "archived": None, "tags": [], "checkin": {"at": now - 8 * 86400},
                   "status": {"text": "在跑基线", "at": now - 3 * 3600}, "activity": {"at": now - 3 * 3600, "source": "status"}},
                  {"name": "new", "revision": 1, "enabled": True, "archived": None, "tags": [], "checkin": None, "status": None,
                   "activity": None}]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/agents(\?.*)?$"), lambda route: route.fulfill(json={"agents": agents}))
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="new"]')
            cells = lambda name: page.locator(f'#agents-list tr[data-agent-row="{name}"] td')   # noqa: E731
            checkin = cells("old").nth(3)
            self.assertEqual(checkin.inner_text(), "8 天前")
            self.assertRegex(checkin.get_attribute("title"), r"^\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2}$")
            when = cells("old").nth(6).locator(".muted")
            self.assertEqual(when.inner_text(), "3 小时前")
            self.assertRegex(when.get_attribute("title"), r"^\d{4}/\d{1,2}/\d{1,2} \d{2}:\d{2}:\d{2}$")
            self.assertEqual(cells("new").nth(3).inner_text(), "从未")


if __name__ == "__main__":
    unittest.main()
