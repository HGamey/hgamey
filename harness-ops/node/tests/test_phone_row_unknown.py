"""A phone the 设备 page knows nothing current about (offline: nothing it is doing, no system, no root reading) has
— in those columns on a computer, under their headings. On a phone, where a row has no headings, those cells are left
out instead of three dashes standing alone."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_warnings import device

CELLS = "r => [...document.querySelector(r).children].filter(c => c.offsetParent !== null).map(c => c.innerText.trim())"


class PhoneRowUnknownTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_unknown_cells_are_dashes_under_headings_and_left_out_on_a_phone(self):
        now = time.time()
        on = {**device("RM1", "REDMAGIC", "NX809J", 60, 40), "os": "Android 16", "root": True}
        phones = [{"ref": f"{self.node_id}/RM1", "node_id": self.node_id, "node_name": "local", "device_id": "RM1", "serial": "RM1",
                   "connected": True, "stale": False, "sampled_at": now, "remote_actions": [], "observation": on},
                  # As omv-lan's phones while it is offline: its computer's link is down and nothing was read.
                  {"ref": "ffa15959/705cd0f0", "node_id": "ffa15959", "node_name": "omv-lan", "device_id": "OP-PHK110", "serial": "705cd0f0",
                   "connected": False, "stale": True, "sampled_at": None, "remote_actions": [], "observation": None}]
        off_row, on_row = f'#all-phones-list li[data-ref$="/705cd0f0"]', f'#all-phones-list li[data-ref$="/RM1"]'
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/phones(\?.*)?$"), lambda route: route.fulfill(json={"items": phones}))
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_selector(off_row)
            self.assertEqual(page.evaluate(CELLS, off_row).count("—"), 3, "under their headings on a computer")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("() => innerWidth === 390")
            cells = page.evaluate(CELLS, off_row)
            self.assertNotIn("—", cells)
            self.assertIn("离线", " ".join(cells))
            self.assertIn("空闲", " ".join(page.evaluate(CELLS, on_row)), "a phone that is there keeps its cells")


if __name__ == "__main__":
    unittest.main()
