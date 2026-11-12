"""A comparison's 补充一句 box on a phone: it takes a row of its own as wide as the answer, with the password and 提交
under it, instead of being squeezed beside them. On a wide screen the three stay on one row."""
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_report_pictures import png

JOB = "dddddddddddd"
FOOT = "#cmp-ev-foot .compare-answer .compare-foot"
BOXES = """f => [...document.querySelector(f).children].filter(e => !e.hidden).map(e => {
    const r = e.getBoundingClientRect(); return [e.tagName, Math.round(r.top), Math.round(r.bottom), Math.round(r.width)]; })"""


class CompareFootNarrowTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def fixture(self):
        out = Path(self.jobs.path(JOB, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        names = {"foot-a": b"\x10\x20\x30", "foot-b": b"\x30\x20\x10"}
        for name, rgb in names.items():
            (out / (name + ".png")).write_bytes(png(rgb))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-foot", "title": "作答素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": JOB, "path": name + ".png"} for name in names]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-foot", self.write(
            "create-foot", {"title": "图片盲评", "objective": "手机上作答"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-foot", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-foot", self.write("create-ev-foot", {
            "research_id": "sr-foot", "type": "evaluation", "title": "哪张更清楚", "need": "选 1 个", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref(name) for name in names],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": "image"} for i in range(2)], "pick": 1}}))[0], 201)

    def test_the_sentence_gets_its_own_row_on_a_phone(self):
        self.fixture()
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-foot" % self.address)
            page.wait_for_selector(FOOT + ' input[type="password"]')
            bottoms = {bottom for _, _, bottom, _ in page.evaluate(BOXES, FOOT)}
            self.assertEqual(len(bottoms), 1, "a wide screen keeps the box, the password and 提交 on one row")

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("() => innerWidth === 390")
            width = page.eval_on_selector(FOOT, "e => e.clientWidth")
            (text, _, end, wide), (password, below, bottom, _), (button, _, beside, _) = page.evaluate(BOXES, FOOT)
            self.assertEqual((text, password, button), ("TEXTAREA", "INPUT", "BUTTON"))
            self.assertGreaterEqual(wide, width - 1, "the box is as wide as the answer")
            self.assertGreaterEqual(below, end, "the password goes under the box")
            self.assertEqual(beside, bottom, "提交 sits beside the password")
            self.assertLessEqual(page.eval_on_selector(FOOT, "e => e.scrollWidth - e.clientWidth"), 0)


if __name__ == "__main__":
    unittest.main()
