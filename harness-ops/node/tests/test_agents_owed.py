"""The Agent directory's 待办 says what an agent owes, not only how many: under the count, in a person's words (回留言 ×2
· 更新状态), the same on hover. Nothing owed is 0 with nothing under it."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

DUTIES = {
    "agent-a": [{"kind": "answer_note", "text": "human 在 sr-1 留言：…"}, {"kind": "answer_note", "text": "yjh 在 sr-2 留言：…"},
                {"kind": "post_status", "text": "状态超过 30 分钟没更新"}],
    "agent-b": [{"kind": "conclude_direction", "text": "sr-3 已交付或准备交付，方向 d-1 仍为 running；给方向下结论"},
                {"kind": "something_new", "text": "一种新的待办"}],
    "agent-c": [],
}


class AgentsOwedTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_what_each_agent_owes_is_named(self):
        now = time.time()
        agents = [{"name": name, "revision": 1, "enabled": True, "archived": None, "tags": ["zone:公网"], "note": None,
                   "checkin": {"at": now - 60}, "status": {"text": "在做", "at": now - 60}, "activity": {"at": now - 60, "source": "checkin"}}
                  for name in DUTIES]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/agents(\?.*)?$"), lambda route: route.fulfill(json={"agents": agents}))
            page.route(re.compile(r".*/dash/api/agents/([^/]+)/duties(\?.*)?$"), lambda route: route.fulfill(json={
                "duties": DUTIES[re.search(r"/agents/([^/]+)/duties", route.request.url).group(1)]}))
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="agent-c"]')
            owed = page.eval_on_selector_all("#agents-list tbody .agent-owed", "e => e.map(td => [td.innerText.replace(/\\n+/g, ' | '), td.title])")
            self.assertEqual(owed, [["3 | 回留言 ×2 · 更新状态", "回留言 ×2\n更新状态"],
                                    ["2 | 给方向下结论 · something_new", "给方向下结论\nsomething_new"],
                                    ["0", ""]])


if __name__ == "__main__":
    unittest.main()
