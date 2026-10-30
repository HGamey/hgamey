"""The jobs page tells where a job runs, waits for the other computer instead of calling it queued or stopped, and
shows each computer's queue on its own."""
import os
import unittest
from unittest.mock import patch

import test_multi_node_jobs as multi_node


class JobsPageMultiNodeTests(unittest.TestCase):
    # Borrow the center + joined worker fixture without rediscovering its tests.
    setUp = multi_node.MultiNodeJobTests.setUp
    start_worker = multi_node.MultiNodeJobTests.start_worker
    submit = multi_node.MultiNodeJobTests.submit
    phone = multi_node.MultiNodeJobTests.phone

    def test_remote_jobs_wait_for_their_computer_and_a_stop_stays_pending_until_it_answers(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.worker.jobs.dispatch.close()
        self.worker.jobs.dispatch.start = lambda: None   # the job stays queued on the worker
        connector = self.start_worker()
        spec = {'bin': 'fixture', 'argv': ['-c', 'pass'], 'device_access': 'none', 'execution_node_id': self.worker.jobs.node_id}
        code, held = self.submit(spec, 'held-on-worker')
        self.assertEqual(code, 200, held)
        connector._json('/node-connect/report', connector.backend.snapshot())
        connector.close()
        # Read as disconnected now rather than after 60 s of silence, and give up on it after 1 s instead of 8.
        for name, value in (('node_directory.REFRESH_S', 0), ('job_center.DEADLINE_S', 1)):
            quiet = patch(name, value)
            quiet.start()
            self.addCleanup(quiet.stop)
        code, unsent = self.submit({**spec, 'argv': ['-c', "print('later')"]}, 'not-yet-accepted')
        self.assertEqual((code, unsent['phase']), (200, 'submission_unknown'), unsent)
        board, base = self.center.board, self.center.url
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base},
                                     {"name": "hg_steer", "value": board._cookie_val("test-steer", b"hgamey-steer-v1"), "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/dash/jobs")
                row = f'#task-list button[data-job="{unsent["id"]}"]'
                page.wait_for_selector(row)
                self.assertIn("等待那台电脑受理", page.text_content(row + " .task-state"))
                self.assertIn("在 worker", page.text_content(row))
                page.wait_for_selector("#execution-queues .execution-queue")
                remote_queue = page.text_content("#execution-queues")
                self.assertIn("worker", remote_queue)
                self.assertIn(held["id"], remote_queue)
                self.assertIn("本机", page.text_content("#queue-summary"))
                self.assertIn("已断开", remote_queue)

                page.click(f'#task-list button[data-job="{held["id"]}"]')
                # Reading one job asks its computer once and waits out the deadline when it is gone.
                page.wait_for_function("() => document.getElementById('execution-info').textContent.includes('worker · 已断开')", timeout=40000)
                page.wait_for_selector("#cancel-job:not([disabled])")
                page.click("#cancel-job")
                page.wait_for_function("() => document.getElementById('cancel-result').textContent.includes('还没送到那台电脑')", timeout=40000)
                status = self.center.call('GET', '/jobs/' + held['id'])[1]
                self.assertTrue(status['stop_requested'])
                self.assertNotEqual(status['queue']['state'], 'done', "a stop that has not reached the worker is not a stop")
                page.wait_for_function(f"() => document.querySelector('#task-list button[data-job=\"{held['id']}\"] .task-state').textContent.includes('停止待确认')", timeout=40000)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_job_can_ask_for_any_idle_phone_of_a_model_on_any_computer(self):
        """平台手机 lists every computer's phones; choosing a model sends device_model, the center picks the idle one,
        and @serial in the arguments becomes its serial on the computer that has it."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.phone(self.center, 'USB-A', 'held')
        self.phone(self.worker, 'USB-B', 'clear')
        self.start_worker()
        board, base = self.center.board, self.center.url
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base},
                                     {"name": "hg_steer", "value": board._cookie_val("test-steer", b"hgamey-steer-v1"), "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/dash/jobs")
                page.click("#submit-panel summary")
                page.wait_for_selector("#submit-bin:not([disabled])")
                page.select_option("#submit-device-scope", "platform")
                page.wait_for_selector('#submit-phone option[value="model:NX809J"]', state="attached")
                labels = page.eval_on_selector_all("#submit-phone option", "e => e.map(x => x.textContent)")
                self.assertIn("NX809J（2 台，1 台最近空闲）", labels)
                self.assertTrue(any("USB-B" in label for label in labels) and any("USB-A" in label for label in labels), labels)
                page.select_option("#submit-phone", "model:NX809J")
                page.select_option("#submit-bin", "fixture")
                page.fill("#submit-argv", '["-c", "pass", "@serial"]')
                page.select_option("#submit-device-access", "write")
                page.fill("#submit-label", "any NX809J")
                page.click("#submit-job")
                page.wait_for_function("() => document.getElementById('submit-message').textContent.includes('任务已记录')")
                [record] = [row for row in self.center.jobs.center.targets.rows() if row['request']['spec'].get('label') == 'any NX809J']
                self.assertEqual((record['device_selection']['model'], record['device_selection']['serial']), ('NX809J', 'USB-B'))
                self.assertEqual(record['execution_node_id'], self.worker.jobs.node_id, "the idle phone of the model, not the held one")
                self.assertEqual(self.worker.jobs.status(record['id'])['argv'][-1], 'USB-B')
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
