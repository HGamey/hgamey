"""A plain-HTTP entry to the center (frp, a LAN address) is not a secure context, so the browser there has no
crypto.randomUUID. The pages write there as they do over HTTPS: a blind comparison is answered and a note is posted."""
import re
import struct
import unittest
import zlib
from pathlib import Path

import test_core_flow_web_cli as core   # puts the node directory on sys.path

# What a page on a plain-HTTP entry sees: Crypto.randomUUID exists only in secure contexts (HTTPS, localhost).
PLAIN = "delete Crypto.prototype.randomUUID;"
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def png(rgb):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * 2) * 2)) + chunk(b"IEND", b""))


class PlainHTTPTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web
    write = staticmethod(core.CoreFlowTests.write)

    def fixture(self):
        """An agent's A/B comparison of two screenshots on a task."""
        jid = "cccccccccccc"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        (out / "native.png").write_bytes(png(b"\x10\x20\x30"))
        (out / "upscaled.png").write_bytes(png(b"\x30\x20\x10"))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-cmp", "title": "对比素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".png"} for name in ("native", "upscaled")]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-cmp", self.write(
            "create-cmp", {"title": "画质对比", "objective": "人评两张截图"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-cmp", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-cmp", self.write("create-ev-cmp", {
            "research_id": "sr-cmp", "type": "evaluation", "title": "同一帧的两种渲染", "need": "哪张更清楚？", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref("native"), ref("upscaled")],
            "evaluation": {"schema": "phonefarm/ab-evaluation@1", "media_kind": "image", "slots": {"A": 1, "B": 0}}}))[0], 201)

    def test_a_blind_comparison_is_answered_and_a_note_posted_without_crypto_random_uuid(self):
        self.fixture()
        base = "http://%s:%d" % self.address
        with self._browser_page() as page:
            name, value = self.steer_cookie.split("=", 1)
            page.context.add_cookies([{"name": name, "value": value, "url": base}])
            page.add_init_script(PLAIN)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/dash/research?kind=research&id=sr-cmp#cmp-ev-cmp")
            self.assertFalse(page.evaluate("Object.hasOwn(Crypto.prototype, 'randomUUID')"), "the page is as on a plain-HTTP entry")
            block = "#task-compare-open #cmp-ev-cmp"
            page.wait_for_selector(block)
            page.wait_for_function("s => [...document.querySelectorAll(s + ' figcaption span')].every(e => e.textContent === '2×2')", arg=block)
            page.click(block + " .chip-btn.letter >> text=B")
            page.click(block + " .compare-diff >> text=中")
            page.click(block + " button[type=submit]")
            done = "#task-compare-done #cmp-ev-cmp .compare-source"
            page.wait_for_function("d => document.querySelector(d) || /没有提交/.test(document.getElementById('cmp-ev-cmp')?.textContent || '')", arg=done)
            self.assertNotIn("没有提交", page.text_content("#cmp-ev-cmp"))
            answers = self.cli("GET", "/research-state/feedback?request_id=ev-cmp")[1]["items"]
            self.assertEqual(len(answers), 1)
            self.assertRegex(answers[0]["id"], r"^fb-[0-9a-f]{12}$")
            self.assertEqual(self.cli("GET", "/research-state/feedback/" + answers[0]["id"])[1]["data"]["text"], "B 更好 · 差别中")

            page.fill("#task-thread form.composer textarea", "明文入口上的留言")
            page.press("#task-thread form.composer textarea", "Enter")
            page.wait_for_function("() => /明文入口上的留言/.test(document.querySelector('#task-thread .discuss-list')?.textContent || '')"
                                   " || /发送失败/.test(document.getElementById('task-thread')?.textContent || '')")
            self.assertNotIn("发送失败", page.text_content("#task-thread"))
            notes = [self.cli("GET", "/research-state/note/" + item["id"])[1]["data"]["text"]
                     for item in self.cli("GET", "/research-state/note?research_id=sr-cmp")[1]["items"]]
            self.assertEqual(notes, ["明文入口上的留言"])

            # The stand-in is a version 4 UUID, a new one each time.
            ids = page.evaluate("[crypto.randomUUID(), crypto.randomUUID(), crypto.randomUUID()]")
            self.assertTrue(all(UUID4.match(i) for i in ids), ids)
            self.assertEqual(len(set(ids)), 3)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
