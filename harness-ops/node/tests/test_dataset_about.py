"""A dataset that does not say what it is for gets a line from what it records: the game's name, how its pairs were
made and how many frames, e.g. 原神画面，模拟低清配对，1.2 万帧. One that says what it is for keeps its own words."""
import re
import unittest

import test_core_flow_web_cli as core
from test_dataset_table_fits import GAME


class DatasetAboutTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf

    def register(self, rid, game_id, pairing, frames, purpose=None):
        dataset = {"name": rid, "version": "20261112", "pairing": pairing, "capture_method": "screencap", "frames": frames,
                   "scenes": None, "splits": None, "size_bytes": None, "location": None, **({"purpose": purpose} if purpose else {})}
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid, "title": rid,
            "labels": {"game_id": game_id}, "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [],
            "dataset": dataset})[0], 201)

    def test_a_dataset_without_a_purpose_is_described_from_its_fields(self):
        self.register("gi-sim", "com.miHoYo.Yuanshen", "simulated", 12000)
        self.register("mc-sim", "com.unity.megacity.metro", "real", 773)
        self.register("doom", "com.karin.idTech4Amm", "mixed", None)
        self.register("told", "com.miHoYo.Yuanshen", "simulated", 500, purpose="红魔上录的原神画面，用来训练超分")
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json={"schema_version": 1, "items": [GAME]}))
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector('tr[data-result="told"]')
            about = page.eval_on_selector_all(".ds-name th", "e => Object.fromEntries(e.map(th => [th.firstChild.textContent, th.querySelector('.ds-purpose')?.textContent]))")
            self.assertEqual(about, {"gi-sim": "原神画面，模拟低清配对，1.2 万帧", "mc-sim": "真实配对，773 帧",
                                     "doom": "混合配对（含模拟）", "told": "红魔上录的原神画面，用来训练超分"})


if __name__ == "__main__":
    unittest.main()
