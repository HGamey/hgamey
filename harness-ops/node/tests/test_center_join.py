"""接入中心: a node running on its own joins a center from its own 节点与设备 page, with the join code the center's page
made. It then restarts as that center's computer: its phones are in the center's 全部手机, it is a card on the 结构 page,
and its own pages are gone."""
import base64
import json
import time
import unittest
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import node_service  # noqa: E402
from node_connector import NodeConnector, LocalHTTP, just_joined  # noqa: E402

PHONE = {"serial": "FAKE1", "transport": "adb", "platform": "android", "state": "device", "online": True,
         "brand": "Xiaomi", "model": "23113RKC6C", "os": "Android 14 (SDK 34)", "root": False}


class CenterJoinTests(unittest.TestCase):
    def setUp(self):
        self.center, self.node = core.CoreFlowTests(), core.CoreFlowTests()
        for side in (self.center, self.node):
            side.setUp()
            self.addCleanup(side.doCleanups)
        self.restarts = mock.patch.object(node_service, "restart_later").start()
        self.addCleanup(mock.patch.stopall)
        # The node's phone answers when sampled; nothing is cached (as just after a restart).
        mock.patch.object(self.node.board.monitor, "request", create=True, side_effect=lambda *a, **k: {
            "schema_version": 1, "stale": False, "sampled_at": time.time(), "devices": [PHONE], "lock": {"status": "free"}}).start()
        mock.patch.object(self.node.board.monitor, "snapshot", return_value={
            "schema_version": 1, "stale": True, "sampled_at": None, "devices": None, "lock": {"status": "free"}}).start()
        # The center has no phones registered (the 结构 page reads its registry).
        mock.patch("device_registry.DeviceRegistry.read", return_value={
            "schema": "phonefarm/device-registry@1", "revision": 1, "updated_at": "2026-11-06T00:00:00Z", "devices": []}).start()
        self.center.jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None, "device_scopes": {
            "schema": "phonefarm/device-scopes@1", "registry_revision": 1, "unknown": False, "queue_waiting": False,
            "global": {"status": "clear", "error": None}, "devices": []}}

    def url(self, side, path):
        return "http://%s:%d%s" % (*side.address, path)

    def code(self, name="lab-c", center=None):
        invite = self.center.web("POST", "/node-invites", {"hours": 1, "note": name})[1]["code"]
        raw = json.dumps({"center": center or self.url(self.center, ""), "invite": invite, "name": name}).encode()
        return "HGJ1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def test_a_click_on_the_nodes_own_page_joins_the_center_and_its_phone_shows_there(self):
        with self.center._browser_page() as page:
            page.context.add_cookies([{"name": self.center.steer_cookie.split("=", 1)[0], "value": self.center.steer_cookie.split("=", 1)[1],
                                       "url": self.url(self.center, "/dash")}])
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # On the center: a code for a node that is already running needs only its name.
            page.goto(self.url(self.center, "/dash/fleet"))
            page.wait_for_selector("#invite-open:not([disabled])")
            page.click("#invite-open")
            page.fill("#invite-name", "lab-c")
            page.click("#invite-submit")
            page.wait_for_selector("#invite-result:not([hidden])")
            self.assertTrue(page.is_hidden("#invite-install"))
            code = page.text_content("#invite-code")

            # On the node: 接入中心 is offered to a center with no computers of its own; paste, click.
            page.goto(self.url(self.node, "/dash/fleet"))
            page.wait_for_selector("#join-open:not([hidden]):not([disabled])")
            page.click("#join-open")
            page.set_viewport_size({"width": 390, "height": 844})
            self.assertEqual(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
            page.set_viewport_size({"width": 1280, "height": 800})
            page.fill("#join-code", code)
            page.click("#join-submit")
            page.wait_for_selector("#join-message a")
            self.assertEqual(page.text_content("#join-message"),
                             f"已接入 {self.url(self.center, '')}，节点名 lab-c。这台电脑正在作为从节点重启。 到中心查看 →")
            self.assertEqual(page.get_attribute("#join-message a", "href"), self.url(self.center, "/dash/fleet"))
            self.assertTrue(page.is_disabled("#join-submit"))
            self.assertEqual(self.restarts.call_count, 1)
            joined = json.loads(Path(self.node.jobs.data_root, "_node", "center.json").read_text())
            self.assertEqual((joined["url"], joined["center_id"]), (self.url(self.center, ""), self.center.node_id))

            # The restart, as the service starts with that file: it runs as the center's computer and reports to it,
            # sampling its phones once because it has just joined (a later start does not).
            self.node.jobs.connection = joined
            sample = just_joined(self.node.jobs.data_root)
            self.assertEqual((sample, just_joined(self.node.jobs.data_root)), (True, False))
            connector = NodeConnector(joined, LocalHTTP(self.url(self.node, ""), core.ADMIN, self.node.jobs.node_id, sample=sample))
            self.addCleanup(connector.close)
            connector.start()
            for _ in range(100):
                entry = self.center.jobs.directory.get("lab-c")
                if (entry.get("snapshot") or {}).get("fleet", {}).get("devices"):
                    break
                time.sleep(0.05)
            self.assertEqual(self.node.request("GET", "/dash/fleet")[0], 404, "its own pages are gone")

            ref = f"{self.node.jobs.node_id}/FAKE1"
            page.goto(self.url(self.center, "/dash/fleet"))
            page.wait_for_selector(f'#all-phones-list .phone-row[data-ref="{ref}"]')
            self.assertEqual(page.eval_on_selector(f'.phone-row[data-ref="{ref}"]', "row => [...row.children].slice(0, 3).map(c => c.textContent)"),
                             ["Xiaomi 23113RKC6CFAKE1", "lab-c", "在线"])
            self.assertTrue(page.is_hidden("#join-open"), "a center with a computer of its own is not offered to join another")
            page.goto(self.url(self.center, "/dash/structure"))
            page.wait_for_selector(f'.st-card[data-id="n:{self.node.jobs.node_id}"]')
            page.wait_for_selector(f'.st-card[data-id="p:{ref}"]')
            self.assertEqual(errors, [])

    def test_a_join_needs_the_steer_unlock_and_a_code_the_center_accepts(self):
        good = self.code()
        self.assertEqual(self.node.web("POST", "/center-join", {"code": good}, steer=False)[0], 403)
        self.assertEqual(self.node.web("POST", "/center-join", {"code": good}, hg=False)[0], 403)
        code, reply = self.node.web("POST", "/center-join", {"code": "hgninv_abc"})
        self.assertEqual((code, reply["error"]), (400, "这不是接入码：它在中心的「节点与设备」页生成，以 HGJ1. 开头"))
        self.assertEqual(self.node.web("POST", "/center-join", {"code": good[:-6]})[0], 400)
        code, reply = self.node.web("POST", "/center-join", {"code": self.code("lab-d", center="http://127.0.0.1:1")})
        self.assertEqual(code, 502)
        self.assertTrue(reply["error"].startswith("连不上中心 http://127.0.0.1:1："), reply)
        self.assertEqual(self.restarts.call_count, 0)
        self.assertFalse(Path(self.node.jobs.data_root, "_node", "center.json").exists())
        self.assertEqual(self.node.web("POST", "/center-join", {"code": good})[0], 201)
        self.assertEqual(self.restarts.call_count, 1)
        code, reply = self.node.web("POST", "/center-join", {"code": good})
        self.assertEqual((code, reply["error"]), (400, "这个接入码已经用过了，到中心重新生成一个"))


if __name__ == "__main__":
    unittest.main()
