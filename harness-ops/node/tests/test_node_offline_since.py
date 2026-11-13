"""A computer that is offline reads 离线 · 从 <when it was last seen> 起 on the 设备 page, in its node card and above its
details. Its updater's last word (更新失败 when the network went: it could not reach the center) is a result of being
offline, not why, so it is a technical detail then. A connected computer still shows where its update stands."""
import re
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_node_updates_page as updates_page
from test_node_updates_page import REPORTED, updates

SINCE = re.compile(r"^离线 · 从 \d{1,2}/\d{1,2} \d{2}:\d{2} 起$")


class NodeOfflineSinceTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli
    fixture, open = updates_page.NodeUpdatesPageTests.fixture, updates_page.NodeUpdatesPageTests.open

    def test_an_offline_computer_says_since_when_and_folds_its_update_failure(self):
        before = dict(REPORTED)
        self.addCleanup(lambda: (REPORTED.clear(), REPORTED.update(before)))
        REPORTED["chengdu"] = updates("failed", "bbbbbbbb" + "3" * 32, behind=1, error="[Errno -3] Temporary failure in name resolution")
        REPORTED["lab"] = updates("failed", "dddddddd" + "4" * 32, behind=2, error="构建失败：cargo build exit 101")
        self.fixture()
        with self._browser_page() as page:
            self.open(page)
            card = '#node-directory .node-choice:has(strong:text-is("%s"))'
            offline = page.inner_text(card % "chengdu")
            self.assertRegex(page.inner_text(card % "chengdu" + " .node-choice-head .task-state"), SINCE)
            for word in ("更新失败", "已断开", "最近联系", "null"):
                self.assertNotIn(word, offline, word)
            self.assertIn("版本 bbbbbbbb", offline)
            self.assertIn("更新失败", page.inner_text(card % "lab"), "a connected computer's failed update still shows")

            page.click(card % "chengdu")
            page.wait_for_function("() => document.getElementById('node-message').textContent.startsWith('离线')")
            self.assertRegex(page.text_content("#node-message").split("。")[0], SINCE)

            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertIn("更新失败", page.inner_text(card % "chengdu"), "folded, not removed")


if __name__ == "__main__":
    unittest.main()
