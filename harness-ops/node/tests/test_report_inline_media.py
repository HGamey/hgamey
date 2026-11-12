"""Pictures in a report's text: a line of its own ![说明](job:<job>:<path>) or ![说明](attachment:<id>) shows the
picture there, as wide as the text, its caption under it, opening large in the viewer; mp4/webm play in place. A
picture that cannot be read says so in its place with its link. A summary of the report names the pictures only. An
agent uploads a picture of its own with research note --attach, which prints its ID, and lists it with
deliver --artifact attachment:<ID>; only attachments of the same research's notes are taken."""
import base64
import json
import secrets
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_report_pictures import GIF, png
import agent_duties

JOB = "e1e1e1e1e1e1"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#1F5FBF"/></svg>'
CLIP = base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQCEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggPs7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiECfQAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhxERJxo71UCZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAX14QDgibCBoLqBWpqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhxERJxo71UCWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDIuMDAwMDAwMDAwAB9DtnVCR+eBAKPWgQAAgPAFAJ0BKqAAWgAARwiFhYiFhIgCAgJ1qgP4AgaaE+CGqpNdxDqqTXcQ6qk13EOqpNdxDqqTXcQYAP7/TRL//FhX8WFfxYV/8WFf/PzO7cX85gCjmIEAZAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQDIABECAAEQEAAYABhYL/QACICBAAAAo5iBASwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEBkAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQH0ABECAAEQEAAYABhYL/QACICBAAAAo5iBAlgAEQIAARAQABgAGFgv9AAIgIEAAACjl4ECvADxAQABEBAUYABhYL/QACICBAAAo5iBAyAAEQIAARAQABgAGFgv9AAIgIEAAACjmIEDhAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQPoABECAAEQEAAYABhYL/QACICBAAAAo5iBBEwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEEsAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQUUABECAAEQEAAYABhYL/QACICBAAAAo5iBBXgAEQIAARAQABgAGFgv9AAIgIEAAACjmIEF3AARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQZAABECAAEQEAAYABhYL/QACICBAAAAo5iBBqQAEQIAARAQABgAGFgv9AAIgIEAAACjl4EHCADxAQABEBAUYABhYL/QACICBAAAo5iBB2wAEQIAARAQABgAGFgv9AAIgIEAAAAcU7trkbuPs4EAt4r3gQHxggGf8IED")


class ReportInlineMediaTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, pf = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.pf
    write = staticmethod(core.CoreFlowTests.write)

    def task(self, rid):
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write("create-" + rid, {"title": "帧率", "objective": "画图"}))[0], 201)

    def attach(self, rid, *files):
        """research note --attach: the IDs come back on stdout (JSON) and stderr."""
        paths = []
        for name, raw in files:
            path = self.tmp / name
            path.write_bytes(raw)
            paths += ["--attach", str(path)]
        code, out, err = self.pf(core.ADMIN, "research", "note", rid, "交付用图", *paths)
        self.assertEqual(code, 0, err)
        attached = json.loads(out)["attachments"]
        for item in attached:
            self.assertIn(f"→ attachment:{item['attachment_id']}", err)
            self.assertEqual(item["ref"], "attachment:" + item["attachment_id"])
        return [item["attachment_id"] for item in attached]

    def fixture(self):
        out = Path(self.jobs.path(JOB, "out"))
        out.mkdir(parents=True)
        for name, raw in (("fig.png", png(b"\x30\x60\x90")), ("anim.gif", GIF), ("chart.svg", SVG), ("clip.webm", CLIP), ("far.png", png(b"\x10\x10\x10"))):
            (out / name).write_bytes(raw)
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "custom", "argv": ["plot"], "research_id": "sr-fig", "started": 1, "exit": 0})
        self.task("sr-fig")
        [drawn] = self.attach("sr-fig", ("drawn.png", png(b"\x90\x30\x30")))
        summary = "\n".join(["## 结论", "第一段。", f"![帧率曲线](job:{JOB}:fig.png)", "中间一段。", f"![动图](job:{JOB}:anim.gif)",
                             f"![示意](job:{JOB}:chart.svg)", f"![自己画的图](attachment:{drawn})", f"![录像](job:{JOB}:clip.webm)",
                             f"![离线](job:{JOB}:far.png)", "最后一段。"])
        code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-fig", "--summary", summary, "--artifact", f"job:{JOB}",
                               "--artifact", f"attachment:{drawn}")
        self.assertEqual(code, 0, err)
        return drawn

    def test_an_agent_places_its_own_picture_by_the_attachment_id_its_note_printed(self):
        drawn = self.fixture()
        artifacts = self.cli("GET", "/research-state/research/sr-fig")[1]["data"]["deliverable"]["artifacts"]
        self.assertEqual([(a["kind"], a.get("filename")) for a in artifacts], [("job", None), ("attachment", "drawn.png")])
        self.assertEqual(artifacts[1]["attachment_id"], drawn)
        # An attachment of another research, or none at all, is refused before anything is written.
        self.task("sr-other")
        [elsewhere] = self.attach("sr-other", ("other.png", png(b"\x00\x00\x00")))
        revision = self.cli("GET", "/research-state/research/sr-fig")[1]["revision"]
        for summary, artifact in ((f"![别处的图](attachment:{elsewhere})", f"job:{JOB}"), ("正文", "attachment:" + secrets.token_hex(16)),
                                  ("正文", f"attachment:{elsewhere}")):
            code, _, err = self.pf(core.ADMIN, "research", "deliver", "sr-fig", "--summary", summary, "--artifact", artifact)
            self.assertNotEqual(code, 0, summary)
            self.assertIn("不是研究 sr-fig 里留言的附件", err)
        self.assertEqual(self.cli("GET", "/research-state/research/sr-fig")[1]["revision"], revision)
        # Where agents read how to deliver.
        code, out, _ = self.pf(core.ADMIN, "research", "--help")
        self.assertIn("![说明](attachment:<附件ID>)", out)
        self.assertIn("deliver --artifact attachment:<ID>", out)
        self.assertIn("![说明](attachment:<附件ID>)", agent_duties.DELIVERY_STYLE)
        self.assertIn("图和表放进正文讲到它的地方", agent_duties.DELIVERY_STYLE)

    def test_pictures_stand_where_the_text_names_them_and_a_summary_only_names_them(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/dash/api/jobs/*/out?path=far.png*", lambda route, request: route.fulfill(status=503, json={"error": "execution node is unavailable"}))
            page.set_viewport_size({"width": 1200, "height": 900})
            page.goto(base + "/dash/research/report?id=sr-fig")
            report = "#report .research-report"
            page.wait_for_selector(report + " figure.report-figure")
            # In the order of the text, each where it is named.
            self.assertEqual(page.eval_on_selector_all(report + " > *", "e => e.map(x => x.tagName === 'FIGURE' ? 'figure:' + x.querySelector('figcaption').textContent : x.tagName + ':' + x.textContent)"),
                             ["H2:结论", "P:第一段。", "figure:帧率曲线", "P:中间一段。", "figure:动图", "figure:示意", "figure:自己画的图",
                              "figure:录像", "figure:离线", "P:最后一段。"])
            figure = lambda caption: f"{report} > figure.report-figure:nth-of-type({['帧率曲线', '动图', '示意', '自己画的图', '录像', '离线'].index(caption) + 1})"   # noqa: E731
            loaded = "s => [...document.querySelectorAll(s)].filter(i => i.complete && i.naturalWidth > 0).length"
            page.wait_for_function(f"() => ({loaded})('{report} figure.report-figure img') === 4")
            width = page.eval_on_selector(report, "e => e.getBoundingClientRect().width")
            for shown in page.eval_on_selector_all(report + " figure.report-figure :is(img, video)", "e => e.map(x => x.getBoundingClientRect().width)"):
                self.assertAlmostEqual(shown, width, delta=2)
            video = figure("录像") + " video"
            self.assertTrue(page.eval_on_selector(video, "v => v.controls"))
            page.wait_for_function("s => document.querySelector(s)?.readyState >= 1", arg=video)
            unread = figure("离线") + " .report-unread"
            page.wait_for_selector(unread)
            self.assertIn("无法预览", page.text_content(unread))
            self.assertIn("path=far.png", page.get_attribute(unread + " a", "href"))
            # A picture opens large.
            page.click(figure("帧率曲线") + " button")
            page.wait_for_selector("dialog.compare-viewer[open] img.viewer-media.shown")
            self.assertEqual(page.get_attribute("dialog.compare-viewer img.viewer-media", "alt"), "帧率曲线")
            page.keyboard.press("Escape")
            page.wait_for_selector("dialog.compare-viewer", state="detached")

            # The task page's summary of the report names the pictures and shows none.
            page.goto(base + "/dash/research?kind=research&id=sr-fig")
            page.wait_for_selector(".summary-conclusion .research-report")
            conclusion = page.text_content(".summary-conclusion .research-report")
            for label in ("[图] 帧率曲线", "[图] 自己画的图", "[视频] 录像"):
                self.assertIn(label, conclusion)
            self.assertEqual(page.locator(".summary-conclusion :is(img, video, figure)").count(), 0)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
