"""Researcher reports F31/F32/F33, through the shipped HTTP and CLI paths."""
import http.client
import hashlib
import json
from pathlib import Path
import unittest

import test_core_flow_web_cli as fixture


class ResearchFrictionTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.CoreFlowTests()
        self.addCleanup(self.env.doCleanups)
        self.env.setUp()
        self.tokens = {}
        for name in ("agent-a", "claude-lead"):
            self.assertEqual(self.env.cli("POST", "/agents", {"name": name})[0], 201)
            self.tokens[name] = self.env.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        self.assertEqual(self.env.web("POST", "/research-state/research/sr-friction", self.env.write(
            "create-friction", {"title": "使用问题", "objective": "回归研究员实际操作"}))[0], 201)
        self.run_cli("agent-a", "claim", "sr-friction")

    def run_cli(self, actor, *args):
        code, out, err = self.env.pf(self.tokens[actor], "research", *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_an_agent_reply_closes_the_question_in_home_thread_and_checkin(self):
        question = self.run_cli("agent-a", "note", "sr-friction", "可以继续吗？", "--ask")["object"]["id"]
        self.assertEqual([row["id"] for row in self.env.board._questions()], [question])
        answer = self.run_cli("claude-lead", "note", "sr-friction", "继续", "--reply-to", question)["object"]["id"]
        self.run_cli("agent-a", "note", "sr-friction", "采纳", "--reply-to", answer, "--decision", "adopt")
        duties = self.env.duties(self.tokens["agent-a"])
        self.assertNotIn(question, [item["request_id"] for item in duties["waiting_on_human"]])
        self.assertEqual([item["id"] for item in duties["question_answers"]], [answer])
        self.assertEqual(self.env.board._questions(), [])
        _, thread = self.env.web("GET", "/research-state/note?research_id=sr-friction&limit=100")
        self.assertIs(next(row for row in thread["items"] if row["id"] == question)["question_unanswered"], False)

    def test_a_cancelled_job_is_named_as_cancelled_in_brief_and_checkin(self):
        self.env.duties(self.tokens["agent-a"])
        status, reply = self.env.agent(self.tokens["agent-a"], "POST", f"/jobs/{self.env.jid}/cancel", {})
        self.assertEqual(status, 200, reply)
        duties = self.env.agent(self.tokens["agent-a"], "GET", "/agents/agent-a/duties")[1]
        self.assertEqual(duties["jobs"]["finished_since_last_checkin"][0].get("outcome"), "cancelled")
        code, text, err = self.env.pf(self.tokens["agent-a"], "checkin")
        self.assertEqual(code, 0, err)
        self.assertIn("其中取消 1", text)
        self.assertIn(self.env.jid + " [cancelled]", text)
        code, text, err = self.env.pf(self.tokens["agent-a"], "jobs", "--brief")
        self.assertEqual(code, 0, err)
        self.assertIn(self.env.jid + "\tcancelled\t", text)

    def test_notes_attach_small_json_and_text_and_serve_the_original_bytes(self):
        for filename, content_type, raw in [("qa_report.json", "application/json", b'{"passed":true}\n'),
                                           ("qa_report.txt", "text/plain", "检查通过\n".encode())]:
            with self.subTest(filename=filename):
                path = self.env.tmp / filename
                path.write_bytes(raw)
                note = self.run_cli("agent-a", "note", "sr-friction", "检查报告", "--attach", str(path))["object"]["id"]
                _, thread = self.env.web("GET", "/research-state/note?research_id=sr-friction&limit=100")
                ref = next(row for row in thread["items"] if row["id"] == note)["evidence_refs"][0]
                self.assertEqual((ref["filename"], ref["content_type"]), (filename, content_type))
                conn = http.client.HTTPConnection(*self.env.address, timeout=5)
                self.addCleanup(conn.close)
                conn.request("GET", f"/dash/api/research-attachments/{ref['attachment_id']}",
                             headers={"Cookie": self.env.view_cookie, "X-Expected-Node-ID": self.env.node_id})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), raw)

    def test_the_note_page_offers_text_attachments_as_downloads(self):
        path = self.env.tmp / 'qa_report.json'
        path.write_text('{"passed": true}')
        note = self.run_cli('agent-a', 'note', 'sr-friction', '检查报告', '--attach', str(path))['object']['id']
        with self.env._browser_page() as page:
            from playwright.sync_api import expect
            page.goto('http://%s:%d/dash/research?kind=research&id=sr-friction' % self.env.address)
            block = page.locator('#note-' + note)
            expect(block).to_be_visible()
            expect(block.locator('img')).to_have_count(0)
            expect(block.locator('a[download="qa_report.json"]')).to_have_text('qa_report.json')

    def test_cli_reads_json_and_text_attachments_from_a_research_note(self):
        for filename in ('manifest.json', 'labels.txt'):
            with self.subTest(filename=filename):
                raw = json.dumps({'labels': 'x' * (2 << 20)}).encode()
                source = self.env.tmp / filename
                source.write_bytes(raw)
                note = self.run_cli('agent-a', 'note', 'sr-friction', '重建标签',
                                    '--attach', str(source))['object']['id']
                _, thread = self.env.web('GET', '/research-state/note?research_id=sr-friction&limit=100')
                ref = next(row for row in thread['items'] if row['id'] == note)['evidence_refs'][0]
                output = self.env.tmp / ('downloaded-' + filename)
                receipt = self.run_cli('agent-a', 'attachment', ref['attachment_id'], '--output', str(output))
                self.assertEqual(receipt['attachment_id'], ref['attachment_id'])
                self.assertEqual(receipt['node_id'], self.env.node_id)
                self.assertEqual(receipt['path'], str(output))
                self.assertEqual(receipt['mime'], ref['content_type'])
                self.assertEqual((receipt['size'], receipt['sha256']), (len(raw), hashlib.sha256(raw).hexdigest()))
                self.assertEqual(output.read_bytes(), raw)
                # Reuse media's existing publish behavior, including preserving the caller's file.
                output.write_bytes(b'caller-owned')
                code, _, _ = self.env.pf(self.tokens['agent-a'], 'research', 'attachment',
                                        ref['attachment_id'], '--output', str(output))
                self.assertEqual(code, 2)
                self.assertEqual(output.read_bytes(), b'caller-owned')

    def test_notes_attach_sixteen_mib_json_and_text_without_loading_them_in_the_page(self):
        for filename in ('manifest.json', 'labels.txt'):
            with self.subTest(filename=filename):
                # A valid JSON string is also a text file, exactly at the requested limit.
                raw = b'"' + b'x' * ((16 << 20) - 2) + b'"'
                path = self.env.tmp / filename
                path.write_bytes(raw)
                note = self.run_cli('agent-a', 'note', 'sr-friction', '数据集标签',
                                    '--attach', str(path))['object']['id']
                with self.env._browser_page() as page:
                    from playwright.sync_api import expect
                    attachment_reads = []
                    page.on('request', lambda request: attachment_reads.append(request.url)
                            if '/research-attachments/' in request.url else None)
                    page.goto('http://%s:%d/dash/research?kind=research&id=sr-friction' % self.env.address)
                    link = page.locator('#note-' + note).locator(f'a[download="{filename}"]')
                    expect(link).to_have_text(filename)
                    self.assertEqual(attachment_reads, [], 'opening the note must not load attachment contents')
                    with page.expect_download() as download:
                        link.click()
                    self.assertEqual(download.value.suggested_filename, filename)
                    self.assertEqual(Path(download.value.path()).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
