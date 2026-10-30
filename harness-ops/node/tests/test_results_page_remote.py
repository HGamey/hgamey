"""Results from another computer live in this center's index: the page says which computer ran each one, and its
details and originals open here after that computer has gone."""
import json
import os
import time
import unittest
from urllib.parse import parse_qs, urlparse

import test_multi_node_artifacts as artifacts_fixture
from test_node_results import metric, registration

REPORT = '{"timing":{"gpu_ms":2.413}}'


class ResultsPageRemoteTests(unittest.TestCase):
    # Borrow the center + joined worker fixture without rediscovering its tests.
    setUp = artifacts_fixture.MultiNodeArtifactsTests.setUp

    def collected(self, jid):
        deadline = time.time() + 20
        while time.time() < deadline:
            code, job = self.center.call('GET', '/jobs/' + jid)
            if code == 200 and job['artifacts_sync']['status'] == 'complete':
                return
            time.sleep(0.2)
        self.fail('the worker job outputs were not collected to the center')

    def remote_result(self):
        """Run a job on the worker, collect its outputs here, let the worker go, then register its result here."""
        connector = self.flow.start_worker()
        program = "from pathlib import Path; import sys; (Path(sys.argv[1])/'report.json').write_text(%r)" % REPORT
        code, job = self.flow.submit({'bin': 'fixture', 'argv': ['-c', program, '@out'], 'device_access': 'none',
                                      'execution_node_id': self.worker.jobs.node_id}, 'remote-result-job')
        self.assertEqual(code, 200, job)
        self.collected(job['id'])
        connector.close()
        self.register(registration('remote-result', job['id']))
        return job['id']

    def register(self, manifest):
        code, result = self.center.call('POST', '/results', manifest,
                                        token=self.flow.agent, headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201, result)
        return result

    def browse(self, check):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        board, base = self.center.board, self.center.url
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                check(page, base)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_remote_detail_and_original_open_through_this_center(self):
        """A result the worker produced lists as 在 worker; its detail names the worker, and its original links to
        /dash/api/results/<id>/sources/<name>, which serves the bytes with the worker disconnected; its job says the
        originals were copied here. Nothing links anywhere but this center."""
        job = self.remote_result()

        def check(page, base):
            page.goto(base + "/dash/results")
            row = page.locator("#result-list .result-choice").filter(has_text="remote-result")
            row.wait_for()
            self.assertIn("在 worker", row.text_content())
            self.assertEqual(page.locator("#federation-panel").count(), 0)
            row.click()
            page.wait_for_selector("#source-list a")
            self.assertIn("worker", page.text_content("#result-info"))
            href = page.get_attribute("#source-list a", "href")
            link = urlparse(href)
            self.assertEqual(link.netloc, "")
            self.assertTrue(link.path.startswith("/dash/api/results/remote-result/sources/report"), href)
            original = page.request.get(base + href)
            self.assertEqual((original.status, original.text()), (200, REPORT))
            hrefs = page.eval_on_selector_all("a[href]", "e => e.map(a => a.href)")
            self.assertTrue(all(h.startswith(base + "/") for h in hrefs), hrefs)
            self.assertNotIn(self.worker.url, " ".join(hrefs))
            page.click("#current-job-link")
            page.wait_for_function(f"() => /{job}/.test(location.hash) && document.getElementById('execution-info').textContent.includes('原件已复制到本机')")
            self.assertIn("worker", page.text_content("#execution-info"))
        self.browse(check)

    def test_results_from_different_computers_compare_with_a_baseline_per_column(self):
        """Putting this computer's result beside the worker's switches to per-column baselines, all on the first
        column, and asks the center with result ids only: no source list, no node id on a column."""
        self.remote_result()
        here = registration('local-result', None)
        here.update(title='本机结果', sources=[], metrics=[{**metric(3.1), 'source': None}])
        self.register(here)

        def check(page, base):
            asked = []
            page.on("request", lambda request: asked.append(request.url) if "/result-cross-compare?" in request.url else None)
            page.goto(base + "/dash/results")
            page.click("#compare-panel summary")
            for result in ("local-result", "remote-result"):
                item = page.locator("#result-list li").filter(has_text=result)
                item.locator(".compare-list-add:not([disabled])").click()
            page.wait_for_function("() => document.getElementById('compare-message').textContent.includes('已改为逐列选择基线')")
            baselines = page.eval_on_selector_all("[data-compare-baseline]", "e => e.map(x => x.value)")
            first = page.eval_on_selector("[data-compare-baseline] option:nth-child(2)", "e => e.value")
            self.assertEqual(baselines, [first, first])
            self.assertIn("worker · remote-result", page.text_content("#compare-columns"))
            self.assertIn("本机 · local-result", page.text_content("#compare-columns"))
            page.click("#compare-add-row")
            for select in page.query_selector_all("#compare-rows select"):
                page.wait_for_function("e => !e.disabled", arg=select)
                select.select_option("0")
            page.click("#compare-run")
            page.wait_for_selector("#compare-output .compare-cell")
            spec = json.loads(parse_qs(urlparse(asked[-1]).query)["spec"][0])
            self.assertEqual(spec["schema"], "phonefarm/result-cross-compare@1")
            self.assertNotIn("targets", spec)
            self.assertEqual([sorted(column) for column in spec["columns"]], [["baseline_column_id", "id", "label", "result_id"]] * 2)
            cells = page.eval_on_selector_all("#compare-output .compare-cell", "e => e.map(x => x.textContent)")
            self.assertIn("本机 · local-result", cells[0])
            self.assertIn("worker · remote-result", cells[1])
            self.assertIn("（另一台电脑的结果）", cells[1])
        self.browse(check)

if __name__ == "__main__":
    unittest.main()
