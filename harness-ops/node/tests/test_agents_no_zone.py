"""An agent with no zone tag reads 未分区 in the Agent directory's 标签, as a computer with none does on the 结构 page,
not a dash standing alone; on a phone too."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class AgentsNoZoneTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_an_agent_without_a_zone_reads_no_zone(self):
        now = time.time()
        agents = [{"name": name, "revision": 1, "enabled": True, "archived": None, "tags": tags, "note": None,
                   "checkin": {"at": now - 60}, "status": None, "activity": {"at": now - 60, "source": "checkin"}}
                  for name, tags in (("zoned", ["zone:公网", "maker:glm"]), ("loose", ["maker:codex"]), ("bare", []))]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/agents(\?.*)?$"), lambda route: route.fulfill(json={"agents": agents}))
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 900})
                page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
                page.wait_for_selector('#agents-list tr[data-agent-row="bare"]')
                zones = page.eval_on_selector_all("#agents-list tbody .agent-zone", "e => e.map(td => td.innerText.trim())")
                self.assertEqual(zones, ["公网", "未分区", "未分区"], width)


if __name__ == "__main__":
    unittest.main()
