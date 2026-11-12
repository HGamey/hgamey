"""Codes in a note's text (job and note IDs, commits, phone serials) read as grey tags saying what they are, a job's
linking to it; 显示技术细节 shows the codes themselves. Next to the word for it, a tag is just "…"."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

TEXT = "已更新到 e231c61，作业 a6895829da1b 跑完了，结果见 n-56afdcc1377；91253241019A 有点烫，采样时间 20261103。"


class NoteCodeTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_codes_in_a_note_are_tags_until_technical_details_are_asked_for(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-codes", self.write(
            "create-sr-codes", {"title": "代号", "objective": "留言"}))[0], 201)
        self.assertEqual(self.cli("POST", "/research-state/note/n-codes", self.write("create-n-codes", {
            "research_id": "sr-codes", "text": TEXT, "reply_to": None, "decision": None}))[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-codes" % self.address)
            note = "#note-n-codes .discuss-text"
            page.wait_for_selector(note)
            self.assertEqual(page.inner_text(note), "已更新到 提交，作业 … 跑完了，结果见 留言；手机 有点烫，采样时间 20261103。")
            job = page.locator(f"{note} a.code-chip")
            self.assertEqual((job.count(), job.get_attribute("title")), (1, "a6895829da1b"))
            self.assertTrue(job.get_attribute("href").startswith("/dash/jobs") and job.get_attribute("href").endswith("#job=a6895829da1b"))
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.inner_text(note), TEXT)


if __name__ == "__main__":
    unittest.main()
