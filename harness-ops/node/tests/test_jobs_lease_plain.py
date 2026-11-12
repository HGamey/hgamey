"""The 作业 page says first whether phones are held: 没有手机被占用, N 台手机被占用, 有手机需要恢复. How the barrier was
judged (the compatibility lock, lease and queue resources checked apart) shows only with 显示技术细节."""
import re
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

QUEUE = re.compile(r".*/dash/api/queue(\?.*)?$")


class JobsLeasePlainTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli = core.CoreFlowTests.request, core.CoreFlowTests.cli

    def shown(self, page, lease=None):
        if lease:
            def held(route):
                value = route.fetch().json()
                value["lease"].update(lease)
                route.fulfill(json=value)
            page.route(QUEUE, held)
        page.goto("http://%s:%d/dash/jobs" % self.address)
        page.wait_for_function("() => !/正在读取|尚未返回/.test(document.getElementById('lease-message').textContent)")
        return page.inner_text("#lease-message"), page.text_content("#lease-message")

    def test_the_lease_line_says_whether_phones_are_held(self):
        with self._browser_page() as page:
            plain, whole = self.shown(page)
            self.assertEqual(plain, "没有手机被占用")
            self.assertIn("兼容锁：", whole, "folded, not removed")
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertIn("兼容锁：", page.inner_text("#lease-message"))
        entries = [{"role": "device_lease", "serial": serial, "state": "active"} for serial in ("A1B2C3D4E5", "F6G7H8I9J0", "A1B2C3D4E5")]
        with self._browser_page() as page:
            self.assertEqual(self.shown(page, {"status": "held", "entries": entries})[0], "2 台手机被占用")
        with self._browser_page() as page:
            self.assertEqual(self.shown(page, {"status": "blocked"})[0], "有手机需要恢复")


if __name__ == "__main__":
    unittest.main()
