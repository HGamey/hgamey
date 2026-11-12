"""The 作业 page calls its jobs 作业 throughout, as the rest of the site does (任务 is a research task), and says which
phones are taken under 手机占用. Where the lease records are kept, the per-scope snapshot and the host's scheduling
slots are technical details."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class JobsWordsTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_jobs_are_called_jobs_and_lease_detail_is_folded(self):
        with self._browser_page() as page:
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto("http://%s:%d/dash/jobs" % self.address)
            page.wait_for_selector(f'#task-list [data-job="{self.jid}"]')
            page.wait_for_function("() => !document.getElementById('lease-state').textContent.includes('尚未读取')")
            self.assertEqual(page.inner_text("#lease-heading"), "手机占用")
            main = page.inner_text("main")
            self.assertNotIn("任务", main)
            for word in ("lease", "slots", "全域与各设备范围快照"):
                self.assertNotIn(word, page.inner_text("#queue-panel"), word)

            page.click(f'#task-list [data-job="{self.jid}"]')
            page.wait_for_selector("#detail-placeholder", state="hidden")
            self.assertNotIn("任务", page.inner_text("main"), "nor in a job's own record")

            page.click(".shell-more-btn")
            page.click("#shell-tech")
            queue = page.inner_text("#queue-panel")
            for word in ("lease 存储", "宿主调度 slots"):
                self.assertIn(word, queue, word)


if __name__ == "__main__":
    unittest.main()
