"""A phone whose screen is off says so on its picture on the home page (息屏), so a black picture reads as a phone
asleep, not as a picture that failed; one with no picture yet says 息屏 in its place. A phone with its screen on, or
one that does not report it, has no mark."""
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_cleanup_shown import Monitor
from test_phone_warnings import device


class HomeScreenOffTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_a_phone_asleep_is_marked_on_its_picture(self):
        phones = [{**device("HN1", "HONOR", "AAP-AN00", 50, 33), "screen_on": False},
                  {**device("RM1", "REDMAGIC", "NX809J", 36, 40), "screen_on": True},
                  {**device("OP1", "OnePlus", "PLC110", 84, None)},
                  {**device("NO1", "OnePlus", "PKG110", 70, 30), "screen_on": False}]
        for serial in ("HN1", "RM1", "OP1"):
            (self.tmp / f"{serial}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        self.board.monitor, self.board.shot_dir = Monitor(phones), str(self.tmp)
        self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
        with self._browser_page() as page, mock.patch.object(self.board, "_elsewhere", return_value=[]):
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector(".phone-card .thumb")
            cards = page.eval_on_selector_all(".phone-card", """cards => cards.map(c => [c.querySelector('.name').textContent,
                !!c.querySelector('.thumb img'), c.querySelector('.thumb').innerText.trim()])""")
            self.assertEqual(cards, [["HONOR AAP-AN00", True, "息屏"], ["REDMAGIC NX809J", True, ""], ["OnePlus PLC110", True, ""],
                                     ["OnePlus PKG110", False, "息屏"]])


if __name__ == "__main__":
    unittest.main()
