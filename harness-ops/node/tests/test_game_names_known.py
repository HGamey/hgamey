"""A common game the center has not registered under 游戏与资源 still reads by the name people call it (原神,
Megacity…), as the GOL page already did: in the 数据集 table and its line about a dataset, and as the app in front of a
phone on the 设备 page. A registered game's own title comes first; an unknown package still shows as itself."""
import re
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_fleet_app_names import device

NO_GAMES = {"schema_version": 1, "items": []}


class KnownGameNameTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf

    def register(self, rid, game_id, pairing, frames):
        dataset = {"name": rid, "version": "20261112", "pairing": pairing, "capture_method": "screencap", "frames": frames,
                   "scenes": None, "splits": None, "size_bytes": None, "location": None}
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid, "title": rid,
            "labels": {"game_id": game_id, "game_version": "1.0"}, "origin": {"evidence_origin": "synthetic"},
            "limitations": ["fixture"], "sources": [], "dataset": dataset})[0], 201)

    def test_datasets_name_a_common_game_outside_the_catalogue(self):
        self.register("mc", "com.unity.megacity.metro", "real", 773)
        self.register("gi", "com.miHoYo.Yuanshen", "simulated", 12000)
        self.register("lab", "com.example.lab", "simulated", 10)
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json=NO_GAMES))
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector('tr[data-result="lab"]')
            cell = 'tr[data-result="%s"] td:nth-child(3)'
            self.assertEqual([page.inner_text(cell % rid) for rid in ("mc", "gi", "lab")], ["Megacity", "原神", "com.example.lab"])
            self.assertEqual(page.text_content(cell % "mc"), "Megacity com.unity.megacity.metro 1.0", "the package is folded")
            about = page.eval_on_selector_all(".ds-name th", "e => Object.fromEntries(e.map(th => [th.firstChild.textContent, th.querySelector('.ds-purpose')?.textContent]))")
            self.assertEqual(about, {"mc": "Megacity画面，真实配对，773 帧", "gi": "原神画面，模拟低清配对，1.2 万帧", "lab": "模拟低清配对，10 帧"})

    def test_the_device_page_names_a_common_game_in_front(self):
        devices = [device("A", "NX809J", "com.miHoYo.Yuanshen/com.miHoYo.GetMobileInfo.MainActivity"), device("B", "PLC110", "com.example.lab")]
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/fleet(\?.*)?$"), lambda route: route.fulfill(json={
                "schema_version": 1, "lock": None, "devices": devices, "sampled_at": time.time(), "age_s": 0, "stale": False}))
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json=NO_GAMES))
            page.goto("http://%s:%d/dash/fleet" % self.address)
            page.wait_for_function("() => document.querySelectorAll('#devices .device-card').length === 2")
            read = """cards => cards.map(c => { const dt = [...c.querySelectorAll('dt')].find(d => d.textContent === '前台应用');
                return dt.nextElementSibling.innerText.trim(); })"""
            page.wait_for_function(f"() => ({read})([...document.querySelectorAll('#devices .device-card')])[0] !== ''")
            self.assertEqual(page.eval_on_selector_all("#devices .device-card", read), ["原神", "com.example.lab"])


if __name__ == "__main__":
    unittest.main()
