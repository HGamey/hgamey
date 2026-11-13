"""回放: the task picker shows a long task name whole, on a computer as on a phone."""
import unittest

import test_core_flow_web_cli as core   # puts the node directory on sys.path

TITLE = "原神超分方向：自研超分+插帧 vs 原生（RedMagic，平台复现）第二轮对比与长时间稳定性"
# How wide the chosen name is in the picker's own font, against the picker's width.
FITS = """() => { const s = document.getElementById('replay-task'), probe = document.createElement('span');
  probe.style.font = getComputedStyle(s).font; probe.style.whiteSpace = 'pre'; probe.textContent = s.selectedOptions[0].text;
  document.body.append(probe); const need = probe.getBoundingClientRect().width; probe.remove();
  return need < s.clientWidth; }"""


class ReplayPickTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, web = core.CoreFlowTests.request, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_a_long_task_name_shows_whole(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-long-name", self.write(
            "create-long-name", {"title": TITLE, "objective": "看名字"}))[0], 201)
        with self._browser_page() as page:
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 800})
                page.goto("http://%s:%d/dash/tour/replay?id=sr-long-name" % self.address)
                page.wait_for_selector("#replay-step .tour-cols")
                self.assertEqual(page.evaluate("() => document.getElementById('replay-task').selectedOptions[0].text"), TITLE)
                if width > 800:
                    self.assertTrue(page.evaluate(FITS), "the whole name fits the picker on a computer")
                self.assertLessEqual(page.evaluate("() => document.scrollingElement.scrollWidth"), width)


if __name__ == "__main__":
    unittest.main()
