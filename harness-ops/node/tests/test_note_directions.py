"""A direction of the task named in a note by its ID reads as its title, 方向「…」 (just 「…」 right after the word 方向);
显示技术细节 shows the ID itself, as for the other codes. A word that only looks like an ID, or another task's
direction, reads as written."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

TEXT = ("跨机验证计划已登记为方向 qrisp-cross-host-validation-20261112。下一步看 doom-btrack-g1 的结果，"
        "qrisp-cross-host-validation-20261112-old 不是它，other-direction 也不在这个任务里。")


class NoteDirectionTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_direction_ids_in_a_note_read_as_their_titles(self):
        for rid in ("sr-dirs", "sr-else"):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "方向"}))[0], 201)
        for did, rid, title in (("qrisp-cross-host-validation-20261112", "sr-dirs", "跨机复现 QRISP 训练"),
                                ("doom-btrack-g1", "sr-dirs", "Doom B 轨第一组"), ("other-direction", "sr-else", "别的任务的方向")):
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write(
                "create-" + did, {"research_id": rid, "title": title, "hypothesis": "验证"}))[0], 201)
        self.assertEqual(self.cli("POST", "/research-state/note/n-dirs", self.write("create-n-dirs", {
            "research_id": "sr-dirs", "text": TEXT, "reply_to": None, "decision": None}))[0], 201)
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-dirs" % self.address)
            note = "#note-n-dirs .discuss-text"
            page.wait_for_selector(f"{note} .code-direction")
            self.assertEqual(page.inner_text(note), "跨机验证计划已登记为方向 「跨机复现 QRISP 训练」。下一步看 方向「Doom B 轨第一组」 的结果，"
                             "qrisp-cross-host-validation-20261112-old 不是它，other-direction 也不在这个任务里。")
            page.click(".shell-more-btn")
            page.click("#shell-tech")
            self.assertEqual(page.inner_text(note), TEXT)


if __name__ == "__main__":
    unittest.main()
