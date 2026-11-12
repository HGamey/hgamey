"""Commands in a delivery report are technical details: each block of them is folded under 复现命令 until opened, and
显示技术细节 opens them all. A code block that is not commands (a table of numbers) reads as written."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

SUMMARY = """## 结论
画面更清楚了，流畅度提高到 2.3 倍。

```sh
pf-node research show sr-cmd
```

## 复现
```sh
P=$(cat plan.txt)
```

```
档位  帧率
开    67
```

```
pf-node --experiment 4f2a9c1 experiment selfsr/run.py --steps 10000
# 出图
sh selfsr/pre_figs/make_figs.sh out/
```
"""


class ReportCommandTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def test_commands_fold_under_a_button_and_numbers_stay(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-cmd", self.write(
            "create-sr-cmd", {"title": "超分", "objective": "复现"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-cmd", "--summary", SUMMARY, "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        url = "http://%s:%d/dash/research/report?id=sr-cmd" % self.address
        with self._browser_page() as page:
            page.goto(url)
            page.wait_for_selector("#report .research-report")
            blocks = "#report .research-report .report-code"
            self.assertEqual(page.locator(blocks).count(), 3)
            self.assertEqual(page.locator(f"{blocks} > pre:visible").count(), 0)
            self.assertEqual(page.eval_on_selector_all(f"{blocks} .report-code-toggle", "e => e.map(b => b.textContent)"), ["复现命令"] * 3)
            self.assertEqual(page.eval_on_selector_all(f"{blocks} .report-code-toggle", "e => e.map(b => b.title)"), ["给要复现的工程师看"] * 3)
            # The table of numbers in a code block is not a command: shown as written.
            self.assertTrue(page.is_visible("#report .research-report > pre:has-text('开    67')"))
            page.locator(f"{blocks} .report-code-toggle").nth(2).click()
            self.assertIn("make_figs.sh", page.locator(f"{blocks} > pre:visible").text_content())
            self.assertEqual(page.get_attribute(f"{blocks} >> nth=2 >> .report-code-toggle", "aria-expanded"), "true")
            # 显示技术细节 shows every command, and the buttons go.
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.locator(f"{blocks} > pre:visible").count(), 3)
            self.assertEqual(page.locator(f"{blocks} .report-code-toggle:visible").count(), 0)
            # The task page's 结论 folds its command the same way.
            page.click("#shell-tech")
            self.assertEqual(page.locator(f"{blocks} > pre:visible").count(), 1)
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-cmd" % self.address)
            page.wait_for_selector(".summary-conclusion .report-code")
            self.assertTrue(page.is_visible(".summary-conclusion .report-code-toggle"))
            self.assertTrue(page.locator(".summary-conclusion .report-code > pre").is_hidden())


if __name__ == "__main__":
    unittest.main()
