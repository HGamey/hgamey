"""Changing a zone on the 结构 page: a computer's or an agent's card opens a choice of the zones there are, 未标区域 or a
new one. Only the zone: tag changes, at the revision the card was read at; the card moves to its new band at once,
whatever place it was dragged to before. Without the steer password nothing is written."""
import collections
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_structure_page as structure
from test_structure_page import CHENGDU, BLUE


class StructureZoneTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    fixture, open, boxes = structure.StructurePageTests.fixture, structure.StructurePageTests.open, structure.StructurePageTests.boxes

    def steered(self, page):
        name, value = self.steer_cookie.split("=", 1)
        page.context.add_cookies([{"name": name, "value": value, "url": "http://%s:%d" % self.address}])

    def tags(self, name):
        return self.jobs.directory.get(name)["tags"]

    def retag(self, name, tags):
        entry = self.jobs.directory.get(name)
        self.jobs.directory.update(name, {"revision": entry["revision"], "tags": tags})

    def choose(self, page, card, zone=None, new=None):
        page.click(f'.st-card[data-id="{card}"] .st-zone-btn')
        page.wait_for_selector("#zone-dialog[open]")
        if new is not None:
            page.fill("#zone-new", new)
        else:
            page.check(f'#zone-choices input[value="{zone}"]')

    def inside(self, page, card, zone):
        cards, bands = self.boxes(page, ".st-card"), self.boxes(page, "#zones rect")
        (x1, y1, x2, y2), (z1, w1, z2, w2) = cards[card], bands[zone]
        return z1 <= x1 and x2 <= z2 and w1 <= y1 and y2 <= w2

    def test_a_computers_zone_changes_from_its_card_keeping_its_other_tags_and_leaves_its_old_band(self):
        self.fixture()
        self.retag("chengdu", ["gpu", "zone:成都公网", "frp"])
        chengdu, phone = "n:" + CHENGDU, f"p:{CHENGDU}/OP1"
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.steered(page)
            self.open(page, 1280)
            # Dragged within its band first: the place is remembered for this browser.
            page.focus(f'.st-card[data-id="{chengdu}"]')
            page.keyboard.press("Shift+ArrowRight")
            stored = lambda: page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('hgamey-structure:')).flatMap(k => Object.keys(JSON.parse(localStorage[k])))")
            self.assertIn(chengdu, stored())
            self.assertTrue(self.inside(page, chengdu, "成都公网"))

            self.choose(page, chengdu, "华为蓝区")
            self.assertEqual(page.text_content("#zone-choices legend"), "现在：成都公网")
            page.click("#zone-save")
            page.wait_for_selector("#zone-dialog", state="hidden")
            self.assertEqual(self.tags("chengdu"), ["gpu", "zone:华为蓝区", "frp"], "only the zone tag changed, in its place")
            page.wait_for_function("() => !document.querySelector('#zones rect[data-zone=\"成都公网\"]')")
            self.assertTrue(self.inside(page, chengdu, "华为蓝区"))
            self.assertTrue(self.inside(page, phone, "华为蓝区"), "its phone goes with it")
            self.assertNotIn(chengdu, stored(), "the place remembered in the old band is dropped")

            # An agent to 未标区域: its zone tag goes, its other tags stay. The center to a new zone.
            self.choose(page, "a:agent-a", "未标区域")
            page.click("#zone-save")
            page.wait_for_selector("#zone-dialog", state="hidden")
            self.assertEqual(self.cli("GET", "/agents/agent-a")[1]["tags"], ["glm"])
            page.wait_for_function("() => document.querySelector('#zones rect[data-zone=\"未标区域\"]')")
            self.assertTrue(self.inside(page, "a:agent-a", "未标区域"))
            self.choose(page, "n:" + self.node_id, new="深圳办公室")
            page.click("#zone-save")
            page.wait_for_selector("#zone-dialog", state="hidden")
            self.assertEqual(self.tags("local"), ["zone:深圳办公室"])
            page.wait_for_function("() => document.querySelector('#zones rect[data-zone=\"深圳办公室\"]')")
            self.assertTrue(self.inside(page, "n:" + self.node_id, "深圳办公室"))
            self.assertEqual(errors, [])

    def test_a_change_made_elsewhere_meanwhile_is_not_overwritten(self):
        self.fixture()
        office = "n:" + BLUE
        with self._browser_page() as page:
            self.steered(page)
            self.open(page, 1280)
            self.choose(page, office, "成都公网")
            self.retag("office", ["zone:华为蓝区", "renamed-elsewhere"])   # while the dialog is open
            page.click("#zone-save")
            page.wait_for_function("() => /刚被别处改过/.test(document.getElementById('zone-message').textContent)")
            self.assertTrue(page.is_visible("#zone-dialog"))
            self.assertEqual(self.tags("office"), ["zone:华为蓝区", "renamed-elsewhere"], "nothing was overwritten")
            # Saved again after reading it: the other change stays.
            page.click("#zone-save")
            page.wait_for_selector("#zone-dialog", state="hidden")
            self.assertEqual(self.tags("office"), ["zone:成都公网", "renamed-elsewhere"])

    def test_without_the_steer_password_it_asks_to_unlock_and_nothing_is_written(self):
        self.fixture()
        office = "n:" + BLUE
        before = self.tags("office"), self.cli("GET", "/agents/agent-b")[1]["tags"]
        with self._browser_page() as page:
            self.open(page, 1280)
            self.choose(page, office, "成都公网")
            page.wait_for_function("() => /先解锁掌舵权限/.test(document.getElementById('zone-message').textContent)")
            self.assertTrue(page.is_visible("#zone-unlock"))
            self.assertTrue(page.is_disabled("#zone-save"))
            # The APIs themselves refuse without it.
            agent = self.cli("GET", "/agents/agent-b")[1]
            self.assertEqual(self.web("POST", "/nodes/office", {"revision": self.jobs.directory.get("office")["revision"], "tags": []}, steer=False)[0], 403)
            self.assertEqual(self.web("POST", "/agents/agent-b", {"revision": agent["revision"], "tags": []}, steer=False)[0], 403)
            self.assertEqual((self.tags("office"), self.cli("GET", "/agents/agent-b")[1]["tags"]), before)
            # Unlocked in the dialog, it saves. (The unlock route counts wrong passwords and sets its cookie as on plain
            # LAN HTTP; Dash.__init__ is skipped here.)
            self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
            page.fill("#zone-steer", core.STEER)
            page.click("#zone-unlock-submit")
            page.wait_for_function("() => !document.getElementById('zone-save').disabled")
            self.assertTrue(page.is_hidden("#zone-unlock"))
            page.click("#zone-save")
            page.wait_for_selector("#zone-dialog", state="hidden")
            self.assertEqual(self.tags("office"), ["zone:成都公网"])

    def test_the_dashboard_changes_an_agents_tags_with_the_steer_password_at_its_revision(self):
        self.fixture()
        agent = self.cli("GET", "/agents/agent-b")[1]
        self.assertEqual(self.web("POST", "/agents/agent-b", {"revision": agent["revision"] + 1, "tags": ["zone:成都公网"]})[0], 409)
        code, saved = self.web("POST", "/agents/agent-b", {"revision": agent["revision"], "tags": ["zone:成都公网"]})
        self.assertEqual((code, saved["tags"], saved["revision"]), (200, ["zone:成都公网"], agent["revision"] + 1))
        self.assertEqual(self.web("POST", "/agents/agent-b", {"revision": saved["revision"], "tags": []}, hg=False)[0], 400)


if __name__ == "__main__":
    unittest.main()
