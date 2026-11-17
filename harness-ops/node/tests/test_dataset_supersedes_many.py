"""A dataset can replace several older ones: its manifest's supersedes names one result ID or a list, and replacements
found after registration are added with `results supersede`, which changes nothing else about either result. On the
数据集 page a superseded version is grey, and a dataset whose every version was replaced is folded at the bottom under
the name of what replaced it."""
import json
import sqlite3
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from node_results import NodeResults


class DatasetSupersedesManyTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf

    def manifest(self, rid, name, supersedes=None):
        body = {"schema": "phonefarm/result@1", "result_id": rid, "title": rid, "origin": {"evidence_origin": "device"},
                "limitations": ["fixture"], "labels": {"game_id": "genshin"}, "sources": [],
                "dataset": {"name": name, "version": rid[-8:], "pairing": "real", "capture_method": "framedump",
                            "frames": 240, "scenes": 1, "splits": None, "size_bytes": 1000, "location": None}}
        if supersedes is not None:
            body["supersedes"] = supersedes
        return body

    def register(self, rid, name, supersedes=None):
        code, value = self.cli("POST", "/results", self.manifest(rid, name, supersedes))
        self.assertEqual(code, 201, value)

    def listed(self):
        return {item["result_id"]: item for item in self.cli("GET", "/results?datasets=1")[1]["items"]}

    def test_a_dataset_replaces_several_and_more_are_added_after_registration(self):
        for place in ("inland", "sea", "beach"):
            self.register(f"gi-seq-{place}-20261113", f"gi-seq-{place}")
        self.register("gi-route-20261117", "gi-route", ["gi-seq-inland-20261113", "gi-seq-sea-20261113"])
        items = self.listed()
        self.assertEqual(items["gi-route-20261117"]["supersedes"], ["gi-seq-inland-20261113", "gi-seq-sea-20261113"])
        self.assertEqual([items[f"gi-seq-{p}-20261113"]["superseded_by"] for p in ("inland", "sea", "beach")],
                         ["gi-route-20261117", "gi-route-20261117", None])

        before = self.cli("GET", "/results/gi-route-20261117")[1]
        code, out, err = self.pf(core.ADMIN, "results", "supersede", "gi-route-20261117",
                                 "gi-seq-beach-20261113", "gi-seq-sea-20261113")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["supersedes"],
                         ["gi-seq-inland-20261113", "gi-seq-sea-20261113", "gi-seq-beach-20261113"])
        after = self.cli("GET", "/results/gi-route-20261117")[1]
        self.assertEqual(after["manifest"], before["manifest"], "the registered manifest is not rewritten")
        self.assertEqual(after["supersedes"], ["gi-seq-inland-20261113", "gi-seq-sea-20261113", "gi-seq-beach-20261113"])
        self.assertEqual(self.cli("GET", "/results/gi-seq-beach-20261113")[1]["superseded_by"], ["gi-route-20261117"])
        self.assertEqual(self.listed()["gi-seq-beach-20261113"]["superseded_by"], "gi-route-20261117")
        # Registering the same manifest again is still the same registration.
        code, value = self.cli("POST", "/results", self.manifest("gi-route-20261117", "gi-route",
                                                                 ["gi-seq-inland-20261113", "gi-seq-sea-20261113"]))
        self.assertEqual(code, 201, value)

        code, _, err = self.pf(core.ADMIN, "results", "supersede", "gi-route-20261117", "gi-seq-lake-20261113")
        self.assertEqual(code, 2)
        self.assertIn("gi-seq-lake-20261113", err)
        code, _, err = self.pf(core.ADMIN, "results", "supersede", "gi-route-20261117", "gi-route-20261117")
        self.assertEqual(code, 2)
        self.assertIn("other than this one", err)
        code, value = self.cli("POST", "/results/gi-route-20261117/supersedes", {"supersedes": ["gi-seq-sea-20261113"],
                                                                                "title": "改名"})
        self.assertEqual(code, 400, value)
        code, value = self.cli("POST", "/results", self.manifest("gi-dup", "gi-dup", ["gi-seq-sea-20261113"] * 2))
        self.assertEqual(code, 400, value)

    def test_replacements_named_by_earlier_manifests_are_kept(self):
        self.register("old-version", "gi-old")
        self.register("new-version", "gi-new", "old-version")
        with sqlite3.connect(self.jobs.results.path) as db:   # an index from before replacements were kept apart
            db.execute("DROP TABLE supersessions")
        NodeResults(self.jobs, self.jobs.node_id)
        self.assertEqual(self.listed()["old-version"]["superseded_by"], "new-version")
        self.assertEqual(self.cli("GET", "/results/new-version")[1]["supersedes"], ["old-version"])

    def test_the_datasets_page_folds_what_was_replaced(self):
        self.register("gi-seq-inland-20261113", "gi-seq-inland")
        self.register("gi-seq-sea-20261113", "gi-seq-sea")
        self.register("genshin-sr-20261101", "genshin-sr")
        self.register("genshin-sr-20261110", "genshin-sr", "genshin-sr-20261101")
        self.register("gi-route-20261117", "gi-route", ["gi-seq-inland-20261113", "gi-seq-sea-20261113"])
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/datasets" % self.address)
            page.wait_for_selector(".ds-version")
            names = page.eval_on_selector_all(".ds-name th", "e => e.map(t => t.firstChild.textContent)")
            self.assertEqual(names, ["gi-route", "genshin-sr", "gi-seq-sea", "gi-seq-inland"])
            replaced = page.locator(".ds-replaced")
            self.assertEqual(replaced.count(), 2)
            self.assertEqual(page.text_content(".ds-replaced .ds-replaced-by"), "已被 gi-route 取代")
            self.assertTrue(page.is_hidden('tr[data-result="gi-seq-sea-20261113"]'))
            self.assertEqual(page.locator(".ds-replaced .ds-samples").count(), 0)
            page.click('.ds-fold:has-text("gi-seq-sea")')
            self.assertTrue(page.is_visible('tr[data-result="gi-seq-sea-20261113"]'))
            self.assertEqual(page.get_attribute('.ds-fold:has-text("gi-seq-sea")', "aria-expanded"), "true")
            self.assertTrue(page.is_hidden('tr[data-result="gi-seq-inland-20261113"]'))
            self.assertIn("ds-superseded", page.get_attribute('tr[data-result="genshin-sr-20261101"]', "class"))
            self.assertNotIn("ds-superseded", page.get_attribute('tr[data-result="genshin-sr-20261110"]', "class"))
            self.assertEqual(page.evaluate("getComputedStyle(document.querySelector('tr[data-result=\"genshin-sr-20261101\"]')).opacity"),
                             "0.55")
            page.click("#refresh")
            page.wait_for_selector('.ds-fold[aria-expanded="true"]')
            self.assertTrue(page.is_visible('tr[data-result="gi-seq-sea-20261113"]'), "an unfolded dataset stays open")


if __name__ == "__main__":
    unittest.main()
