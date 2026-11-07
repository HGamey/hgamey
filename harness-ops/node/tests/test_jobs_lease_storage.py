"""The jobs page reads lease storage as phonefarm reports it now: where its lock and lease records live, and an error
when they cannot be used. Nothing else marks the storage unusable."""
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path

STORAGE = {"schema_version": 1, "state_dir": "/pf/state", "devlock_dir": "/pf/state/locks",
           "device_lease_dir": "/pf/state/locks/devices", "error": None}


class JobsLeaseStorageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def lease(self, storage):
        patch = mock.patch.object(self.jobs.leases, "snapshot", return_value={
            "schema_version": 1, "status": "clear", "bridge_held": False, "entries": [], "error": None, "storage": storage})
        patch.start()
        self.addCleanup(patch.stop)
        return patch

    def read(self, page):
        page.goto("http://%s:%d/dash/jobs" % self.address)
        page.wait_for_function("() => !/尚未/.test(document.getElementById('lease-state').textContent)")
        return [page.text_content(s) for s in ("#lease-state", "#lease-storage-state", "#lease-storage-message")]

    def test_storage_with_its_paths_and_no_error_is_usable_and_an_error_is_not(self):
        self.lease(STORAGE)
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.assertEqual(self.read(page), ["屏障已清除", "存储正常", ""])
            self.assertEqual(page.eval_on_selector_all("#lease-storage-paths dd", "e => e.map(x => x.textContent)"),
                             ["/pf/state", "/pf/state/locks", "/pf/state/locks/devices"])
            self.jobs.leases.snapshot.return_value = {**self.jobs.leases.snapshot.return_value,
                                                      "storage": {**STORAGE, "error": "permission denied: /pf/state/locks"}}
            self.assertEqual(self.read(page), ["存储不可用", "存储不可用", "错误：permission denied: /pf/state/locks"])
            self.assertEqual(page.text_content("#lease-message"), "存储不可用：permission denied: /pf/state/locks")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
