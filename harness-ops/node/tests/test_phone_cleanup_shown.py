"""A phone whose leftover processes its computer stopped in the last day says so in amber on the 设备 page and on the
home page: 清理了 n 个残留进程, each process (name, PID, how long it ran) on hover. A clean-up older than a day does
not show."""
import re
import time
import types
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_phone_warnings import device

TITLE = "toybox yes（PID 4242，跑了 12 小时 40 分）\ntoybox yes（PID 4250，跑了 1 天 2 小时）"


def cleaned(now):
    return [{"at": now - 2 * 86400, "processes": [{"pid": 7, "name": "old", "cpu_s": 99, "elapsed_s": 99}]},
            {"at": now - 600, "processes": [{"pid": 4242, "name": "toybox yes", "cpu_s": 45065, "elapsed_s": 45600},
                                            {"pid": 4250, "name": "toybox yes", "cpu_s": 90000, "elapsed_s": 93600}]}]


class Monitor(core.NoDevices):
    def __init__(self, devices):
        self.devices = devices

    def request(self, *args, **kwargs):   # the home page asks for a fresh reading; this one is fixed
        return self.snapshot()

    def snapshot(self):
        return {**super().snapshot(), "devices": self.devices, "sampled_at": time.time(), "age_s": 0, "error": None, "deferred": None}


class PhoneCleanupShownTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page

    def test_a_clean_up_shows_for_a_day(self):
        now = time.time()
        honor, quiet = {**device("HN1", "HONOR", "AAP-AN00", 2, 51), "cleanups": cleaned(now)}, device("OP1", "OnePlus", "PLC110", 94, 30)
        phones = [{"ref": f"{self.node_id}/{d['serial']}", "node_id": self.node_id, "node_name": "local", "device_id": d["serial"],
                   "serial": d["serial"], "connected": True, "stale": False, "sampled_at": now, "remote_actions": [],
                   "observation": d} for d in (honor, quiet)]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/fleet(\?.*)?$"), lambda route: route.fulfill(json={
                "schema_version": 1, "lock": None, "devices": [honor, quiet], "sampled_at": now, "age_s": 0, "stale": False}))
            page.route(re.compile(r".*/dash/api/phones(\?.*)?$"), lambda route: route.fulfill(json={"items": phones}))
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_selector('#all-phones-list li[data-ref$="/OP1"]')
            page.wait_for_selector("#devices .device-card")
            for where in ('#all-phones-list li[data-ref$="/HN1"]', '#devices .device-card:has(h3:text-is("AAP-AN00"))'):
                tag = page.locator(where + " .phone-cleaned")
                self.assertEqual((tag.text_content(), tag.get_attribute("title")), ("清理了 2 个残留进程", TITLE), where)
            self.assertEqual(page.locator('#all-phones-list li[data-ref$="/OP1"] .phone-cleaned').count(), 0)

            # The home page: this computer's phones and another computer's, from its last report.
            far = {**device("RM1", "REDMAGIC", "NX809J", 60, 40), "cleanups": cleaned(now)}
            directory = types.SimpleNamespace(list=lambda: [{"node_id": "5108fd699aea642d3a3bf2f10ee932f7", "name": "mini3",
                "connection": {"status": "ready"}, "snapshot": {"fleet": {"devices": [far], "sampled_at": now, "stale": False}}}])
            with mock.patch.object(self.board.job_service, "directory", directory), mock.patch.object(self.board, "_remote_holders", return_value={}):
                elsewhere = self.board._elsewhere()
            self.board.monitor, self.board.shot_dir = Monitor([honor, quiet]), str(self.tmp)
            self.board.goals = types.SimpleNamespace(list=lambda: [], harness_support=lambda: True)
            with mock.patch.object(self.board, "_elsewhere", return_value=elsewhere):
                page.goto("http://%s:%d/dash" % self.address)
                page.wait_for_selector('.phone-card[data-ref] .cleaned')
                lines = page.eval_on_selector_all(".phone-card", "cards => cards.map(c => [c.querySelector('.name').textContent, c.querySelector('.cleaned')?.textContent || null])")
                self.assertEqual(lines, [["HONOR AAP-AN00", "清理了 2 个残留进程"], ["OnePlus PLC110", None], ["REDMAGIC NX809J", "清理了 2 个残留进程"]])
                self.assertEqual(page.get_attribute(".phone-card .cleaned", "title"), TITLE)
                self.assertNotEqual(page.eval_on_selector(".phone-card .cleaned", "e => getComputedStyle(e).color"),
                                    page.eval_on_selector(".phone-card .name", "e => getComputedStyle(e).color"), "amber")


if __name__ == "__main__":
    unittest.main()
