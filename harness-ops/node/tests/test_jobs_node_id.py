"""The 作业 page names its computer by host name; the node ID shows only with 显示技术细节."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class JobsNodeIdTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_the_node_id_is_a_technical_detail(self):
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/jobs" % self.address)
            page.wait_for_selector("#node-summary:not([hidden])")
            hostname = self.cli("GET", "/node")[1]["host"]["hostname"]
            self.assertEqual(page.inner_text("#node-summary"), hostname)
            self.assertEqual(page.text_content("#node-summary"), f"{self.node_id} · {hostname}", "folded, not removed")
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertIn(self.node_id, page.inner_text("#node-summary"))


if __name__ == "__main__":
    unittest.main()
