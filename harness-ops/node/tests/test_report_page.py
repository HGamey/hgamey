"""A task's delivery report on its own page: the newest version in full, earlier deliveries folded and read when
opened."""
import json
import unittest

import test_core_flow_web_cli as core

FIRST = "## 结论\n第一版结论"
FINAL = "## 结论\n终版结论\n\n| 档位 | 帧率 |\n|---|---|\n| 开 | 67 |"


class ReportPageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, title):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write("create-" + rid, {"title": title, "objective": "交付页"}))[0], 201)

    def deliver(self, rid, summary):
        code, _, err = self.pf(core.ADMIN, "research", "deliver", rid, "--summary", summary, "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)

    def test_the_newest_delivery_reads_in_full_and_earlier_ones_fold(self):
        self.task("sr-rep", "Doom 装回手机")
        self.deliver("sr-rep", FIRST)
        record = self.cli("GET", "/research-state/research/sr-rep")[1]
        source = self.tmp / "title.json"
        source.write_text(json.dumps({"title": "Doom 装回手机（终）"}))   # a revision that leaves the delivery alone
        code, _, err = self.pf(core.ADMIN, "research", "update", "research", "sr-rep", str(source), "--revision", str(record["revision"]), "--reason", "改标题")
        self.assertEqual(code, 0, err)
        self.deliver("sr-rep", FINAL)
        self.task("sr-open", "还在做")
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.assertEqual(page.goto(base + "/dash/research/report?id=sr-rep").status, 200)
            page.wait_for_selector("#report:not([hidden])")
            self.assertEqual(page.text_content("#report-title"), "Doom 装回手机（终）")
            self.assertEqual(page.get_attribute('.shell-tab[data-tab="task"]', "aria-current"), "page")
            self.assertIn("/dash/research?kind=research&id=sr-rep", page.get_attribute("#report-back", "href"))
            kicker = page.text_content("#report-kicker")
            self.assertTrue(kicker.startswith("交付报告 · 第 2 版 · "), kicker)
            self.assertIn("已交付", kicker)
            report = page.text_content("#report")
            self.assertIn("终版结论", report)
            self.assertNotIn("第一版结论", report)
            self.assertEqual(page.eval_on_selector_all("#report .research-report td", "cells => cells.map(c => c.textContent)"), ["开", "67"])
            self.assertIn("成果文件 · 1", report)
            # One earlier delivery, folded, and drawn only when opened.
            self.assertEqual(page.text_content("#report-history > summary"), "历史版本 · 1")
            self.assertIsNone(page.get_attribute("#report-history", "open"))
            version = page.locator("#report-versions .report-version")
            self.assertEqual(version.count(), 1)
            self.assertTrue(version.locator("summary").text_content().startswith("第 1 版 · "))
            self.assertEqual(version.locator(".research-report").count(), 0)
            page.click("#report-history > summary")
            version.locator("summary").click()
            version.locator(".research-report").wait_for()
            self.assertIn("第一版结论", version.text_content())

            page.goto(base + "/dash/research/report?id=sr-open")
            page.wait_for_function("() => document.getElementById('message').textContent === '这个任务还没有交付报告。'")
            self.assertTrue(page.is_hidden("#report"))
            self.assertTrue(page.is_hidden("#report-history"))
            self.assertEqual(page.text_content("#report-kicker"), "交付报告推进中")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
