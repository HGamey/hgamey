"""The top of a task page: the conclusion of its delivery, its key comparison and the tasks that follow it, before
the working sections."""
import json
import unittest
from pathlib import Path

import test_core_flow_web_cli as core

REPORT = "# 收口版\n\n前言不算结论\n\n## 结论\n结论第一句\n\n- 结论第二条\n\n## 细节\n细节不在顶部"


class TaskSummaryTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, title, follows=None):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-" + rid, {"title": title, "objective": "顶部", "follows": follows}))[0], 201)

    def deliver(self, rid, summary):
        code, _, err = self.pf(core.ADMIN, "research", "deliver", rid, "--summary", summary, "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)

    def rank(self, rid):
        """agent-a asks for a ranking of four pictures on `rid`; a person answers C ›小› A ›中› D ›大› B. Returns the
        file name behind each letter."""
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        args = ["research", "compare", rid, "--title", "超分排序", "--question", "从好到差排列"]
        for name in ("native", "ours", "gsr", "bilinear"):
            (output / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            args += ["--item", f"job:{self.jid}:{name}.png"]
        code, out, err = self.pf(token, *args)
        self.assertEqual(code, 0, err)
        comparison = json.loads(out)
        assessment = {"schema": "phonefarm/compare-feedback@1", "picked": ["C", "A", "D", "B"], "unable": False,
                      "gaps": ["small", "moderate", "large"], "difference": None,
                      "client_display": {slot: "decoded" for slot in "ABCD"}, "identity_revealed": False}
        self.assertEqual(self.web("POST", "/research-state/feedback/f-top", self.write("rank-top", {
            "request_id": comparison["request_id"], "request_revision": 1, "text": "C、A、D、B", "evaluation": assessment}))[0], 201)
        return comparison

    def test_conclusion_key_comparison_and_follow_ons_lead_the_task(self):
        self.task("sr-top", "根任务")
        self.task("sr-kid", "接续一", "sr-top")
        self.task("sr-grandkid", "接续二", "sr-kid")
        self.task("sr-plain", "无结论标题")
        self.task("sr-empty", "刚建好")
        comparison = self.rank("sr-top")
        self.deliver("sr-top", REPORT)
        self.deliver("sr-kid", "接续一做完")
        self.deliver("sr-plain", "## 背景\n第一段就是结论\n\n第二段不算")
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/research?kind=research&id=sr-top")
            page.wait_for_selector("#task-summary .summary-compare .summary-chip span")
            page.wait_for_selector("#task-summary .summary-outputs h2")   # 成果 is read after the page shows
            blocks = page.eval_on_selector_all("#task-summary .summary-block h2", "hs => hs.map(h => h.textContent)")
            self.assertEqual(blocks, ["结论", "成果", "关键对比", "子任务"])
            # The summary comes before every working section (a comparison waiting for a person stays right under the owner).
            order = page.eval_on_selector_all("#task-detail > section", "ss => ss.map(s => s.id || s.className)")
            self.assertEqual(order[:2], ["research-section task-owner", "task-summary"])

            conclusion = page.text_content("#task-summary .summary-conclusion .research-report")
            self.assertIn("结论第一句", conclusion)
            self.assertIn("结论第二条", conclusion)
            self.assertNotIn("细节不在顶部", conclusion)
            self.assertNotIn("前言不算结论", conclusion)
            self.assertIn("/dash/research/report?id=sr-top", page.get_attribute("#task-summary .summary-conclusion .summary-link", "href"))
            self.assertIn("/dash/research/report?id=sr-top", page.get_attribute(".task-owner .owner-jump", "href"))

            # C ›小› A ›中› D ›大› B, each letter with the file behind it; the best one marked.
            names = {slot: Path(source.split(":")[-1]).stem for slot, source in comparison["slots"].items()}   # job:<id>:<file>
            page.wait_for_function("() => [...document.querySelectorAll('.summary-chip')].every(c => c.querySelector('span'))")
            chips = page.eval_on_selector_all(".summary-chip", "cs => cs.map(c => [c.querySelector('b').textContent, c.querySelector('span').textContent, c.classList.contains('best')])")
            self.assertEqual(chips, [[s, names[s], s == "C"] for s in "CADB"])
            self.assertEqual(page.eval_on_selector_all(".summary-gap", "gs => gs.map(g => g.textContent)"), ["›小›", "›中›", "›大›"])
            self.assertEqual(page.get_attribute(".summary-compare .summary-link", "href"), "#cmp-" + comparison["request_id"])

            # Every task that follows it, indented under the one it follows, each a link to its own page.
            tasks = page.eval_on_selector_all(".summary-task", "ts => ts.map(t => [t.querySelector('.summary-task-title').textContent, t.style.getPropertyValue('--depth'), t.querySelector('.tag').textContent, new URL(t.href).searchParams.get('id')])")
            self.assertEqual(tasks, [["接续一", "0", "已交付", "sr-kid"], ["接续二", "1", "推进中", "sr-grandkid"]])

            # Without a 结论 heading, the report's first paragraph; a task with nothing to sum up shows no summary.
            page.goto(base + "/dash/research?kind=research&id=sr-plain")
            page.wait_for_selector("#task-summary .summary-conclusion")
            self.assertEqual(page.text_content("#task-summary .research-report"), "第一段就是结论")
            page.goto(base + "/dash/research?kind=research&id=sr-empty")
            page.wait_for_selector("#task-detail .task-owner")
            page.wait_for_selector("#task-thread")
            self.assertTrue(page.is_hidden("#task-summary"))
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
