"""Automatic updates in the 设备 page's node directory: each computer's running version, how far it is behind the
center's (the recommended one) and where its update stands; an old snapshot says when it was reported."""
import time
import unittest
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import node_directory
from test_structure_page import BLUE, CHENGDU, snapshot

LAB = "d" * 32
RECOMMENDED = "c0ffee00" + "1" * 32


def updates(status, current, behind=None, error=None, paused=False):
    return {"enabled": status != "not_installed", "paused": paused, "status": status, "current_revision": current,
            "recommended_revision": RECOMMENDED, "behind": behind, "error": error, "observed_at": time.time() - 600}


# What each computer reports about its updates (the shape of K2's identity.updates).
REPORTED = {
    "office": updates("waiting", "aaaaaaaa" + "2" * 32, behind=3),
    "chengdu": {"enabled": False, "status": "not_installed", "current_revision": "bbbbbbbb" + "3" * 32},
    "lab": updates("rolled_back", "dddddddd" + "4" * 32, behind=2, error="构建失败：cargo build exit 101"),
}


class NodeUpdatesPageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request = core.CoreFlowTests.request
    cli = core.CoreFlowTests.cli

    def fixture(self):
        """office reports now and waits to update; chengdu was last heard from an hour ago and has no updater; lab
        reports now and rolled back a failed update."""
        directory = self.jobs.directory
        for name, node_id in (("office", BLUE), ("chengdu", CHENGDU), ("lab", LAB)):
            directory.join({"invite": directory.invite()["code"], "name": name, "node_id": node_id})
        directory.report(BLUE, snapshot("thinkcentre", "Linux", "aaaaaaaa", "RM1", "REDMAGIC", "NX809J", "Android 16 (SDK 36)"))
        directory.report(LAB, snapshot("lab-box", "Linux", "dddddddd", "LB1", "OnePlus", "PHK110", "Android 15 (SDK 35)"))
        with mock.patch.object(node_directory.time, "time", return_value=time.time() - 3600):
            directory.report(CHENGDU, snapshot("kamico-mac", "Darwin", "bbbbbbbb", "OP1", "OnePlus", "PHK110", "Android 15 (SDK 35)"))

    def open(self, page):
        def reported(route):
            response = route.fetch()
            listed = response.json()
            listed["local"]["updates"] = {"recommended_revision": RECOMMENDED}
            for entry in listed["items"]:
                identity = entry["snapshot"]["identity"]
                if REPORTED[entry["name"]] is None:   # a version from before automatic updates reports nothing about them
                    identity.pop("updates", None)
                else:
                    identity["updates"] = REPORTED[entry["name"]]
            route.fulfill(response=response, json=listed)
        page.route("**/dash/api/nodes", reported)
        page.route("**/dash/api/nodes?*", reported)
        page.set_viewport_size({"width": 1280, "height": 900})
        self.assertEqual(page.goto("http://%s:%d/dash/fleet" % self.address).status, 200)
        page.wait_for_selector("#node-directory .node-version .task-state")

    def test_each_computer_shows_its_version_how_far_behind_and_where_its_update_stands(self):
        self.fixture()
        local = self.cli("GET", "/nodes")[1]["local"]["service"]["revision"]
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.open(page)
            lines = page.eval_on_selector_all("#node-directory .node-choice", """cs => cs.map(c => [c.querySelector('strong').textContent,
                c.querySelector('.node-version').textContent, c.querySelector('.node-version .task-state')?.title || ''])""")
            self.assertEqual(lines[0], ["本节点", f"版本 {(local or '未知')[:8]} · 推荐版本", ""])
            rows = {name: (line, title) for name, line, title in lines[1:]}
            self.assertEqual(rows["office"], ("版本 aaaaaaaa · 落后 3 个提交 等空闲再更新", ""))
            self.assertEqual(rows["lab"], ("版本 dddddddd · 落后 2 个提交 更新失败，已回滚到原版本", "构建失败：cargo build exit 101"))
            line, _ = rows["chengdu"]
            self.assertTrue(line.startswith("版本 bbbbbbbb 未装自动更新"), line)
            self.assertNotIn("上报于", line, "without an updater there is no update report to date")

            # The selected computer's details carry its update facts.
            page.click('#node-directory .node-choice:has-text("lab")')
            page.wait_for_function("() => document.getElementById('node-info').textContent.includes('自动更新')")
            facts = dict(zip(page.eval_on_selector_all("#node-info dt", "e => e.map(x => x.textContent)"),
                             page.eval_on_selector_all("#node-info dd", "e => e.map(x => x.textContent)")))
            self.assertEqual((facts["自动更新"], facts["运行版本"], facts["推荐版本"], facts["落后"], facts["更新暂停"], facts["更新错误"]),
                             ("更新失败，已回滚到原版本", REPORTED["lab"]["current_revision"], RECOMMENDED, "2 个提交", "未暂停", "构建失败：cargo build exit 101"))
            self.assertEqual(errors, [])

    def test_an_old_report_says_when_it_was_reported_and_an_unknown_gap_is_not_zero(self):
        self.fixture()
        REPORTED_BEFORE = dict(REPORTED)
        self.addCleanup(lambda: (REPORTED.clear(), REPORTED.update(REPORTED_BEFORE)))
        REPORTED["chengdu"] = updates("paused", "bbbbbbbb" + "3" * 32, behind=5, paused=True)
        REPORTED["office"] = updates("waiting", "aaaaaaaa" + "2" * 32, behind=None)
        REPORTED["lab"] = updates("current", RECOMMENDED, behind=0)
        with self._browser_page() as page:
            self.open(page)
            rows = dict(page.eval_on_selector_all("#node-directory .node-choice", """cs => cs.slice(1).map(c => [c.querySelector('strong').textContent,
                c.querySelector('.node-version').textContent])"""))
            self.assertTrue(rows["chengdu"].startswith("版本 bbbbbbbb · 落后 5 个提交 自动更新已暂停 · 上报于 "), rows["chengdu"])
            self.assertEqual(rows["office"], "版本 aaaaaaaa · 落后多少未知 等空闲再更新")
            self.assertEqual(rows["lab"], f"版本 {RECOMMENDED[:8]} 已是推荐版本")

    def test_a_computer_that_reports_nothing_about_updates_runs_a_version_without_an_updater(self):
        self.fixture()
        REPORTED_BEFORE = dict(REPORTED)
        self.addCleanup(lambda: (REPORTED.clear(), REPORTED.update(REPORTED_BEFORE)))
        REPORTED["chengdu"] = None
        with self._browser_page() as page:
            self.open(page)
            rows = dict(page.eval_on_selector_all("#node-directory .node-choice", """cs => cs.slice(1).map(c => [c.querySelector('strong').textContent,
                c.querySelector('.node-version').textContent])"""))
            self.assertEqual(rows["chengdu"], "版本 bbbbbbbb 未装自动更新")
            page.click('#node-directory .node-choice:has-text("chengdu")')
            page.wait_for_function("() => document.getElementById('node-info').textContent.includes('自动更新')")
            facts = dict(zip(page.eval_on_selector_all("#node-info dt", "e => e.map(x => x.textContent)"),
                             page.eval_on_selector_all("#node-info dd", "e => e.map(x => x.textContent)")))
            self.assertEqual(facts["自动更新"], "未装自动更新")


if __name__ == "__main__":
    unittest.main()
