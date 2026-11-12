"""Archiving, disabling and new tokens are for whoever holds the steer password. On the Agent page a read-only viewer
does not see them; 管理 asks for the steer password, and once it is unlocked they are there."""
import collections
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class AgentManageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web

    def test_changes_show_only_once_steer_is_unlocked(self):
        for name in ("worker", "idle"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        revision = next(a for a in self.cli("GET", "/agents")[1]["agents"] if a["name"] == "idle")["revision"]
        self.assertEqual(self.web("POST", "/agents/idle/archive", {"revision": revision, "archived": True})[0], 200)
        # Unlocking needs the failure counter, and the steer cookie over plain HTTP, as on the LAN.
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector("#agents-manage:visible")
            page.click("#agents-archived > summary")
            self.assertEqual(page.locator("#agents-list [data-act]:visible").count(), 0)
            self.assertTrue(page.locator("#agents-list th.agent-actions").first.is_hidden())
            self.assertEqual(page.inner_text("#agents-manage"), "管理")
            page.click("#agents-manage")
            page.fill("#agent-steer", core.STEER)
            page.click("#agent-unlock")
            page.wait_for_selector('#agents-list tr[data-agent-row="worker"] [data-act="disable"]:visible')
            self.assertTrue(page.is_hidden("#agents-manage"))
            self.assertFalse(page.locator("#agent-dialog").evaluate("d => d.open"))
            self.assertTrue(page.is_visible('#agents-list tr[data-agent-row="idle"] [data-act="unarchive"]'))
            # Reopened later, still unlocked: no need to unlock again.
            page.reload()
            page.wait_for_selector('#agents-list tr[data-agent-row="worker"] [data-act="archive"]:visible')
            self.assertTrue(page.is_hidden("#agents-manage"))


if __name__ == "__main__":
    unittest.main()
