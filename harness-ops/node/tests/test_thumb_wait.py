"""A picture's thumbnail says 读取中… until its picture is in, on the report page and on the task page: an image with no
picture yet would draw as a broken picture to anyone who looks before it arrives."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_report_pictures as pictures


class ThumbWaitTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)
    fixture = pictures.ReportPicturesTests.fixture

    def held(self, page, url, thumb):
        """Open `url` with the picture's read held back: a waiting box and no image; let go, the picture and no box."""
        held = []
        page.route("**/dash/api/jobs/*/out?path=shots%2Fframe.png*", lambda route: held.append(route))
        page.goto(url)
        box = page.locator(thumb).first
        box.scroll_into_view_if_needed()
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(50)
        self.assertTrue(held, "the picture was asked for once near the screen")
        self.assertEqual(box.inner_text(), "读取中…")
        self.assertEqual(box.locator("img").count(), 0)
        held[0].continue_()
        page.wait_for_function("t => document.querySelector(t + ' img')?.naturalWidth > 0", arg=thumb)
        self.assertEqual(box.inner_text(), "")
        self.assertEqual(box.locator("img").get_attribute("alt"), "frame.png")

    def test_a_thumbnail_waits_in_a_plain_box(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            self.held(page, base + "/dash/research/report?id=sr-pic", '#outputs .output-file:has(> a[title="shots/frame.png"]) .image-thumb')
        with self._browser_page() as page:
            self.held(page, base + "/dash/research?kind=research&id=sr-pic", 'button.media-preview[aria-label="放大查看 frame.png"]')


if __name__ == "__main__":
    unittest.main()
