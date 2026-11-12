"""A phone the home page has no model reading for (it is offline) is named by its registration, as on the structure
page (OP-PHK110), not by its serial; the serial stays on hover. This computer's phones and another computer's alike."""
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_cleanup_shown import Monitor


class HomeRegisteredNameTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_an_offline_phone_reads_by_its_registered_name(self):
        now = time.time()
        scopes = {"registry_revision": 1, "global": {"status": "clear"},
                  "devices": [{"device_id": "OP-PHK110", "aliases": ["705cd0f0"], "status": "clear"}]}
        directory = types.SimpleNamespace(list=lambda: [{"node_id": "ffa1595983a84505ac36fa767775c213", "name": "omv-lan",
            "connection": {"status": "error"}, "snapshot": {"fleet": {"devices": [], "sampled_at": now, "stale": False},
                                                            "queue": {"lease": {"device_scopes": scopes}}}}])
        with mock.patch.object(self.board.job_service, "directory", directory), mock.patch.object(self.board, "_remote_holders", return_value={}):
            elsewhere = self.board._elsewhere()
        registry = types.SimpleNamespace(read=lambda: {"devices": [{"device_id": "HONOR-AAP", "aliases": ["AVXB6R5C19000481"]}]})
        self.board.monitor, self.board.shot_dir = Monitor([{"serial": "AVXB6R5C19000481", "online": False}]), str(self.tmp)
        self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
        with self._browser_page() as page, mock.patch.object(self.board, "_elsewhere", return_value=elsewhere), \
                mock.patch("dash.DeviceRegistry", return_value=registry), mock.patch.dict(self.board.job_service.bins, {"phonefarm": ("pf", "/")}):
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('.phone-card[data-ref] .name')
            names = page.eval_on_selector_all(".phone-card .name", "e => e.map(n => [n.textContent, n.title])")
            self.assertEqual(names, [["HONOR-AAP", "AVXB6R5C19000481"], ["OP-PHK110", "705cd0f0"]])


if __name__ == "__main__":
    unittest.main()
