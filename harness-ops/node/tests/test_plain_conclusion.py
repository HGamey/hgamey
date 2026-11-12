"""A delivery's conclusion opens with one sentence for anyone: what came of it and whether it is worth it, the numbers
in the sentences after. Agents are told so; the report page and the task page set that sentence a size up and leave
the rest of the report, numbers included, as written."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import agent_duties

LEAD = "画质增强在高分屏上和原生画质几乎看不出差别，流畅度提高到原来的 2.3 倍。"
REST = "SSIM 0.9817，PSNR 37.9 dB，比 bicubic 高 0.41 dB。"
SUMMARY = f"## 结论\n{LEAD}{REST}\n\n## 依据\n- 基线 base 10000 步，exit 0"


class PlainConclusionTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_the_first_sentence_of_the_conclusion_is_for_anyone_and_stands_out(self):
        # The rule; check-in, help and SKILL carry the same text (test_delivery_style_is_shared_by_checkin_help_and_skill).
        self.assertIn("`## 结论` 下第一句给不懂技术的人看，说清做成了什么、值不值，数字放在后面的句子里", agent_duties.DELIVERY_STYLE)

        self.assertEqual(self.web("POST", "/research-state/research/sr-sr", self.write(
            "create-sr-sr", {"title": "原神超分方向", "objective": "画质不降时提高帧率"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-sr", "--summary", SUMMARY, "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for url, block in (("/dash/research?kind=research&id=sr-sr", "#task-summary"), ("/dash/research/report?id=sr-sr", "#report")):
                page.goto(base + url)
                page.wait_for_selector(f"{block} .report-lead")
                self.assertEqual(page.eval_on_selector_all(f"{block} .report-lead", "e => e.map(p => p.textContent)"), [LEAD], url)
                size = lambda selector: page.eval_on_selector(selector, "e => parseFloat(getComputedStyle(e).fontSize)")   # noqa: E731
                self.assertGreater(size(f"{block} .report-lead"), size(f"{block} .report-lead + p"), url)
                # The numbers stay where the agent put them: right after, shown, not folded away.
                self.assertEqual(page.text_content(f"{block} .report-lead + p"), REST, url)
                self.assertTrue(page.is_visible(f"{block} .report-lead + p"), url)
            self.assertTrue(page.is_visible("#report li:has-text('base 10000 步')"))   # the report's other sections as written
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
