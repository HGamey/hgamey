"""A job a delivery report names by its ID, in the text or as code, stays as written and links to the job; hovering it
shows the job's name. A note's ID, a commit and other codes in the text stay plain text."""
from pathlib import Path
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

NAMED, BARE = "a1a1b2b2c3c3", "d4d4e5e5f6f6"
SUMMARY = f"""## 结论
画面更清楚了。

## 依据的作业
- 对比跑在 {NAMED} 上，复跑见 `{BARE}`
- 讨论见留言 n-c0ffee123456，代码在提交 0123456789abcdef0123，作业 {NAMED}0 不是作业号
"""


class ReportJobLinkTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_job_ids_in_a_report_link_to_their_jobs_and_name_them_on_hover(self):
        for jid, label in ((NAMED, "超分对比 1/4"), (BARE, None)):
            Path(self.jobs.path(jid, "out")).mkdir(parents=True)
            self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0, **({"label": label} if label else {})})
        self.assertEqual(self.web("POST", "/research-state/research/sr-jl", self.write(
            "create-sr-jl", {"title": "超分", "objective": "附录里的作业"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-jl", "--summary", SUMMARY, "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research/report?id=sr-jl" % self.address)
            links = "#report .research-report a.report-job"
            page.wait_for_function("s => [...document.querySelectorAll(s)].filter(a => a.title).length === 2", arg=links)
            self.assertEqual(page.eval_on_selector_all(links, "e => e.map(a => [a.textContent, a.title, new URL(a.href).hash])"),
                             [[NAMED, "超分对比 1/4", "#job=" + NAMED], [BARE, "custom capture", "#job=" + BARE]])
            self.assertEqual(page.locator(links + " >> nth=1 >> code").count(), 1, "the code stays code inside its link")
            text = page.text_content("#report .research-report")
            for kept in (f"对比跑在 {NAMED} 上，复跑见 {BARE}", "留言 n-c0ffee123456", "提交 0123456789abcdef0123", f"作业 {NAMED}0 不是"):
                self.assertIn(kept, text)


if __name__ == "__main__":
    unittest.main()
