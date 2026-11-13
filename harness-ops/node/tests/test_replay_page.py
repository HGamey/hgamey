"""回放: a delivered task's page links to its replay. The replay shows one step at a time in three columns — what was
sent (a sentence, the request folded), what the platform answered, what the page showed (here a note's picture and a
blind comparison's two candidates) — moves with ←/→ and the time strip, keeps the step in its address, and on a phone
fits the screen. Watching it sends nothing but reads."""
import json
import re
import struct
import time
import unittest
import zlib
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

RID = "sr-replay-page"
COLUMNS = "() => [...document.querySelectorAll('#replay-step .tour-col h3')].map(h => h.textContent)"
SAY = "() => document.querySelector('#replay-step .replay-say')?.textContent"


def png(shade):
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([shade, 0x20, 0x30]) * 64) * 32)) + chunk(b"IEND", b""))


class ReplayPageTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, agent, pf = (core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web,
                                    core.CoreFlowTests.agent, core.CoreFlowTests.pf)
    write = staticmethod(core.CoreFlowTests.write)

    def task(self):
        """A task a person wrote, an agent took, ran on a phone, showed a picture of, put to a blind comparison and
        delivered."""
        self.assertEqual(self.web("POST", f"/research-state/research/{RID}", self.write(
            "create-replay-page", {"title": "回放页的任务", "objective": "看一遍"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", RID)[0], 0)
        ran = "cdcdcdcdcdcd"
        out = Path(self.jobs.path(ran, "out"))
        out.mkdir(parents=True)
        for name, shade in (("shot.png", 0x10), ("a.png", 0x60), ("b.png", 0xa0)):
            (out / name).write_bytes(png(shade))
        self.jobs._write_meta(ran, {"id": ran, "bin": "experiment", "argv": ["selfsr/repro.sh"], "label": "冒烟", "actor_id": "agent-a",
                                    "research_id": RID, "device_access": "write", "started": time.time(), "ended": time.time() + 600,
                                    "outcome": "succeeded", "exit": 0})
        for args in (("note", RID, "画面见图", "--evidence", f"job:{ran}:shot.png"),
                     ("compare", RID, "--item", f"job:{ran}:a.png", "--item", f"job:{ran}:b.png", "--pick", "1",
                      "--title", "哪张更清楚", "--question", "选更清楚的一张"),
                     ("deliver", RID, "--summary", "## 结论\n**画面**正常。", "--artifact", f"job:{ran}")):
            code, _, err = self.pf(token, "research", *args)
            self.assertEqual(code, 0, err)

    def test_a_delivered_task_plays_back_from_its_page(self):
        self.task()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            sent = []
            page.on("request", lambda request: sent.append((request.method, request.url)) if request.method != "GET" else None)
            page.goto(f"{base}/dash/research?kind=research&id={RID}")
            page.click("a.owner-jump:text-is('逐步回放')")
            page.wait_for_function(SAY + " === '新建任务：回放页的任务'")
            self.assertEqual(page.input_value("#replay-task"), RID)
            self.assertEqual(page.evaluate(COLUMNS), ["人在网页上发出的", "平台返回的", "网页上看到的"])
            self.assertIn(f"POST /dash/api/research-state/research/{RID}", page.text_content("#replay-step .tour-raw pre"))
            self.assertFalse(page.evaluate("() => document.querySelector('#replay-step .tour-raw').open"), "the request starts folded")

            page.keyboard.press("ArrowRight")
            page.wait_for_function(SAY + " === '认领这个任务'")
            self.assertEqual(page.evaluate(COLUMNS)[0], "Agent 发出的")
            self.assertIn("改了负责人", page.text_content("#replay-step .tour-col:nth-child(2)"))
            self.assertIn("step=2", page.url)

            # The note with its picture, reached from the time strip.
            note = page.evaluate("() => [...document.querySelectorAll('#replay-list button')].find(b => b.textContent.includes('画面见图')).dataset.n")
            page.click(f"#replay-strip button[data-n='{note}']")
            page.wait_for_function("() => document.querySelector('#replay-step .media-preview img')?.naturalWidth > 0")
            self.assertIn("在任务页看", page.text_content("#replay-step .tour-card"))

            page.keyboard.press("ArrowRight")
            page.wait_for_function(SAY + " === '发出盲评：哪张更清楚'")
            page.wait_for_function("() => [...document.querySelectorAll('#replay-step .replay-slot img')].filter(i => i.naturalWidth > 0).length === 2")
            self.assertEqual(page.evaluate("() => [...document.querySelectorAll('.replay-slot figcaption')].map(f => f.textContent)"), ["A", "B"])

            # Every step, not only the key ones: the job's end is there too.
            key = page.evaluate("() => document.querySelectorAll('#replay-strip button').length")
            page.uncheck("#replay-key")
            page.wait_for_function(f"() => document.querySelectorAll('#replay-strip button').length > {key}")
            self.assertIn("作业「冒烟」结束：成功", page.text_content("#replay-list"))

            page.reload()
            page.wait_for_function(SAY + " === '发出盲评：哪张更清楚'")

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("() => innerWidth === 390")
            self.assertLessEqual(page.evaluate("() => document.scrollingElement.scrollWidth"), 390)
        self.assertEqual(sent, [], "watching a replay sends nothing")

    def test_the_tour_page_leads_to_the_replay(self):
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/tour" % self.address)
            page.click("text=回放真实任务")
            page.wait_for_url(re.compile(r".*/dash/tour/replay.*"))
            page.wait_for_function("() => document.getElementById('replay-msg').textContent === '还没有交付过的任务'")


if __name__ == "__main__":
    unittest.main()
