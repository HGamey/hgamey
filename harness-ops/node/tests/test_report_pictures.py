"""Pictures in a delivery report: each picture among the outputs shows a small thumbnail under its file, and a picture
the text names as job:<job>:<path> can be clicked; both open large on the page, in the viewer comparisons use. Other
files stay as they are, and a picture that cannot be read says so beside its link."""
import struct
import unittest
import zlib
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

JOB = "f1f1f1f1f1f1"
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


def png(rgb):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * 2) * 2)) + chunk(b"IEND", b""))


class ReportPicturesTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def fixture(self):
        """A delivered task whose job wrote two pictures (a PNG, and a GIF the node serves as plain bytes), a third
        picture that will not be readable, data and notes; the report names a picture and a data file in its text."""
        out = Path(self.jobs.path(JOB, "out"))
        for path, data in (("shots/frame.png", png(b"\x30\x60\x90")), ("anim.gif", GIF), ("far.png", png(b"\x10\x10\x10")),
                           ("data.json", b"{}"), ("notes.md", b"# notes")):
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["capture"], "research_id": "sr-pic", "started": 1, "exit": 0})
        self.assertEqual(self.web("POST", "/research-state/research/sr-pic", self.write(
            "create-sr-pic", {"title": "刷新率", "objective": "看图"}))[0], 201)
        summary = f"## 结论\n设置页图片：`job:{JOB}:shots/frame.png`\n\n原始数据：`job:{JOB}:data.json`"
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-pic", "--summary", summary, "--artifact", f"job:{JOB}")
        self.assertEqual(code, 0, err)

    def viewer(self, page):
        page.wait_for_selector("dialog.compare-viewer[open] img.viewer-media.shown")
        page.wait_for_function("() => document.querySelector('dialog.compare-viewer img.viewer-media').naturalWidth > 0")
        self.assertTrue(page.is_hidden("dialog.compare-viewer .viewer-rail"), "one picture: nothing to switch to")
        return page.get_attribute("dialog.compare-viewer img.viewer-media", "alt")

    def test_pictures_show_thumbnails_and_open_large_and_other_files_do_not(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # far.png is on a computer that cannot be reached.
            page.route("**/dash/api/jobs/*/out?path=far.png*", lambda route, request: route.fulfill(status=503, json={"error": "execution node is unavailable"}))
            page.goto(base + "/dash/research/report?id=sr-pic")
            page.wait_for_selector("#outputs .output-file")
            row = lambda name: f'#outputs .output-file:has(> a[title="{name}"])'
            for name in ("shots/frame.png", "anim.gif"):
                page.wait_for_function("s => document.querySelector(s + ' .image-thumb img')?.naturalWidth > 0", arg=row(name))
                self.assertLessEqual(page.eval_on_selector(row(name) + " .image-thumb img", "e => e.getBoundingClientRect().height"), 160)
                # The file name and its download stay.
                self.assertIn("/out?path=", page.get_attribute(row(name) + " > a", "href"))
            for name in ("data.json", "notes.md"):
                self.assertEqual(page.locator(row(name) + " .image-thumb").count(), 0, name)
            page.wait_for_selector(row("far.png") + " .image-unread")
            self.assertEqual(page.text_content(row("far.png") + " .image-unread"), "无法预览")
            self.assertIn("path=far.png", page.get_attribute(row("far.png") + " > a", "href"))

            # A thumbnail opens the picture large; Esc closes it, and so does a click beside the picture.
            page.click(row("shots/frame.png") + " .image-thumb")
            self.assertEqual(self.viewer(page), "frame.png")
            page.keyboard.press("Escape")
            page.wait_for_selector("dialog.compare-viewer", state="detached")
            page.click(row("anim.gif") + " .image-thumb")
            self.assertEqual(self.viewer(page), "anim.gif")
            box = page.eval_on_selector("dialog.compare-viewer", "e => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }")
            page.mouse.click(box[0] + box[2] / 2, box[1] + box[3] / 2)
            self.assertTrue(page.is_visible("dialog.compare-viewer[open]"), "a click on the picture keeps it open")
            page.mouse.click(box[0] + 20, box[1] + 20)
            page.wait_for_selector("dialog.compare-viewer", state="detached")

            # The text: the picture it names opens in the viewer; the data file it names is left as it was.
            refs = page.eval_on_selector_all("#report .research-report code", "e => e.map(x => [x.textContent, x.parentElement.matches('a.report-picture')])")
            self.assertEqual(refs, [[f"job:{JOB}:shots/frame.png", True], [f"job:{JOB}:data.json", False]])
            page.click("#report .research-report a.report-picture")
            self.assertEqual(self.viewer(page), "frame.png")
            page.keyboard.press("Escape")
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
