"""The results page reads a joined computer's result details and originals through this center."""
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlparse

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
import test_node_result_search as federation_fixture

REPORT = '{"timing": {"gpu_ms": 2.413}}\n'


class ResultsPageRemoteTests(unittest.TestCase):
    # Borrow fixture helpers without inheriting or rediscovering its tests.
    setUp = federation_fixture.NodeResultSearchTests.setUp
    cleanup = federation_fixture.NodeResultSearchTests.cleanup
    node = federation_fixture.NodeResultSearchTests.node

    def publish(self, env, rid):
        jid = ("a" if env.label == "alpha" else "b") * 12
        out = Path(env.jobs.path(jid)) / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(REPORT, encoding="utf-8")
        env.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["fixture"], "label": "fixture", "started": 1, "exit": 0})
        return env.jobs.results.register({
            "schema": "phonefarm/result@1", "result_id": rid, "title": env.label + " " + rid, "job_id": jid,
            "labels": {"game_id": "fixture-game", "device": env.label}, "outcome": "unknown",
            "origin": {"evidence_origin": "synthetic"},
            "sources": [{"name": "report", "kind": "job_output", "job_id": jid, "path": "report.json"}],
            "metrics": [{"name": "gpu_render", "condition": "candidate", "value": 2.413, "unit": "ms", "scope": "module",
                         "method": "reported", "source": {"name": "report", "pointer": "/timing/gpu_ms"}}]})

    def test_remote_detail_and_original_open_through_this_center(self):
        """Searching a joined computer, its detail opens on this page and its original links to
        /dash/api/nodes/<name>/read/…, which serves the bytes. Nothing links to the source's own address."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        hub, alpha = self.hub, self.alpha
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_cookies([{"name": dash.COOKIE, "value": hub.board._cookie_val("test-view"), "url": hub.url}])
                page = context.new_page()
                page.goto(hub.url + "/dash/results")
                page.click("#federation-panel summary")
                page.wait_for_selector('#federation-targets input[value="alpha"]:not([disabled])')
                self.assertIn("已连接", page.text_content("#federation-targets"))
                page.check('#federation-targets input[value="alpha"]')
                page.click("#federation-search")
                item = f'.federation-group[data-node="{alpha.jobs.games.node_id}"] .federation-items li'
                page.wait_for_selector(item)
                page.locator(item).filter(has_text="alpha same-result").get_by_role("button", name="读取指标详情").click()
                page.wait_for_selector("#remote-result-content .source-list a")
                href = page.get_attribute("#remote-result-content .source-list a", "href")
                link = urlparse(href)
                self.assertEqual(link.netloc, "")
                self.assertTrue(link.path.startswith("/dash/api/nodes/alpha/read/results/same-result/sources/report"), href)
                original = page.request.get(hub.url + href)
                self.assertEqual((original.status, original.text()), (200, REPORT))
                hrefs = page.eval_on_selector_all("#federation-results a, #remote-result-detail a", "e => e.map(a => a.href)")
                self.assertTrue(all(h.startswith(hub.url + "/") for h in hrefs), hrefs)
                self.assertNotIn(alpha.url, json.dumps(hrefs))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
