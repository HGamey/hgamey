"""The home page lists other computers' phones under their computer and a task's runs on them."""
import os
import unittest

import test_multi_node_jobs as multi_node


class HomeMultiNodeTests(unittest.TestCase):
    # Borrow the center + joined worker fixture without rediscovering its tests.
    setUp = multi_node.MultiNodeJobTests.setUp
    start_worker = multi_node.MultiNodeJobTests.start_worker
    submit = multi_node.MultiNodeJobTests.submit
    phone = multi_node.MultiNodeJobTests.phone

    def test_phones_group_by_computer_and_a_remote_run_shows_under_its_task(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.phone(self.worker, 'USB-B', 'clear')   # also keeps the worker's queue from starting jobs
        self.start_worker()
        code, _ = self.center.call('POST', '/research-state/research/sr-far', {
            "schema": "phonefarm/research-write@1", "operation_id": "create-sr-far", "base_revision": None,
            "data": {"title": "另一台电脑上的任务", "objective": "看首页"}, "actor_label": None, "reason": None},
            headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201)
        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none', 'research_id': 'sr-far',
                'label': 'far run', 'execution_node_id': self.worker.jobs.node_id}
        code, job = self.submit(spec, 'far-run')
        self.assertEqual(code, 200, job)

        class Goals:
            def list(self):
                return []

            def harness_support(self):
                return {"ok": False}
        board, base = self.center.board, self.center.url
        board.goals = Goals()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/dash")
                group = '#phones .phone-group[data-computer="worker"]'
                page.wait_for_selector(group)
                self.assertIn("NX809J", page.text_content(group))
                self.assertIn("空闲", page.text_content(group + " .tag"))
                self.assertEqual(page.eval_on_selector_all("#phones .phone-computer", "e => e.map(x => x.firstChild.textContent)"), ["本机", "worker"])
                row = '#tasks li[data-task="sr-far"]'
                page.wait_for_selector(row + " .run")
                self.assertIn("far run", page.text_content(row))
                self.assertIn("在 worker", page.text_content(row))
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
