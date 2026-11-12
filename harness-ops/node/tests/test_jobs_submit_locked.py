"""The 作业 page's 提交作业 form, with 按请求 ID 找回 inside it, is there only for someone who has unlocked job
operations: a read-only viewer could not submit anything and does not see it."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class JobsSubmitLockedTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def test_only_someone_unlocked_sees_the_submit_form(self):
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            page.goto(base + "/dash/jobs")
            page.wait_for_function("() => document.getElementById('access-toggle').textContent === '解锁任务操作'")
            self.assertTrue(page.is_hidden("#submit-panel"))
            name, value = self.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": name, "value": value, "url": base}])
            page.reload()
            page.wait_for_selector("#submit-panel:not([hidden])")
            self.assertEqual(page.text_content("#submit-panel > summary"), "提交作业")
            page.click("#submit-panel > summary")
            self.assertTrue(page.is_visible("#request-lookup-form"))


if __name__ == "__main__":
    unittest.main()
