"""The 指挥中心 rail counts each agent's finished experiments as the map does: 3 次实验, not a bare 3 次."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


def model(now):
    job = lambda i, agent: {"id": "%012x" % (0xa0 + i), "kind": "实验", "agent": agent, "research_id": "sr-1", "device": None,
                            "started": now - 3600 + i * 60, "ended": now - 3500 + i * 60, "outcome": "succeeded", "label": None,
                            "phone": False, "direction_id": "d-1", "state": "finished", "phase": "finished",
                            "queued": now - 3600 + i * 60, "claimed": now - 3600 + i * 60, "waits": []}
    return {"at": now, "live": True, "source": "test", "phones": [], "phones_sampled_at": None, "notes": [],
            "agents": ["kimi-researcher", "glm-researcher"],
            "research": [{"id": "sr-1", "title": "原神超分", "state": "active", "owner": "kimi-researcher", "follows": None,
                          "created_at": now - 7200, "updated_at": now - 60}],
            "directions": [{"id": "d-1", "research_id": "sr-1", "title": "小模型", "state": "verified", "based_on": None,
                            "created_at": now - 7000, "updated_at": now - 100}],
            "jobs": [job(i, "kimi-researcher") for i in range(3)] + [job(9, "glm-researcher")]}


class CommandRailRunsTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_each_agent_counts_its_finished_experiments(self):
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/command(\?.*)?$"), lambda route: route.fulfill(json=model(time.time())))
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto("http://%s:%d/dash/command" % self.address)
            page.wait_for_function("() => window.__ready === true", timeout=20000)
            page.wait_for_function("() => document.querySelector('#roster li .n')?.textContent.startsWith('3')")
            rows = page.eval_on_selector_all("#roster li", "e => e.map(li => [li.querySelector('.who b').textContent, li.querySelector('.n').textContent])")
            self.assertEqual(rows, [["kimi", "3次实验"], ["glm", "1次实验"]])
            fits = page.eval_on_selector_all("#roster li .n", "e => e.every(n => n.scrollWidth <= n.clientWidth + 1)")
            self.assertTrue(fits)


if __name__ == "__main__":
    unittest.main()
