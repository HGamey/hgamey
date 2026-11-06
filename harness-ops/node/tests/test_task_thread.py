"""The thread on a task page: the newest five threads show; older ones fold behind a count that opens them in place.
A question still waiting for a person, and the note a link opens, stay out of the fold."""
import json
import unittest

import test_core_flow_web_cli as core


class TaskThreadTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def note(self, nid, text, reply_to=None):
        self.assertEqual(self.web("POST", f"/research-state/note/{nid}", self.write("create-" + nid, {
            "research_id": "sr-thread", "text": text, "reply_to": reply_to, "decision": None}))[0], 201)

    def shown(self, page):
        return page.eval_on_selector_all("#task-thread .discuss-list > .discuss-entry", "es => es.filter(e => !e.hidden).map(e => e.dataset.note)")

    def test_older_notes_fold_behind_a_count_except_a_question_waiting_for_a_person(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-thread", self.write(
            "create-sr-thread", {"title": "很多留言", "objective": "只看最近的"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-thread")[0], 0)
        code, out, err = self.pf(token, "research", "note", "sr-thread", "先测哪一档？", "--ask")
        self.assertEqual(code, 0, err)
        question = json.loads(out)["object"]["id"]
        self.note("n-0", "进展 0")
        self.note("n-0r", "回复 0", reply_to="n-0")
        for i in range(1, 8):
            self.note(f"n-{i}", f"进展 {i}")
        base = "http://%s:%d/dash/research?kind=research&id=sr-thread" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.wait_for_selector("#task-thread .notes-more")
            self.assertEqual(page.text_content("#task-thread .section-head .count"), "10")
            # The question waits for a person, so it stays; the four notes of the three older threads fold.
            self.assertEqual(self.shown(page), [question, "n-3", "n-4", "n-5", "n-6", "n-7"])
            self.assertEqual(page.text_content("#task-thread .notes-more"), "更早的 4 条留言")
            page.click("#task-thread .notes-more")
            self.assertEqual(self.shown(page), [question] + [f"n-{i}" for i in range(8)])
            self.assertTrue(page.is_visible("#note-n-0r"))
            self.assertEqual(page.locator("#task-thread .notes-more").count(), 0)

            # A link to an older note opens with that note showing.
            page.goto(base + "#note-n-1")
            page.reload()
            page.wait_for_selector("#task-thread .notes-more")
            self.assertIn("n-1", self.shown(page))
            self.assertEqual(page.text_content("#task-thread .notes-more"), "更早的 3 条留言")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
