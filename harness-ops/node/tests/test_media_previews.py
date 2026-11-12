"""Every page that links to a picture or a video previews it under the link: the task page's 成果, the report's
outputs, the data flow, a job's files and a result's registered originals. Pictures are thumbnails that open large
(in the comparison viewer where the page has it, in a new tab elsewhere), videos are small players; other files
stay links, and one that cannot be read says so."""
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_report_pictures import png
from test_report_inline_media import CLIP

JOB = "c4c4c4c4c4c4"


class MediaPreviewTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def fixture(self):
        out = Path(self.jobs.path(JOB, "out"))
        out.mkdir(parents=True)
        for name, raw in (("shot.png", png(b"\x30\x60\x90")), ("clip.webm", CLIP), ("data.json", b"{}"), ("gone.png", png(b"\x01\x01\x01"))):
            (out / name).write_bytes(raw)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["capture"], "research_id": "sr-media", "started": 1, "exit": 0})
        self.assertEqual(self.web("POST", "/research-state/research/sr-media", self.write("create-sr-media", {"title": "预览", "objective": "看图"}))[0], 201)
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-media", "title": "截图",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"],
            "sources": [{"name": "shot", "kind": "job_output", "job_id": JOB, "path": "shot.png"}]})[0], 201)
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-media", "--summary", "## 结论\n看图", "--artifact", f"job:{JOB}")
        self.assertEqual(code, 0, err)

    def loaded(self, page, scope, count):
        """Previews are read when they come near the screen: bring them there."""
        page.eval_on_selector_all(scope + " .media-preview", "e => e.forEach(x => x.scrollIntoView({block: 'center'}))")
        page.wait_for_function("([s, n]) => [...document.querySelectorAll(s + ' .media-preview img')].filter(i => i.complete && i.naturalWidth > 0).length === n",
                               arg=[scope, count])

    def test_pictures_and_videos_preview_wherever_a_page_links_them(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/out?path=gone.png*", lambda route, request: route.fulfill(status=503, json={"error": "execution node is unavailable"}))

            # The task page's 成果: a row of previews; a picture opens in the viewer.
            page.goto(base + "/dash/research?kind=research&id=sr-media")
            strip = ".summary-outputs .output-media"
            page.wait_for_selector(strip)
            self.loaded(page, strip, 1)
            self.assertEqual(page.locator(strip + " .media-preview.video video").count(), 1)
            page.wait_for_selector(strip + " .media-unread")
            page.click(strip + " button.media-preview")
            page.wait_for_selector("dialog.compare-viewer[open] img.viewer-media.shown")
            self.assertEqual(page.get_attribute("dialog.compare-viewer img.viewer-media", "alt"), "shot.png")
            page.keyboard.press("Escape")

            # The report's outputs: the video plays small under its row.
            page.goto(base + "/dash/research/report?id=sr-media")
            page.wait_for_selector('#outputs .output-file:has(> a[title="clip.webm"]) .media-preview.video video')
            self.assertEqual(page.locator('#outputs .output-file:has(> a[title="data.json"]) :is(.media-preview, .image-thumb)').count(), 0)

            # The data flow: each output picture and video.
            page.goto(base + "/dash/research/flow?id=sr-media")
            page.wait_for_selector("#flow-jobs .media-preview.video video")
            self.loaded(page, "#flow-jobs", 1)

            # A job's files: previews in the file list, and a picture opens in a new tab here.
            page.goto(base + f"/dash/jobs#job={JOB}")
            page.wait_for_selector(".artifact-item .media-preview.video video")
            self.loaded(page, ".artifact-item", 1)
            json_row = page.locator(".artifact-item", has_text="data.json")
            self.assertEqual(json_row.locator(".media-preview").count(), 0)
            page.locator(".artifact-item", has_text="gone.png").scroll_into_view_if_needed()
            page.locator(".artifact-item", has_text="gone.png").locator(".media-unread").wait_for()
            with page.expect_popup() as popup:
                page.locator(".artifact-item", has_text="shot.png").locator("button.media-preview").click()
            self.assertTrue(popup.value.url.startswith("blob:"))
            popup.value.close()

            # A result's registered original.
            page.goto(base + "/dash/results#result=r-media")
            page.wait_for_selector("#source-list .media-preview img")
            self.loaded(page, "#source-list", 1)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
