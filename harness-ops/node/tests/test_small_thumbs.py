"""A preview reads the center's small copy of a picture, about 480 px on the long side, made the first time it is asked
for and kept on disk; opening the preview reads the original, and a download is the original as before. A picture that
is that small already, a GIF, or a center without Pillow sends the original."""
import http.client
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_report_pictures as pictures

try:
    from PIL import Image
except ImportError:
    Image = None

JOB = pictures.JOB


def picture(size, mode="RGB", kind="PNG", shade=90):
    out = io.BytesIO()
    Image.new(mode, size, (40, shade, 160, 128)[:len(mode)]).save(out, kind)
    return out.getvalue()


@unittest.skipIf(Image is None, "Pillow makes the small copies")
class SmallThumbTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def get(self, path):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", path, headers={"Cookie": self.view_cookie, "X-Expected-Node-ID": self.node_id})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, 200, body[:300])
        return response.getheader("Content-Type"), body

    def outputs(self, files):
        out = Path(self.jobs.path(JOB, "out"))
        for path, data in files.items():
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["capture"], "research_id": "sr-small", "started": 1, "exit": 0})

    def test_a_large_picture_comes_small_and_is_made_once(self):
        big = picture((1920, 1080))
        files = {"big.png": big, "tall.jpg": picture((600, 1200), kind="JPEG"), "clear.png": picture((1000, 500), "RGBA"),
                 "tiny.png": picture((320, 180)), "anim.gif": pictures.GIF, "fresh.png": picture((1000, 1000), shade=10)}
        self.outputs(files)
        url = lambda path, thumb=True: f"/dash/api/jobs/{JOB}/out?path={path}" + ("&thumb=1" if thumb else "")

        kind, small = self.get(url("big.png"))
        self.assertEqual((kind, Image.open(io.BytesIO(small)).size), ("image/jpeg", (480, 270)))
        kept = list(Path(self.jobs.data_root, "_thumbs").iterdir())
        self.assertEqual([path.read_bytes() for path in kept], [small])
        with mock.patch("PIL.Image.open", side_effect=AssertionError("the kept copy is sent, not made again")):
            self.assertEqual(self.get(url("big.png")), ("image/jpeg", small))
        self.assertEqual(Image.open(io.BytesIO(self.get(url("tall.jpg"))[1])).size, (240, 480))
        kind, clear = self.get(url("clear.png"))
        self.assertEqual((kind, Image.open(io.BytesIO(clear)).mode), ("image/png", "RGBA"))

        # The original download, a picture small already, and a GIF (it may move) are the files as they are.
        self.assertEqual(self.get(url("big.png", thumb=False)), ("image/png", big))
        self.assertEqual(self.get(url("tiny.png"))[1], files["tiny.png"])
        self.assertEqual(self.get(url("anim.gif"))[1], pictures.GIF)
        with mock.patch.dict(sys.modules, {"PIL": None}):
            self.assertEqual(self.get(url("fresh.png")), ("image/png", files["fresh.png"]))

    def test_an_attached_picture_comes_small_too(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-small", self.write(
            "create-sr-small", {"title": "小图", "objective": "附件也出小图"}))[0], 201)
        shot = self.tmp / "shot.png"
        shot.write_bytes(picture((1600, 900)))
        code, _, err = self.pf(core.ADMIN, "research", "note", "sr-small", "截图", "--attach", str(shot))
        self.assertEqual(code, 0, err)
        ref = self.web("GET", "/research-state/note?research_id=sr-small&limit=10")[1]["items"][0]["evidence_refs"][0]
        kind, small = self.get(f"/dash/api/research-attachments/{ref['attachment_id']}?thumb=1")
        self.assertEqual((kind, Image.open(io.BytesIO(small)).size), ("image/jpeg", (480, 270)))
        self.assertEqual(self.get(f"/dash/api/research-attachments/{ref['attachment_id']}"), ("image/png", shot.read_bytes()))

    def test_previews_show_the_small_copy_and_open_the_original(self):
        self.outputs({"shots/frame.png": picture((1920, 1080))})
        self.assertEqual(self.web("POST", "/research-state/research/sr-small", self.write(
            "create-sr-small", {"title": "小图", "objective": "预览用小图"}))[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-small", "--summary", "## 结论\n看图", "--artifact", f"job:{JOB}")
        self.assertEqual(code, 0, err)
        base = "http://%s:%d" % self.address
        for url, thumb in ((base + "/dash/research/report?id=sr-small", '#outputs .output-file:has(> a[title="shots/frame.png"]) .image-thumb'),
                           (base + "/dash/research?kind=research&id=sr-small", 'button.media-preview[aria-label="放大查看 frame.png"]')):
            with self._browser_page() as page:
                page.goto(url)
                page.locator(thumb).first.scroll_into_view_if_needed()
                page.wait_for_function("t => document.querySelector(t + ' img')?.naturalWidth > 0", arg=thumb)
                self.assertEqual(page.eval_on_selector(thumb + " img", "e => e.naturalWidth"), 480, url)
                page.click(thumb)
                page.wait_for_function("() => document.querySelector('dialog.compare-viewer[open] img.viewer-media')?.naturalWidth > 0")
                self.assertEqual(page.eval_on_selector("dialog.compare-viewer img.viewer-media", "e => e.naturalWidth"), 1920, url)


if __name__ == "__main__":
    unittest.main()
