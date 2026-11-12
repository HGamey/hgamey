"""The Agent page's panel builds its table, details and dialogs element by element: nothing is parsed from a string,
so what an agent writes (its note, its status) shows as written, and a change to one part does not touch the rest."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

NOTE = "<b>粗体</b>；第二句"
STATUS = "<img src=x onerror=\"window.hit=1\"> 在跑"


class AgentsPanelTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_the_panel_builds_elements_and_text_stays_text(self):
        source = (core.NODE / "dash_static/agents-panel.js").read_text(encoding="utf-8")
        for parsed in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertFalse(parsed in source, f"agents-panel.js uses {parsed}")
        self.assertEqual(self.cli("POST", "/agents", {"name": "odd", "note": NOTE})[0], 201)
        token = self.cli("POST", "/agents/odd/token", {})[1]["token"]
        headers = {"Authorization": f"Bearer {token}", "X-Expected-Node-ID": self.node_id, "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/agents/odd/status", {"text": STATUS}, headers)[0], 200)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            row = page.locator('#agents-list tr[data-agent-row="odd"]')
            row.wait_for()
            self.assertEqual(row.locator(".agent-role").inner_text(), "<b>粗体</b>")
            self.assertIn(STATUS, row.inner_text())
            self.assertEqual(row.locator("b, img").count(), 0)
            page.click('#agents-list .agent-name[data-agent="odd"]')
            page.wait_for_function("() => document.getElementById('agent-detail-status').textContent.endsWith('在跑')")
            self.assertEqual(page.inner_text("#agent-detail-who"), NOTE)
            self.assertEqual(page.inner_text("#agent-detail-status"), STATUS)
            self.assertEqual(page.locator("#agent-detail b, #agent-detail img").count(), 0)
            self.assertIsNone(page.evaluate("window.hit"))


if __name__ == "__main__":
    unittest.main()
