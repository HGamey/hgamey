"""When no phone is held, the 设备 page's lock card says one thing: 没有手机被占用. How the lock reads (its state,
heartbeat, records, events, the note on what it proves) is a technical detail. A held lock shows in full."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

FREE = {"status": "free", "held_by": None, "since": None, "heartbeat_age_s": None, "free_for_s": 120, "stale": False,
        "lease": {"status": "clear", "entries": []}, "queue": [], "recent": []}
HELD = {**FREE, "status": "held", "held_by": "job-0123456789ab@HN1", "since": "2026-11-12T05:00:00Z", "heartbeat_age_s": 3,
        "lease": {"status": "held", "entries": [{"role": "device_lease", "state": "active", "serial": "HN1", "label": "job-0123456789ab",
                                                 "instance_id": "i-1", "directory": "/tmp/lease"}]}}
PARTS = ["#lock-state", "#lock-message", "#lock-info", ".lock-identities", ".lock-scope"]


class FleetLockTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def show(self, page, lock):
        page.route(re.compile(r".*/dash/api/fleet(\?.*)?$"), lambda route: route.fulfill(json={
            "schema_version": 1, "lock": lock, "devices": [], "sampled_at": time.time(), "age_s": 0, "stale": False}))
        page.goto("http://%s:%d/dash/fleet" % self.address)
        page.wait_for_function("() => !['尚未读取', ''].includes(document.getElementById('lock-owner').textContent)")

    def test_an_idle_lock_is_one_line(self):
        with self._browser_page() as page:
            self.show(page, FREE)
            self.assertEqual(page.inner_text("#lock-owner"), "没有手机被占用")
            for part in PARTS:
                self.assertTrue(page.locator(f".lock-panel {part}").first.is_hidden(), part)
            self.assertTrue(page.is_visible("#node-queue-details"))   # the node's job queue is not about the lock
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            for part in PARTS:
                self.assertTrue(page.locator(f".lock-panel {part}").first.is_visible(), part)

    def test_a_held_lock_shows_in_full(self):
        with self._browser_page() as page:
            self.show(page, HELD)
            self.assertEqual(page.inner_text("#lock-owner"), "标签：job-0123456789ab@HN1")
            for part in PARTS:
                self.assertTrue(page.locator(f".lock-panel {part}").first.is_visible(), part)


if __name__ == "__main__":
    unittest.main()
