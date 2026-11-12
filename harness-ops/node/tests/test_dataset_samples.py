"""Each dataset on the 数据集 page shows a sample under its name, taken from files it already has: a registered
low-res / high-res pair, a registered picture, or the first frame next to its frame manifest. A dataset whose files the
center does not hold shows none, and nothing new is asked of the agent that registered it."""
import shutil
import unittest
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path
from test_report_pictures import png

LOW, HIGH, FRAME, SHEET = png(b"\x10\x10\x10"), png(b"\xf0\xf0\xf0"), png(b"\x40\x80\xc0"), png(b"\x80\x40\x20")


class DatasetSampleTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def job(self, jid, files):
        for rel, raw in files.items():
            path = Path(self.jobs.path(jid, "out", *rel.split("/")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})

    def dataset(self, rid, name, pairing, sources):
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": rid, "title": rid,
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "labels": {}, "sources": sources,
            "dataset": {"name": name, "version": "v1", "pairing": pairing, "capture_method": "screencap", "frames": 2,
                        "scenes": 1, "splits": None, "size_bytes": 1000, "location": None}})[0], 201)

    def test_each_dataset_shows_a_sample_from_its_own_files(self):
        out = lambda name, jid, path: {"name": name, "kind": "job_output", "job_id": jid, "path": path}   # noqa: E731
        self.job("a1a1a1a1a1a1", {"pairs/lr/0001.png": LOW, "pairs/hr/0001.png": HIGH, "notes.txt": b"x"})
        self.dataset("ds-pair", "doom-pairs", "real", [out("notes", "a1a1a1a1a1a1", "notes.txt"),
                     out("hr-1", "a1a1a1a1a1a1", "pairs/hr/0001.png"), out("lr-1", "a1a1a1a1a1a1", "pairs/lr/0001.png")])
        self.job("b2b2b2b2b2b2", {"cap/frames/manifest.jsonl": b'{"i": 0, "file": "frame_00000.png"}\n',
                                  "cap/frames/frame_00000.png": FRAME, "cap/frames/frame_00001.png": HIGH})
        self.dataset("ds-sim", "gi-sim", "simulated", [out("frames", "b2b2b2b2b2b2", "cap/frames/manifest.jsonl")])
        aid = "7" * 32
        self.jobs.research.upload_attachment(aid, "contact_sheet.png", "image/png", SHEET)
        self.dataset("ds-sheet", "mc-sim", "simulated", [{"name": "contact", "kind": "attachment", "attachment_id": aid,
                                                          "filename": "contact_sheet.png", "local_path": "qa/contact_sheet.png"}])
        self.job("c3c3c3c3c3c3", {"cap/frames/manifest.jsonl": b"{}\n", "cap/frames/frame_00000.png": FRAME})
        self.dataset("ds-gone", "lost-frames", "simulated", [out("frames", "c3c3c3c3c3c3", "cap/frames/manifest.jsonl")])
        shutil.rmtree(self.jobs.path("c3c3c3c3c3c3", "out", "cap"))   # the center no longer holds its files

        code, value = self.web("GET", "/datasets/samples")
        self.assertEqual(code, 200, value)
        labels = {rid: [s["label"] for s in samples] for rid, samples in value["items"].items()}
        self.assertEqual(labels, {"ds-pair": ["低清", "高清"], "ds-sim": ["高清原图"], "ds-sheet": ["样例"], "ds-gone": []})
        self.assertIn("frame_00000.png", value["items"]["ds-sim"][0]["url"])

        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/datasets" % self.address)
            name = lambda text: page.locator(".ds-name", has_text=text)   # noqa: E731
            page.wait_for_selector(".ds-sample img")
            page.wait_for_function("() => [...document.querySelectorAll('.ds-sample img')].every(i => i.complete && i.naturalWidth === 2)")
            captions = lambda text: name(text).locator(".ds-sample figcaption").all_text_contents()   # noqa: E731
            self.assertEqual(captions("doom-pairs"), ["低清", "高清"])
            self.assertEqual(captions("gi-sim"), ["高清原图"])
            self.assertEqual(captions("mc-sim"), ["样例"])
            self.assertEqual(name("lost-frames").locator(".ds-sample").count(), 0)
            self.assertTrue(name("lost-frames").locator(".ds-samples").is_hidden())
            # The pair is the files themselves, in order.
            shown = page.evaluate("""async () => Promise.all([...document.querySelectorAll('.ds-name')].find(n => n.textContent.includes('doom-pairs'))
                .querySelectorAll('.ds-sample img').values().map(async i => [...new Uint8Array(await (await fetch(i.src)).arrayBuffer())].length))""")
            self.assertEqual(shown, [len(LOW), len(HIGH)])
            page.click('.ds-name:has-text("gi-sim") .ds-sample img')
            page.wait_for_selector("#zoom[open]")


if __name__ == "__main__":
    unittest.main()
