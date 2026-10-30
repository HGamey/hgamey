"""The game preparation panel picks a phone on any joined computer and recognises that computer's receipts."""
import json
import os
import time
import unittest
from unittest.mock import patch

import test_multi_node_games as games_fixture


class GamesPageMultiNodeTests(unittest.TestCase):
    # Borrow the center + enrolled computer (fake PF) fixture without rediscovering its tests.
    setUp = games_fixture.MultiNodeGameTests.setUp
    source = games_fixture.MultiNodeGameTests.source

    def finished(self, request_id):
        deadline = time.time() + 20
        while time.time() < deadline:
            code, job = self.center.call('GET', '/requests/' + request_id)
            if code == 200 and job['state'] == 'finished':
                return job
            time.sleep(0.2)
        self.fail('the deployment on the enrolled computer did not finish')

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
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base},
                                     {"name": "hg_steer", "value": board._cookie_val("test-steer", b"hgamey-steer-v1"), "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                check(page, base)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def read_receipt(self, page, job):
        row = page.locator("#deploy-history li").filter(has_text=job['id'])
        row.wait_for()
        self.assertNotIn("报告身份与目标不符", row.text_content())
        row.get_by_text("读取回执与恢复资格").click()
        row.get_by_role("button", name="读取最新回执").click()
        row.locator(".deploy-facts").wait_for()
        self.assertNotIn("回执目标不匹配", row.text_content())
        self.assertIn("最近有据记录", page.text_content("#game-deployment .deploy-status"))
        return row

    def test_a_phone_on_another_computer_is_prepared_and_its_receipts_are_recognised(self):
        """The phone list comes from /dash/api/phones (every computer). Choosing the enrolled computer's registered
        phone sends its platform ref and no binding; the center attaches the phone's registered binding. A lost
        reply is found again by its request ID, and the receipt (target = that computer + the phone's own serial,
        report.serial = the phone's own serial) is accepted and shown; recovery runs on the same computer and phone."""
        scopes = {'schema': 'phonefarm/device-scopes@1', 'registry_revision': 1, 'unknown': False, 'queue_waiting': False,
                  'global': {'status': 'clear', 'error': None},
                  'devices': [{'device_id': 'phone-a', 'aliases': ['usb-game'], 'status': 'clear', 'error': None}]}
        registered = patch.object(self.worker.leases, 'snapshot', return_value={'status': 'clear', 'entries': [], 'device_scopes': scopes})
        registered.start()
        self.addCleanup(registered.stop)
        self.center.jobs.directory.report(self.worker.node_id, self.flow.connector.backend.snapshot())
        ref = self.worker.node_id + '/phone-a'
        expected = {'schema': 'phonefarm/device-binding@1', 'registry_revision': 1, 'device_id': 'phone-a', 'serial': 'usb-game'}

        def check(page, base):
            sent = []

            def lost(route, request):
                # The center records the job; its reply never reaches the page.
                sent.append({'body': json.loads(request.post_data), 'request_id': request.headers['x-request-id']})
                route.fetch()
                route.abort()
            page.route("**/dash/api/games/demo/versions/v1/deploy", lost)
            page.goto(base + "/dash/games#game=demo&version=v1")
            option = f'#deploy-device option[value="{ref}"]'
            page.wait_for_selector(option, state="attached")
            self.assertIn("enrolled · usb-game", page.text_content(option))
            page.select_option("#deploy-device", ref)
            self.assertEqual(page.input_value("#deploy-serial"), ref)
            self.assertTrue(page.is_hidden("#deploy-device-scope"))
            self.assertIn("自动带上它的登记绑定", page.text_content("#deploy-binding-message"))
            self.assertEqual(json.loads(page.text_content("#deploy-binding-info")), expected)
            page.wait_for_selector("#deploy-apply:not([disabled])")
            page.click("#deploy-apply")
            page.wait_for_function("() => document.querySelector('#game-deployment .deploy-request')?.hidden === false")
            [request] = sent
            self.assertEqual(request['body'], {'node_id': self.center.jobs.node_id, 'serial': ref, 'mode': 'apply'})
            self.assertIn("enrolled · usb-game", page.text_content("#game-deployment .deploy-request"))
            job = self.finished(request['request_id'])
            self.assertEqual((job['execution_node_id'], job['device_binding']), (self.worker.node_id, expected))

            page.click("#deploy-query")
            page.wait_for_function(f"() => document.getElementById('game-deployment').textContent.includes('已找回资源部署原任务 {job['id']}')")
            row = self.read_receipt(page, job)
            self.assertIn("需要恢复", row.text_content())
            row.get_by_role("button", name="提交恢复任务").click()
            page.wait_for_function("() => document.getElementById('game-deployment').textContent.includes('接续事务恢复任务已记录')")
            [recovery] = [item for item in self.center.jobs.center.targets.rows() if item['request']['spec'].get('recovery_for') == job['id']]
            recovered = self.finished(recovery['request_id'])
            self.assertEqual((recovered['execution_node_id'], recovered['device_binding'], recovered['exit']), (self.worker.node_id, expected, 0))
        self.browse(check)

    def test_a_typed_platform_ref_is_prepared_when_the_phone_list_has_not_seen_it(self):
        """No computer has sampled the phone, so the list is empty. A hand-typed node_id/raw-serial ref is still
        submitted as is (the backend accepts it unsampled), runs on that computer's PF holding only that phone, and
        its receipt is matched to that computer and the serial the job froze."""
        ref = self.worker.node_id + '/usb-game'

        def check(page, base):
            sent = []
            page.on("request", lambda request: sent.append((json.loads(request.post_data), request.headers['x-request-id']))
                    if request.method == "POST" and request.url.endswith("/versions/v1/deploy") else None)
            page.goto(base + "/dash/games#game=demo&version=v1")
            page.wait_for_function("() => document.getElementById('game-deployment').textContent.includes('0 台手机')")
            page.fill("#deploy-serial", ref)
            self.assertTrue(page.is_hidden("#deploy-device-scope"))
            self.assertIn("手机列表里还没有这台手机", page.text_content("#deploy-binding-message"))
            self.assertNotIn("全域排队", page.text_content("#game-deployment"))
            page.wait_for_selector("#deploy-apply:not([disabled])")
            page.click("#deploy-apply")
            page.wait_for_function("() => document.getElementById('game-deployment').textContent.includes('部署到新目录任务已记录')")
            [(body, request_id)] = sent
            self.assertEqual(body, {'node_id': self.center.jobs.node_id, 'serial': ref, 'mode': 'apply'})
            job = self.finished(request_id)
            self.assertEqual((job['execution_node_id'], job['device_selection']['serial'], job.get('device_binding')),
                             (self.worker.node_id, 'usb-game', None))
            self.assertEqual(job['queue']['device_scope']['binding']['serial'], 'usb-game', "it holds only that phone")
            page.click("#refresh-detail")
            self.read_receipt(page, job)
        self.browse(check)


if __name__ == "__main__":
    unittest.main()
