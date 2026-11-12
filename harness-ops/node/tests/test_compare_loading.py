"""Comparison files are read one at a time across a task page. A comparison on screen does not wait behind another's
video: pictures go before videos, and a long read gives way, going on later from where it was. A slot still waiting
says whether its file is queued or coming in."""
import json
import unittest
from pathlib import Path

import test_core_flow_web_cli as core
from test_report_pictures import png
from test_report_inline_media import CLIP


class CompareLoadingTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def compare(self, token, title, files):
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        args = ["research", "compare", "sr-load", "--title", title, "--question", "哪个好"]
        for name, raw in files.items():
            (output / name).write_bytes(raw)
            args += ["--item", f"job:{self.jid}:{name}"]
        code, out, err = self.pf(token, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)["request_id"]

    def test_pictures_on_screen_do_not_wait_behind_a_video(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-load", self.write(
            "create-sr-load", {"title": "加载顺序", "objective": "先看得到的"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-load")[0], 0)
        clips = self.compare(token, "两段录屏", {"a.webm": CLIP, "b.webm": CLIP + b"\0"})   # waiting: its videos come in by themselves
        shots = self.compare(token, "两张截图", {"a.png": png(b"\x10\x20\x30"), "b.png": png(b"\x30\x20\x10")})
        self.assertEqual(self.web("POST", "/research-state/feedback/f-shots", self.write("answer-shots", {
            "request_id": shots, "request_revision": 1, "text": "A", "evaluation": {
                "schema": "phonefarm/compare-feedback@1", "picked": ["A", "B"], "unable": False, "gaps": ["small"],
                "difference": None, "client_display": {"A": "decoded", "B": "decoded"}, "identity_revealed": False}}))[0], 201)

        held = []

        def media(route, request):
            # A video's long read (anything past its first byte) hangs, as over a slow link.
            if f"/{clips}/media/" in request.url and request.headers.get("range") != "bytes=0-0":
                held.append(route)
                return
            route.continue_()
        with self._browser_page() as page:
            page.route("**/media/*", media)
            page.goto("http://%s:%d/dash/research?kind=research&id=sr-load" % self.address)
            page.wait_for_function("() => document.querySelectorAll('.compare-media').length === 4")
            page.locator(f"#cmp-{shots}").scroll_into_view_if_needed()
            page.wait_for_function(f"() => [...document.querySelectorAll('#cmp-{shots} img')].filter(i => i.complete && i.naturalWidth > 0).length === 2",
                                   timeout=10000)
            # The videos are still coming in (one hangs), and their slots say so instead of staying blank.
            page.wait_for_function(f"() => [...document.querySelectorAll('#cmp-{clips} .compare-media')].map(m => m.dataset.wait).join() === '下载中,排队中'")
            self.assertTrue(held, "a video read is hanging")

            # A comparison that shows up while the video read hangs (here: after a refresh) cuts in; the video goes on
            # from where it was afterwards.
            later = self.compare(token, "又两张截图", {"c.png": png(b"\x50\x50\x50"), "d.png": png(b"\x60\x60\x60")})
            self.assertEqual(self.web("POST", "/research-state/feedback/f-later", self.write("answer-later", {
                "request_id": later, "request_revision": 1, "text": "B", "evaluation": {
                    "schema": "phonefarm/compare-feedback@1", "picked": ["B", "A"], "unable": False, "gaps": ["small"],
                    "difference": None, "client_display": {"A": "decoded", "B": "decoded"}, "identity_revealed": False}}))[0], 201)
            page.click("#refresh-detail")
            page.wait_for_selector(f"#cmp-{later}")
            page.locator(f"#cmp-{later}").scroll_into_view_if_needed()
            page.wait_for_function(f"() => [...document.querySelectorAll('#cmp-{later} img')].filter(i => i.complete && i.naturalWidth > 0).length === 2",
                                   timeout=10000)
            for route in held:
                try:
                    route.abort()
                except Exception:   # noqa: BLE001 - the page may have given up on it already
                    pass


if __name__ == "__main__":
    unittest.main()
