"""Technical details (job IDs, hashes, commands) are folded away by default: 更多 → 显示技术细节 shows them, on every
page of this browser until it is switched off again. 更多 lists the pages for reading results apart from the
engineers' tools. Cards on the home page say nothing they cannot fill in: no dash for an unknown reading, no internal
code for what an agent owes; each agent says who it is (its registration note) and how long it has been silent."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_media_previews as media


class TechDetailsTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)
    fixture = media.MediaPreviewTests.fixture

    def test_technical_details_are_folded_until_asked_for_and_stay_as_chosen(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        job = "#outputs .output-job"
        with self._browser_page() as page:
            page.goto(base + "/dash/research/report?id=sr-media")
            page.wait_for_selector(job, state="attached")
            self.assertTrue(page.is_hidden(job))
            self.assertIn(media.JOB, page.get_attribute(job, "href"), "folded, not removed")

            page.click(".shell-more-btn")
            self.assertEqual(page.eval_on_selector_all(".shell-menu-label", "e => e.map(x => x.textContent)")[:2], ["看结果", "工程师用"])
            self.assertEqual(page.text_content("#shell-tech"), "显示技术细节")
            page.click("#shell-tech")
            self.assertTrue(page.is_visible(job))
            self.assertEqual((page.text_content("#shell-tech"), page.get_attribute("#shell-tech", "aria-pressed")), ("隐藏技术细节", "true"))

            # Another page in the same browser opens with them shown, and switching off holds too.
            page.goto(base + "/dash/research?kind=research&id=sr-media")
            page.wait_for_selector(".summary-outputs")
            self.assertTrue(page.evaluate("document.documentElement.classList.contains('show-tech')"))
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            page.goto(base + "/dash/research/report?id=sr-media")
            page.wait_for_selector(job, state="attached")
            self.assertTrue(page.is_hidden(job))

    def test_home_cards_leave_out_unknown_readings_and_internal_codes(self):
        now = time.time()
        home = {"now": now, "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "fleet_age_s": 0,
                "elsewhere": [{"name": "mini3", "connected": True, "stale": False, "sampled_at": now,
                               "phones": [{"ref": "mini3/emu", "serial": "emu", "model": "sdk_gphone", "idle": True,
                                           "soc_c": None, "battery_c": 25, "battery_pct": 100}]}],
                "agents": [{"name": "agent-a", "checkin_at": now, "status": None, "owes": ["assigned_task", "post_status", "something_new"],
                            "research": [], "archived": None, "note": "GLM 5.3（DSH 会话），外部研究员；试用平台做研究"},
                           {"name": "agent-b", "checkin_at": now - 9 * 86400 - 60, "status": None, "owes": [], "research": [],
                            "archived": None, "note": None}]}
        with self._browser_page() as page:
            page.route("**/dash/api/home", lambda route: route.fulfill(json=home))
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('#agents li[data-agent="agent-a"] .tag')
            self.assertEqual(page.eval_on_selector_all('#agents li[data-agent="agent-a"] .tag', "e => e.map(x => x.textContent)"),
                             ["接手新任务", "报状态", "待办"])
            self.assertEqual(page.eval_on_selector_all('.phone-card[data-ref="mini3/emu"] .pstat', "e => e.map(x => x.textContent)"),
                             ["25°电池", "100%电量"])
            # Who each agent is (its registration note, first clause), and how long one has been silent.
            self.assertEqual(page.text_content('#agents li[data-agent="agent-a"] .agent-role'), "GLM 5.3（DSH 会话），外部研究员")
            self.assertEqual(page.locator('#agents li[data-agent="agent-b"] .agent-role').count(), 0)
            self.assertEqual(page.text_content('#agents li[data-agent="agent-b"] .fresh'), "9 天没签到")


if __name__ == "__main__":
    unittest.main()
