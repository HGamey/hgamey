"""Every online phone on the home page shows the same readings, 芯片 · 电池 · 电量, so the cards line up: one the phone
does not give (a OnePlus without root has no SoC sensor to read) shows — with why on hover, instead of a card one
reading short. A phone that is offline shows none."""
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_cleanup_shown import Monitor
from test_phone_warnings import device

READ = """cards => cards.map(c => [c.querySelector('.name').textContent,
    [...c.querySelectorAll('.pstat')].map(s => [s.querySelector('b').textContent, s.querySelector('span').textContent, s.title])])"""


class HomePhoneReadingTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_an_online_phone_shows_every_reading(self):
        red = {**device("RM1", "REDMAGIC", "NX809J", 36, 40.1)}
        red["battery"]["temp_c"] = 35.0
        plus = {**device("OP1", "OnePlus", "PLC110", 84, None)}
        plus["battery"]["temp_c"] = 33.8
        gone = {**device("OP2", "OnePlus", "PKG110", None, None), "online": False}
        self.board.monitor, self.board.shot_dir = Monitor([red, plus, gone]), str(self.tmp)
        self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
        now = time.time()
        far = {**device("HN1", "HONOR", "AAP-AN00", 50, None)}
        directory = types.SimpleNamespace(list=lambda: [{"node_id": "5108fd699aea642d3a3bf2f10ee932f7", "name": "mini3",
            "connection": {"status": "ready"}, "snapshot": {"fleet": {"devices": [far], "sampled_at": now, "stale": False}}}])
        with mock.patch.object(self.board.job_service, "directory", directory), mock.patch.object(self.board, "_remote_holders", return_value={}):
            elsewhere = self.board._elsewhere()
        with self._browser_page() as page, mock.patch.object(self.board, "_elsewhere", return_value=elsewhere):
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector(".phone-card[data-ref] .pstat")
            cards = page.eval_on_selector_all(".phone-card", READ)
            chip, battery, level = "这台手机没有给出芯片温度", "这台手机没有给出电池温度", "这台手机没有给出电量"
            self.assertEqual(cards, [
                ["REDMAGIC NX809J", [["40°", "芯片", ""], ["35°", "电池", ""], ["36%", "电量", ""]]],
                ["OnePlus PLC110", [["—", "芯片", chip], ["34°", "电池", ""], ["84%", "电量", ""]]],
                ["OnePlus PKG110", []],
                ["HONOR AAP-AN00", [["—", "芯片", chip], ["—", "电池", battery], ["50%", "电量", ""]]]])
            self.assertTrue(level)


if __name__ == "__main__":
    unittest.main()
