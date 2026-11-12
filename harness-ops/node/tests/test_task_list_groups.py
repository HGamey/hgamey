"""The task list is in the home page's groups: 进行中, 已交付, 已关闭, each with its count and most recently active
first. Other kinds of records stay one list."""
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path


class TaskListGroupTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, title, state=None):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(f"create-{rid}", {"title": title, "objective": "分组"}))[0], 201)
        if state == "delivered":
            job = "d0d0d0d0d0d0"
            Path(self.jobs.path(job, "out")).mkdir(parents=True)
            Path(self.jobs.path(job, "out", "result.json")).write_text("{}")
            self.jobs._write_meta(job, {"id": job, "bin": "custom", "argv": ["run"], "research_id": rid, "started": 1, "exit": 0})
            code, _, err = self.pf(core.ADMIN, "research", "deliver", rid, "--summary", "## 结论\n做完了", "--artifact", f"job:{job}")
            self.assertEqual(code, 0, err)
        elif state:
            current = self.cli("GET", f"/research-state/research/{rid}")[1]
            change = dict(self.write(f"{state}-{rid}", {**current["data"], "state": state, "state_reason": "分组"}),
                          base_revision=current["revision"], reason="分组")
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", change)[0], 201)

    def test_tasks_are_listed_in_progress_delivered_and_closed_groups(self):
        self.task("sr-old", "早开的任务")
        self.task("sr-done", "交付了的任务", "delivered")
        self.task("sr-shut", "关掉的任务", "closed")
        self.task("sr-paused", "暂停的任务", "paused")
        self.task("sr-new", "新开的任务")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research" % self.address)
            page.wait_for_selector('#object-list li[data-id="sr-old"]')
            rows = page.eval_on_selector_all("#object-list > li", """items => items.map(li => li.classList.contains('research-group')
                ? '# ' + [...li.children].map(x => x.textContent).join(' ') : li.dataset.id)""")
            self.assertEqual(rows, ["# 进行中 3", "sr-new", "sr-paused", "sr-old", "# 已交付 1", "sr-done", "# 已关闭 1", "sr-shut"])
            page.click('#object-list li[data-id="sr-done"] .research-choice')
            page.wait_for_function("() => document.querySelector('#object-title').textContent === '交付了的任务'")

            # Other kinds of records are not grouped.
            page.click("details.research-filters > summary")
            page.select_option("#filter-kind", "direction")
            page.click("#filter-submit")
            page.wait_for_function("() => document.querySelector('#objects-heading').textContent !== '任务'")
            self.assertEqual(page.locator("#object-list > .research-group").count(), 0)


if __name__ == "__main__":
    unittest.main()
