"""The 指挥中心 shortens agent names (kimi-researcher reads kimi), but two agents whose short names are the same read
in full, so claude-lead and claude-researcher are not two rows both called claude; nor are they on the map."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


def model(now):
    return {"at": now, "live": True, "source": "test", "phones": [], "phones_sampled_at": None, "notes": [], "jobs": [], "directions": [],
            "agents": ["claude-lead", "kimi-researcher", "claude-researcher"],
            "research": [{"id": "sr-1", "title": "原神超分", "state": "active", "owner": "claude-researcher", "follows": None,
                          "created_at": now - 7200, "updated_at": now - 60}]}


class CommandAgentNameTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_two_agents_with_the_same_short_name_read_in_full(self):
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/command(\?.*)?$"), lambda route: route.fulfill(json=model(time.time())))
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto("http://%s:%d/dash/command" % self.address)
            page.wait_for_function("() => window.__ready === true", timeout=20000)
            self.assertEqual(page.eval_on_selector_all("#roster li .who b", "e => e.map(b => b.textContent)"),
                             ["claude-lead", "kimi", "claude-researcher"])
            page.wait_for_function("() => [...document.querySelectorAll('.mono')].some(e => e.textContent.startsWith('claude-researcher ·'))")


if __name__ == "__main__":
    unittest.main()
