"""A task's 作业 line says what is going on now (在跑 n / 排队 n); how many succeeded or failed is a technical detail, as
a failure retried later would read like trouble to a person who does not know."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

JOBS = [{"id": f"{i:012x}", "bin": "fixture", "argv": ["run"], "label": label, "started": 1700000000 + i, **state}
        for i, (label, state) in enumerate([
            ("在跑的", {"state": "running", "phase": "running"}), ("排队的", {"state": "queued"}),
            ("成功的", {"state": "finished", "outcome": "succeeded"}), ("失败的", {"state": "finished", "outcome": "failed"}),
            ("又失败的", {"state": "finished", "outcome": "failed"})])]


class RunCountTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_the_jobs_line_says_what_runs_and_folds_how_they_went(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-runs", self.write(
            "create-sr-runs", {"title": "计数", "objective": "作业"}))[0], 201)

        def jobs(route):
            if "outcome=failed" in route.request.url:
                return route.continue_()
            route.fulfill(json={"schema_version": 1, "node_id": self.node_id, "items": JOBS, "next_cursor": None})
        with self._browser_page() as page:
            page.route("**/dash/api/jobs?research=sr-runs*", jobs)
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-runs" % self.address)
            line = "#task-runs .task-fold > summary"
            page.wait_for_function(f"() => document.querySelector('{line} .count')?.textContent === '5'")
            self.assertEqual(page.locator(f"{line} .fold-brief").inner_text(), "在跑 1 / 排队 1")
            self.assertTrue(page.locator(f"{line} .fold-brief .tech").is_hidden())
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.locator(f"{line} .fold-brief .tech").inner_text().strip(), "成功 1 · 失败 2")


if __name__ == "__main__":
    unittest.main()
