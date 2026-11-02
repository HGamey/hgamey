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


    def test_a_refresh_that_changes_nothing_keeps_the_focused_task(self):
        """The overview polls every 8 s; an unchanged list is not rewritten, so a focused (or hovered, or selected)
        task link stays the same element instead of focus falling back to the page."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        code, _ = self.center.call('POST', '/research-state/research/sr-focus', {
            "schema": "phonefarm/research-write@1", "operation_id": "create-sr-focus", "base_revision": None,
            "data": {"title": "停在这里的任务", "objective": "键盘"}, "actor_label": None, "reason": None},
            headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201)

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
                page.goto(base + "/dash")
                link = '#tasks li[data-task="sr-focus"] a.task-title'
                page.wait_for_selector(link)
                page.focus(link)
                page.eval_on_selector(link, "a => { a.dataset.mark = 'kept'; }")
                for _ in range(2):
                    with page.expect_response(lambda r: r.url.endswith("/dash/api/home")):
                        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")   # the poll's own wake-up
                    page.wait_for_timeout(100)
                    self.assertEqual(page.evaluate("document.activeElement.dataset.mark || document.activeElement.tagName"), "kept")

                # A refresh where the row's own text changes (its relative time ticks on) updates that text in place:
                # the focused link is still the same element.
                ahead = [0]

                def later(route):
                    response = route.fetch()
                    data = response.json()
                    ahead[0] += 1
                    data["now"] = data["now"] + 3600 * ahead[0]
                    route.fulfill(response=response, json=data)
                page.route("**/dash/api/home", later)
                page.eval_on_selector(link, "a => { a.kept = true; }")
                times = []
                for _ in range(2):
                    with page.expect_response(lambda r: r.url.endswith("/dash/api/home")):
                        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    page.wait_for_timeout(100)
                    times.append(page.text_content('#tasks li[data-task="sr-focus"] .task-time'))
                    self.assertTrue(page.evaluate("document.activeElement.kept === true"), times)
                self.assertNotEqual(times[0], times[1], "the row's time did change")
            finally:
                browser.close()

    def test_retry_after_a_first_load_that_could_not_reach_the_node_loads_the_page(self):
        """If the node could not be reached when the page opened, 重试 (and the next poll) read the node's identity
        again instead of failing forever on the first attempt."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        code, _ = self.center.call('POST', '/research-state/research/sr-retry', {
            "schema": "phonefarm/research-write@1", "operation_id": "create-sr-retry", "base_revision": None,
            "data": {"title": "重试之后", "objective": "断网"}, "actor_label": None, "reason": None},
            headers={'X-Expected-Node-ID': self.center.jobs.node_id})
        self.assertEqual(code, 201)

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
                page.route("**/dash/api/**", lambda route: route.abort())
                page.goto(base + "/dash")
                page.wait_for_selector("#tasks [data-retry]")
                page.unroute("**/dash/api/**")
                page.click("#tasks [data-retry]")
                page.wait_for_selector('#tasks li[data-task="sr-retry"]', timeout=10000)
            finally:
                browser.close()

    def test_a_lock_that_fails_says_so_on_the_button(self):
        """Clicking 可操作 to lock again: if the lock request fails, the button itself says 上锁失败 (also on a phone,
        where the label is otherwise hidden) and stays unlocked."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")

        class Goals:
            def list(self):
                return []

            def harness_support(self):
                return {"ok": False}
        board, base = self.center.board, self.center.url
        board.goals = Goals()

        def unlocked(route):
            response = route.fetch()
            route.fulfill(response=response, json={**response.json(), "steer": True})
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844})
                context.add_cookies([{"name": "hg_dash", "value": board._cookie_val("test-view"), "url": base}])
                page = context.new_page()
                page.route("**/dash/api/home*", unlocked)
                page.route("**/dash/api/steer/lock*", lambda route: route.fulfill(status=500, json={"error": "磁盘满"}))
                page.goto(base + "/dash")
                page.wait_for_selector('#steer-btn[aria-pressed="true"]')
                page.click("#steer-btn")
                page.wait_for_function("() => document.querySelector('#steer-btn .steer-label').textContent === '上锁失败'", timeout=10000)
                self.assertTrue(page.is_visible("#steer-btn .steer-label"))
                self.assertEqual(page.get_attribute("#steer-btn", "aria-pressed"), "true")
            finally:
                browser.close()

if __name__ == "__main__":
    unittest.main()
