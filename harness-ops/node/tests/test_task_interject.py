"""A person is not only there at the end to judge: while a task runs they can write to its agent at any time. The home
page's line says so, and a task that is not closed has 插话 at the end of its 现在 line, which takes them to the box in
留言 ready to type, without changing the address."""
import time
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

HOME = {"now": 0, "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "fleet_age_s": 0,
        "elsewhere": [], "agents": []}


class TaskInterjectTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid, **data):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-" + rid, {"title": rid, "objective": "插话", **data}))[0], 201)

    def test_the_home_line_says_a_person_can_step_in(self):
        with self._browser_page() as page:
            page.route("**/dash/api/home", lambda route: route.fulfill(json={**HOME, "now": time.time()}))
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector(".home-intro")
            self.assertIn("Agent 在真手机上做实验，人随时插话 →", page.text_content(".home-intro"))

    def test_a_running_task_offers_to_step_in_and_a_closed_one_does_not(self):
        self.task("sr-talk", state="active")
        self.task("sr-done", state="closed", state_reason="不做了")
        with self._browser_page() as page:
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 844})
                page.goto("http://%s:%d/dash/research?kind=research&id=sr-talk" % self.address)
                page.wait_for_selector("#task-note-text")
                page.wait_for_function("() => document.querySelector('.task-now span')?.textContent")
                talk = page.locator(".task-now-row a.now-talk")
                self.assertEqual(talk.inner_text(), "插话", width)
                box = page.locator(".task-now-row .task-now").bounding_box()
                link = talk.bounding_box()
                self.assertLess(abs((link["y"] + link["height"] / 2) - (box["y"] + min(box["height"], 26) / 2)), 8, width)
                self.assertGreater(link["x"], box["x"], width)
                talk.click()
                page.wait_for_function("() => document.activeElement?.id === 'task-note-text'")
                page.wait_for_function("() => { const r = document.getElementById('task-note-text').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }")
                self.assertEqual(page.evaluate("location.hash"), "", width)
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-done" % self.address)
            page.wait_for_selector("#task-note-text")
            self.assertEqual(page.locator(".now-talk").count(), 0)


if __name__ == "__main__":
    unittest.main()
