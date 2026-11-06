"""The 结构 page: the center, the computers that joined it, their phones and the agents, drawn by network zone from the
same APIs the other pages read. Cards and zones can be moved; only what is running is marked as running."""
import time
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import node_directory

BLUE, CHENGDU = "b" * 32, "c" * 32


def snapshot(hostname, platform, revision, serial, brand, model, os_name):
    return {"identity": {"host": {"hostname": hostname, "platform": platform}, "service": {"revision": revision}},
            "fleet": {"stale": False, "sampled_at": time.time(),
                      "devices": [{"serial": serial, "transport": "adb", "platform": "android", "state": "device", "online": True,
                                   "brand": brand, "model": model, "os": os_name, "root": True}]},
            "queue": {"items": []}}


class StructurePageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request = core.CoreFlowTests.request
    cli = core.CoreFlowTests.cli

    def fixture(self):
        """The center (no zone) with three registered phones: HONOR read five hours ago, HUAWEI never read, and a
        registration the center has disabled (a phone that moved to another computer). A blue-zone computer that reports
        now; a Chengdu computer last heard from an hour ago, whose own last snapshot still says its phone is online. One
        agent in the blue zone and one on the public network (no city). agent-a's job runs on the blue computer's phone
        (the page is shown that job as running)."""
        directory = self.jobs.directory
        directory.join({"invite": directory.invite()["code"], "name": "office", "node_id": BLUE, "tags": ["zone:华为蓝区"]})
        directory.report(BLUE, snapshot("thinkcentre", "Linux", "95f55e5c72ae", "RM1", "REDMAGIC", "NX809J", "Android 16 (SDK 36)"))
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
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a", "tags": ["glm", "zone:华为蓝区"]})[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-b", "tags": ["zone:公网"]})[0], 201)

    def open(self, page, width):
        def running(route):   # the fixture's job by agent-a, shown as running on the blue computer's phone
            response = route.fetch()
            listed = response.json()
            for job in listed["items"]:
                if job["id"] == self.jid:
                    job.update(state="running", phase="running", execution_node_id=BLUE, queue={"state": "claimed"},
                               device_binding={"schema": "phonefarm/device-binding@1", "registry_revision": 1, "device_id": "RM1", "serial": "RM1"})
            route.fulfill(response=response, json=listed)
        page.route("**/dash/api/jobs?*", running)
        page.set_viewport_size({"width": width, "height": 900})
        self.assertEqual(page.goto("http://%s:%d/dash/structure" % self.address).status, 200)
        page.wait_for_selector(".st-card")

    def boxes(self, page, selector):
        return page.eval_on_selector_all(selector, """items => Object.fromEntries(items.map(item => {
            const r = item.getBoundingClientRect();
            return [item.dataset.id || item.dataset.zone || item.getAttribute('class'), [r.left, r.top, r.right, r.bottom]];
        }))""")

    def test_cards_sit_in_their_zones_and_only_running_things_are_marked(self):
        self.fixture()
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.open(page, 1280)
            self.assertEqual(page.get_attribute('.shell-tab[data-tab="structure"]', "aria-current"), "page")
            self.assertEqual(page.text_content("#facts"), "3 个节点 · 4 台手机 · 2 个 Agent · 3 个网络区域 · 1 个作业在运行")
            # The center's zone comes first (here the one without a tag); 公网 is a zone of its own, not a city's.
            labels = page.eval_on_selector_all(".st-zone-label", "items => items.map(item => item.textContent)")
            self.assertEqual(labels, ["未标区域1 个节点 · 2 台手机", "成都公网1 个节点 · 1 台手机", "公网1 个 Agent",
                                      "华为蓝区1 个节点 · 1 台手机 · 1 个 Agent"])
            center = "n:" + self.node_id
            # The disabled registration is not drawn.
            members = {"未标区域": [center, f"p:{self.node_id}/HONOR", f"p:{self.node_id}/HUAWEI"], "成都公网": ["n:" + CHENGDU, f"p:{CHENGDU}/OP1"],
                       "公网": ["a:agent-b"], "华为蓝区": ["n:" + BLUE, f"p:{BLUE}/RM1", "a:agent-a"]}
            cards, zones = self.boxes(page, ".st-card"), self.boxes(page, ".st-zone")
            self.assertEqual(sorted(cards), sorted(sum(members.values(), [])))
            for name, ids in members.items():
                zone = zones[name]
                for card in ids:
                    x1, y1, x2, y2 = cards[card]
                    self.assertTrue(zone[0] < x1 and zone[1] < y1 and x2 < zone[2] and y2 < zone[3], (name, card, cards[card], zone))
            self.assertFalse(any(self.overlap(a, b) for a in cards.values() for b in cards.values() if a is not b))
            # Zones never overlap; a zone of agents alone takes the free agent column beside the center's zone.
            self.assertFalse(any(self.overlap(a, b) for a in zones.values() for b in zones.values() if a is not b))
            self.assertEqual(zones["公网"][1], zones["未标区域"][1])

            classes = dict(page.eval_on_selector_all(".st-card", "cs => cs.map(c => [c.dataset.id, c.className])"))
            running = {card for card, name in classes.items() if "is-running" in name}
            self.assertEqual(running, {"a:agent-a", "n:" + BLUE, f"p:{BLUE}/RM1"})
            self.assertEqual({card for card, name in classes.items() if "is-off" in name}, {"n:" + CHENGDU, f"p:{CHENGDU}/OP1"})
            self.assertEqual(page.eval_on_selector_all(".st-link.is-running", "ls => ls.length"), 3)
            self.assertEqual(page.eval_on_selector_all(".st-packet", "ps => ps.length"), 3)
            self.assertEqual(page.eval_on_selector_all(".st-link.is-off", "ls => ls.length"), 2)
            self.assertIn("从节点", page.text_content(f'[data-id="n:{BLUE}"]'))
            self.assertIn("thinkcentre", page.text_content(f'[data-id="n:{BLUE}"]'))
            self.assertIn("离线 · 最后在线", page.text_content(f'[data-id="n:{CHENGDU}"]'))
            self.assertIn("主节点", page.text_content(f'[data-id="{center}"]'))
            self.assertEqual(page.text_content(f'[data-id="p:{BLUE}/RM1"] .st-line'), "Android 16 · root · adb")
            self.assertEqual(page.text_content('[data-id="a:agent-a"] .st-line'), "glm · 运行 1 个作业")
            # Online is what the center sees. A computer whose link is down has offline phones, whatever its own last
            # snapshot said; a phone on a linked computer with an old reading is shown as usual, with the reading's age.
            note = lambda card: page.eval_on_selector(f'[data-id="{card}"]', "c => c.querySelector('.st-note')?.textContent || ''")
            self.assertEqual(note(f"p:{CHENGDU}/OP1"), "离线")
            self.assertEqual(note(f"p:{self.node_id}/HONOR"), "读数 5 小时前")
            self.assertEqual(page.text_content(f'[data-id="p:{self.node_id}/HONOR"] .st-line'), "Android 16 · 无 root · adb")
            self.assertEqual(note(f"p:{self.node_id}/HUAWEI"), "")
            self.assertEqual(page.text_content(f'[data-id="p:{self.node_id}/HUAWEI"] .st-line'), "暂无读数")
            self.assertEqual(note(f"p:{BLUE}/RM1"), "")
            self.assertEqual(errors, [])

    def test_a_moved_card_or_zone_stays_where_it_was_put_until_relaid(self):
        self.fixture()
        with self._browser_page() as page:
            self.open(page, 1280)
            card = f'[data-id="n:{BLUE}"]'
            trunk = "#links-busy path.is-trunk"
            before, path = self.boxes(page, ".st-card")["n:" + BLUE], page.get_attribute(trunk, "d")
            self.assertTrue(page.is_disabled("#relayout"))
            page.mouse.move(before[0] + 40, before[1] + 20)
            page.mouse.down()
            page.mouse.move(before[0] + 100, before[1] + 60, steps=4)
            page.mouse.move(before[0] + 160, before[1] + 100, steps=4)
            page.mouse.up()
            after = self.boxes(page, ".st-card")["n:" + BLUE]
            self.assertAlmostEqual(after[0] - before[0], 120, delta=1.5)
            self.assertAlmostEqual(after[1] - before[1], 80, delta=1.5)
            self.assertNotEqual(page.get_attribute(trunk, "d"), path)
            zone = self.boxes(page, ".st-zone")["华为蓝区"]
            self.assertTrue(zone[0] < after[0] and after[2] < zone[2] and after[3] < zone[3])   # the zone follows its card
            self.assertTrue(page.is_enabled("#relayout"))

            page.reload()
            page.wait_for_selector(card)
            kept = self.boxes(page, ".st-card")["n:" + BLUE]
            self.assertEqual([round(v) for v in kept], [round(v) for v in after])

            # A focused card moves with the arrow keys; Shift moves it further.
            page.focus(card)
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Shift+ArrowUp")
            scale = page.evaluate("() => document.getElementById('world').getBoundingClientRect().width / document.getElementById('world').offsetWidth")
            keyed = self.boxes(page, ".st-card")["n:" + BLUE]
            self.assertAlmostEqual(keyed[0] - kept[0], -8 * scale, delta=1)
            self.assertAlmostEqual(keyed[1] - kept[1], -40 * scale, delta=1)

            # Dragging a zone's label moves everything in it together.
            label = self.boxes(page, ".st-zone-label")["成都公网"]
            members = [f"n:{CHENGDU}", f"p:{CHENGDU}/OP1"]
            start = self.boxes(page, ".st-card")
            page.mouse.move(label[0] + 10, label[1] + 8)
            page.mouse.down()
            page.mouse.move(label[0] - 20, label[1] + 38, steps=5)
            page.mouse.up()
            moved = self.boxes(page, ".st-card")
            for member in members:
                self.assertAlmostEqual(moved[member][0] - start[member][0], -30, delta=1.5)
                self.assertAlmostEqual(moved[member][1] - start[member][1], 30, delta=1.5)
            self.assertEqual(moved["a:agent-b"], start["a:agent-b"])

            page.click("#relayout")
            self.assertEqual([round(v) for v in self.boxes(page, ".st-card")["n:" + BLUE]], [round(v) for v in before])
            self.assertTrue(page.is_disabled("#relayout"))
            page.reload()
            page.wait_for_selector(card)
            self.assertEqual([round(v) for v in self.boxes(page, ".st-card")["n:" + BLUE]], [round(v) for v in before])

    def test_on_a_phone_every_card_fits_the_screen_without_overlap(self):
        """390 wide, and 250 (narrower than the narrow layout itself, which then scales down)."""
        self.fixture()
        with self._browser_page() as page:
            errors = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            for width in (390, 250):
                self.open(page, width)
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"), 0)
                stage = page.eval_on_selector("#stage", "s => { const b = s.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; }")
                cards = self.boxes(page, ".st-card")
                for card, (x1, y1, x2, y2) in cards.items():
                    self.assertTrue(stage[0] < x1 and x2 < stage[2] and stage[1] < y1 and y2 < stage[3], (width, card, stage))
                self.assertFalse(any(self.overlap(a, b) for a in cards.values() for b in cards.values() if a is not b))
                # Zones are stacked, each holding its own cards: top to bottom, the order of the zone labels.
                tops = [cards[card][1] for card in ("n:" + self.node_id, "n:" + CHENGDU, "n:" + BLUE)]
                self.assertEqual(tops, sorted(tops))
                page.unroute("**/dash/api/jobs?*")
            # A window resized to nothing (as a full-page screenshot does for a moment) draws nothing invalid.
            page.set_viewport_size({"width": 1, "height": 900})
            page.wait_for_timeout(200)
            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(200)
            self.assertEqual(errors, [])

    @staticmethod
    def overlap(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


if __name__ == "__main__":
    unittest.main()
