"""The datasets table fits a desktop screen: how the data was captured is a technical detail, and the game column reads
as the game's name where the center has it registered, its package and version folded. On a phone, where the table
is wider than the screen, the edge that has more columns is shaded."""
import re
import unittest

import test_core_flow_web_cli as core

GAME = {"game_id": "genshin", "title": "原神 / Genshin Impact", "revision": 1,
        "manifest": {"title": "原神 / Genshin Impact", "application": {"package": "com.miHoYo.Yuanshen"}}}
METHOD = "RenderDoc Vulkan捕获与同设备GPU同帧几何重栅格；固定像素映射与深度掩码；逐帧对齐后按场景切分，丢掉运动模糊过大的帧"


class DatasetTableFitsTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf

    def register(self, rid, labels):
        dataset = {"name": rid, "version": "20261014-history-20261106-import", "pairing": "simulated", "capture_method": METHOD,
                   "frames": 12000, "scenes": 17, "splits": {"train": 9600, "val": 1200, "test": 1200}, "size_bytes": 1 << 31, "location": None}
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid, "title": rid, "labels": labels,
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [], "dataset": dataset})[0], 201)

    def test_the_table_fits_and_names_the_game(self):
        self.register("ds-gi", {"game_id": "com.miHoYo.Yuanshen", "game_version": "7.1.0_48052158_48145775",
                                "device": "REDMAGIC NX809J / 91253241019A / Android 16 / root"})
        self.register("ds-other", {"game_id": "com.example.unknown", "game_version": "2.0"})
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json={"schema_version": 1, "items": [GAME]}))
            page.set_viewport_size({"width": 1400, "height": 900})
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector('tr[data-result="ds-other"]')
            self.assertEqual(page.evaluate("() => { const s = document.getElementById('datasets'); return s.scrollWidth - s.clientWidth; }"), 0,
                             "every column is in view")
            self.assertTrue(page.is_hidden("thead th.ds-method"))
            self.assertEqual(page.inner_text('tr[data-result="ds-gi"] td:nth-child(3)'), "原神")
            self.assertEqual(page.text_content('tr[data-result="ds-gi"] td:nth-child(3)'), "原神 com.miHoYo.Yuanshen 7.1.0_48052158_48145775")
            self.assertEqual(page.inner_text('tr[data-result="ds-other"] td:nth-child(3)'), "com.example.unknown")
            self.assertEqual(page.text_content('tr[data-result="ds-gi"] td:nth-child(5)'), METHOD, "folded, not removed")

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("() => innerWidth === 390")
            self.assertGreater(page.evaluate("() => { const s = document.getElementById('datasets'); return s.scrollWidth - s.clientWidth; }"), 0)
            shade = page.evaluate("() => { const s = getComputedStyle(document.getElementById('datasets')); return [s.backgroundImage, s.backgroundAttachment]; }")
            self.assertEqual(shade[0].count("linear-gradient"), 4)
            self.assertTrue(shade[1].startswith("local, local, scroll, scroll"), shade[1])

    def test_a_long_device_wraps_so_the_table_fits_a_laptop_screen(self):
        # As recorded on the center: a device label of words, not a model the phones know.
        self.register("ds-history", {"game_id": "com.unity.megacity.metro", "game_version": "1.0",
                                     "device": "Historical rooted OnePlus / Adreno750 / 16GB"})
        self.register("ds-import", {"game_id": "doom3-demo", "device": "Historical OnePlus PKG110; import on kamico Mac"})
        with self._browser_page() as page:
            page.route(re.compile(r".*/dash/api/games(\?.*)?$"), lambda route: route.fulfill(json={"schema_version": 1, "items": [GAME]}))
            page.set_viewport_size({"width": 1200, "height": 900})
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector('tr[data-result="ds-import"]')
            for width in (1200, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_function(f"() => innerWidth === {width}")
                self.assertEqual(page.evaluate("() => { const s = document.getElementById('datasets'); return s.scrollWidth - s.clientWidth; }"), 0, width)
            self.assertEqual(page.inner_text('tr[data-result="ds-history"] td:nth-child(4)').replace("\n", " "),
                             "Historical rooted OnePlus / Adreno750 / 16GB")
            page.set_viewport_size({"width": 1600, "height": 900})
            page.wait_for_function("() => innerWidth === 1600")
            cell = page.locator('tr[data-result="ds-history"] td:nth-child(4)')
            self.assertLess(cell.bounding_box()["height"], 2 * 22, "on one line where there is room")


if __name__ == "__main__":
    unittest.main()
