"""The 设备 page names computers without their node IDs (a string nobody needs to copy): in the computer cards and in
the selected computer's line they show only with 显示技术细节. A computer's zone reads by name; its other tags are
technical details too."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

REMOTE = "5108fd699aea642d3a3bf2f10ee932f7"


class FleetNodeIdTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_node_ids_are_technical_details(self):
        directory = self.jobs.directory
        directory.join({"invite": directory.invite()["code"], "name": "mini3", "node_id": REMOTE, "tags": ["zone:深圳公司", "gray"]})
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_selector('#node-directory .node-choice:has-text("mini3")')
            page.wait_for_function("() => document.getElementById('node-summary').textContent.includes('本节点')")
            shown = lambda selector: page.inner_text(selector)   # noqa: E731
            for node_id in (self.node_id, REMOTE):
                self.assertNotIn(node_id, shown("#node-directory"))
                self.assertNotIn(node_id, shown("#node-summary"))
            card = page.locator('#node-directory .node-choice:has-text("mini3")')
            self.assertIn("深圳公司", card.inner_text())
            self.assertNotIn("zone:", card.inner_text())
            self.assertNotIn("gray", card.inner_text())
            self.assertTrue(shown("#node-summary").startswith("本节点"))
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertIn(REMOTE, card.inner_text())
            self.assertIn("gray", card.inner_text())
            self.assertIn(self.node_id, shown("#node-summary"))
            self.assertIn(self.node_id, shown("#node-directory"))


if __name__ == "__main__":
    unittest.main()
