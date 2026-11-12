"""全部手机 on the 设备 page: every phone on every computer as the center sees it, what can be done to it remotely, and
links that open remote control or the job form on that phone."""
import time
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import node_directory
from test_structure_page import BLUE, CHENGDU, snapshot

ACTIONS = ["acquire", "screenshot", "tap", "swipe", "text", "key", "shell", "install", "push"]


class AllPhonesTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request = core.CoreFlowTests.request
    cli = core.CoreFlowTests.cli

    def fixture(self):
        """A blue-zone computer that reports now and declares remote actions; a Chengdu computer last heard from an hour
        ago, which declares none. The center: HONOR in a reading from five hours ago, HUAWEI not in it, and a disabled
        registration (a phone that moved to another computer)."""
        directory = self.jobs.directory
        directory.join({"invite": directory.invite()["code"], "name": "office", "node_id": BLUE, "tags": ["zone:华为蓝区"]})
        directory.report(BLUE, {**snapshot("thinkcentre", "Linux", "95f55e5c72ae", "RM1", "REDMAGIC", "NX809J", "Android 16 (SDK 36)"),
                                "remote": {"capabilities": {"available": True, "actions": ACTIONS}}})
        directory.join({"invite": directory.invite()["code"], "name": "chengdu", "node_id": CHENGDU, "tags": ["zone:成都公网"]})
        with mock.patch.object(node_directory.time, "time", return_value=time.time() - 3600):
            directory.report(CHENGDU, snapshot("kamico-mac", "Darwin", "e0005d4a11", "OP1", "OnePlus", "PHK110", "Android 15 (SDK 35)"))
        local = {"stale": True, "sampled_at": time.time() - 5 * 3600, "devices": [{"serial": "HN1", "transport": "adb", "online": True,
                 "brand": "HONOR", "model": "AAP-AN00", "os": "Android 16 (SDK 36)", "root": False}]}
        registered = (("HONOR", "HN1", True), ("HUAWEI", "HD1", True), ("REDMAGIC", "retired-RM1", False))
        scopes = {"schema": "phonefarm/device-scopes@1", "registry_revision": 3, "unknown": False, "queue_waiting": False,
                  "global": {"status": "clear", "error": None},
                  "devices": [{"device_id": d, "aliases": [a], "status": "clear", "error": None} for d, a, _ in registered]}
        registry = {"schema": "phonefarm/device-registry@1", "revision": 3, "updated_at": "2026-11-06T00:00:00Z",
                    "devices": [{"device_id": d, "label": d, "aliases": [a], "enabled": on} for d, a, on in registered]}
        for patch in (mock.patch.object(self.jobs.fleet, "snapshot", return_value=local),
                      mock.patch.object(self.jobs.leases, "snapshot", return_value={"status": "clear", "error": None, "device_scopes": scopes}),
                      mock.patch("device_registry.DeviceRegistry.read", return_value=registry)):
            patch.start()
            self.addCleanup(patch.stop)

    maxDiff = None

    def rows(self, page):
        page.wait_for_selector("#all-phones-list .phone-row")
        return page.eval_on_selector_all("#all-phones-list .phone-row", """rows => Object.fromEntries(rows.map(row => [row.dataset.ref,
            [...row.children].slice(0, 7).map(cell => cell.textContent)
              .concat([[...row.querySelectorAll('.phone-links a')].map(a => a.textContent + ' ' + a.getAttribute('href').replace(/&expected_node_id=[0-9a-f]+$/, ''))])]))""")

    def test_every_computers_phones_are_listed_with_what_can_be_done_and_links_to_do_it(self):
        self.fixture()
        center = self.cli("GET", "/nodes")[1]["local"]
        host, local = center["host"]["hostname"], center["node_id"]
        blue = BLUE + "/RM1"
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_viewport_size({"width": 1280, "height": 900})
            self.assertEqual(page.goto("http://%s:%d/dash/fleet" % self.address).status, 200)
            rows = self.rows(page)
            self.assertEqual(rows, {
                blue: ["REDMAGIC NX809JRM1", "office", "在线", "空闲", "Android 16", "root", "装包 · 推文件 · 命令 · 截图",
                       [f"远程控制 /dash/control?device={BLUE}%2FRM1", f"派作业 /dash/jobs?phone={BLUE}%2FRM1"]],
                CHENGDU + "/OP1": ["OnePlus PHK110OP1", "chengdu", "离线", "—", "Android 15", "root", "不可远程", []],
                local + "/HONOR": ["HONOR AAP-AN00HN1", host, "在线读数 5 小时前", "空闲", "Android 16", "无 root", "不可远程",
                                   [f"派作业 /dash/jobs?phone={local}%2FHONOR"]],
                local + "/HUAWEI": ["HUAWEIHD1", host, "未接入", "—", "—", "—", "不可远程", []],
            })
            self.assertEqual(page.text_content("#all-phones-count"), "4 台 · 2 台在线")
            # A phone that is not there gets neither link: nothing can be sent to it.
            # A table: every column starts at the same place on every row, whatever links a row has.
            self.assertEqual(page.eval_on_selector_all("#all-phones .phone-row", """rows => [...Array(8).keys()].map(i =>
                new Set(rows.map(row => Math.round(row.children[i].getBoundingClientRect().left))).size)"""), [1] * 8)
            self.assertEqual(sorted(page.eval_on_selector_all(".phone-row:is(.is-off, .is-gone)", "rows => rows.map(row => row.dataset.ref)")),
                             sorted([CHENGDU + "/OP1", local + "/HUAWEI"]))

            # 派作业 opens the job form on that phone.
            page.click(f'.phone-row[data-ref="{blue}"] a:has-text("派作业")')
            page.wait_for_url("**/dash/jobs**")
            page.wait_for_function(f"() => document.getElementById('submit-phone')?.value === 'ref:{blue}'")
            self.assertTrue(page.evaluate("document.getElementById('submit-panel').open"))
            self.assertEqual(page.input_value("#submit-device-scope"), "platform")
            self.assertNotIn("phone=", page.url, "the link chooses the phone once")

            # On a phone the rows wrap and nothing runs off the screen.
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto("http://%s:%d/dash/fleet" % self.address)
            self.rows(page)
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            self.assertTrue(page.is_hidden(".phone-head"))
            self.assertEqual(errors, [])


    def test_the_newest_reading_says_whether_a_phone_is_there_and_with_none_it_shows_as_usual(self):
        """A fresh reading: HONOR in it is online (no age), HUAWEI not in it is not there. With no reading at all the
        center cannot say, so both show as usual with nothing read."""
        self.fixture()
        self.jobs.fleet.snapshot.return_value = {"stale": False, "sampled_at": time.time(), "devices": [{"serial": "HN1",
            "transport": "adb", "online": True, "brand": "HONOR", "model": "AAP-AN00", "os": "Android 16 (SDK 36)", "root": False}]}
        local = self.cli("GET", "/nodes")[1]["local"]["node_id"]
        state = lambda rows, device: rows[f"{local}/{device}"][2]
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/fleet" % self.address)
            rows = self.rows(page)
            self.assertEqual((state(rows, "HONOR"), state(rows, "HUAWEI")), ("在线", "未接入"))
            self.jobs.fleet.snapshot.return_value = {"stale": True, "sampled_at": None, "devices": []}
            page.reload()
            rows = self.rows(page)
            self.assertEqual((state(rows, "HONOR"), state(rows, "HUAWEI")), ("在线暂无读数", "在线暂无读数"))


    def test_the_live_page_lists_the_other_computers_phones_that_are_there_and_opens_them_in_remote_control(self):
        """实时画面 streams this computer's phones. The other computers' phones that are there are listed after them and
        open in 远程控制, which reaches them through the center; a computer's phone that is not there is not listed."""
        self.fixture()
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://%s:%d/dash/live" % self.address)
            page.wait_for_selector("#devs-other a.dev")
            self.assertTrue(page.is_visible("#devs-other-box"))
            self.assertEqual(page.eval_on_selector_all("#devs-other a.dev", """e => e.map(a => [a.querySelector('.m').textContent,
                a.querySelector('.s').textContent, a.getAttribute('href').replace(/&expected_node_id=[0-9a-f]+$/, '')])"""),
                [["REDMAGIC NX809J", "office · RM1 · 在远程控制里看", f"/dash/control?device={BLUE}%2FRM1"]])
            self.assertEqual(errors, [])

if __name__ == "__main__":
    unittest.main()
