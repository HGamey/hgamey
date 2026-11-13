"""回放: what a job left shows under its end as its first pictures, found a few directories down; the directories of
one level are asked for together (all asked before the first answer), so a deep job costs its depth in round trips,
not its number of directories. A picture's preview is no wider than the picture."""
import struct
import time
import unittest
import zlib
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

RID = "sr-replay-media"
JOB = "efefefefefef"
LEVEL = ("a", "b", "c", "d")


def png(width, height):
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x30\x60\x90" * width) * height)) + chunk(b"IEND", b""))


class ReplayMediaTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, web = core.CoreFlowTests.request, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def test_a_jobs_pictures_are_found_level_by_level_and_previewed_at_their_size(self):
        self.assertEqual(self.web("POST", f"/research-state/research/{RID}", self.write(
            "create-replay-media", {"title": "输出很深的作业", "objective": "看图"}))[0], 201)
        out = Path(self.jobs.path(JOB, "out"))
        for path, data in (("a/one.txt", b"1"), ("b/two.txt", b"2"), ("c/deep/wide.png", png(1600, 400)), ("d/pic.png", png(64, 32))):
            (out / path).parent.mkdir(parents=True, exist_ok=True)
            (out / path).write_bytes(data)
        now = time.time()
        self.jobs._write_meta(JOB, {"id": JOB, "bin": "experiment", "argv": ["deep.sh"], "label": "深目录", "research_id": RID,
                                    "device_access": "write", "started": now, "ended": now + 60, "outcome": "succeeded", "exit": 0})
        events = []
        listing = lambda r: "/artifacts?" in r.url and any(f"path={d}&" in r.url for d in LEVEL)
        with self._browser_page() as page:
            page.on("request", lambda r: events.append("asked") if listing(r) else None)
            page.on("requestfinished", lambda r: events.append("answered") if listing(r) else None)
            page.goto("http://%s:%d/dash/tour/replay?id=%s&step=2" % (*self.address, RID))
            page.wait_for_function("() => [...document.querySelectorAll('#replay-step .media-preview img')].filter(i => i.naturalWidth > 0).length === 2")
            self.assertEqual(events[:len(LEVEL)], ["asked"] * len(LEVEL), events)
            wide = page.evaluate("""() => { const image = document.querySelector('#replay-step .media-preview img[alt="wide.png"]');
                                            return [image.closest('.media-preview').getBoundingClientRect().width, image.getBoundingClientRect().width]; }""")
            self.assertLessEqual(wide[0], wide[1] + 4, "the preview is as wide as its picture")


if __name__ == "__main__":
    unittest.main()
