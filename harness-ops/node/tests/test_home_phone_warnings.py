"""A phone a person should look at says so on the home page as on the 设备 page: battery under 10% or a SoC at 45 °C
or more gets an amber tag beside its state, this computer's phones and another computer's. An offline phone shows no
warning from an old reading."""
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_cleanup_shown import Monitor
from test_phone_warnings import device


class HomePhoneWarningTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_low_battery_and_heat_get_amber_tags_on_the_home_page(self):
        now = time.time()
        local = [device("HN1", "HONOR", "AAP-AN00", 3, 51), device("OP1", "OnePlus", "PLC110", 94, 30),
                 {**device("GONE", "OnePlus", "PHK110", 1, 50), "online": False}]
        directory = types.SimpleNamespace(list=lambda: [{"node_id": "5108fd699aea642d3a3bf2f10ee932f7", "name": "mini3",
            "connection": {"status": "ready"}, "snapshot": {"fleet": {"devices": [device("RM1", "REDMAGIC", "NX809J", 8, 40)], "sampled_at": now, "stale": False}}}])
        with mock.patch.object(self.board.job_service, "directory", directory), mock.patch.object(self.board, "_remote_holders", return_value={}):
            elsewhere = self.board._elsewhere()
        self.board.monitor, self.board.shot_dir = Monitor(local), str(self.tmp)
        self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
        with self._browser_page() as page, mock.patch.object(self.board, "_elsewhere", return_value=elsewhere):
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('.phone-card[data-ref] .name')
            cards = page.eval_on_selector_all(".phone-card", "e => e.map(c => [c.querySelector('.name').textContent, [...c.querySelectorAll('.phone-head .tag.warn')].map(t => t.textContent)])")
            self.assertEqual([[name, [tag for tag in tags if tag.startswith(("电量低", "过热"))]] for name, tags in cards],
                             [["HONOR AAP-AN00", ["电量低 3%", "过热 51 °C"]], ["OnePlus PLC110", []], ["OnePlus PHK110", []], ["REDMAGIC NX809J", ["电量低 8%"]]])


if __name__ == "__main__":
    unittest.main()
