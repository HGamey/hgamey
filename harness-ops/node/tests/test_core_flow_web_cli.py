"""The human-agent interaction chain must read back identically through the web page and the CLI.

Requests are shaped the way the shipped clients send them: dashboard calls carry only the
cookies and headers the page scripts set, token calls mirror pf-node. No workers or devices.
"""
import collections
import contextlib
import hashlib
import http.client
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import agent_duties
import dash
from http_server_fixture import start_http_server
from node_service import Jobs, make_handler
from agent_duties import NOTE_STYLE, DELIVERY_STYLE, COMPARE_DUTY, COMPARE_HELP, COLLABORATION

ADMIN = "core-flow-fixture-admin-token"
VIEW = "core-flow-view-password"
STEER = "core-flow-steer-password"
COMPARE_MAPPING_HINT = "字母和来源的对应不要写进留言、报告、方向或标签；人答完以后，页面和签到都会给出来源，不用另外记。"


def forbidden(*args, **kwargs):
    raise AssertionError("the interaction chain must not touch devices or workers")


class NoDevices:
    handle = request = forbidden

    def snapshot(self):
        return {"schema_version": 1, "devices": [], "lock": {"status": "unknown"},
                "sampled_at": None, "age_s": None}


class EndsEarly:
    """A stored file whose long read stops after `cuts[slot]` bytes, the way a dropped link ends a response."""

    def __init__(self, stream, slot, cuts):
        self.stream, self.slot, self.cuts = stream, slot, cuts

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *details):
        return self.stream.__exit__(*details)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size):
        if size == 1 or self.slot not in self.cuts:
            return self.stream.read(size)
        if not self.cuts[self.slot]:
            del self.cuts[self.slot]
            time.sleep(0.3)   # what was sent reaches the browser before the connection drops
            return b""
        data = self.stream.read(min(size, self.cuts[self.slot]))
        self.cuts[self.slot] -= len(data)
        return data


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        root = str(self.tmp / "node")
        self.jobs = Jobs(root, {"fixture": (sys.executable, root)}, data_dir=root)
        self.addCleanup(self.jobs._read_root.close)
        self.jobs.dispatch.start = lambda: None
        self.jobs.leases.snapshot = lambda **kw: {"status": "clear", "error": None}
        board = dash.Dash.__new__(dash.Dash)
        board._pass = lambda: VIEW
        board._steer_pass = lambda: STEER
        board.job_service = self.jobs
        board.last_view, board.wake, board.monitor = 0, threading.Event(), NoDevices()
        board.showcase_dir = str(self.tmp / "showcase")
        self.board = board
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(NoDevices(), self.jobs, ADMIN, board))
        thread = start_http_server(server)

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        self.address = server.server_address
        self.node_id = self.jobs.games.node_id
        self.view_cookie = f"{dash.COOKIE}={board._cookie_val(VIEW)}"
        self.steer_cookie = f"{dash.STEER_COOKIE}={board._cookie_val(STEER, b'hgamey-steer-v1')}"
        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                               "resources": [], "actor_id": "agent-a"}, b"", "core-flow-job")
        self.jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]

    def _large_comparison(self, rid, kinds, title="哪个更清楚", need="选 1 个"):
        """A comparison (`kinds`: slot → media kind) of real media followed by noise, long enough for a read to take
        many writes, waiting on the page of task sr-<rid>. Returns the files by slot and the page's path."""
        import base64, struct, zlib

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x10\x20\x30" * 64) * 32)) + chunk(b"IEND", b""))
        clip = base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQCEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggPs7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiECfQAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhxERJxo71UCZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAX14QDgibCBoLqBWpqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhxERJxo71UCWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDIuMDAwMDAwMDAwAB9DtnVCR+eBAKPWgQAAgPAFAJ0BKqAAWgAARwiFhYiFhIgCAgJ1qgP4AgaaE+CGqpNdxDqqTXcQ6qk13EOqpNdxDqqTXcQYAP7/TRL//FhX8WFfxYV/8WFf/PzO7cX85gCjmIEAZAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQDIABECAAEQEAAYABhYL/QACICBAAAAo5iBASwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEBkAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQH0ABECAAEQEAAYABhYL/QACICBAAAAo5iBAlgAEQIAARAQABgAGFgv9AAIgIEAAACjl4ECvADxAQABEBAUYABhYL/QACICBAAAo5iBAyAAEQIAARAQABgAGFgv9AAIgIEAAACjmIEDhAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQPoABECAAEQEAAYABhYL/QACICBAAAAo5iBBEwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEEsAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQUUABECAAEQEAAYABhYL/QACICBAAAAo5iBBXgAEQIAARAQABgAGFgv9AAIgIEAAACjmIEF3AARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQZAABECAAEQEAAYABhYL/QACICBAAAAo5iBBqQAEQIAARAQABgAGFgv9AAIgIEAAACjl4EHCADxAQABEBAUYABhYL/QACICBAAAo5iBB2wAEQIAARAQABgAGFgv9AAIgIEAAAAcU7trkbuPs4EAt4r3gQHxggGf8IED")
        jid = "abababababab"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        files, sources = {}, []
        for slot, kind in kinds.items():
            files[slot] = (clip if kind == "video" else png) + os.urandom(3 << 20)
            name = f"{rid}-{slot}" + (".webm" if kind == "video" else ".png")
            (out / name).write_bytes(files[slot])
            sources.append({"name": slot, "kind": "job_output", "job_id": jid, "path": name})
        result = "r-" + rid
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": result, "title": "下载素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": sources})[0], 201)
        research = "sr-" + rid
        self.assertEqual(self.web("POST", f"/research-state/research/{research}", self.write(
            "create-" + research, {"title": "大文件盲评", "objective": "断了也能下完"}))[0], 201)
        self.assertEqual(self.cli("POST", f"/research-state/request/{rid}", self.write("create-" + rid, {
            "research_id": research, "type": "evaluation", "title": title, "need": need, "why": "fixture",
            "context": {"research_revision": 1, "directions": []},
            "evidence_refs": [{"kind": "result_source", "node_id": self.node_id, "result_id": result, "source_name": slot} for slot in kinds],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": kind} for i, kind in enumerate(kinds.values())], "pick": 1}}))[0], 201)
        return files, f"/dash/research?kind=research&id={research}"

    def _reads_ending_early(self, cuts):
        """Long reads of the slots in `cuts` end after that many bytes, once."""
        real = self.jobs.research.media

        def media(oid, slot, revision):
            stream, info = real(oid, slot, revision)
            return EndsEarly(stream, slot, cuts), info
        return mock.patch.object(self.jobs.research, "media", media)

    @contextlib.contextmanager
    def _browser_page(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": "http://%s:%d" % self.address}])
                yield context.new_page()
            finally:
                browser.close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def web(self, method, path, body=None, steer=True, hg=True):
        """What HGTarget.fetch sends from a logged-in page (view cookie, optional steer unlock)."""
        headers = {"Cookie": self.view_cookie + (f"; {self.steer_cookie}" if steer else ""),
                   "X-Expected-Node-ID": self.node_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
            if hg:
                headers["X-HG"] = "1"
        return self.request(method, "/dash/api" + path, body, headers)

    def cli(self, method, path, body=None):
        """What pf-node sends with an explicit target and the admin token."""
        headers = {"Authorization": f"Bearer {ADMIN}", "X-Expected-Node-ID": self.node_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.request(method, path, body, headers)

    def agent(self, token, method, path, body=None):
        """What pf-node sends when it holds an agent's own token."""
        headers = {"Authorization": f"Bearer {token}", "X-Expected-Node-ID": self.node_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.request(method, path, body, headers)

    def pf(self, token, *args):
        """The real pf-node CLI, holding `token` in a private file."""
        token_file = self.tmp / ("token-" + token[-8:])
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PF_")}
        env.update(PF_NODE_URL="http://%s:%d" % self.address, PF_NODE_TOKEN_FILE=str(token_file), PF_NODE_ID=self.node_id)
        done = subprocess.run([sys.executable, str(NODE / "pf-node"), *args], env=env, capture_output=True,
                              text=True, timeout=60, cwd=self.tmp)
        return done.returncode, done.stdout, done.stderr

    def duties(self, token, rules=False):
        args = ("checkin", "--rules", "--json") if rules else ("checkin", "--json")
        code, out, err = self.pf(token, *args)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_dashboard_can_prepare_and_revoke_agent_invitation_with_steer(self):
        body = {"url": "https://node.example", "note": "web invitation"}
        self.assertEqual(self.web("POST", "/agent-invites", body, steer=False)[0], 403)
        self.assertEqual(self.web("POST", "/agent-invites", body, hg=False)[0], 400)
        status, invited = self.web("POST", "/agent-invites", body)
        self.assertEqual(status, 201, invited)
        for fact in [body["url"], self.node_id, invited["code"], "./bin/pf-node checkin"]:
            self.assertIn(fact, invited["prompt"])
        self.assertEqual(self.web("GET", "/agent-invites", steer=False)[0], 403)
        status, listed = self.web("GET", "/agent-invites")
        self.assertEqual(status, 200, listed)
        self.assertEqual([item["id"] for item in listed["invites"]], [invited["id"]])
        self.assertEqual(self.web("DELETE", "/agent-invites/" + invited["id"], {})[0], 200)
        self.assertEqual(self.web("GET", "/agent-invites")[1]["invites"], [])

    def test_home_page_invites_an_agent_and_revokes_unused_invitations(self):
        """接入新 agent: one click shows the whole first prompt with a copy button and lists the unused invitation."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")

        class Quiet:   # the home page's phone sampling, without phones
            def request(self, **kw):
                return None

            def snapshot(self):
                return {"devices": [], "lock": {"status": "free", "lease": {"entries": []}}, "age_s": 1, "error": None, "deferred": False}

        class Goals:
            def list(self):
                return []

            def harness_support(self):
                return {"ok": False}
        self.board.monitor, self.board.goals, self.board.shot_dir = Quiet(), Goals(), str(self.tmp)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash")
                page.wait_for_function("() => document.querySelector('main.home')?.getAttribute('aria-busy') === 'false'")
                page.click("#invite-open")
                page.wait_for_selector("#invite-prompt:not(:empty)")
                listed = self.web("GET", "/agent-invites")[1]["invites"]
                self.assertEqual(len(listed), 1)
                prompt = page.inner_text("#invite-prompt")
                for fact in (self.node_id, "hginv_", "pf-node join --invite", "./bin/pf-node checkin"):
                    self.assertIn(fact, prompt)
                self.assertTrue(page.is_visible("#invite-copy"))
                page.wait_for_selector(f'#invite-list li[data-invite="{listed[0]["id"]}"]')
                page.click(f'#invite-list [data-revoke="{listed[0]["id"]}"]')
                page.wait_for_function("() => !document.querySelector('#invite-list [data-revoke]')")
                self.assertEqual(self.web("GET", "/agent-invites")[1]["invites"], [])
            finally:
                browser.close()

    @staticmethod
    def write(operation, data):
        return {"schema": "phonefarm/research-write@1", "operation_id": operation, "base_revision": None,
                "data": data, "actor_label": None, "reason": None}

    def test_cached_fleet_returns_without_device_work_or_handler_exception(self):
        code, result = self.cli("GET", "/fleet?cached=1")
        self.assertEqual(code, 200)
        self.assertEqual(result["devices"], [])
        self.assertEqual(result["node_id"], self.node_id)

    def test_guidance_response_status_matches_the_delivery_gate(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-guidance", self.write(
            "web-guidance-task", {"title": "指导回应", "objective": "网页状态与交付闸门一致"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-guidance")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-guide", self.write(
            "web-guide", {"research_id": "sr-guidance", "text": "先做 1080p", "reply_to": None, "decision": None}))[0], 201)

        def notes():
            status, value = self.web("GET", "/research-state/note?research_id=sr-guidance&limit=100")
            self.assertEqual(status, 200)
            return {item["id"]: item for item in value["items"]}

        self.assertIs(notes()["n-guide"]["unanswered"], True)
        code, _, err = self.pf(token, "research", "note", "sr-guidance", "采纳，先做 1080p",
                               "--reply-to", "n-guide", "--decision", "adopt")
        self.assertEqual(code, 0, err)
        self.assertIs(notes()["n-guide"]["unanswered"], False)
        self.assertEqual(self.web("POST", "/research-state/note/n-later", self.write(
            "web-later", {"research_id": "sr-guidance", "text": "不要动 HUD", "reply_to": None, "decision": None}))[0], 201)
        current = notes()
        self.assertIs(current["n-guide"]["unanswered"], False)
        self.assertIs(current["n-later"]["unanswered"], True)
        code, _, err = self.pf(token, "research", "deliver", "sr-guidance", "--summary", "完成",
                               "--artifact", f"job:{self.jid}")
        self.assertNotEqual(code, 0)
        self.assertIn("research_guidance_unanswered", err)
        code, _, err = self.pf(token, "research", "note", "sr-guidance", "暂缓 HUD 改动",
                               "--reply-to", "n-later", "--decision", "defer")
        self.assertEqual(code, 0, err)
        self.assertIs(notes()["n-later"]["unanswered"], False)
        self.assertEqual(self.pf(token, "research", "deliver", "sr-guidance", "--summary", "完成",
                                 "--artifact", f"job:{self.jid}")[0], 0)

    def test_agent_questions_reach_home_and_checkin_then_clear_on_human_reply(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-questions", self.write(
            "web-question-task", {"title": "问人流程", "objective": "异步问题在首页可见并回到 agent"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-questions")[0], 0)
        code, out, err = self.pf(token, "research", "note", "sr-questions", "先测 1080p 还是 720p？", "--ask")
        self.assertEqual(code, 0, err)
        question_id = json.loads(out)["object"]["id"]
        _, thread = self.web("GET", "/research-state/note?research_id=sr-questions&limit=100")
        self.assertIs(thread["items"][0]["ask"], True)
        self.assertIs(thread["items"][0]["question_unanswered"], True)

        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        self.assertEqual([(item["id"], item["research_title"]) for item in board._questions()],
                         [(question_id, "问人流程")])
        self.assertEqual(board._questions()[0]["url"],
                         f"/dash/research?kind=research&id=sr-questions#note-{question_id}")
        pending = self.duties(token)
        self.assertIn(question_id, [item["request_id"] for item in pending["waiting_on_human"]])
        self.assertEqual(pending["question_answers"], [])

        self.assertEqual(self.web("POST", "/research-state/note/n-human-answer", self.write(
            "web-human-answer", {"research_id": "sr-questions", "text": "先测 1080p",
                                  "reply_to": question_id, "decision": None}))[0], 201)
        _, thread = self.web("GET", "/research-state/note?research_id=sr-questions&limit=100")
        question = next(item for item in thread["items"] if item["id"] == question_id)
        self.assertIs(question["question_unanswered"], False)
        self.assertEqual(board._questions(), [])
        answered = self.duties(token)
        self.assertNotIn(question_id, [item["request_id"] for item in answered["waiting_on_human"]])
        self.assertEqual([(item["reply_to"], item["text"], item["unread"]) for item in answered["question_answers"]],
                         [(question_id, "先测 1080p", True)])

    def test_admin_reply_closes_agent_question(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-admin-question", self.write(
            "create-admin-question", {"title": "管理员回答", "objective": "平台问题由维护者处理"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-admin-question")[0], 0)
        code, out, err = self.pf(token, "research", "note", "sr-admin-question", "平台接下来怎么做？", "--ask")
        self.assertEqual(code, 0, err)
        question_id = json.loads(out)["object"]["id"]
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        self.assertEqual(len(board._questions()), 1)
        self.assertEqual(self.cli("POST", "/research-state/note/n-admin-answer", self.write(
            "admin-answer", {"research_id": "sr-admin-question", "text": "按现有方向继续", "reply_to": question_id,
                              "decision": None}))[0], 201)
        self.assertEqual(board._questions(), [])
        _, thread = self.web("GET", "/research-state/note?research_id=sr-admin-question&limit=10")
        question = next(item for item in thread["items"] if item["id"] == question_id)
        self.assertIs(question["question_unanswered"], False)
        self.assertEqual([(item["reply_to"], item["text"]) for item in self.duties(token)["question_answers"]],
                         [(question_id, "按现有方向继续")])

    def test_direction_job_links_do_not_require_registered_results(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-direction", self.write(
            "create-direction-task", {"title": "方向运行", "objective": "无结果也显示运行"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-other", self.write(
            "create-other-task", {"title": "其他研究", "objective": "方向不能串研究"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-direction")[0], 0)
        self.assertEqual(self.pf(token, "research", "claim", "sr-other")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/direction/d-direction", self.write(
            "create-direction", {"research_id": "sr-direction", "title": "方向 A", "hypothesis": "验证"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-other", self.write(
            "create-other-direction", {"research_id": "sr-other", "title": "方向 B", "hypothesis": "不属于"}))[0], 201)
        code, jid, err = self.pf(token, "--research", "sr-direction", "--direction", "d-direction",
                                 "--device-access", "none", "--detach", "fixture", "-c", "pass")
        self.assertEqual(code, 0, err)
        status = self.cli("GET", f"/jobs/{jid.strip()}")[1]
        self.assertEqual((status["research_id"], status["direction_id"]), ("sr-direction", "d-direction"))
        code, _, err = self.pf(token, "--research", "sr-direction", "--direction", "d-other",
                               "--device-access", "none", "--detach", "fixture", "-c", "pass")
        self.assertNotEqual(code, 0)
        self.assertIn("direction_id", err)
        script = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        self.assertIn("job.direction_id === direction.id", script)

    def test_direction_based_on_reads_and_validates_chain_time_and_job(self):
        def research(rid, follows=None):
            data = {"title": rid, "objective": "记录实验继承"}
            if follows is not None:
                data["follows"] = follows
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, data))[0], 201)

        def direction(rid, did):
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write(
                "create-" + did, {"research_id": rid, "title": did, "hypothesis": "验证继承关系"}))[0], 201)
            status, record = self.web("GET", f"/research-state/direction/{did}")
            self.assertEqual(status, 200)
            return record

        def update(record, operation, based_on):
            payload = self.write(operation, {**record["data"], "based_on": based_on})
            payload.update(base_revision=record["revision"], reason="记录方向继承")
            return self.web("POST", f"/research-state/direction/{record['id']}", payload)

        research("sr-base")
        research("sr-next", "sr-base")
        research("sr-unrelated")
        base = direction("sr-base", "d-base")
        other = direction("sr-next", "d-other")
        direction("sr-unrelated", "d-unrelated")
        current = direction("sr-next", "d-current")
        direction("sr-next", "d-later")
        self.assertIsNone(base["data"]["based_on"])

        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                               "resources": [], "actor_id": "agent-a", "research_id": "sr-base",
                                               "direction_id": "d-base"}, b"", "based-on-job")
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]

        status, changed = update(current, "based-on-update", {"direction_id": "d-base", "job_id": None})
        self.assertEqual(status, 201, changed)
        current = self.web("GET", "/research-state/direction/d-current")[1]
        self.assertEqual(current["data"]["based_on"], {"direction_id": "d-base", "job_id": None})
        status, changed = update(current, "based-on-change", {"direction_id": "d-other", "job_id": None})
        self.assertEqual(status, 201, changed)
        current = self.web("GET", "/research-state/direction/d-current")[1]
        self.assertEqual(current["data"]["based_on"], {"direction_id": "d-other", "job_id": None})
        status, failure = update(current, "based-on-later", {"direction_id": "d-later", "job_id": None})
        self.assertEqual((status, failure["code"]), (400, "invalid_research"))

        for did, based_on in (
                ("d-cross-chain", {"direction_id": "d-unrelated", "job_id": None}),
                ("d-wrong-job", {"direction_id": "d-other", "job_id": jid})):
            with self.subTest(rejected=did):
                status, failure = self.web("POST", f"/research-state/direction/{did}", self.write(
                    "create-" + did, {"research_id": "sr-next", "title": did, "hypothesis": "拒绝无效继承",
                                       "based_on": based_on}))
                self.assertEqual((status, failure["code"]), (400, "invalid_research"))
                self.assertEqual(self.web("GET", f"/research-state/direction/{did}")[0], 404)

        source = self.tmp / "based-on.json"
        source.write_text(json.dumps({"research_id": "sr-next", "title": "C 之后继续", "hypothesis": "以选中的 C 为基线"}))
        code, out, err = self.pf(ADMIN, "research", "direction", "add", "d-continuation", str(source),
                                 "--based-on", f"d-base:{jid}")
        self.assertEqual(code, 0, err)
        code, out, err = self.pf(ADMIN, "research", "show", "direction", "d-continuation")
        self.assertEqual(code, 0, err)
        expected = {"direction_id": "d-base", "job_id": jid}
        self.assertEqual(json.loads(out)["data"]["based_on"], expected)
        status, listing = self.web("GET", "/research-state/direction?research_id=sr-next&limit=100")
        self.assertEqual(status, 200)
        self.assertEqual(next(item for item in listing["items"] if item["id"] == "d-continuation")["based_on"], expected)

        code, out, err = self.pf(ADMIN, "research", "direction", "add", "d-independent", str(source))
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.web("GET", "/research-state/direction/d-independent")[1]["data"]["based_on"])
        self.assertIn("--based-on", self.pf(ADMIN, "research", "--help")[1])
        self.assertTrue(any("--based-on" in step for step in agent_duties.LOOP))

    def test_research_update_merges_only_supplied_fields(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-partial", self.write(
            "create-partial", {"title": "部分更新", "objective": "补继承不能清掉结论和证据"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-partial-base", self.write(
            "create-partial-base", {"research_id": "sr-partial", "title": "基线", "hypothesis": "先得到基线"}))[0], 201)
        experiment = {"title": "复测", "plan": "同场景复测并保留原始证据", "research_revision": 1}
        self.assertEqual(self.web("POST", "/research-state/direction/d-partial-target", self.write(
            "create-partial-target", {"research_id": "sr-partial", "title": "后续", "hypothesis": "在基线上继续",
                                       "next_experiment": experiment}))[0], 201)
        output = Path(self.jobs.path(self.jid, "out"))
        (output / "verified.json").write_text('{"valid":true}')
        code, out, err = self.pf(ADMIN, "research", "conclude", "d-partial-target", "verified",
                                 "--reason", "证据已复核", "--evidence", f"job:{self.jid}:verified.json")
        self.assertEqual(code, 0, err)
        concluded = self.cli("GET", "/research-state/direction/d-partial-target")[1]

        patch = {"based_on": {"direction_id": "d-partial-base", "job_id": None}}
        source = self.tmp / "partial-update.json"
        source.write_text(json.dumps(patch))
        code, out, err = self.pf(ADMIN, "research", "update", "direction", "d-partial-target", str(source),
                                 "--revision", str(concluded["revision"]), "--reason", "补写继承",
                                 "--operation-id", "partial-direction")
        self.assertEqual(code, 0, err)
        receipt = json.loads(out)
        self.assertEqual(receipt["changed_fields"], ["based_on"])
        updated = self.cli("GET", "/research-state/direction/d-partial-target")[1]
        for field in ("state", "state_reason", "next_experiment", "evidence_refs"):
            self.assertEqual(updated["data"][field], concluded["data"][field], field)
        self.assertEqual(updated["data"]["based_on"], patch["based_on"])
        self.assertEqual(updated["data"]["evidence_refs"][0]["path"], "verified.json")

        # Replaying the original operation still merges against its base revision, after the object has advanced.
        envelope = self.write("partial-direction", patch)
        envelope.update(base_revision=concluded["revision"], reason="补写继承")
        self.assertEqual(self.cli("POST", "/research-state/direction/d-partial-target", envelope), (201, receipt))

        source.write_text(json.dumps({"based_on": None}))
        code, out, err = self.pf(ADMIN, "research", "update", "direction", "d-partial-target", str(source),
                                 "--revision", str(updated["revision"]), "--reason", "显式取消继承")
        self.assertEqual(code, 0, err)
        cleared = self.cli("GET", "/research-state/direction/d-partial-target")[1]
        self.assertIsNone(cleared["data"]["based_on"])
        self.assertEqual(cleared["data"]["state"], "verified")
        source.write_text(json.dumps({"title": None}))
        code, _, _ = self.pf(ADMIN, "research", "update", "direction", "d-partial-target", str(source),
                             "--revision", str(cleared["revision"]), "--reason", "必填字段不能清空")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.cli("GET", "/research-state/direction/d-partial-target")[1], cleared)

        self.assertEqual(self.web("POST", "/research-state/research/sr-partial-task", self.write(
            "create-partial-task", {"title": "已交付任务", "objective": "改标题不改变交付状态"}))[0], 201)
        self.assertEqual(self.pf(ADMIN, "research", "deliver", "sr-partial-task", "--summary", "完成",
                                 "--artifact", f"job:{self.jid}")[0], 0)
        delivered = self.cli("GET", "/research-state/research/sr-partial-task")[1]
        source.write_text(json.dumps({"title": "改后的标题"}))
        code, out, err = self.pf(ADMIN, "research", "update", "research", "sr-partial-task", str(source),
                                 "--revision", str(delivered["revision"]), "--reason", "只改标题")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["changed_fields"], ["title"])
        renamed = self.cli("GET", "/research-state/research/sr-partial-task")[1]["data"]
        self.assertEqual((renamed["title"], renamed["state"], renamed["objective"], renamed["deliverable"]),
                         ("改后的标题", "delivered", delivered["data"]["objective"], delivered["data"]["deliverable"]))
        self.assertIn("update FILE 只写要改的字段", self.pf(ADMIN, "research", "--help")[1])

    def test_closing_a_comparison_by_research_command_gives_request_update_syntax(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-close-guide", self.write(
            "create-close-guide", {"title": "关闭旧盲评", "objective": "指出请求关闭命令"}))[0], 201)
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        for name in ("a", "b"):
            (output / (name + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-close-guide",
            "title": "关闭指引素材", "sources": [
                {"name": name, "kind": "job_output", "job_id": self.jid, "path": name + ".png"} for name in ("a", "b")]})[0], 201)
        refs = [{"kind": "result_source", "node_id": self.node_id, "result_id": "r-close-guide", "source_name": name}
                for name in ("a", "b")]
        request = {"research_id": "sr-close-guide", "type": "evaluation", "title": "旧盲评", "need": "排序", "why": "fixture",
                   "context": {"research_revision": 1, "directions": []}, "evidence_refs": refs,
                   "evaluation": {"schema": "phonefarm/compare@1", "pick": 2,
                                  "items": [{"ref": 0, "media_kind": "image"}, {"ref": 1, "media_kind": "image"}]}}
        created = self.web("POST", "/research-state/request/ev-close-guide", self.write("create-close-guide-request", request))
        self.assertEqual(created[0], 201, created)
        code, _, err = self.pf(ADMIN, "research", "close", "ev-close-guide", "--reason", "新版替代")
        self.assertNotEqual(code, 0, err)
        self.assertIn("research update request ev-close-guide FILE", err)
        self.assertIn('{"state":"closed","close_reason":"…"}', err)

    def test_request_close_reason_error_says_state_must_change_too(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-close-reason", self.write(
            "create-close-reason", {"title": "请求关闭字段", "objective": "同时写状态和原因"}))[0], 201)
        request = {"research_id": "sr-close-reason", "type": "guidance", "title": "旧请求", "need": "选择", "why": "fixture",
                   "context": {"research_revision": 1, "directions": []}}
        self.assertEqual(self.web("POST", "/research-state/request/ev-close-reason",
                                  self.write("create-close-reason-request", request))[0], 201)
        source = self.tmp / "close-reason-only.json"
        source.write_text(json.dumps({"close_reason": "新版替代"}))
        code, _, err = self.pf(ADMIN, "research", "update", "request", "ev-close-reason", str(source),
                               "--revision", "1", "--reason", "关闭旧请求")
        self.assertNotEqual(code, 0, err)
        self.assertIn("state=closed", err)
        self.assertIn("close_reason", err)

    def test_update_error_names_the_missing_deliverable_summary(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-deliverable-field", self.write(
            "create-deliverable-field", {"title": "交付字段定位", "objective": "指出缺失的总结字段"}))[0], 201)
        source = self.tmp / "artifacts-only.json"
        source.write_text(json.dumps({"deliverable": {"artifacts": [
            {"kind": "job", "job_id": self.jid, "node_id": self.node_id}]}}))
        code, _, err = self.pf(ADMIN, "research", "update", "research", "sr-deliverable-field", str(source),
                               "--revision", "1", "--reason", "只补产物")
        self.assertNotEqual(code, 0, err)
        self.assertIn("deliverable.summary", err)

    def test_changed_loop_step_is_marked_new_once_per_agent_in_json_and_text(self):
        tokens = {}
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        first_json = self.duties(tokens["agent-a"])
        first_text = self.pf(tokens["agent-b"], "checkin")
        self.assertEqual(first_text[0], 0, first_text[2])
        self.assertFalse(any(step.startswith("[新]") for step in first_json["loop"]))
        self.assertNotIn("[新]", first_text[1].split("每轮做法：", 1)[1])
        self.assertIn(agent_duties.LOOP[0], first_text[1])

        repeated_json = self.duties(tokens["agent-a"])
        self.assertEqual(repeated_json["loop"], [])
        self.assertIn("checkin --rules", repeated_json["loop_hint"])
        repeated_text = self.pf(tokens["agent-b"], "checkin")
        self.assertEqual(repeated_text[0], 0, repeated_text[2])
        self.assertNotIn(agent_duties.LOOP[0], repeated_text[1])
        self.assertIn("每轮做法：用 pf-node checkin --rules 查看全文。", repeated_text[1])
        self.assertEqual(self.duties(tokens["agent-a"], rules=True)["loop"], agent_duties.LOOP)
        rules_text = self.pf(tokens["agent-b"], "checkin", "--rules")
        self.assertEqual(rules_text[0], 0, rules_text[2])
        self.assertIn(agent_duties.LOOP[0], rules_text[1])

        changed_loop = list(agent_duties.LOOP)
        changed_loop[5] += "（已修改）"
        with mock.patch.object(agent_duties, "LOOP", changed_loop):
            changed_json = self.duties(tokens["agent-a"])
            self.assertEqual(changed_json["loop"][5], "[新] " + changed_loop[5])
            self.assertEqual([step for step in changed_json["loop"] if step.startswith("[新]")],
                             [changed_json["loop"][5]])
            self.assertFalse(any(step.startswith("[新]") for step in self.duties(tokens["agent-a"], rules=True)["loop"]))

            changed_text = self.pf(tokens["agent-b"], "checkin")
            self.assertEqual(changed_text[0], 0, changed_text[2])
            loop_text = changed_text[1].split("每轮做法：", 1)[1]
            self.assertIn(f" 6. [新] {changed_loop[5]}", loop_text)
            self.assertEqual(loop_text.count("[新]"), 1)
            next_text = self.pf(tokens["agent-b"], "checkin")
            self.assertNotIn("[新]", next_text[1].split("每轮做法：", 1)[1])
            self.assertNotIn(changed_loop[0], next_text[1])
            self.assertIn("checkin --rules", next_text[1])

    def test_status_duty_ignores_idle_or_paused_research_without_active_jobs(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "status-agent"})[0], 201)
        token = self.cli("POST", "/agents/status-agent/token", {})[1]["token"]
        rid = "sr-status-duty"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-status-duty", {"title": "状态提醒条件", "objective": "空闲时不催状态"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
        self.assertIn("post_status", [item["kind"] for item in self.duties(token)["duties"]])
        for state in ("idle", "paused"):
            with self.subTest(state=state):
                current = self.cli("GET", f"/research-state/research/{rid}")[1]
                source = self.tmp / f"status-{state}.json"
                source.write_text(json.dumps({"state": state, "state_reason": "当前没有工作进行"}))
                code, _, err = self.pf(token, "research", "update", "research", rid, str(source),
                                       "--revision", str(current["revision"]), "--reason", "暂停活动")
                self.assertEqual(code, 0, err)
                self.assertNotIn("post_status", [item["kind"] for item in self.duties(token)["duties"]])

    def test_note_style_is_shared_by_checkin_help_and_skill(self):
        self.assertLessEqual(len(NOTE_STYLE), 200)
        self.assertEqual(self.web("POST", "/research-state/research/sr-note-style", self.write(
            "create-note-style", {"title": "留言风格", "objective": "简洁说人话"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-note-style")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-note-style", self.write(
            "human-note-style", {"research_id": "sr-note-style", "text": "请回复", "reply_to": None, "decision": None}))[0], 201)
        value = self.duties(token)
        self.assertIn(NOTE_STYLE, json.dumps(value, ensure_ascii=False))
        note = next(item for item in value["duties"] if item["kind"] == "answer_note")
        self.assertEqual(note["style"], NOTE_STYLE)
        self.assertIn(NOTE_STYLE, self.pf(token, "research", "--help")[1])
        self.assertIn(NOTE_STYLE, self.pf(token, "--help")[1])
        self.assertIn(NOTE_STYLE, (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))

    def test_delivery_style_is_shared_by_checkin_help_and_skill(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        value = self.duties(token)
        self.assertIn(DELIVERY_STYLE, json.dumps(value, ensure_ascii=False))
        self.assertIn(DELIVERY_STYLE, self.pf(token, "research", "--help")[1])
        self.assertIn(DELIVERY_STYLE, self.pf(token, "--help")[1])
        self.assertIn(DELIVERY_STYLE, (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))

    def test_request_feedback_remains_owner_duty_after_terminal_state(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        for terminal in ("delivered", "closed"):
            with self.subTest(terminal=terminal):
                rid, request_id = "sr-request-" + terminal, "rq-after-" + terminal
                self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                    "create-" + rid, {"title": rid, "objective": "终态后仍处理请求回答"}))[0], 201)
                self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
                current = self.cli("GET", f"/research-state/research/{rid}")[1]
                self.assertEqual(self.agent(token, "POST", f"/research-state/request/{request_id}", self.write(
                    "create-" + request_id, {"research_id": rid, "type": "guidance", "title": "选择下一步",
                                              "need": "确定复测范围", "why": "影响后续方向",
                                              "context": {"research_revision": current["revision"], "directions": []},
                                              "due_at": None}))[0], 201)
                if terminal == "delivered":
                    self.assertEqual(self.pf(token, "research", "deliver", rid, "--summary", "先交付",
                                             "--artifact", f"job:{self.jid}")[0], 0)
                else:
                    self.assertEqual(self.pf(token, "research", "close", rid, "--reason", "先停止")[0], 0)
                self.assertNotIn(request_id, [item["request_id"] for item in self.duties(token)["waiting_on_human"]])
                self.assertEqual(self.web("POST", f"/research-state/feedback/f-after-{terminal}", self.write(
                    "answer-after-" + terminal, {"request_id": request_id, "request_revision": 1,
                                                  "text": "按同场景复测", "supersedes": None}))[0], 201)
                pending = self.duties(token)["duties"]
                owed = next(item for item in pending if item["kind"] == "process_feedback" and item["target"] == request_id)
                self.assertEqual(owed["research_id"], rid)
                self.assertEqual(self.cli("GET", f"/research-state/request/{request_id}")[1]["data"]["state"], "open")

    def test_compare_reaches_human_feedback_and_agent_checkin(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-compare", self.write(
            "create-compare", {"title": "主观画质", "objective": "让人比较两个候选"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-compare", self.write(
            "create-compare-direction", {"research_id": "sr-compare", "title": "候选超分", "hypothesis": "画质需要人评"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-compare")[0], 0)
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        (output / "off.png").write_bytes(b"\x89PNG\r\n\x1a\noff")
        local = self.tmp / "local.mp4"
        local.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42")
        items = [f"job:{self.jid}:off.png", str(local)]
        code, out, err = self.pf(token, "research", "compare", "sr-compare",
                                 "--item", items[0], "--item", items[1], "--pick", "1",
                                 "--title", "小字对照", "--question", "哪边的小字更清楚？", "--direction", "d-compare")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count(COMPARE_MAPPING_HINT), 2)
        comparison = json.loads(out)
        request_id = comparison["request_id"]
        self.assertIn(f"/dash/research?kind=request&id={request_id}", comparison["url"])
        self.assertEqual(comparison["research_id"], "sr-compare")
        self.assertEqual(set(comparison["slots"].values()), set(items))
        result = self.cli("GET", "/results/" + comparison["result_id"])[1]
        self.assertEqual([source["kind"] for source in result["manifest"]["sources"]], ["job_output", "attachment"])
        self.assertEqual(result["manifest"]["sources"][1]["local_path"], str(local))
        self.assertEqual(result["manifest"]["labels"]["direction_id"], "d-compare")
        request = self.web("GET", "/research-state/request/" + request_id)[1]
        self.assertEqual(request["data"]["type"], "evaluation")
        self.assertEqual(request["data"]["need"], "哪边的小字更清楚？")
        self.assertEqual(request["data"]["context"]["directions"], [{"id": "d-compare", "revision": 1}])
        self.assertEqual(request["data"]["evaluation"]["schema"], "phonefarm/compare@1")
        self.assertEqual(request["data"]["evaluation"]["pick"], 1)
        self.assertCountEqual([item["media_kind"] for item in request["data"]["evaluation"]["items"]], ["image", "video"])
        self.assertEqual((request["data"]["evidence_refs"], request["evidence_bindings"]), ([], []))
        self.assertNotIn(comparison["result_id"], json.dumps(request))
        full = self.cli("GET", "/research-state/request/" + request_id)[1]
        for position, item in enumerate(full["data"]["evaluation"]["items"]):
            slot = chr(ord("A") + position)
            source_name = full["data"]["evidence_refs"][item["ref"]]["source_name"]
            source = result["manifest"]["sources"][item["ref"]]
            original = items[item["ref"]]
            self.assertEqual(comparison["slots"][slot], original)
            self.assertEqual(source["name"], source_name)
        local_slot = next(slot for slot, original in comparison["slots"].items() if original == str(local))
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", f"/dash/api/research-state/request/{request_id}/media/{local_slot}?revision=1", headers={
            "Cookie": self.view_cookie, "X-Expected-Node-ID": self.node_id})
        response = connection.getresponse()
        body = response.read()
        self.assertEqual((response.status, response.getheader("Content-Type"), body), (200, "video/mp4", local.read_bytes()))
        connection.close()
        waiting = self.duties(token)
        self.assertIn(request_id, [item["request_id"] for item in waiting["waiting_on_human"]])
        self.assertNotIn(request_id, [item.get("target") for item in waiting["duties"]])

        assessment = {"schema": "phonefarm/compare-feedback@1", "picked": ["A"], "unable": False,
                      "difference": "moderate", "client_display": {"A": "decoded", "B": "decoded"},
                      "identity_revealed": False}
        code, feedback = self.web("POST", "/research-state/feedback/f-compare", self.write(
            "human-compare-feedback", {"request_id": request_id, "request_revision": 1,
                                        "text": "A 的小字更清楚", "evaluation": assessment}))
        self.assertEqual(code, 201, feedback)
        revealed = self.web("GET", "/research-state/request/" + request_id)[1]
        self.assertEqual(len(revealed["data"]["evidence_refs"]), 2)
        duties = self.duties(token)["duties"]
        owed = next(item for item in duties if item["kind"] == "process_feedback" and item["target"] == request_id)
        self.assertIn("采纳", owed["text"])
        self.assertEqual(owed["picked"], [{"slot": "A", "source": comparison["slots"]["A"]}])
        self.assertIn("research list feedback --request-id " + request_id, owed["command"])
        self.assertNotIn(request_id, [item["request_id"] for item in self.duties(token)["waiting_on_human"]])

    def test_compare_defaults_to_full_ranking_with_gaps(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-ranking", self.write(
            "create-ranking", {"title": "四格排序", "objective": "全部素材从好到差排列"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-ranking")[0], 0)
        output = Path(self.jobs.path(self.jid, "out"))
        output.mkdir(parents=True, exist_ok=True)
        items = []
        for name in ("native", "ours", "gsr", "bilinear"):
            path = output / (name + ".png")
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            items.append(f"job:{self.jid}:{name}.png")
        args = ["research", "compare", "sr-ranking", "--title", "超分排序", "--question", "从好到差排列"]
        for item in items:
            args += ["--item", item]
        code, out, err = self.pf(token, *args)
        self.assertEqual(code, 0, err)
        comparison = json.loads(out)
        request_id = comparison["request_id"]
        request = self.cli("GET", "/research-state/request/" + request_id)[1]
        self.assertEqual(request["data"]["evaluation"]["pick"], len(items))

        ranking, gaps = ["C", "A", "D", "B"], ["small", "moderate", "large"]
        assessment = {"schema": "phonefarm/compare-feedback@1", "picked": ranking, "unable": False,
                      "gaps": gaps, "difference": None,
                      "client_display": {slot: "decoded" for slot in "ABCD"}, "identity_revealed": False}
        for index, bad_gaps in enumerate((["none"], ["small", "unknown", "large"])):
            rejected = self.web("POST", f"/research-state/feedback/f-ranking-bad-{index}", self.write(
                f"ranking-bad-{index}", {"request_id": request_id, "request_revision": 1,
                                          "text": "无效间距", "evaluation": {**assessment, "gaps": bad_gaps}}))
            self.assertEqual(rejected[0], 400, rejected)
        code, feedback = self.web("POST", "/research-state/feedback/f-ranking", self.write(
            "human-ranking", {"request_id": request_id, "request_revision": 1,
                                "text": "C、A、D、B", "evaluation": assessment}))
        self.assertEqual(code, 201, feedback)
        saved = self.cli("GET", "/research-state/feedback/f-ranking")[1]["data"]["evaluation"]
        self.assertEqual(saved["picked"], ranking)
        self.assertEqual(saved["gaps"], gaps)
        self.assertIsNone(saved["difference"])
        owed = next(item for item in self.duties(token)["duties"]
                    if item["kind"] == "process_feedback" and item["target"] == request_id)
        self.assertEqual(owed["picked"], [{"slot": slot, "source": comparison["slots"][slot]} for slot in ranking])
        self.assertEqual(owed["gaps"], gaps)

    def test_request_chain_reads_full_comparisons_and_latest_feedback_in_one_page(self):
        for rid, title, follows in (("sr-chain-root", "根任务", None), ("sr-chain-next", "接续任务", "sr-chain-root")):
            data = {"title": title, "objective": "一次读取链上的盲评", "follows": follows}
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write("create-" + rid, data))[0], 201)
        out = Path(self.jobs.path(self.jid, "out"))
        out.mkdir(parents=True, exist_ok=True)
        for name in ("chain-a.png", "chain-b.png"):
            (out / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-chain",
            "title": "链上盲评素材", "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"],
            "sources": [{"name": name, "kind": "job_output", "job_id": self.jid, "path": name + ".png"}
                        for name in ("chain-a", "chain-b")]})[0], 201)
        refs = [{"kind": "result_source", "node_id": self.node_id, "result_id": "r-chain", "source_name": name}
                for name in ("chain-a", "chain-b")]
        for rid, request_id in (("sr-chain-root", "ev-chain-root"), ("sr-chain-next", "ev-chain-next")):
            request = {"research_id": rid, "type": "evaluation", "title": request_id, "need": "选一张", "why": "fixture",
                       "context": {"research_revision": 1, "directions": []}, "evidence_refs": refs,
                       "evaluation": {"schema": "phonefarm/compare@1",
                                      "items": [{"ref": 0, "media_kind": "image"}, {"ref": 1, "media_kind": "image"}],
                                      "pick": 1}}
            self.assertEqual(self.web("POST", f"/research-state/request/{request_id}",
                                      self.write("create-" + request_id, request))[0], 201)
        for feedback_id, slot, text in (("f-chain-old", "A", "第一次回答"), ("f-chain-new", "B", "第二次回答")):
            assessment = {"schema": "phonefarm/compare-feedback@1", "picked": [slot], "unable": False,
                          "difference": "small", "client_display": {"A": "decoded", "B": "decoded"},
                          "identity_revealed": False}
            feedback = {"request_id": "ev-chain-root", "request_revision": 1, "text": text, "evaluation": assessment}
            self.assertEqual(self.web("POST", f"/research-state/feedback/{feedback_id}",
                                      self.write("create-" + feedback_id, feedback))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        path = ("/research-state/request?research_id=sr-chain-root&research_id=sr-chain-next"
                "&full=1&feedback=latest&limit=100")
        for reader, dashboard_blind in ((self.web, True), (lambda method, path: self.agent(token, method, path), False)):
            code, batch = reader("GET", path)
            self.assertEqual(code, 200, batch)
            self.assertIsNone(batch["next_cursor"])
            items = {item["id"]: item for item in batch["items"]}
            self.assertEqual(set(items), {"ev-chain-root", "ev-chain-next"})
            for request_id in items:
                single = reader("GET", "/research-state/request/" + request_id)[1]
                latest = None
                if request_id == "ev-chain-root":
                    latest = reader("GET", "/research-state/feedback/f-chain-new")[1]
                self.assertEqual(items[request_id], {**single, "latest_feedback": latest})
            self.assertEqual(items["ev-chain-root"]["latest_feedback"]["data"]["text"], "第二次回答")
            self.assertIsNone(items["ev-chain-next"]["latest_feedback"])
            self.assertEqual(items["ev-chain-next"]["data"]["evidence_refs"], [] if dashboard_blind else refs)
            self.assertEqual(len(items["ev-chain-next"]["evidence_bindings"]), 0 if dashboard_blind else 2)
            self.assertEqual(len(items["ev-chain-root"]["data"]["evidence_refs"]), 2)

        paged_path = path.replace("limit=100", "limit=1")
        code, first = self.agent(token, "GET", paged_path)
        self.assertEqual(code, 200, first)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        code, second = self.agent(token, "GET", paged_path + "&cursor=" + first["next_cursor"])
        self.assertEqual(code, 200, second)
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertIn("latest_feedback", second["items"][0])

        headers = {"Authorization": f"Bearer {token}", "X-Expected-Node-ID": "f" * 32}
        mismatched_batch = self.request("GET", path, headers=headers)
        mismatched_single = self.request("GET", "/research-state/request/ev-chain-root", headers=headers)
        self.assertEqual(mismatched_batch, mismatched_single)
        self.assertEqual(mismatched_batch[0], 409)

    def test_compare_help_is_shared_by_checkin_help_and_skill(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        value = self.duties(token)
        hint = "题目和说明里不要写能对上来源的线索，比如某种方法的画面有什么不同、哪几段来自同一次录制；素材有差别就只说有差别。"
        self.assertTrue(any(hint in step for step in value["loop"]))
        self.assertTrue(any(COMPARE_MAPPING_HINT in step for step in value["loop"]))
        self.assertIn(COMPARE_DUTY, value["loop"])
        self.assertIn("要换掉旧的盲评", COMPARE_HELP)
        help_text = self.pf(token, "research", "--help")[1]
        self.assertIn(hint, help_text)
        self.assertIn(COMPARE_MAPPING_HINT, help_text)
        self.assertIn(COMPARE_HELP, help_text)
        skill = (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(hint, skill)
        self.assertIn(COMPARE_MAPPING_HINT, skill)
        self.assertIn(COMPARE_HELP, skill)

    def test_addressed_note_brings_another_agent_into_the_same_research(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-collab", self.write(
            "create-collab", {"title": "同一题目协作", "objective": "复现留在原任务"}))[0], 201)
        tokens = {}
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        self.assertEqual(self.pf(tokens["agent-a"], "research", "claim", "sr-collab")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-collab", self.write(
            "human-collab", {"research_id": "sr-collab", "text": "请 B 复现并回报", "to": "agent-b",
                              "reply_to": None, "decision": None}))[0], 201)
        owed = [item for item in self.duties(tokens["agent-b"])["duties"] if item["kind"] == "answer_note"]
        self.assertEqual([(item["research_id"], item["target"]) for item in owed], [("sr-collab", "n-collab")])
        notes = self.web("GET", "/research-state/note?research_id=sr-collab&limit=10")[1]["items"]
        self.assertIs(next(item for item in notes if item["id"] == "n-collab")["unanswered"], True)
        self.assertNotIn("n-collab", [item.get("target") for item in self.duties(tokens["agent-a"])["duties"]])
        self.assertIn(COLLABORATION, self.duties(tokens["agent-b"], rules=True)["loop"])
        self.assertIn(COLLABORATION, self.pf(tokens["agent-b"], "research", "--help")[1])
        self.assertIn(COLLABORATION, (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))

        code, jid, err = self.pf(tokens["agent-b"], "--research", "sr-collab", "--device-access", "none",
                                 "--detach", "fixture", "-c", "pass")
        self.assertEqual(code, 0, err)
        jid = jid.strip()
        self.assertEqual(self.cli("GET", "/jobs/" + jid)[1]["research_id"], "sr-collab")
        code, _, err = self.pf(tokens["agent-b"], "research", "note", "sr-collab", "复现已挂到原任务",
                               "--reply-to", "n-collab", "--decision", "adopt", "--evidence", "job:" + jid)
        self.assertEqual(code, 0, err)
        self.assertNotIn("n-collab", [item.get("target") for item in self.duties(tokens["agent-b"])["duties"]])
        notes = self.web("GET", "/research-state/note?research_id=sr-collab&limit=10")[1]["items"]
        self.assertIs(next(item for item in notes if item["id"] == "n-collab")["unanswered"], False)
        code, _, err = self.pf(tokens["agent-a"], "research", "deliver", "sr-collab", "--summary", "原任务继续交付",
                               "--artifact", "job:" + jid)
        self.assertEqual(code, 0, err)
        code, _, err = self.pf(tokens["agent-a"], "research", "note", "sr-collab", "请 B 再核对长稳", "--to", "agent-b")
        self.assertEqual(code, 0, err)
        notes = self.web("GET", "/research-state/note?research_id=sr-collab&limit=10")[1]["items"]
        self.assertEqual(notes[0]["to"], "agent-b")

    def test_research_link_and_unlink_preserve_follows_in_reads_and_lists(self):
        for rid in ("sr-root", "sr-child"):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "把拆开的任务接起来"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-child")[0], 0)
        code, _, err = self.pf(token, "research", "link", "sr-child", "sr-root")
        self.assertEqual(code, 0, err)
        linked = self.web("GET", "/research-state/research/sr-child")[1]
        self.assertEqual((linked["data"]["follows"], linked["audit"]["reason"]), ("sr-root", "接续自 sr-root"))
        studies = self.web("GET", "/research-state/research?limit=100")[1]["items"]
        self.assertEqual({item["id"]: item["follows"] for item in studies},
                         {"sr-child": "sr-root", "sr-root": None})
        children = self.web("GET", "/research-state/research?follows=sr-root&limit=100")
        self.assertEqual(children[0], 200, children[1])
        self.assertEqual([item["id"] for item in children[1]["items"]], ["sr-child"])
        code, out, err = self.pf(token, "research", "list", "research", "--follows", "sr-root")
        self.assertEqual(code, 0, err)
        self.assertEqual([item["id"] for item in json.loads(out)["items"]], ["sr-child"])
        self.assertIn("research link", self.pf(token, "research", "--help")[1])
        code, _, err = self.pf(token, "research", "link", "sr-child", "none")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.web("GET", "/research-state/research/sr-child")[1]["data"]["follows"])
        for target in ("sr-child", "missing"):
            code, _, err = self.pf(token, "research", "link", "sr-child", target)
            self.assertNotEqual(code, 0, err)
        self.assertIsNone(self.web("GET", "/research-state/research/sr-child")[1]["data"]["follows"])

    def test_close_reopen_and_automatic_reopen(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-reopen", self.write(
            "create-reopen", {"title": "可重开的任务", "objective": "交付后仍能继续"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-reopen")[0], 0)
        self.assertEqual(self.pf(token, "research", "deliver", "sr-reopen", "--summary", "第一版",
                                 "--artifact", "job:" + self.jid)[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-reopen", self.write(
            "human-reopen", {"research_id": "sr-reopen", "text": "还需要补一组复现", "reply_to": None,
                              "decision": None}))[0], 201)
        opened = self.web("GET", "/research-state/research/sr-reopen")[1]
        self.assertEqual(opened["data"]["state"], "active")
        self.assertIn("n-reopen", opened["data"]["state_reason"])
        reminder = next(item for item in self.duties(token)["duties"] if item["kind"] == "research_reopened")
        self.assertEqual(reminder["research_id"], "sr-reopen")
        self.assertIn("n-reopen", reminder["text"])
        self.assertEqual(self.pf(token, "research", "note", "sr-reopen", "采纳，补复现", "--reply-to",
                                 "n-reopen", "--decision", "adopt")[0], 0)
        code, _, err = self.pf(token, "research", "close", "sr-reopen", "--reason", "先并入另一个实验")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.web("GET", "/research-state/research/sr-reopen")[1]["data"]["state"], "closed")
        source = self.tmp / "reopen-direction.json"
        source.write_text(json.dumps({"research_id": "sr-reopen", "title": "补充复现", "hypothesis": "新方向会重开任务"}))
        code, _, err = self.pf(token, "research", "create", "direction", "d-reopen", str(source))
        self.assertEqual(code, 0, err)
        opened = self.web("GET", "/research-state/research/sr-reopen")[1]
        self.assertEqual(opened["data"]["state"], "active")
        self.assertIn("d-reopen", opened["data"]["state_reason"])
        self.assertEqual(self.pf(token, "research", "close", "sr-reopen", "--reason", "暂时不做")[0], 0)
        closed = self.web("GET", "/research-state/research?state=closed&limit=100")[1]["items"]
        self.assertIn("sr-reopen", [item["id"] for item in closed])
        code, _, err = self.pf(token, "research", "reopen", "sr-reopen", "--reason", "继续完成第二版")
        self.assertEqual(code, 0, err)
        opened = self.web("GET", "/research-state/research/sr-reopen")[1]
        self.assertEqual((opened["data"]["state"], opened["data"]["state_reason"]), ("active", "继续完成第二版"))
        reminders = [item for item in self.duties(token)["duties"] if item["kind"] == "research_reopened"]
        self.assertTrue(any("继续完成第二版" in item["text"] for item in reminders))
        self.assertNotEqual(self.pf(token, "research", "close", "sr-reopen")[0], 0)
        self.assertEqual(self.web("GET", "/research-state/research/sr-reopen")[1]["data"]["state"], "active")
        concept = "任务开着就是进行中；关上分两种，有成果的是已交付，没成果的是已关闭（不做了，或者并入了别的任务，要写原因）。关上的任务可以重新打开，接着做完再交付一次，交付会多一个版本。"
        self.assertIn(concept, self.pf(token, "research", "--help")[1])
        self.assertIn(concept, (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))

    def test_deliver_duty_keeps_the_reopen_reason_after_the_note_is_answered(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        for rid in ("sr-reopened-delivery", "sr-first-delivery"):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "交付待办保留重开原因"}))[0], 201)
            self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
            did = "d-" + rid
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write(
                "create-" + did, {"research_id": rid, "title": "已验证方向", "hypothesis": "结论已复核"}))[0], 201)
            self.assertEqual(self.pf(token, "research", "conclude", did, "verified", "--reason", "复测通过")[0], 0)
        original = "sr-first-delivery 的方向都已有结论；交付，或者再开一个方向"
        first = next(item for item in self.duties(token)["duties"]
                     if item["kind"] == "deliver" and item["research_id"] == "sr-first-delivery")
        self.assertEqual(first["text"], original)

        self.assertEqual(self.pf(token, "research", "deliver", "sr-reopened-delivery", "--summary", "第一版",
                                 "--artifact", f"job:{self.jid}")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-delivery-reopen", self.write(
            "human-delivery-reopen", {"research_id": "sr-reopened-delivery", "text": "请补一组复现",
                                       "reply_to": None, "decision": None}))[0], 201)
        reason = self.web("GET", "/research-state/research/sr-reopened-delivery")[1]["data"]["state_reason"]
        self.assertEqual(reason, "人工留言 n-delivery-reopen 重新打开任务")
        self.assertEqual(self.pf(token, "research", "note", "sr-reopened-delivery", "采纳，补复现",
                                 "--reply-to", "n-delivery-reopen", "--decision", "adopt")[0], 0)
        expected = ("sr-reopened-delivery 的方向都已有结论；交付，或者再开一个方向"
                    "（任务是因为「人工留言 n-delivery-reopen 重新打开任务」重新打开的）")
        for _ in range(2):
            pending = self.duties(token)["duties"]
            reopened = next(item for item in pending if item["kind"] == "deliver"
                            and item["research_id"] == "sr-reopened-delivery")
            self.assertEqual(reopened["text"], expected)
            first = next(item for item in pending if item["kind"] == "deliver"
                         and item["research_id"] == "sr-first-delivery")
            self.assertEqual(first["text"], original)

    def test_pause_resume_clears_stale_reason_without_claiming_a_reopen(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-pause-resume", self.write(
            "create-pause-resume", {"title": "暂停再恢复", "objective": "恢复不是重新打开"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-pause-resume")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/direction/d-pause-resume", self.write(
            "create-d-pause-resume", {"research_id": "sr-pause-resume", "title": "已有结论",
                                       "hypothesis": "恢复后继续交付"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "conclude", "d-pause-resume", "verified",
                                 "--reason", "复测通过")[0], 0)
        source = self.tmp / "research-state-patch.json"
        current = self.cli("GET", "/research-state/research/sr-pause-resume")[1]
        source.write_text(json.dumps({"state": "paused", "state_reason": "等设备到货"}))
        self.assertEqual(self.pf(token, "research", "update", "research", "sr-pause-resume", str(source),
                                 "--revision", str(current["revision"]), "--reason", "暂停等待设备")[0], 0)
        paused = self.cli("GET", "/research-state/research/sr-pause-resume")[1]
        source.write_text(json.dumps({"state": "active"}))
        code, out, err = self.pf(token, "research", "update", "research", "sr-pause-resume", str(source),
                                 "--revision", str(paused["revision"]), "--reason", "设备已到，恢复")
        self.assertEqual(code, 0, err)
        resumed = self.cli("GET", "/research-state/research/sr-pause-resume")[1]
        self.assertEqual((resumed["data"]["state"], resumed["data"]["state_reason"]), ("active", None))
        self.assertEqual(json.loads(out)["changed_fields"], ["state", "state_reason"])
        deliver = next(item for item in self.duties(token)["duties"] if item["kind"] == "deliver"
                       and item["research_id"] == "sr-pause-resume")
        self.assertEqual(deliver["text"], "sr-pause-resume 的方向都已有结论；交付，或者再开一个方向")

        source.write_text(json.dumps({"state": "paused"}))
        code, _, _ = self.pf(token, "research", "update", "research", "sr-pause-resume", str(source),
                             "--revision", str(resumed["revision"]), "--reason", "缺少新的暂停理由")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.cli("GET", "/research-state/research/sr-pause-resume")[1], resumed)

    def test_research_note_attaches_local_video(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-video", self.write(
            "create-video", {"title": "视频附件", "objective": "留言直接带本机视频"}))[0], 201)
        video = self.tmp / "clip.mp4"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42")
        code, _, err = self.pf(ADMIN, "research", "note", "sr-video", "同画面视频", "--attach", str(video))
        self.assertEqual(code, 0, err)
        thread = self.web("GET", "/research-state/note?research_id=sr-video&limit=10")[1]
        ref = thread["items"][0]["evidence_refs"][0]
        self.assertEqual((ref["kind"], ref["filename"], ref["content_type"]),
                         ("attachment", "clip.mp4", "video/mp4"))
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", f"/dash/api/research-attachments/{ref['attachment_id']}", headers={
            "Cookie": self.view_cookie, "X-Expected-Node-ID": self.node_id})
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200, body.decode(errors="replace"))
        self.assertEqual(response.getheader("Content-Type"), "video/mp4")
        self.assertEqual(body, video.read_bytes())
        connection.close()

    def test_cli_times_are_utc_and_checkin_declares_timezone(self):
        from agent_client import render_checkin
        value = {"agent": "agent-a", "at": 1700000000, "previous_checkin_at": 1700000001,
                 "duties": [], "waiting_on_human": [],
                 "jobs": {"active": [], "blocked": [], "finished_since_last_checkin": []},
                 "research": {"mine": [], "unclaimed": []}, "loop": []}
        first = render_checkin(value).splitlines()[0]
        self.assertEqual(first, "agent-a · 节点时区 UTC · 当前时间 2023-11-14T22:13:20Z · 上次签到 2023-11-14T22:13:21Z")
        self.assertEqual(self.web("POST", "/research-state/research/sr-time", self.write(
            "create-time", {"title": "时间口径", "objective": "CLI 一律 UTC Z"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/note/n-time", self.write(
            "note-time", {"research_id": "sr-time", "text": "检查时间", "reply_to": None, "decision": None}))[0], 201)
        code, out, err = self.pf(ADMIN, "research", "thread", "sr-time")
        self.assertEqual(code, 0, err)
        self.assertRegex(out.split()[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_agent_own_token_carries_its_identity(self):
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        status, issued = self.cli("POST", "/agents/agent-a/token", {})
        self.assertEqual(status, 201)
        token = issued["token"]
        self.assertTrue(issued["agent"]["credential_present"])
        self.assertNotIn("credential", self.cli("GET", "/agents/agent-a")[1])
        self.assertEqual(self.agent(token, "GET", "/whoami")[1]["agent"], "agent-a")
        self.assertEqual(self.cli("GET", "/whoami")[1]["credential"], "admin")

        # It reports and speaks only as itself, and cannot manage agents.
        self.assertEqual(self.agent(token, "POST", "/agents/agent-a/status", {"text": "跑插帧 A/B"})[0], 200)
        self.assertEqual(self.agent(token, "POST", "/agents/agent-b/status", {"text": "x"})[0], 403)
        self.assertEqual(self.agent(token, "POST", "/agents", {"name": "agent-c"})[0], 403)
        self.assertEqual(self.agent(token, "POST", "/agents/agent-b/token", {})[0], 403)
        status, _ = self.agent(token, "POST", "/research-state/research/sr-a", self.write("agent-task", {"title": "t", "objective": "o"}))
        self.assertEqual(status, 201)
        status, posted = self.agent(token, "POST", "/research-state/note/n-agent-a", self.write(
            "agent-note", {"research_id": "sr-a", "text": "收到", "reply_to": None, "decision": None}))
        self.assertEqual(status, 201)
        audit = self.cli("GET", "/research-state/note/n-agent-a")[1]["audit"]
        self.assertEqual((audit["actor_label"], audit["auth_channel"]), ("agent-a", "agent_token"))
        posing = dict(self.write("agent-task-2", {"title": "t", "objective": "o"}), actor_label="agent-b")
        self.assertEqual(self.agent(token, "POST", "/research-state/research/sr-b", posing)[0], 400)

        # Jobs it submits are its own.
        status, job = self.request("POST", "/jobs", None, {
            "Authorization": f"Bearer {token}", "X-Expected-Node-ID": self.node_id,
            "X-Job": json.dumps({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none"})})
        self.assertEqual(status, 200, job)
        jid = job["job"]["id"] if "job" in job else job["id"]
        self.assertEqual(self.jobs.dispatch.queue.get(jid)["spec"]["actor_id"], "agent-a")

        # Issuing again retires the old token; disabling the agent retires the new one.
        fresh = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.agent(token, "GET", "/whoami")[0], 401)
        self.assertEqual(self.agent(fresh, "GET", "/whoami")[1]["agent"], "agent-a")
        revision = self.cli("GET", "/agents/agent-a")[1]["revision"]
        self.assertEqual(self.cli("POST", "/agents/agent-a", {"revision": revision, "enabled": False})[0], 200)
        self.assertEqual(self.agent(fresh, "GET", "/whoami")[0], 401)

    def test_research_and_direction_writes_require_owner(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-owned", self.write(
            "create-owned", {"title": "负责人隔离", "objective": "其他 agent 只能留言"}))[0], 201)
        tokens = {}
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        self.assertEqual(self.pf(tokens["agent-a"], "research", "claim", "sr-owned")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/direction/d-other", self.write(
            "create-d-other", {"research_id": "sr-owned", "title": "他人方向", "hypothesis": "不可越权"}))[0], 201)
        code, _, err = self.pf(tokens["agent-b"], "research", "deliver", "sr-owned", "--summary", "越权交付",
                               "--artifact", f"job:{self.jid}")
        self.assertNotEqual(code, 0)
        self.assertIn("research_owned", err)
        self.assertIn("agent-a", err)
        self.assertIn("research note", err)
        source = self.tmp / "other-direction.json"
        source.write_text(json.dumps({"research_id": "sr-owned", "title": "越权方向", "hypothesis": "不允许"}))
        code, _, err = self.pf(tokens["agent-b"], "research", "create", "direction", "d-other-agent", str(source))
        self.assertNotEqual(code, 0)
        self.assertIn("research_owned", err)
        code, _, err = self.pf(tokens["agent-b"], "research", "conclude", "d-other", "verified", "--reason", "越权结论")
        self.assertNotEqual(code, 0)
        self.assertIn("research_owned", err)
        self.assertIn("agent-a", err)
        self.assertEqual(self.pf(tokens["agent-b"], "research", "note", "sr-owned", "开放讨论仍然允许")[0], 0)
        self.assertEqual(self.pf(tokens["agent-a"], "research", "conclude", "d-other", "verified",
                                 "--reason", "负责人结论")[0], 0)

    def test_research_conclude_command_and_checkin_reminder(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-conclude", self.write(
            "create-conclude", {"title": "方向结论", "objective": "交付后方向不再停留待探索"}))[0], 201)
        for did in ("d-done", "d-open"):
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write(
                "create-" + did, {"research_id": "sr-conclude", "title": did, "hypothesis": "验证结论路径"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-conclude")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-guide", self.write(
            "guide-conclude", {"research_id": "sr-conclude", "text": "先回应再下结论", "reply_to": None, "decision": None}))[0], 201)
        code, _, err = self.pf(token, "research", "conclude", "d-done", "verified", "--reason", "A/B 已复测")
        self.assertNotEqual(code, 0)
        self.assertIn("research_guidance_unanswered", err)
        self.assertEqual(self.cli("GET", "/research-state/direction/d-done")[1]["data"]["state"], "proposed")
        self.assertEqual(self.pf(token, "research", "note", "sr-conclude", "采纳", "--reply-to", "n-guide",
                                 "--decision", "adopt")[0], 0)
        output = Path(self.jobs.path(self.jid, "out"))
        (output / "report.json").write_text('{"gain": true}')
        code, _, err = self.pf(token, "research", "conclude", "d-done", "verified", "--reason", "A/B 已复测",
                               "--evidence", f"job:{self.jid}:report.json")
        self.assertEqual(code, 0, err)
        concluded = self.cli("GET", "/research-state/direction/d-done")[1]
        self.assertEqual((concluded["revision"], concluded["data"]["state"], concluded["data"]["state_reason"]),
                         (2, "verified", "A/B 已复测"))
        self.assertEqual(concluded["data"]["evidence_refs"][0]["path"], "report.json")
        code, help_text, _ = self.pf(ADMIN, "research", "--help")
        self.assertIn("research conclude <方向ID> verified|no_gain|rejected|deferred", help_text)

        self.assertEqual(self.pf(token, "research", "deliver", "sr-conclude", "--summary", "完成",
                                 "--artifact", f"job:{self.jid}")[0], 0)
        pending = self.duties(token)
        self.assertTrue(any("pf-node research conclude <方向ID> verified|no_gain|rejected|deferred" in step
                            for step in pending["loop"]))
        reminder = next(item for item in pending["duties"] if item["kind"] == "conclude_direction")
        self.assertEqual((reminder["research_id"], reminder["target"]), ("sr-conclude", "d-open"))
        self.assertIn("pf-node research conclude d-open verified|no_gain|rejected|deferred --reason", reminder["command"])
        self.assertIn("已交付", reminder["text"])
        self.assertEqual(self.pf(token, "research", "conclude", "d-open", "no_gain", "--reason", "没有收益")[0], 0)
        self.assertNotIn("conclude_direction", [item["kind"] for item in self.duties(token)["duties"]])

    def test_delivered_human_note_is_owner_duty_and_list_help_names_kind(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-after-delivery", self.write(
            "create-after-delivery", {"title": "交付后指导", "objective": "交付后留言仍回应"}))[0], 201)
        tokens = {}
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        self.assertEqual(self.pf(tokens["agent-a"], "research", "claim", "sr-after-delivery")[0], 0)
        self.assertEqual(self.pf(tokens["agent-a"], "research", "deliver", "sr-after-delivery",
                                 "--summary", "先交付", "--artifact", f"job:{self.jid}")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-after-delivery", self.write(
            "human-after-delivery", {"research_id": "sr-after-delivery", "text": "交付后请补充复测",
                                      "reply_to": None, "decision": None}))[0], 201)
        owed = [item for item in self.duties(tokens["agent-a"])["duties"] if item["kind"] == "answer_note"]
        self.assertEqual([(item["target"], item["research_id"]) for item in owed], [("n-after-delivery", "sr-after-delivery")])
        self.assertNotIn("answer_note", [item["kind"] for item in self.duties(tokens["agent-b"])["duties"]])
        self.assertEqual(self.pf(tokens["agent-a"], "research", "note", "sr-after-delivery", "补充后重新交付",
                                 "--reply-to", "n-after-delivery", "--decision", "adopt")[0], 0)
        self.assertNotIn("answer_note", [item["kind"] for item in self.duties(tokens["agent-a"])["duties"]])
        code, help_text, _ = self.pf(tokens["agent-a"], "research", "--help")
        self.assertIn("research list 必须带 KIND", help_text)
        self.assertIn("research/direction/request/feedback/note", help_text)

    def test_research_note_attaches_local_images(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-attach", self.write(
            "create-attach", {"title": "本机附件", "objective": "留言直接显示本机图片"}))[0], 201)
        svg = self.tmp / "chart.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="#2ab57d"/></svg>')
        png = self.tmp / "tiny.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        code, _, err = self.pf(ADMIN, "research", "note", "sr-attach", "本机画的图", "--attach", str(svg), "--attach", str(png))
        self.assertEqual(code, 0, err)
        _, thread = self.web("GET", "/research-state/note?research_id=sr-attach&limit=10")
        refs = thread["items"][0]["evidence_refs"]
        self.assertEqual([ref["kind"] for ref in refs], ["attachment", "attachment"])
        self.assertEqual([ref["filename"] for ref in refs], ["chart.svg", "tiny.png"])
        self.assertEqual([ref["content_type"] for ref in refs], ["image/svg+xml", "image/png"])
        for ref in refs:
            self.assertEqual(len(ref["sha256"]), 64)
        attachment_id = refs[0]["attachment_id"]
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", f"/dash/api/research-attachments/{attachment_id}", headers={
            "Cookie": self.view_cookie, "X-Expected-Node-ID": self.node_id})
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200, body.decode(errors="replace"))
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
        self.assertEqual(response.getheader("Content-Security-Policy"),
                         "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        self.assertEqual(body, svg.read_bytes())
        connection.close()
        oversized = self.tmp / "oversized.svg"
        with oversized.open("wb") as stream:
            stream.truncate((16 << 20) + 1)
        code, _, err = self.pf(ADMIN, "research", "note", "sr-attach", "不能上传", "--attach", str(oversized))
        self.assertNotEqual(code, 0)

    def test_agent_loop_claim_guidance_and_delivery_through_cli(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", self.write(
            "web-task", {"title": "优化原神超分方向", "objective": "画质不明显下降时提高帧率"}))[0], 201)
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        code, token_a, err = self.pf(ADMIN, "agents", "token", "agent-a")
        self.assertEqual(code, 0, err)
        token_a = token_a.strip()
        token_b = self.cli("POST", "/agents/agent-b/token", {})[1]["token"]

        # First check-in: nothing owned yet, the human's research is claimable, status never posted.
        first = self.duties(token_a)
        self.assertEqual([item["research_id"] for item in first["research"]["unclaimed"]], ["sr-genshin"])
        self.assertEqual([duty["kind"] for duty in first["duties"]], ["post_status"])
        self.assertTrue(first["loop"])
        code, text, _ = self.pf(token_a, "checkin")
        self.assertIn("pf-node research claim sr-genshin", text)
        self.assertEqual(self.pf(ADMIN, "checkin")[0], 1)  # the admin token has no agent to check in as
        self.assertEqual(self.agent(token_b, "POST", "/agents/agent-a/checkin", {})[0], 403)

        code, _, err = self.pf(token_a, "research", "claim", "sr-genshin")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli("GET", "/research-state/research/sr-genshin")[1]["data"]["owner"], "agent-a")
        self.assertEqual(self.pf(token_a, "agents", "status", "agent-a", "规划超分方向")[0], 0)
        self.assertEqual([duty["kind"] for duty in self.duties(token_a)["duties"]], ["plan_directions"])

        # The human gives guidance on the page; the next check-in shows it as new, until answered.
        status, receipt = self.web("POST", "/research-state/note/n-guide", self.write(
            "web-guide", {"research_id": "sr-genshin", "text": "先做 1080p，别动 HUD", "reply_to": None, "decision": None}))
        self.assertEqual(status, 201, receipt)
        owed = [duty for duty in self.duties(token_a)["duties"] if duty["kind"] == "answer_note"]
        self.assertEqual([(duty["target"], duty["unread"]) for duty in owed], [("n-guide", True)])
        owed = [duty for duty in self.duties(token_a)["duties"] if duty["kind"] == "answer_note"]
        self.assertEqual([(duty["target"], duty["unread"]) for duty in owed], [("n-guide", False)])
        code, _, err = self.pf(token_a, "research", "note", "sr-genshin", "采纳：先做 1080p，HUD 区域不处理",
                               "--reply-to", "n-guide", "--decision", "adopt")
        self.assertEqual(code, 0, err)
        self.assertNotIn("answer_note", [duty["kind"] for duty in self.duties(token_a)["duties"]])
        code, thread, err = self.pf(ADMIN, "research", "thread", "sr-genshin")
        self.assertEqual(code, 0, err)
        lines = thread.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("human n-guide: 先做 1080p", lines[0])
        self.assertIn("agent-a", lines[1])
        self.assertIn("↳n-guide [adopt]", lines[1])
        self.assertEqual(self.web("GET", "/agents/agent-a/duties")[0], 200)

        # Another agent cannot take it; a human can reassign it.
        self.assertNotEqual(self.pf(token_b, "research", "claim", "sr-genshin")[0], 0)
        current = self.cli("GET", "/research-state/research/sr-genshin")[1]
        taking = dict(self.write("b-takes", {**current["data"], "owner": "agent-b"}), base_revision=current["revision"], reason="take")
        status, body = self.agent(token_b, "POST", "/research-state/research/sr-genshin", taking)
        self.assertEqual((status, body["code"]), (409, "research_owned"))
        reassign = dict(self.write("web-reassign", {**current["data"], "owner": "agent-b"}),
                        base_revision=current["revision"], reason="改派给 agent-b")
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", reassign)[0], 201)
        self.assertEqual(self.duties(token_a)["research"]["mine"], [])

        # The new owner keeps what the earlier owner answered: n-guide is not its to answer again. A human note
        # written after the handover is, and holds back its delivery until it answers.
        self.assertEqual(self.web("POST", "/research-state/note/n-after", self.write(
            "web-after", {"research_id": "sr-genshin", "text": "接手后照旧先做 1080p", "reply_to": None, "decision": None}))[0], 201)
        self.assertEqual([duty["target"] for duty in self.duties(token_b)["duties"] if duty["kind"] == "answer_note"], ["n-after"])
        code, _, err = self.pf(token_b, "research", "deliver", "sr-genshin", "--summary", "1080p 自训练超分，帧率 +18%",
                               "--artifact", f"job:{self.jid}")
        self.assertNotEqual(code, 0)
        self.assertIn("code=research_guidance_unanswered", err)
        self.assertEqual(self.pf(token_b, "research", "note", "sr-genshin", "接手：照旧先做 1080p，不动 HUD",
                                 "--reply-to", "n-after", "--decision", "adopt")[0], 0)

        # Delivery needs a deliverable; the CLI writes summary, artifact and scope together.
        current = self.cli("GET", "/research-state/research/sr-genshin")[1]
        bare = dict(self.write("b-bare", {**current["data"], "state": "delivered"}), base_revision=current["revision"], reason="done")
        self.assertEqual(self.agent(token_b, "POST", "/research-state/research/sr-genshin", bare)[0], 400)
        code, _, err = self.pf(token_b, "research", "deliver", "sr-genshin", "--summary", "1080p 自训练超分，帧率 +18%",
                               "--artifact", f"job:{self.jid}", "--game-version", "5.1", "--device", "RedMagic NX809J")
        self.assertEqual(code, 0, err)
        delivered = self.web("GET", "/research-state/research/sr-genshin")[1]
        self.assertEqual(delivered["data"]["state"], "delivered")
        self.assertEqual(delivered["data"]["deliverable"]["artifacts"][0]["job_id"], self.jid)
        self.assertEqual(delivered["data"]["deliverable"]["applies_to"], {"game_version": "5.1", "devices": ["RedMagic NX809J"]})
        self.assertEqual(delivered["audit"]["actor_label"], "agent-b")

    def test_delivered_research_has_a_home_report_link(self):
        for rid in ("sr-active", "sr-report"):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "首页任务与报告"}))[0], 201)
        summary = "# 结果\n\n| 指标 | 变化 |\n| --- | --- |\n| 帧率 | +18% |"
        self.assertEqual(self.pf(ADMIN, "research", "deliver", "sr-report", "--summary", summary,
                                 "--artifact", f"job:{self.jid}", "--game-version", "5.1",
                                 "--device", "FixturePhone")[0], 0)
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        tasks = {item["id"]: item for item in board._tasks()}
        self.assertIn("sr-active", tasks)
        self.assertEqual(tasks["sr-report"]["state"], "delivered")
        self.assertEqual(tasks["sr-report"]["report_url"], "/dash/research/report?id=sr-report")

    def test_job_artifact_paths_are_shared_by_cli_notes_and_delivery(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-artifacts", self.write(
            "web-artifacts", {"title": "图像证据", "objective": "在任务讨论与交付中保留过程图"}))[0], 201)
        output = Path(self.jobs.path(self.jid, "out"))
        (output / "charts").mkdir()
        (output / "charts/frame.png").write_bytes(b"fixture png bytes")
        reference = f"job:{self.jid}:charts/frame.png"
        code, _, err = self.pf(ADMIN, "research", "note", "sr-artifacts", "第一代帧时图", "--evidence", reference)
        self.assertEqual(code, 0, err)
        status, thread = self.web("GET", "/research-state/note?research_id=sr-artifacts&limit=100")
        self.assertEqual(status, 200)
        self.assertEqual(thread["items"][0]["evidence_refs"], [
            {"kind": "job", "job_id": self.jid, "node_id": self.node_id, "path": "charts/frame.png"}])

        code, _, err = self.pf(ADMIN, "research", "deliver", "sr-artifacts", "--summary", "交付过程图",
                               "--artifact", reference)
        self.assertEqual(code, 0, err)
        delivered = self.web("GET", "/research-state/research/sr-artifacts")[1]
        self.assertEqual(delivered["data"]["deliverable"]["artifacts"][0]["path"], "charts/frame.png")

        (self.tmp / "outside.png").write_bytes(b"outside")
        (output / "escape.png").symlink_to(self.tmp / "outside.png")
        for path in ("../outside.png", "/absolute.png", "charts//frame.png", "charts/missing.png", "escape.png"):
            with self.subTest(path=path):
                code, _, err = self.pf(ADMIN, "research", "note", "sr-artifacts", "不能保存的引用",
                                       "--evidence", f"job:{self.jid}:{path}")
                self.assertNotEqual(code, 0, err)
        _, notes = self.web("GET", "/research-state/note?research_id=sr-artifacts&limit=100")
        self.assertEqual(len(notes["items"]), 1, "rejected artifact references must not create notes")

    def test_delivery_error_names_the_missing_job_or_artifact_path(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-delivery-errors", self.write(
            "create-delivery-errors", {"title": "交付错误定位", "objective": "指出哪条产物引用无法解析"}))[0], 201)
        missing_job_id = "0" * 12 if self.jid != "0" * 12 else "f" * 12
        cases = ((f"job:{self.jid}:charts/missing.png", "任务里没有这个路径"),
                 (f"job:{missing_job_id}:charts/missing.png", "任务不存在"))
        for reference, reason in cases:
            with self.subTest(reference=reference):
                code, _, err = self.pf(ADMIN, "research", "deliver", "sr-delivery-errors", "--summary", "定位错误",
                                       "--artifact", f"job:{self.jid}", "--artifact", reference)
                self.assertNotEqual(code, 0, err)
                self.assertIn(reference, err)
                self.assertIn(reason, err)

    def test_delivery_error_names_the_missing_result(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-result-errors", self.write(
            "create-result-errors", {"title": "结果引用错误定位", "objective": "指出哪个结果引用无法解析"}))[0], 201)
        reference = "result:r-missing-evidence"
        code, _, err = self.pf(ADMIN, "research", "deliver", "sr-result-errors", "--summary", "定位错误",
                               "--artifact", f"job:{self.jid}", "--artifact", reference)
        self.assertNotEqual(code, 0, err)
        self.assertIn(reference, err)
        self.assertIn("结果不存在", err)

    def test_jobs_carry_their_research_to_the_task_page_and_the_phone(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", self.write(
            "web-task", {"title": "优化原神超分方向", "objective": "画质不明显下降时提高帧率"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        code, out, err = self.pf(token, "--research", "sr-genshin", "--label", "selfsr 1080p A/B", "--device-access", "none",
                                 "--detach", "fixture", "-c", "pass")
        self.assertEqual(code, 0, err)
        jid = out.strip()
        code, _, err = self.pf(token, "--research", "no-such-study", "--device-access", "none", "--detach", "fixture", "-c", "pass")
        self.assertNotEqual(code, 0)
        self.assertIn("research_id", err)

        # Both surfaces list the research's jobs and nothing else; the job keeps its agent.
        code, out, err = self.pf(ADMIN, "jobs", "--research", "sr-genshin")
        self.assertEqual(code, 0, err)
        self.assertEqual([(item["id"], item["actor_id"]) for item in json.loads(out)["items"]], [(jid, "agent-a")])
        status, listed = self.web("GET", "/jobs?research=sr-genshin&limit=50")
        self.assertEqual((status, [item["id"] for item in listed["items"]]), (200, [jid]))
        self.assertEqual(self.cli("GET", f"/jobs/{jid}")[1]["research_id"], "sr-genshin")
        self.assertEqual(self.web("GET", "/jobs?research=bad/id")[0], 400)
        active = self.duties(token)["jobs"]["active"]
        self.assertEqual({item["job_id"]: item["research_id"] for item in active}, {self.jid: None, jid: "sr-genshin"})
        self.assertEqual(self.pf(token, "research", "claim", "sr-genshin")[0], 0)
        code, text, err = self.pf(token, "checkin")
        self.assertEqual(code, 0, err)
        self.assertIn(f"未关联研究的任务：{self.jid}（", text)

        # A phone shows the task its holder works for: by device lease, by the node's global lock,
        # or the bare label an outside experiment put on the lock.
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        by_lease = {"status": "held", "held_by": None, "lease": {"entries": [
            {"job_id": jid, "serial": "S1", "state": "active", "label": f"job-{jid}"}]}}
        expected = {"kind": "job", "job_id": jid, "label": "selfsr 1080p A/B", "actor_id": "agent-a",
                    "research_id": "sr-genshin", "research_title": "优化原神超分方向", "state": "queued"}
        self.assertEqual(board._holder("S1", by_lease), expected)
        self.assertIsNone(board._holder("S2", {**by_lease, "status": "free"}))
        self.assertEqual(board._holder("S2", {"status": "held", "held_by": f"job-{jid}", "lease": {"entries": []}}), expected)
        # Under the global lock, a job that names its phone works on that phone and only blocks the others.
        aimed = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass", "--device", "S1"], "device_access": "none",
                                           "resources": [], "actor_id": "agent-a"}, b"", "aimed-job")
        aimed = aimed["job"]["id"] if "job" in aimed else aimed["id"]
        held = {"status": "held", "held_by": f"job-{aimed}", "lease": {"entries": []}}
        self.assertEqual((board._holder("S1", held)["kind"], board._holder("S2", held)["kind"]), ("job", "global"))
        # An experiment locks under its job's label and names the phone after @.
        nested = {"status": "held", "held_by": f"job-{jid}-selfsr-native-1@S2", "lease": {"entries": []}}
        self.assertEqual((board._holder("S2", nested)["kind"], board._holder("S1", nested)["kind"]), ("job", "global"))
        self.assertEqual(board._holder("S2", nested)["research_id"], "sr-genshin")
        outside = {"status": "held", "held_by": "selfsr@S2", "lease": {"entries": []}}
        self.assertEqual(board._holder("S2", outside), {"kind": "external", "label": "selfsr@S2"})
        self.assertIsNone(board._holder("S1", outside))
        # The home page lists the task in progress with its owner and the jobs still working for it.
        tasks = board._tasks()
        self.assertEqual([(t["id"], t["title"], t["owner"]) for t in tasks], [("sr-genshin", "优化原神超分方向", "agent-a")])
        self.assertEqual([(j["job_id"], j["label"], j["state"], j["actor_id"]) for j in tasks[0]["jobs"]],
                         [(jid, "selfsr 1080p A/B", "queued", "agent-a")])

    def test_two_phones_keep_distinct_research_agents_and_jobs(self):
        jobs = {}
        for suffix, serial in (("a", "PHONE-A"), ("b", "PHONE-B")):
            rid, agent = "sr-" + suffix, "agent-" + suffix
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "任务 " + suffix.upper(), "objective": "双手机并行占用"}))[0], 201)
            self.assertEqual(self.cli("POST", "/agents", {"name": agent})[0], 201)
            token = self.cli("POST", f"/agents/{agent}/token", {})[1]["token"]
            self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
            code, jid, err = self.pf(token, "--research", rid, "--label", "实验 " + suffix.upper(),
                                     "--device-access", "none", "--detach", "fixture", "-c", "pass")
            self.assertEqual(code, 0, err)
            jobs[serial] = (rid, agent, jid.strip())
            status = self.cli("GET", f"/jobs/{jid.strip()}")[1]
            self.assertEqual((status["research_id"], status["actor_id"], status["label"]), (rid, agent, "实验 " + suffix.upper()))
            code, out, err = self.pf(ADMIN, "jobs", "--research", rid)
            self.assertEqual(code, 0, err)
            self.assertEqual([(item["id"], item["actor_id"]) for item in json.loads(out)["items"]], [(jid.strip(), agent)])

        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        lock = {"status": "held", "held_by": None, "lease": {"entries": [
            {"job_id": jobs[serial][2], "serial": serial, "state": "active", "label": "job-" + jobs[serial][2]}
            for serial in ("PHONE-A", "PHONE-B")]}}
        for serial, (rid, agent, jid) in jobs.items():
            self.assertEqual(board._holder(serial, lock), {
                "kind": "job", "job_id": jid, "label": "实验 " + serial[-1], "actor_id": agent,
                "research_id": rid, "research_title": "任务 " + serial[-1], "state": "queued"})
        tasks = {item["id"]: item for item in board._tasks()}
        for rid, agent, jid in jobs.values():
            self.assertEqual(tasks[rid]["owner"], agent)
            self.assertEqual([(job["job_id"], job["actor_id"]) for job in tasks[rid]["jobs"]], [(jid, agent)])
        script = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        research_branch = script.split("entry.research_id", 1)[1].split(": `<a", 1)[0]
        self.assertIn("esc(entry.label || entry.job_id)", research_branch,
                      "a research-linked phone must retain the job label as well as the research and agent")

    def test_home_task_carries_its_latest_note_and_delivered_ones_do_not(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-latest", self.write(
            "create-latest", {"title": "最新进展", "objective": "首页一眼看到任务最近说了什么"}))[0], 201)
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        self.assertIsNone(board._tasks()[0]["latest_note"])
        for operation, nid, text in (("note-1", "n-first", "先测 1080p"), ("note-2", "n-second", "1080p 帧率 +18%，开始复测")):
            self.assertEqual(self.web("POST", f"/research-state/note/{nid}", self.write(
                operation, {"research_id": "sr-latest", "text": text, "reply_to": None, "decision": None}))[0], 201)
        latest = board._tasks()[0]["latest_note"]
        self.assertEqual((latest["id"], latest["text"], latest["auth_channel"]),
                         ("n-second", "1080p 帧率 +18%，开始复测", "dashboard_steer"))
        self.assertEqual(self.web("POST", "/research-state/research/sr-latest-done", self.write(
            "create-latest-done", {"title": "已交付", "objective": "交付后首页只给报告入口"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-latest-done")[0], 0)
        summary = self.tmp / "latest-summary.md"
        summary.write_text("结论：1080p 可用", encoding="utf-8")
        code, _, err = self.pf(token, "research", "deliver", "sr-latest-done", "--summary", "@" + str(summary),
                               "--artifact", "url:https://example.invalid/report")
        self.assertEqual(code, 0, err)
        delivered = {task["id"]: task for task in board._tasks()}["sr-latest-done"]
        self.assertEqual(delivered["state"], "delivered")
        self.assertNotIn("latest_note", delivered)

    def test_home_keeps_delivered_reports_when_many_tasks_are_active(self):
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.web("POST", "/research-state/research/sr-old-report", self.write(
            "create-old-report", {"title": "较早交付", "objective": "很多进行中的任务之后仍在首页"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "claim", "sr-old-report")[0], 0)
        summary = self.tmp / "old-report.md"
        summary.write_text("结论", encoding="utf-8")
        code, _, err = self.pf(token, "research", "deliver", "sr-old-report", "--summary", "@" + str(summary),
                               "--artifact", "url:https://example.invalid/report")
        self.assertEqual(code, 0, err)
        for i in range(12):
            self.assertEqual(self.web("POST", f"/research-state/research/sr-busy-{i}", self.write(
                f"create-busy-{i}", {"title": f"进行中 {i}", "objective": "占满首页"}))[0], 201)
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        tasks = board._tasks()
        self.assertEqual(sum(task["state"] == "active" for task in tasks), 10)
        self.assertIn("sr-old-report", [task["id"] for task in tasks if task["state"] == "delivered"])

    def test_home_lists_a_chain_of_tasks_as_one_row_under_its_root(self):
        """Like the task list, the home page shows a chain (research.follows) as one row under its root: open while one
        of its tasks is, delivered once one delivered. The owner is the latest open task's that has one, so a newer
        unclaimed follow-on does not make an owned chain read 无人负责."""
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        create = lambda rid, follows: self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-" + rid, {"title": "任务 " + rid, "objective": "首页按链", "follows": follows}))[0], 201)
        create("sr-chain-root", None)
        create("sr-chain-next", "sr-chain-root")
        self.assertEqual(self.pf(token, "research", "claim", "sr-chain-root")[0], 0)
        code, _, err = self.pf(token, "research", "deliver", "sr-chain-root", "--summary", "第一段结论",
                               "--artifact", "url:https://example.invalid/report")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.pf(token, "research", "claim", "sr-chain-next")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-chain-next", self.write("create-n-chain-next", {
            "research_id": "sr-chain-next", "text": "第二段进展", "reply_to": None, "decision": None}))[0], 201)
        create("sr-chain-third", "sr-chain-next")
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        [row] = board._tasks()
        self.assertEqual((row["id"], row["title"], row["state"], row["owner"], row["follow_ons"], row["latest_note"]["text"]),
                         ("sr-chain-root", "任务 sr-chain-root", "active", "agent-a", 2, "第二段进展"))
        for rid in ("sr-chain-next", "sr-chain-third"):
            code, _, err = self.pf(ADMIN, "research", "close", rid, "--reason", "并入第一段")
            self.assertEqual(code, 0, err)
        [row] = board._tasks()
        self.assertEqual((row["id"], row["state"], row["owner"], row["follow_ons"], row["report_url"]),
                         ("sr-chain-root", "delivered", "agent-a", 2, "/dash/research/report?id=sr-chain-root"))
        script = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        self.assertIn("`含 ${t.follow_ons} 个接续任务`", script)

    def test_home_tasks_keep_public_job_phase_with_queue_state(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-phase", self.write(
            "create-phase", {"title": "状态口径", "objective": "queued 不被误报为运行中"}))[0], 201)
        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                               "resources": [], "actor_id": "agent-a", "research_id": "sr-phase"},
                                              b"", "phase-job")
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
        self.jobs.dispatch.queue.claim_next(4, admit=lambda row: row["job_id"] == jid)
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        item = board._tasks()[0]["jobs"][0]
        self.assertEqual(item["state"], "claimed")
        self.assertEqual(item["public"]["state"], "running")
        self.assertEqual(item["public"]["phase"], "queued")
        self.assertEqual(item["public"]["queue"]["state"], "claimed")

    def test_phonefarm_sampling_a_phone_is_not_someone_holding_it(self):
        """PF's own ~30 s sampling (label "observation") holds each idle phone for a moment; the home page shows those
        phones idle, and the count of idle phones matches, while a real holder on another phone still shows."""
        serials = ("S1", "S2", "S3")
        lock = {}

        class Goals:
            def list(self):
                return []

            def harness_support(self):
                return {"ok": False}

        class Monitor:
            @staticmethod
            def snapshot():
                return {"devices": [{"serial": s, "online": True, "state": "device"} for s in serials],
                        "lock": lock, "age_s": 1, "error": None, "deferred": False}

        def held_by(labels):
            entries = [{"scope": "device", "device_id": "d" + s, "serial": s, "label": label, "job_id": None, "state": "active"}
                       for s, label in labels.items()]
            lock.clear()
            lock.update({"status": "held", "held_by": ", ".join(labels.values()),
                         "lease": {"status": "held", "entries": entries, "device_scopes": {"devices": [
                             {"device_id": "d" + s, "status": "held" if s in labels else "clear"} for s in serials]}}})

        board = dash.Dash.__new__(dash.Dash)
        board.job_service, board.goals, board.monitor = self.jobs, Goals(), Monitor()
        board.shot_dir, board.steered, board._steer_pass = str(self.tmp), lambda _h: False, lambda: "steer"
        registry = {"devices": [{"device_id": "d" + s, "aliases": [s]} for s in serials]}

        held_by({s: "observation" for s in serials})   # all three sampled at once, as on mbp1
        home = board.home(object())
        self.assertEqual([(slot["holder"], slot["busy"]) for slot in home["slots"] if slot], [(None, False)] * 3)
        self.assertFalse(home["busy"])
        self.assertEqual([dash.Dash._device_busy(s, lock, registry) for s in serials], [False] * 3)

        held_by({"S1": "selfsr@S1", "S2": "observation", "S3": "observation"})   # a real holder beside the sampling
        home = board.home(object())
        self.assertEqual([(slot["holder"], slot["busy"]) for slot in home["slots"] if slot],
                         [({"kind": "external", "label": "selfsr@S1"}, True), (None, False), (None, False)])
        self.assertTrue(home["busy"])
        self.assertEqual([dash.Dash._device_busy(s, lock, registry) for s in serials], [True, False, False])

        script = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        self.assertIn("idle = on.filter(s => s.online !== false && !s.busy).length", script)
        self.assertIn("${idle} 台空闲", script)
        self.assertNotIn("空 ${free} 位", script, "the count is of idle phones, not of empty slots")

    def test_home_has_no_gol_or_demo_panels_and_reports_are_separate(self):
        page = (NODE / "dash_static/home.html").read_text(encoding="utf-8")
        script = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        gol_script = (NODE / "dash_static/gol.js").read_text(encoding="utf-8")
        for element in ('id="now-text"', 'id="human"', 'id="goalmd"', 'id="dlg-new"'):
            self.assertNotIn(element, page, "GOL panels belong on the GOL page, not the home page")
        self.assertIn('href="/dash/gol"', page)
        self.assertIn('id="reports"', page)
        for text in ("占用或待核查", "占用或状态待核查"):
            self.assertNotIn(text, script, "phone copy must follow the post-release state machine")

        class DemoGoals:
            def list(self):
                return [{"run_id": "demo", "demo": True, "status": "running", "state": {}}]

            def harness_support(self):
                return {"ok": False}

        class EmptyMonitor:
            @staticmethod
            def snapshot():
                return {"devices": [], "lock": {"status": "free", "lease": {"entries": []}},
                        "age_s": 1, "error": None, "deferred": False}

        board = dash.Dash.__new__(dash.Dash)
        board.job_service, board.goals, board.monitor = self.jobs, DemoGoals(), EmptyMonitor()
        board.shot_dir, board.steered, board._steer_pass = str(self.tmp), lambda _h: False, lambda: "steer"
        self.assertEqual(board.home(object())["runs"], [], "no demo run may reach either page's data")
        self.assertIn('h.runs = (h.runs || []).filter(r => !r.demo);', gol_script)
        self.assertIn('没有目标在跑', gol_script)
        self.assertIn('还没有目标', gol_script, "an empty GOL must say there is no goal, never show a demo")

    def test_a_web_note_can_be_addressed_to_another_agent(self):
        """点名: the composer lists the other registered agents; the saved note carries `to` and shows → agent."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-to", self.write(
            "create-to", {"title": "点名", "objective": "叫别的 agent 来帮忙", "owner": "agent-a"}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research?kind=research&id=sr-to")
                page.wait_for_selector("#task-note-to")
                self.assertEqual(page.eval_on_selector_all("#task-note-to option", "e => e.map(o => o.value)"), ["", "agent-b"])
                page.select_option("#task-note-to", "agent-b")
                page.fill("#task-note-text", "帮忙复现")
                page.click("#task-note-submit")
                page.wait_for_selector(".discuss-to")
                notes = self.web("GET", "/research-state/note?research_id=sr-to&limit=5")[1]["items"]
                self.assertEqual([(n["text"], n["to"]) for n in notes], [("帮忙复现", "agent-b")])
                self.assertEqual(page.text_content(".discuss-to"), "→ agent-b")
                self.assertEqual(page.input_value("#task-note-to"), "", "the next note is not addressed by accident")
            finally:
                browser.close()

    def test_a_chain_of_tasks_links_each_task_to_its_own_page(self):
        """Tasks that follow one another (research.follows) list as their root with a count. Each task has its own page
        with only its own records: the chain is a tree of links under the root's row and 子任务 / 接续自 at the top of
        the page; a note is written to the task on the page; the command view draws one tree."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for rid, follows in (("sr-root", None), ("sr-next", "sr-root")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "任务 " + rid, "objective": "接续", "follows": follows}))[0], 201)
            self.assertEqual(self.web("POST", f"/research-state/note/n-{rid}", self.write("create-n-" + rid, {
                "research_id": rid, "text": "进展 " + rid, "reply_to": None, "decision": None, "ask": False, "evidence_refs": []}))[0], 201)
        listing = self.web("GET", "/tasks")[1]["items"]
        self.assertEqual([(t["id"], t["chain"], t["follow_ons"]) for t in listing], [("sr-root", ["sr-root", "sr-next"], 1)])
        self.assertEqual([t["id"] for t in self.web("GET", "/tasks?chain=sr-next")[1]["items"]], ["sr-root", "sr-next"])
        self.assertEqual(sorted(t["id"] for t in self.web("GET", "/tasks?all=1")[1]["items"]), ["sr-next", "sr-root"])
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research")
                page.wait_for_selector(".research-choice")
                self.assertEqual(page.eval_on_selector_all(".research-choice strong", "e => e.map(x => x.textContent)"), ["任务 sr-root"])
                self.assertIn("含 1 个接续任务", page.text_content(".research-choice .research-meta"))
                page.goto(f"{base}/dash/research?kind=research&id=sr-root")
                # Read in the same step that finds the chain: a repaint drops the strip before drawing it again.
                segments = page.wait_for_function("""() => { const s = [...document.querySelectorAll('.chain-seg')];
                    return s.length && s.map(x => [x.dataset.seg, x.getAttribute('aria-current')]); }""").json_value()
                self.assertEqual(segments, [["sr-root", "page"], ["sr-next", None]])
                page.wait_for_function("() => document.querySelectorAll('#task-thread .discuss-entry').length === 1")
                self.assertEqual(page.eval_on_selector_all("#task-thread .discuss-text", "e => e.map(x => x.textContent)"), ["进展 sr-root"],
                                 "a task's page holds only its own notes")
                self.assertEqual(page.eval_on_selector_all("#task-summary .summary-task-title", "e => e.map(x => x.textContent)"), ["任务 sr-next"])
                # The tree in the list and the 子任务 card both go to the follow-on's own page.
                page.click('.chain-seg[data-seg="sr-next"]')
                page.wait_for_url("**id=sr-next**")
                page.wait_for_function("() => [...document.querySelectorAll('#task-thread .discuss-text')].map(x => x.textContent).join() === '进展 sr-next'")
                self.assertEqual(page.get_attribute('.chain-seg[data-seg="sr-next"]', "aria-current"), "page")
                self.assertEqual(page.text_content("#task-summary .summary-parent"), "接续自 任务 sr-root")
                self.assertIn("id=sr-root", page.get_attribute("#task-summary .summary-parent a", "href"))
                page.fill("#task-note-text", "写到第二段")
                page.click("#task-note-submit")
                page.wait_for_function("() => [...document.querySelectorAll('.discuss-text')].some(e => e.textContent === '写到第二段')")
                saved = [n for n in self.web("GET", "/research-state/note?research_id=sr-next&limit=5")[1]["items"] if n["text"] == "写到第二段"]
                self.assertEqual(len(saved), 1, "a note written on ②'s page belongs to ②")
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.evaluate("() => [...window.__command.world.trees.keys()].filter(id => id.startsWith('sr-'))"), ["sr-root"])
            finally:
                browser.close()

    def test_a_task_in_a_chain_reads_only_its_own_records_and_folds_what_agents_did(self):
        """A follow-on task's page reads its own directions, jobs (once for both sections), notes and comparisons, never
        the records of the tasks around it; 探索过程 and 作业 open folded to a heading with a count; the folded agent
        directory is not read at all."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for rid, follows in (("sr-a", None), ("sr-b", "sr-a"), ("sr-c", "sr-b")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "长链 " + rid, "objective": "只读本任务", "follows": follows}))[0], 201)
            self.assertEqual(self.web("POST", f"/research-state/note/n-{rid}", self.write("create-n-" + rid, {
                "research_id": rid, "text": "进展 " + rid, "reply_to": None, "decision": None}))[0], 201)
        for rid in ("sr-a", "sr-b"):
            self.assertEqual(self.pf(ADMIN, "research", "deliver", rid, "--summary", "# 交付 " + rid, "--artifact", f"job:{self.jid}",
                                     "--game-version", "5.1", "--device", "FixturePhone")[0], 0)
        for i in range(20):
            self.assertEqual(self.web("POST", f"/research-state/direction/d-long-{i}", self.write(f"create-d-long-{i}", {
                "research_id": "sr-c", "title": f"方向 {i}", "hypothesis": "收起来的探索过程"}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                asked = []
                page.on("request", lambda request: asked.append(request.url.split("/dash/api", 1)[-1]) if "/dash/api/" in request.url else None)
                reads = lambda part: [url for url in asked if part in url]
                page.goto(f"{base}/dash/research?kind=research&id=sr-c")
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 20")
                page.wait_for_function("() => document.querySelectorAll('#task-thread .discuss-entry').length === 1")
                self.assertEqual(page.text_content("#task-thread .discuss-text"), "进展 sr-c")
                which = lambda part, key: sorted(url.split(key + "=")[1].split("&")[0] for url in reads(part))
                # 作业 sections share one read; the top banner adds its own bounded outcome=failed read (limit=1).
                self.assertEqual(sorted(url.split("/dash/api", 1)[-1] for url in reads("/jobs?research=")),
                                 ["/jobs?research=sr-c&limit=50", "/jobs?research=sr-c&outcome=failed&limit=1"],
                                 "one jobs read for both sections plus the banner's bounded failed read")
                self.assertEqual(which("/research-state/note?", "research_id"), ["sr-c"])
                self.assertEqual(which("/research-state/direction?", "research_id"), ["sr-c"])
                self.assertEqual(which("/research-state/request?", "research_id"), ["sr-c"])
                self.assertEqual(reads("/research-state/research/sr-a") + reads("/research-state/research/sr-b"), [],
                                 "the tasks around it are links, not reads")
                self.assertEqual(page.eval_on_selector_all("#task-detail .task-report", "e => e.length"), 0, "only this task's delivery (none yet)")
                # 探索过程 and 作业 are folded to their heading line, with a count.
                folds = page.eval_on_selector_all(".task-fold", "e => e.map(x => [x.closest('section').id, x.open, x.querySelector('summary h3').textContent, x.querySelector('summary .count').textContent])")
                self.assertEqual(folds, [["task-directions", False, "探索过程", "20"], ["task-runs", False, "作业", ""]])
                self.assertFalse(page.is_visible("#task-directions .research-direction"))
                page.click("#task-directions .task-fold > summary")
                self.assertTrue(page.is_visible('#task-directions [data-direction="d-long-0"]'))
                self.assertEqual(reads("/duties") + reads("/jobs?limit=50"), [], "the folded agent directory is not read")
            finally:
                browser.close()

    def test_the_task_page_shows_a_mosaic_of_job_screenshots_and_labels_identical_ones(self):
        """K3: a job's images appear as a mosaic on the task page without opening the job; byte-identical screenshots
        collapse into one tile labelled with their count. The folded section asks for nothing."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        rid = "sr-mosaic"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-mosaic", {"title": "Doom D 轨截图", "objective": "出图的作业直接看到画面"}))[0], 201)
        submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                               "resources": [], "actor_id": "agent-a", "research_id": rid}, b"", "mosaic-job")
        jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True, exist_ok=True)
        import struct, zlib

        def png(payload):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            fill = bytes([(payload[0] % 250) + 1]) * 3
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + fill * 64) * 32)) + chunk(b"IEND", b""))
        frame = png(b"same")
        (out / "shot-a.png").write_bytes(frame)
        (out / "shot-b.png").write_bytes(frame)   # same tier, byte for byte
        (out / "shot-c.png").write_bytes(png(b"changed"))
        (out / "metrics.json").write_text('{"fps": 60}')
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                asked = []
                page.on("request", lambda request: asked.append(request.url) if "/jobs/" in request.url and "/images" in request.url else None)
                page.goto(f"{base}/dash/research?kind=research&id={rid}")
                page.wait_for_function("() => document.querySelectorAll('#task-runs .run-row').length === 1")
                page.wait_for_timeout(400)
                self.assertEqual(asked, [], "the folded 作业 section reads no image inventories")
                page.click("#task-runs .task-fold > summary")
                page.wait_for_function("() => document.querySelectorAll('#task-runs .mosaic-tile img').length === 2")
                page.wait_for_function("() => [...document.querySelectorAll('#task-runs .mosaic-tile img')]"
                                       ".every(img => img.complete && img.naturalWidth > 0)")
                labels = page.eval_on_selector_all("#task-runs .mosaic-tile .dup", "e => e.map(x => x.textContent)")
                self.assertEqual(labels, ["2 张完全相同"], f"identical screenshots collapse with a count: {labels}")
                paths = page.eval_on_selector_all("#task-runs .mosaic-tile img", "e => e.map(x => x.alt)")
                # Either of the byte-identical pair may represent the group; shot-c must be its own tile.
                self.assertEqual(len(paths), 2)
                self.assertIn("shot-c.png", paths)
                self.assertIn(paths[0] if paths[0] != "shot-c.png" else paths[1], ("shot-a.png", "shot-b.png"))
                self.assertEqual(len(asked), 1, f"one inventory read for the job: {asked}")
            finally:
                browser.close()

    def test_a_revision_that_just_touches_the_task_is_not_an_assignment(self):
        """第 29 批复查 A：assigned_task 只在 owner 从别人变成本人时报；人改目标、关任务、重新打开都不算指派，
        重新打开也不和 research_reopened 重复报。"""
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        rid = "sr-editnotassign"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-ena", {"title": "改字段不是改派", "objective": "原目标"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "claim", rid)[0], 0)
        kinds = [d["kind"] for d in self.duties(token)["duties"]]
        self.assertNotIn("assigned_task", kinds, f"claim 后最新修订是本人，首签到不应报指派: {kinds}")
        current = self.cli("GET", f"/research-state/research/{rid}")[1]
        edit = dict(self.write("edit-ena", {**current["data"], "objective": "补充后的目标"}),
                    base_revision=current["revision"], reason="补充说明")
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", edit)[0], 201)
        self.assertNotIn("assigned_task", [d["kind"] for d in self.duties(token)["duties"]],
                         "人只改了 objective，owner 没动，却报了 assigned_task")
        current = self.cli("GET", f"/research-state/research/{rid}")[1]
        close = dict(self.write("close-ena", {**current["data"], "state": "closed", "state_reason": "并入别的任务"}),
                     base_revision=current["revision"], reason="关掉")
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", close)[0], 201)
        kinds = [d["kind"] for d in self.duties(token)["duties"]]
        self.assertNotIn("assigned_task", kinds, f"任务被人关掉，agent 却被告知「指派给你…再写计划」: {kinds}")
        current = self.cli("GET", f"/research-state/research/{rid}")[1]
        reopen = dict(self.write("reopen-ena", {**current["data"], "state": "active", "state_reason": "接着做完"}),
                      base_revision=current["revision"], reason="重新打开")
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", reopen)[0], 201)
        kinds = [d["kind"] for d in self.duties(token)["duties"]]
        self.assertNotIn("assigned_task", kinds, f"重新打开被当成了改派，和 research_reopened 重复: {kinds}")
        self.assertIn("research_reopened", kinds)
        # 真改派仍然报。
        current = self.cli("GET", f"/research-state/research/{rid}")[1]
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-b"})[0], 201)
        reassign = dict(self.write("reassign-ena", {**current["data"], "owner": "agent-b"}),
                        base_revision=current["revision"], reason="改派给 agent-b")
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", reassign)[0], 201)
        token_b = self.cli("POST", "/agents/agent-b/token", {})[1]["token"]
        self.assertIn("assigned_task", [d["kind"] for d in self.duties(token_b)["duties"]])

    def test_urgent_notes_and_new_assignments_lead_the_next_checkin(self):
        """K3-2: a note marked 紧急 lands at the top of the addressee's next check-in duties (or the task owner's,
        when no one is named), and a task newly assigned to an agent shows up there too; self-claims, repeat
        check-ins and answered notes do not re-raise it."""
        tokens = {}
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
            tokens[name] = self.cli("POST", f"/agents/{name}/token", {})[1]["token"]
        rid = "sr-assign"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-assign-urgent", {"title": "指派与紧急", "objective": "签到要看见"}))[0], 201)
        # A self-claim is not an assignment: the claimer already knows.
        self.assertEqual(self.pf(tokens["agent-a"], "research", "claim", rid)[0], 0)
        self.assertNotIn("assigned_task", [item["kind"] for item in self.duties(tokens["agent-a"])["duties"]])
        # A human reassignment lands in the assignee's next check-in, once.
        current = self.cli("GET", f"/research-state/research/{rid}")[1]
        reassign = dict(self.write("assign-urgent", {**current["data"], "owner": "agent-b"}),
                        base_revision=current["revision"], reason="指派给 agent-b")
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", reassign)[0], 201)
        duties = self.duties(tokens["agent-b"])["duties"]
        assigned = [item for item in duties if item["kind"] == "assigned_task"]
        self.assertEqual([(item["research_id"], item["unread"]) for item in assigned], [(rid, True)])
        self.assertNotIn("assigned_task", [item["kind"] for item in self.duties(tokens["agent-b"])["duties"]])
        self.assertNotIn("assigned_task", [item["kind"] for item in self.duties(tokens["agent-a"])["duties"]])
        # An urgent note leads the addressee's duties and never duplicates as an ordinary one; an urgent note on the
        # owned task reaches the owner even when nobody is named.
        self.assertEqual(self.pf(tokens["agent-a"], "research", "note", rid, "先修设备锁，别的事放一放",
                                 "--to", "agent-b", "--urgent")[0], 0)
        duties = self.duties(tokens["agent-b"])["duties"]
        self.assertEqual(duties[0]["kind"], "urgent_note", f"the urgent note leads: {[d['kind'] for d in duties]}")
        self.assertIn("紧急", duties[0]["text"])
        self.assertNotIn("answer_note", [item["kind"] for item in duties])
        self.assertEqual(self.pf(tokens["agent-b"], "research", "note", rid, "收到，先修锁",
                                 "--reply-to", self.cli("GET", f"/research-state/note?research_id={rid}&limit=5")[1]["items"][-1]["id"],
                                 "--decision", "adopt")[0], 0)
        self.assertNotIn("urgent_note", [item["kind"] for item in self.duties(tokens["agent-b"])["duties"]])
        self.assertEqual(self.web("POST", f"/research-state/note/n-owner-urgent", self.write(
            "note-owner-urgent", {"research_id": rid, "text": "负责人在就现在看", "reply_to": None, "decision": None,
                                   "ask": False, "to": None, "urgent": True}))[0], 201)
        duties = self.duties(tokens["agent-b"])["duties"]
        self.assertEqual(duties[0]["kind"], "urgent_note")
        self.assertEqual(self.pf(tokens["agent-b"], "research", "note", rid, "看过了",
                                 "--reply-to", "n-owner-urgent", "--decision", "adopt")[0], 0)
        self.assertEqual(self.web("POST", "/research-state/note/n-plain", self.write(
            "note-plain", {"research_id": rid, "text": "普通的一条", "reply_to": None, "decision": None,
                            "ask": False, "to": "agent-b", "urgent": False}))[0], 201)
        kinds = [item["kind"] for item in self.duties(tokens["agent-b"])["duties"]]
        self.assertEqual(kinds[0], "answer_note", f"a plain note does not lead: {kinds}")
        self.assertNotIn("urgent_note", kinds)

    def test_the_task_page_leads_with_the_latest_failed_job_and_marks_urgent_notes(self):
        """K3-2: the task page top shows the latest failed job (job id, one line of reason, time) when there is one
        and nothing when there is not; the composer's 紧急 checkbox saves a note the page marks 紧急."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        rid = "sr-failed-lead"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-failed-lead", {"title": "失败置顶", "objective": "不用翻作业就知道为什么黄了"}))[0], 201)

        def fail(key, label):
            submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                                   "resources": [], "actor_id": "agent-a", "research_id": rid,
                                                   "label": label}, b"", key)
            jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
            self.jobs.dispatch.queue.set_state(jid, "done")
            now = time.time()
            (Path(self.jobs.path(jid)) / "meta.json").write_text(json.dumps(
                {"id": jid, "bin": "fixture", "argv": ["-c", "pass"], "label": label, "actor_id": "agent-a",
                 "research_id": rid, "started": now - 60, "ended": now, "state": "finished", "phase": "finished",
                 "outcome": "failed", "exit": 2, "error": "minicap 不在这个镜像里：minicap: not found",
                 "execution": {"kind": "supervised-v1"}}))
            return jid
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research?kind=research&id={rid}")
                page.wait_for_selector("#task-thread")
                self.assertFalse(page.is_visible("#task-failed"), "a task with no failed job shows no banner")
                older = fail("fail-old", "第一次失败")
                newest = fail("fail-new", "第二次失败")
                page.reload()
                page.wait_for_selector("#task-failed .failed-line")
                self.assertIn(f"作业 {newest}", page.text_content("#task-failed .failed-line"))
                self.assertIn("minicap", page.text_content("#task-failed .failed-why"))
                link = page.get_attribute("#task-failed .failed-line a", "href")
                self.assertIn(newest, link, f"the banner links the latest failure: {link}")
                # The composer's checkbox saves an urgent note; the thread marks it.
                page.fill("#task-note-text", "这个失败挡住了 D 轨，请优先看")
                page.check("#task-note-urgent")
                page.click("#task-note-submit")
                page.wait_for_function("() => [...document.querySelectorAll('.discuss-head span')]"
                                       ".some(x => x.textContent === '紧急')")
                notes = self.web("GET", f"/research-state/note?research_id={rid}&limit=10")[1]["items"]
                self.assertEqual([(n["text"], n["urgent"]) for n in notes],
                                 [("这个失败挡住了 D 轨，请优先看", True)])
                self.assertEqual(page.is_checked("#task-note-urgent"), False, "the checkbox does not stick")
            finally:
                browser.close()

    def test_a_failure_outside_the_latest_fifty_jobs_still_leads_the_task_page(self):
        """第 29 批复查 B：「最近失败」按 outcome=failed 单独读最新一条，不落在「作业」区最新 50 条窗口内也显示。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        rid = "sr-window"
        self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
            "create-window", {"title": "五十条以外", "objective": "窗口外的失败"}))[0], 201)
        base = time.time() - 5000
        for i in range(55):
            submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                                   "resources": [], "actor_id": "agent-a", "research_id": rid,
                                                   "label": f"run-{i}"}, b"", f"key-window-{i}")
            jid = submitted["job"]["id"] if "job" in submitted else submitted["id"]
            self.jobs.dispatch.queue.set_state(jid, "done")
            (Path(self.jobs.path(jid)) / "meta.json").write_text(json.dumps(
                {"id": jid, "bin": "fixture", "argv": ["-c", "pass"], "label": f"run-{i}", "actor_id": "agent-a",
                 "research_id": rid, "started": base + i, "ended": base + i + 10, "state": "finished",
                 "phase": "finished", "outcome": "failed" if i == 0 else "succeeded",
                 "exit": 2 if i == 0 else 0, "error": "minicap 不在这个镜像里: not found" if i == 0 else None,
                 "execution": {"kind": "supervised-v1"}}))
        page50 = self.web("GET", f"/jobs?research={rid}&limit=50")[1]
        self.assertEqual(len(page50["items"]), 50)
        self.assertNotIn("failed", [job["outcome"] for job in page50["items"]], "窗口内看不见失败")
        latest = self.web("GET", f"/jobs?research={rid}&outcome=failed&limit=1")[1]["items"]
        self.assertEqual([job["label"] for job in latest], ["run-0"], "outcome=failed 直接取到那次失败")
        bad_code, bad = self.web("GET", f"/jobs?research={rid}&outcome=victory&limit=1")
        self.assertEqual(bad_code, 400)
        url = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": c.split("=", 1)[0], "value": c.split("=", 1)[1], "url": url}
                                     for c in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{url}/dash/research?kind=research&id={rid}")
                page.wait_for_selector("#task-thread")
                page.wait_for_selector("#task-failed .failed-line")
                self.assertIn("run-0", page.text_content("#task-failed"),
                              "任务有失败作业（在最新 50 条之外），页面的「最近失败」却整块消失")
            finally:
                browser.close()

    def test_a_chain_grows_along_what_each_direction_is_based_on(self):
        """Directions hang under the direction (and run) they are based on, on the task page and in the command view.
        The chain's tasks are a tree of links in the task list under the root; a task's 探索过程 is indented by based_on
        with 「基于 …」 (naming a direction of the task it follows too), the line to the newest standing direction is
        marked and a rejected dead end folds. In the command view a
        direction is one generation right of its parent, the run it builds on is a pill on the edge and a gold dot,
        the main line runs to the newest standing direction, and a follow-on task without directions is a stub."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for rid, follows in (("lin-root", None), ("lin-next", "lin-root"), ("lin-side", "lin-root"), ("lin-rep", "lin-next")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "继承 " + rid, "objective": "沿着继承往外长", "follows": follows}))[0], 201)

        def direction(did, rid, title, based_on=None, **extra):
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write("create-" + did, {
                "research_id": rid, "title": title, "hypothesis": "继承 " + did, "based_on": based_on, **extra}))[0], 201)

        def run(did, rid, operation):
            submitted = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": [],
                                                   "actor_id": "agent-a", "research_id": rid, "direction_id": did}, b"", operation)
            return submitted["job"]["id"] if "job" in submitted else submitted["id"]

        direction("d-a", "lin-root", "方向 A：基线")
        direction("d-b", "lin-root", "方向 B：在 A 上改", {"direction_id": "d-a", "job_id": None})
        picked, other = run("d-b", "lin-root", "run-b-c"), run("d-b", "lin-root", "run-b-e")
        direction("d-x", "lin-root", "方向 X：试过放弃", {"direction_id": "d-a", "job_id": None}, state="rejected", state_reason="没有收益")
        direction("d-c", "lin-next", "方向 C：以 B 的那次运行为基础", {"direction_id": "d-b", "job_id": picked})
        direction("d-d", "lin-rep", "方向 D：复现 C", {"direction_id": "d-c", "job_id": None})
        self.assertEqual({d["id"]: d["based_on"] for d in self.web("GET", "/command")[1]["directions"] if d["id"] in ("d-a", "d-c")},
                         {"d-a": None, "d-c": {"direction_id": "d-b", "job_id": picked}}, "the command model carries based_on")
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/research?kind=research&id=lin-root")
                # The chain is a tree under its root's row in the task list, not a strip over the task.
                tree = '#object-list li[data-id="lin-root"] .chain-strip'
                page.wait_for_selector(tree)
                self.assertEqual(page.locator("#task-detail .chain-strip").count(), 0)
                self.assertEqual(page.eval_on_selector_all(tree + " .chain-seg", "e => e.map(x => [x.dataset.seg, x.classList.contains('nested')])"),
                                 [["lin-root", False], ["lin-next", True], ["lin-rep", True], ["lin-side", True]])
                self.assertEqual(page.get_attribute('.chain-seg[data-seg="lin-rep"]', "style"), "--depth: 2;")
                # 探索过程 (opened): this task's directions, each under the one it builds on, the main line marked.
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 3")
                page.click("#task-directions .task-fold > summary")
                rows = page.eval_on_selector_all("#task-directions .research-direction", """e => e.map(x => [x.dataset.direction,
                  getComputedStyle(x).getPropertyValue('--depth').trim(), x.classList.contains('on-main'), x.offsetParent !== null])""")
                self.assertEqual(rows, [["d-a", "0", True, True], ["d-b", "1", True, True], ["d-x", "1", False, False]])
                self.assertIn("1 条没往下走的分支：方向 X：试过放弃（已排除）", page.text_content("#task-directions .direction-fold"))
                based = lambda did: page.text_content(f'#task-directions [data-direction="{did}"] .direction-base')
                self.assertEqual(based("d-b"), "基于 「方向 A：基线」")
                self.assertEqual(sorted(page.eval_on_selector_all('[data-direction="d-b"] .run-row', "e => e.map(x => x.dataset.job)")), sorted([picked, other]))
                page.click("#task-directions .direction-fold")
                self.assertTrue(page.is_visible('[data-direction="d-x"]'), "the fold opens in place")
                # The follow-on's own page: its direction names the one it builds on in the task before, and the job.
                page.click('.chain-seg[data-seg="lin-next"]')
                page.wait_for_url("**id=lin-next**")
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 1")
                page.wait_for_function("() => document.querySelector('#task-directions .direction-base')?.textContent.endsWith('那次作业')")
                self.assertTrue(based("d-c").startswith("基于 「方向 B：在 A 上改」 里的 ") and based("d-c").endswith(" 那次作业"), based("d-c"))
                self.assertTrue(page.get_attribute('[data-direction="d-c"] .direction-base a', "href").endswith(f"#job={picked}"))
                self.assertEqual(page.evaluate("() => getComputedStyle(document.querySelector('[data-direction=\"d-c\"]')).getPropertyValue('--depth').trim()"), "0")
                # Command view: one generation per based_on, the picked run as a pill and a gold dot, the main line, a stub.
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                shape = page.evaluate("""() => { const tr = window.__command.world.trees.get('lin-root');
                  return {depth: Object.fromEntries(tr.shape.nodes.map(n => [n.key, n.depth])), main: tr.main,
                          pills: tr.shape.pills.map(p => [p.parent.key, p.job.id, p.kids.map(k => k.key)]),
                          picked: [...document.querySelectorAll('.tree[data-tree="lin-root"] .leaf.picked')].map(x => x.dataset.key),
                          tags: Object.fromEntries([...document.querySelectorAll('.tree[data-tree="lin-root"] .chip')].map(x => [x.dataset.key, x.querySelector('.step')?.textContent || ''])),
                          x: Object.fromEntries([...document.querySelectorAll('.tree[data-tree="lin-root"] .chip.rejected')].map(x => [x.dataset.key, x.classList.contains('main')])),
                          stub: document.querySelector('.tree[data-tree="lin-root"] .stub')?.textContent}; }""")
                self.assertEqual(shape["depth"], {"d-a": 1, "d-b": 2, "d-x": 2, "d-c": 3, "d-d": 4, "stub:lin-side": 1})
                self.assertEqual(shape["pills"], [["d-b", picked, ["d-c"]]])
                self.assertEqual(shape["picked"], [picked])
                self.assertEqual(shape["main"], "d-d|d-c|d-b|d-a")
                self.assertEqual(shape["x"], {"d-x": False}, "the rejected branch is drawn, off the main line")
                self.assertEqual((shape["tags"]["d-a"], shape["tags"]["d-c"][:1], shape["tags"]["d-d"][:1]), ("", "②", "③"), "a follow-on's directions name their task")
                self.assertTrue(shape["stub"].startswith("④ 继承 lin-side") and shape["stub"].endswith("无方向"), shape["stub"])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_task_page_shows_its_own_report_and_links_the_task_that_follows(self):
        """A task page shows only its own delivery (its conclusion at the top, a link to its report at the bottom), never
        that of the task that follows it; that task is a 子任务 card leading to its own page, which shows its own and
        names the task it follows."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for rid, follows in (("rp-old", None), ("rp-new", "rp-old")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "交付 " + rid, "objective": "最新的交付在前", "follows": follows}))[0], 201)
            self.assertEqual(self.pf(ADMIN, "research", "deliver", rid, "--summary", f"# 报告 {rid}\n\n结论 {rid}", "--artifact", f"job:{self.jid}",
                                     "--game-version", "5.1", "--device", "FixturePhone")[0], 0)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors, reads = [], []
                page.on("request", lambda request: reads.append(request.url) if "/research-state/" in request.url else None)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/research?kind=research&id=rp-old")
                page.wait_for_selector("#task-report")
                page.wait_for_selector("#task-thread .section-head")
                report = lambda: (page.text_content("#task-summary .summary-conclusion .research-report"),
                                  page.get_attribute("#task-report .summary-link", "href").split("?", 1)[1].split("&")[0])
                self.assertEqual(page.locator(".task-report").count(), 1)
                self.assertEqual(report(), ("结论 rp-old", "id=rp-old"), "the delivery of the task that follows is on its own page")
                self.assertFalse([url for url in reads if "rp-new" in url], "nothing of the task that follows is read")
                page.click('#task-summary .summary-task[href*="id=rp-new"]')
                page.wait_for_url("**id=rp-new**")
                page.wait_for_selector('#task-report .summary-link[href*="id=rp-new"]')
                self.assertEqual(page.locator(".task-report").count(), 1)
                self.assertEqual(report(), ("结论 rp-new", "id=rp-new"))
                self.assertIn("交付 rp-old", page.text_content("#task-summary .summary-parent"))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_the_main_line_ends_at_the_newest_direction_even_after_an_older_one_is_edited(self):
        """Both views end the main line at the newest standing direction by creation. Editing an older direction (as a
        based_on backfill with research update does) moves its updated_at, not where the line ends. List items carry
        created_at, the same as a single read."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.assertEqual(self.web("POST", "/research-state/research/ml-root", self.write(
            "create-ml-root", {"title": "主线", "objective": "主线停在最新的方向"}))[0], 201)
        for did, based_on in (("m-a", None), ("m-b", "m-a"), ("m-c", "m-a")):
            self.assertEqual(self.web("POST", f"/research-state/direction/{did}", self.write("create-" + did, {
                "research_id": "ml-root", "title": "方向 " + did, "hypothesis": "主线 " + did,
                "based_on": {"direction_id": based_on, "job_id": None} if based_on else None}))[0], 201)
        older = self.web("GET", "/research-state/direction/m-b")[1]
        payload = self.write("edit-m-b", {**older["data"], "hypothesis": "后来补写的假设"})
        payload.update(base_revision=older["revision"], reason="补写")
        self.assertEqual(self.web("POST", "/research-state/direction/m-b", payload)[0], 201)
        items = {item["id"]: item for item in self.web("GET", "/research-state/direction?research_id=ml-root&limit=10")[1]["items"]}
        self.assertEqual({did: item["created_at"] for did, item in items.items()},
                         {did: self.web("GET", f"/research-state/direction/{did}")[1]["created_at"] for did in items})
        self.assertGreater(items["m-b"]["updated_at"], items["m-c"]["updated_at"], "the edit made m-b the latest touched")
        self.assertLess(items["m-b"]["created_at"], items["m-c"]["created_at"])
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/research?kind=research&id=ml-root")
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 3")
                self.assertEqual(page.eval_on_selector_all("#task-directions .research-direction.on-main", "e => e.map(x => x.dataset.direction)"), ["m-a", "m-c"])
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.evaluate("() => window.__command.world.trees.get('ml-root').main"), "m-c|m-a")
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_task_closes_with_a_reason_stays_findable_and_reopens(self):
        """关闭任务 asks why; the closed task shows the reason, folds into 已关闭 on the home page, and 重新打开 makes it active."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")

        class Quiet:
            def request(self, **kw):
                return None

            def snapshot(self):
                return {"devices": [], "lock": {"status": "free", "lease": {"entries": []}}, "age_s": 1, "error": None, "deferred": False}

        class Goals:
            def list(self):
                return []

            def harness_support(self):
                return {"ok": False}
        self.board.monitor, self.board.goals, self.board.shot_dir = Quiet(), Goals(), str(self.tmp)
        self.assertEqual(self.web("POST", "/research-state/research/sr-close", self.write(
            "create-close", {"title": "要关的任务", "objective": "关闭与重开"}))[0], 201)
        task = lambda: self.web("GET", "/research-state/research/sr-close")[1]["data"]
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research?kind=research&id=sr-close")
                page.click("#task-state-open")
                page.click(".task-state-form button[type=submit]")
                self.assertEqual(task()["state"], "active", "closing needs a reason")
                page.fill("#task-state-reason", "并入别的任务")
                page.click(".task-state-form button[type=submit]")
                page.wait_for_selector(".task-closed")
                self.assertEqual((task()["state"], task()["state_reason"]), ("closed", "并入别的任务"))
                self.assertIn("并入别的任务", page.text_content(".task-closed"))
                self.assertEqual(page.text_content("#task-state-open"), "重新打开")
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                # The model behind the view is cached for a few seconds; the live view picks the close up on a later poll.
                page.wait_for_function("() => window.__command.world.trees.get('sr-close')?.label.querySelector('.tag').textContent === '已关闭'", timeout=20000)
                self.assertEqual(page.text_content('#counts span[data-k="closed"] b'), "1", "a closed task is not counted as 进行中")
                self.assertEqual(page.evaluate("""async () => {
                    const {taskAt} = await import('/dash/static/command-util.js');
                    const a = {state: 'delivered', created_at: 1, updated_at: 5}, b = {state: 'closed', created_at: 6, updated_at: 9};
                    return [4, 5, 7, 10].map(t => taskAt({...a, steps: [a, b]}, t)); }"""), ["active", "delivered", "active", "delivered"],
                    "a chain whose last step closed after an earlier one delivered reads as delivered")
                page.goto(f"{base}/dash")
                page.wait_for_selector('#closed li[data-closed="sr-close"]', state="attached")
                self.assertFalse(page.is_hidden("#closed-sec"))
                self.assertIn("并入别的任务", page.text_content('#closed li[data-closed="sr-close"]'))
                self.assertEqual(page.eval_on_selector_all('#tasks [data-task="sr-close"]', "e => e.length"), 0)
                page.goto(f"{base}/dash/research?kind=research&id=sr-close")
                page.click("#task-state-open")
                page.click(".task-state-form button[type=submit]")
                page.wait_for_function("() => document.getElementById('task-state-open')?.textContent === '关闭任务'")
                self.assertEqual((task()["state"], task()["state_reason"]), ("active", "网页上重新打开"))
            finally:
                browser.close()

    def test_the_command_view_collapses_its_rail_when_the_window_narrows(self):
        """Narrowing a desktop window to phone width collapses the rail as a phone load does, so the map keeps the
        width; widening again restores it. A rail the user opened by hand stays open."""
        with self._browser_page() as page:
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto("http://%s:%d/dash/command" % self.address)
            page.wait_for_function("() => window.__ready === true", timeout=20000)
            collapsed = "() => document.getElementById('app').classList.contains('rail-collapsed')"
            self.assertFalse(page.evaluate(collapsed))
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function(collapsed, timeout=5000)
            page.wait_for_timeout(400)
            self.assertGreater(page.evaluate("() => document.getElementById('stage').getBoundingClientRect().width"), 300)
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_function("() => !document.getElementById('app').classList.contains('rail-collapsed')", timeout=5000)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function(collapsed, timeout=5000)
            page.click("#rail-toggle")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(200)
            self.assertFalse(page.evaluate(collapsed), "the rail opened by hand stays open")

    def test_the_gol_top_bar_on_a_phone_keeps_its_actions_clear_of_the_brand(self):
        """At 390 px GOL's four top actions (status, unlock, new goal, home) do not fit beside the brand and node: they
        take their own row instead of squeezing the 实时 badge over the HG mark."""
        with self._browser_page() as page:
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto("http://%s:%d/dash/gol" % self.address)
            page.wait_for_selector(".shell-top .shell-actions #live")
            page.wait_for_timeout(300)
            found = page.evaluate("""() => {
              const items = [document.querySelector('.shell-brand'), document.querySelector('.shell-node'), ...document.querySelectorAll('.shell-actions > *')]
                .filter(e => e.getBoundingClientRect().width);
              const boxes = items.map(e => e.getBoundingClientRect());
              let overlaps = 0, outside = 0;
              for (let i = 0; i < boxes.length; i++) {
                if (boxes[i].left < 0 || boxes[i].right > innerWidth) outside++;
                for (let j = i + 1; j < boxes.length; j++) { const a = boxes[i], b = boxes[j]; if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) overlaps++; }
              }
              return {overlaps, outside, live: document.getElementById('live').getBoundingClientRect().height};
            }""")
            self.assertEqual((found["overlaps"], found["outside"]), (0, 0), found)
            self.assertLess(found["live"], 30, "the status badge stays on one line")

    def test_every_input_pattern_compiles_as_the_browser_compiles_it(self):
        """Browsers compile an input's pattern with the v flag, where a bare - in a character class is an error: the
        field then logs a console error and its native check does nothing."""
        static = Path(dash.__file__).resolve().parent / "dash_static"
        patterns = {f"{page.name}: {value}": value.replace("&quot;", '"').replace("&amp;", "&")
                    for page in sorted(static.glob("*.html")) for value in re.findall(r'\bpattern="([^"]*)"', page.read_text(encoding="utf-8"))}
        self.assertTrue(patterns)
        with self._browser_page() as page:
            bad = page.evaluate("""patterns => Object.entries(patterns).filter(([, p]) => {
              try { new RegExp('^(?:' + p + ')$', 'v'); return false; } catch (_) { return true; } }).map(([where]) => where)""", patterns)
        self.assertEqual(bad, [])

    def test_a_link_to_a_missing_task_says_so_in_chinese(self):
        """/dash/research?kind=research&id=<unknown> explains in the page's language that this node has no such task,
        instead of showing the API's English error text."""
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/research?kind=research&id=nope" % self.address)
            page.wait_for_function("() => document.getElementById('detail-message').classList.contains('task-error')", timeout=20000)
            shown = page.text_content("#detail-message")
            self.assertIn("这台节点上没有这个任务（nope）", shown)
            self.assertNotIn("not found", shown)

    def test_login_button_and_command_view_secondary_text_meet_normal_text_contrast(self):
        """WCAG AA asks 4.5:1 for normal-size text: the login button's white label on its blue (light and dark scheme),
        and the command view's muted labels on every panel background they sit on."""
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def contrast(a, b):
            high, low = sorted((luminance(a), luminance(b)), reverse=True)
            return (high + 0.05) / (low + 0.05)

        def tokens(css):
            return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css))
        login = dash.LOGIN_HTML
        button = re.search(r"button\{[^}]*background:var\(--([a-z]+)\)[^}]*color:(#fff)", login)
        for scheme in login.split("@media (prefers-color-scheme:dark)"):
            value = tokens(scheme).get(button.group(1))
            if value:
                self.assertGreaterEqual(contrast(value, "#ffffff"), 4.5, value)
        css = tokens((Path(dash.__file__).resolve().parent / "dash_static" / "command.css").read_text(encoding="utf-8"))
        for background in ("bg", "bg2", "raise", "raise2"):
            self.assertGreaterEqual(contrast(css["muted"], css[background]), 4.5, background)

    def test_every_page_title_uses_the_same_size(self):
        """The page titles of the pages under the shared top bar use one scale (the shell's --fs-xl), on a desktop and
        on a phone."""
        pages = ("research", "fleet", "jobs", "results", "games", "doctor", "datasets", "structure", "research/report", "research/flow", "research/open")
        with self._browser_page() as page:
            page.route("**/dash/api/**", lambda route: route.abort())   # the titles are static; no page data is needed
            for width in (1280, 390):
                page.set_viewport_size({"width": width, "height": 900})
                sizes = {}
                for name in pages:
                    page.goto("http://%s:%d/dash/%s" % (self.address + (name,)))
                    page.wait_for_selector("main h1")
                    sizes[name] = page.eval_on_selector("main h1", "h => getComputedStyle(h).fontSize + ' ' + getComputedStyle(h).fontWeight")
                self.assertEqual(set(sizes.values()), {"20px 650"}, (width, sizes))

    def test_the_task_list_puts_the_most_recently_active_task_first(self):
        """A note on an older task makes it the most recent: the list follows activity, not the task record's revision."""
        for rid in ("sr-older", "sr-newer"):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": rid, "objective": "排序"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/note/n-older", self.write("create-n-older", {
            "research_id": "sr-older", "text": "新进展", "reply_to": None, "decision": None, "ask": False, "evidence_refs": []}))[0], 201)
        status, listing = self.web("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(listing["node_id"], self.node_id)
        self.assertEqual([item["id"] for item in listing["items"]], ["sr-older", "sr-newer"])
        older = listing["items"][0]
        self.assertGreater(older["active_at"], older["updated_at"], "the note, not the record, made it recent")
        self.assertEqual([item["id"] for item in self.web("GET", "/tasks?research_id=sr-newer")[1]["items"]], ["sr-newer"])
        self.assertEqual(self.web("GET", "/tasks?limit=5")[0], 400)
        script = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        self.assertIn("listKind === 'research' ? `${API}/tasks?${query}`", script)
        self.assertIn("time(item.active_at || item.updated_at)", script)

    def test_direction_list_exposes_hypothesis_for_task_page(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-direction-view", self.write(
            "create-direction-view", {"title": "方向展示", "objective": "任务页直接看到探索假设"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-view", self.write(
            "create-d-view", {"research_id": "sr-direction-view", "title": "低渲染分辨率",
                               "hypothesis": "降低内部渲染分辨率可提高帧率"}))[0], 201)
        status, listing = self.web("GET", "/research-state/direction?research_id=sr-direction-view&limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"][0]["hypothesis"], "降低内部渲染分辨率可提高帧率")

    def test_research_detail_puts_objective_directions_runs_thread_then_report(self):
        page = (NODE / "dash_static/research.html").read_text(encoding="utf-8")
        script = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        self.assertLess(page.index('id="object-body"'), page.index('id="task-detail"'))
        self.assertIn('directionsSection', script)
        # The 现在 line comes first, then the owner. Comparisons waiting for a person sit right under the owner, then
        # the summary; answered ones follow the thread. A chain's task tree sits in the task list under its root's row;
        # the task's own delivery report is last.
        self.assertIn('container.append(...[now, ownerSection(record, ctx), failed, waiting, summary, directions, runs, thread, judged, deliverableSection(record)]', script)
        self.assertIn("waiting.replaceWith(found.open || ''); judged.replaceWith(found.done || '');", script)

    def test_owner_agents_refresh_with_detail_and_access(self):
        task = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        research = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        self.assertNotIn('if (agents === null)', task, "owner options must not be pinned to the first agent query")
        self.assertIn('agents = (await api(\'/agents\')', task)
        self.assertIn('renderDetail(detail)', research, "unlock must repaint the current task and its owner controls")

    def test_the_event_history_says_which_job_held_a_queued_job(self):
        submit = lambda key: self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                                         "resources": ["phone-a"], "actor_id": "agent-a"}, b"", key)
        first, second = (value["job"]["id"] if "job" in value else value["id"] for value in (submit("hold-1"), submit("hold-2")))
        self.jobs.dispatch.queue.set_state(first, "claimed")
        self.jobs.dispatch.record_waiting()
        status, page = self.cli("GET", f"/job-events?job_id={second}&after=0&limit=200")
        self.assertEqual(status, 200, page)
        self.assertEqual(page["items"][-1]["committed_status"]["waiting"], {"kind": "resource", "blocking_jobs": [first]})
        live = self.cli("GET", f"/jobs/{second}")[1]["queue"]["waiting"]
        self.assertEqual((live["kind"], live["blocking_jobs"], live["reason"]),
                         ("resource", [first], "declared resources overlap an active reservation"), "the live field is unchanged")
        self.jobs.dispatch.record_waiting()
        self.assertEqual(len(self.cli("GET", f"/job-events?job_id={second}&after=0&limit=200")[1]["items"]), len(page["items"]))
        first_events = self.cli("GET", f"/job-events?job_id={first}&after=0&limit=200")[1]["items"]
        self.assertFalse(any("waiting" in item["committed_status"] for item in first_events))
        script = (NODE / "dash_static/jobs.js").read_text(encoding="utf-8")
        self.assertIn("const held = state.waiting ? [['等待', `${waitingText(state.waiting)}", script,
                      "the jobs page's event history names what held the job")

    def test_the_command_view_model_is_this_nodes_own_state(self):
        self.assertEqual(self.web("POST", "/research-state/research/sr-cmd", self.write(
            "cmd-task", {"title": "指挥中心任务", "objective": "从本节点读出"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/direction/d-cmd", self.write(
            "cmd-dir", {"research_id": "sr-cmd", "title": "一个方向", "hypothesis": "会更快"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/note/n-cmd", self.write(
            "cmd-note", {"research_id": "sr-cmd", "text": "先看规划", "reply_to": None, "decision": None}))[0], 201)
        submit = lambda key, argv: self.jobs.dispatch.submit({"bin": "fixture", "argv": argv, "device_access": "none",
                                                             "resources": ["phone-a"], "actor_id": "agent-a", "research_id": "sr-cmd"}, b"", key)
        first, second = (v["job"]["id"] if "job" in v else v["id"] for v in (submit("cmd-1", ["honor_refresh/refresh_ab.py"]),
                                                                             submit("cmd-2", ["selfsr/soak30.sh"])))
        self.jobs.dispatch.queue.set_state(first, "claimed")
        self.jobs.dispatch.record_waiting()
        status, model = self.web("GET", "/command")
        self.assertEqual(status, 200, model)
        self.assertTrue(model["live"])
        self.assertEqual([t["title"] for t in model["research"] if t["id"] == "sr-cmd"], ["指挥中心任务"])
        self.assertIsNotNone(next(t for t in model["research"] if t["id"] == "sr-cmd")["created_at"])
        self.assertEqual([d["id"] for d in model["directions"]], ["d-cmd"])
        self.assertEqual(next(n for n in model["notes"] if n["id"] == "n-cmd")["who"], "human")
        jobs = {j["id"]: j for j in model["jobs"]}
        self.assertEqual((jobs[first]["kind"], jobs[second]["kind"]), ("刷新率 A/B", "30 分钟长测"))
        self.assertIsNotNone(jobs[second]["queued"])
        self.assertEqual([(w["kind"], w["blocking_jobs"]) for w in jobs[second]["waits"]], [("resource", [first])])
        self.assertIsNotNone(jobs[first]["claimed"])
        self.assertEqual(self.request("GET", "/dash/api/command")[0], 401, "the model needs the dashboard login")
        self.assertEqual(self.raw("/dash/command")[0], 200)

    def test_the_jobs_page_resets_through_one_entry_and_never_replays(self):
        # The page's only recovery action: POST /dash/api/jobs/<id>/reset {note}, steer required, repeatable.
        script = (NODE / "dash_static/jobs.js").read_text(encoding="utf-8")
        self.assertIn("post(`${API}/jobs/${encodeURIComponent(id)}/reset`, {note}, {}, undefined, {wait: true})", script)
        for retired in ("queue-release", "retry-finalize", "release_eligibility", "storage_blocked"):
            self.assertNotIn(retired, script)
        path = f"/jobs/{self.jid}/reset"
        self.assertEqual(self.web("POST", path, {"note": "页面重置"}, steer=False)[0], 403)
        self.assertEqual(self.web("POST", path, {})[0], 400)
        status, first = self.web("POST", path, {"note": "页面重置"})
        self.assertEqual(status, 200, first)
        self.assertEqual((first["released"], first["storage_committed"]), (True, True))
        self.assertEqual((first["job"]["queue"]["state"], first["job"]["outcome"]), ("done", "cancelled"))
        status, again = self.web("POST", path, {"note": "再按一次"})
        self.assertEqual((status, again["released"], again["job"]["queue"]["state"]), (200, True, "done"))
        self.assertFalse(os.path.exists(self.jobs.path(self.jid, "launch.json")), "reset must not start the command")

    def test_the_jobs_page_waits_for_a_reset_that_takes_longer_than_fifteen_seconds(self):
        """The node answers a reset only after INT, 15 s, TERM, 15 s, KILL; the page must wait for that answer."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.jobs.dispatch.queue.set_state(self.jid, "claimed")
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.clock.install()
                page.goto(f"{base}/dash/jobs#job={self.jid}")
                page.wait_for_function("() => !document.getElementById('reset-panel').hidden")
                held = []
                page.route("**/dash/api/jobs/*/reset", lambda route: held.append(route))
                page.click("#reset-panel summary")
                page.fill("#reset-note", "长时间重置")
                page.click("#reset-submit")
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(20)
                self.assertEqual(len(held), 1)
                page.clock.run_for(31000)  # the page's own clock passes 31 s while the node is still answering
                held[0].continue_()
                page.wait_for_function("() => (document.getElementById('reset-result')?.textContent || '').length > 0")
                self.assertIn("已重置", page.text_content("#reset-result"))
                self.assertEqual(self.web("GET", f"/jobs/{self.jid}")[1]["queue"]["state"], "done")
            finally:
                browser.close()

    def test_job_pages_describe_the_stop_the_node_sends_and_keep_the_real_state_beside_save_errors(self):
        jobs = (NODE / "dash_static/jobs.js").read_text(encoding="utf-8")
        deploy = (NODE / "dash_static/games-deploy.js").read_text(encoding="utf-8")
        self.assertIn("请求停止先发送 SIGINT；15 秒后仍在运行发送 SIGTERM，再 15 秒后发送 SIGKILL。", jobs)
        self.assertIn("contained(currentJob) ? '请求停止只发送 SIGINT，不会自动升级终止。", jobs,
                      "only contained jobs keep a stop at SIGINT unless forced")
        self.assertIn("job.storage_error?`${runLabel(job)} · 结果保存失败`:runLabel(job)", deploy)
        self.assertNotIn("job.storage_error?'结果保存失败'", deploy)

    def test_only_contained_jobs_offer_a_forced_stop(self):
        """An ordinary job's stop already goes INT → TERM → KILL; a second "升级终止" button would only suggest a faster path."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                seen = {}

                def running(route, policy):  # the real job, shown as running under the given execution policy
                    response = route.fetch()
                    job = response.json()
                    job.update(state="running", phase="running", execution_policy=policy,
                               execution={**(job.get("execution") or {}), "kind": "supervised-v1"},
                               controls={"cancel_supported": True, "force_supported": True, "reason": None})
                    route.fulfill(response=response, json=job)

                def serve(policy):
                    return lambda route: running(route, policy)

                for policy, shown in (("process_group", False), ("contained", True)):
                    page.unroute(f"**/dash/api/jobs/{self.jid}")
                    page.route(f"**/dash/api/jobs/{self.jid}", serve(policy))
                    page.goto(f"{base}/dash/jobs#job={self.jid}")
                    page.reload()
                    page.wait_for_function("() => !document.getElementById('cancel-job').disabled")
                    seen[policy] = page.is_visible("#force-open")
                    self.assertEqual(seen[policy], shown, policy)
                    self.assertIn("SIGINT", page.text_content("#cancel-help"))
            finally:
                browser.close()

    def raw(self, path, login=True):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request("GET", path, headers={"Cookie": self.view_cookie} if login else {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type") or "", body

    def showcase_snapshot(self):
        t0 = 1793200000
        data = {"at": t0 + 3600, "source": "测试快照", "agents": ["agent-a"],
                "phones": [{"serial": "S1", "brand": "HONOR", "model": "M1", "temp_c": 36.0, "soc_c": 37.5, "state": "device"}],
                "research": [{"id": "r1", "title": "测试任务", "state": "delivered", "owner": "agent-a", "updated_at": t0 + 3000,
                              "created_at": t0, "created_by": "agent-a"}],
                "directions": [{"id": "d1", "research_id": "r1", "title": "一个方向", "state": "verified", "updated_at": t0 + 2000, "created_at": t0 + 100}],
                "notes": [{"id": "n1", "research_id": "r1", "who": "agent-a", "decision": None, "reply_to": None, "text": "开始",
                           "at": t0 + 200, "ask": False, "open": False}],
                "jobs": [{"id": "j1", "kind": "刷新率 A/B", "agent": "agent-a", "research_id": "r1", "device": "HONOR", "started": t0 + 300,
                          "ended": t0 + 900, "outcome": "succeeded", "label": "", "phone": True, "direction_id": "d1",
                          "queued": t0 + 300, "claimed": t0 + 420}],
                "history": {"since": "2026-08-29", "commits": {"a": 1}, "experiment_lines": ["x"], "sr_loop_generations": 1,
                            "sr_networks": 2, "sr_networks_kept": 1}}
        folder = Path(self.board.showcase_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def test_showcase_pages_load_every_module_they_import(self):
        self.assertIn(self.raw("/dash/showcase", login=False)[0], (302, 303, 401))
        seen, todo = set(), []
        for page in ("/dash/showcase", "/dash/showcase/command"):
            status, kind, body = self.raw(page)
            self.assertEqual((status, kind.split(";")[0]), (200, "text/html"), page)
            html = body.decode()
            self.assertNotIn("<script>", html, "the dash CSP allows no inline script")
            self.assertNotIn("importmap", html)
            todo += re.findall(r'(?:src|href)="(/dash/static/[^"?]+)', html)
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            status, kind, body = self.raw(path)
            self.assertEqual(status, 200, path)
            if path.endswith(".js"):
                self.assertEqual(kind.split(";")[0], "text/javascript", path)
                for spec in re.findall(r"""(?:import|export)\b[^'";]*?\bfrom\s*['"]([^'"]+)['"]""", body.decode()):
                    self.assertTrue(spec.startswith("./"), f"{path} imports {spec}: only flat relative imports resolve here")
                    todo.append("/dash/static/" + spec[2:])
        self.assertIn("/dash/static/three.core.js", seen)

    def test_dashboard_pages_name_their_own_empty_speculation_rules(self):
        """Every dashboard response names the node's own speculation rules, which prefetch nothing: a CDN in front then
        does not add rules that prefetch each link on pointerdown (refused with a 503 the browser logs as an error)."""
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        for path, login in (("/dash/jobs", True), ("/dash/login", False)):
            connection.request("GET", path, headers={"Cookie": self.view_cookie} if login else {})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.getheader("Speculation-Rules"), '"/dash/speculation-rules"', path)
        connection.close()
        status, kind, body = self.raw("/dash/speculation-rules", login=False)
        self.assertEqual((status, kind), (200, "application/speculationrules+json"))
        rules = json.loads(body)
        self.assertEqual({key: value for key, value in rules.items() if value}, {}, "no prefetch or prerender rule")

    def test_showcase_data_comes_from_the_node_local_snapshot(self):
        self.assertEqual(self.raw("/dash/api/showcase")[0], 404)
        data = self.showcase_snapshot()
        status, kind, body = self.raw("/dash/api/showcase")
        self.assertEqual((status, json.loads(body)), (200, data))
        self.assertEqual(self.raw("/dash/api/showcase/screen/HONOR")[0], 404)
        (Path(self.board.showcase_dir) / "screen-HONOR.jpg").write_bytes(b"\xff\xd8jpeg")
        self.assertEqual(self.raw("/dash/api/showcase/screen/HONOR")[1:], ("image/jpeg", b"\xff\xd8jpeg"))
        for bad in ("honor", "..%2Fdata", "HONOR.jpg"):
            self.assertEqual(self.raw("/dash/api/showcase/screen/" + bad)[0], 404, bad)
        self.assertEqual(self.raw("/dash/api/showcase", login=False)[0], 401)

    def test_showcase_command_view_replays_a_snapshot(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        data = self.showcase_snapshot()
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1600, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/showcase/command?t={data['at']}&paused=1")
                page.wait_for_function("() => document.querySelectorAll('.leaf').length === 1")
                self.assertEqual(page.locator(".root").count(), 1)
                self.assertIn("刷新率 A/B", page.text_content(".leaf"))
                self.assertIn("8m", page.text_content(".leaf"), "run time counts from the claim (8 min), not the submit (10 min)")
                self.assertIn("测试快照", page.text_content("#source"))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_the_command_view_follows_only_from_the_play_button(self):
        """Z1: free camera movement never follows; pressing play enters follow; dragging mid-play leaves it again."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        data = self.showcase_snapshot()
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(args=["--use-gl=angle", "--use-angle=vulkan"])
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1600, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/showcase/command?t={data['at']}&paused=1")
                page.wait_for_function("() => !!window.__command && document.querySelectorAll('.leaf').length === 1")
                S = lambda expr: page.evaluate(f"() => {{ const c = window.__command; return {expr}; }}")

                def drag():
                    box = page.locator("#stage").bounding_box()
                    x, y = box["x"] + box["width"] * .5, box["y"] + box["height"] * .8
                    page.mouse.move(x, y); page.mouse.down()
                    for i in range(1, 9):
                        page.mouse.move(x - 30 * i, y - 10 * i); page.wait_for_timeout(20)
                    page.mouse.up(); page.wait_for_timeout(120)

                # 1) A manual camera move follows nothing, playing or not.
                drag()
                self.assertFalse(S("c.S.follow"))

                # 2) Pressing play is what enters follow.
                page.click("#t-play")
                self.assertTrue(S("c.S.playing"))
                self.assertTrue(S("c.S.follow"))
                self.assertEqual(page.text_content("#b-follow"), "跟随中")

                # 3) Dragging mid-play leaves follow, and stays out while playback continues.
                drag()
                self.assertFalse(S("c.S.follow"))
                page.wait_for_timeout(900)
                self.assertTrue(S("c.S.playing"))
                self.assertFalse(S("c.S.follow"), "free movement during playback is not pulled back into follow")
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_the_story_steps_through_the_snapshot_and_names_each_step(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.showcase_snapshot()
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/showcase")
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.text_content("#title"), "1 个 Agent，1 台手机")
                self.assertIn("测试快照", page.text_content("#src"))
                page.keyboard.press("ArrowRight")
                page.wait_for_function("() => location.hash === '#2'")
                page.wait_for_function("() => document.getElementById('title').textContent === '1 次提交'")
                page.keyboard.press("End")
                page.wait_for_function("() => document.getElementById('kicker').textContent === '下一步'")
                self.assertTrue(page.is_disabled("#next"))
                page.goto(f"{base}/dash/showcase#5")   # same page: the hash alone moves to step 5
                page.wait_for_function("() => document.getElementById('title').textContent === '1 份交付报告'")
                page.reload()
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.text_content("#title"), "1 份交付报告", "a reload opens the step the address names")
                self.assertTrue(page.is_visible('a.back[href="/dash"]'))
                self.assertTrue(page.is_visible('a[href="/dash/command"]'))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_the_story_on_a_phone_keeps_captions_apart_and_numbers_inside_their_columns(self):
        """At 390 px the three phones' captions do not run into each other, each number fits its column, and the
        top bar's 控制台 link stays on one line."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        data = self.showcase_snapshot()
        t0 = data["at"] - 3600
        data["phones"] = [{"serial": f"S{i}", "brand": brand, "model": model, "temp_c": 38.0, "state": "device"}
                          for i, (brand, model) in enumerate([("REDMAGIC", "NX809J"), ("HONOR", "AAP-AN00"), ("HUAWEI", "EMA-AL00U")])]
        # 10.5 hours of phone runs in the last day: the widest number the first step shows.
        data["jobs"] = [dict(data["jobs"][0], id=f"j{i}", started=t0 - 80000 + i * 7000, queued=t0 - 80000 + i * 7000,
                             claimed=t0 - 80000 + i * 7000, ended=t0 - 80000 + i * 7000 + 5400) for i in range(7)]
        (Path(self.board.showcase_dir) / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/showcase#1")
                page.wait_for_function("() => window.__ready === true")
                page.wait_for_timeout(600)
                self.assertIn("10.5", page.text_content("#facts"))
                found = page.evaluate("""() => {
                  const shown = [...document.querySelectorAll('.tag')].filter(t => !t.classList.contains('off') && !t.classList.contains('crowd') && t.style.display !== 'none');
                  const boxes = shown.map(t => t.getBoundingClientRect());
                  let overlaps = 0;
                  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
                    const a = boxes[i], b = boxes[j];
                    if (a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) overlaps++;
                  }
                  const phones = shown.filter(t => t.classList.contains('big')).length;
                  const wide = [...document.querySelectorAll('#facts > div')].filter(d => d.querySelector('dt').scrollWidth > d.clientWidth + 1).length;
                  return {overlaps, phones, wide, back: document.querySelector('.back').getBoundingClientRect().height};
                }""")
                self.assertEqual(found["phones"], 3, found)
                self.assertEqual(found["overlaps"], 0, found)
                self.assertEqual(found["wide"], 0, found)
                self.assertLess(found["back"], 40, found)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_the_datasets_page_keeps_versions_together_and_always_says_how_they_were_paired(self):
        """Empty, the page just says so. Registered, a name's versions sit together, highest first, and every row says
        whether the low-res side is simulated. Only an opened row reads its previews, manifest and QA report; results
        with no dataset are not here."""
        import struct, zlib

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 32, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x10\x20\x30" * 64) * 32)) + chunk(b"IEND", b""))
        jid = "cdcdcdcdcdcd"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        files = {"preview-1.png": png, "preview-2.png": png, "qa_report.json": json.dumps({"kept": 12000, "dropped": {"misaligned": 512}}).encode(),
                 "manifest.json": b"[" + b"0," * (800 << 10) + b"0]"}
        for name, data in files.items():
            (out / name).write_bytes(data)
        base = "http://%s:%d" % self.address
        reads, errors = [], []
        with self._browser_page() as page:
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: reads.append(request.url.split("/sources/")[1].split("?")[0]) if "/sources/" in request.url else None)
            self.assertEqual(page.goto(base + "/dash/datasets").status, 200)
            page.wait_for_function("() => document.getElementById('message').textContent === '还没有登记过数据集。'", timeout=20000)
            self.assertTrue(page.is_hidden("#datasets"))
            self.assertEqual(page.get_attribute(".shell-more-btn", "aria-current"), "page")
            self.assertEqual(page.text_content(".shell-more-btn"), "数据集")
            self.assertEqual(self.web("POST", "/research-state/research/sr-data", self.write("create-sr-data", {"title": "采数据", "objective": "试点"}))[0], 201)

            def register(rid, dataset=None, names=(), supersedes=None):
                body = {"schema": "phonefarm/result@1", "result_id": rid, "title": f"结果 {rid}", "job_id": jid,
                        "labels": {"game_id": "genshin", "game_version": "5.1", "device": "HONOR", "research_id": "sr-data"},
                        "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"],
                        "sources": [{"name": name, "kind": "job_output", "job_id": jid, "path": name} for name in names]}
                if dataset:
                    body["dataset"] = dataset
                if supersedes:
                    body["supersedes"] = supersedes
                self.assertEqual(self.cli("POST", "/results", body)[0], 201)
            blank = {"capture_method": None, "frames": None, "scenes": None, "splits": None, "size_bytes": None, "location": None}
            register("plain-result")
            register("ds-old", {**blank, "name": "genshin-sr", "version": "v20", "pairing": "simulated", "capture_method": "framedump", "frames": 12000, "scenes": 16,
                                "splits": {"train": 9600, "val": 1200, "test": 1200}, "size_bytes": 44023414784, "location": "mbp1 /Volumes/data/genshin-sr-v2"}, files)
            register("ds-other", {**blank, "name": "ui-crops", "version": None, "pairing": "unpaired"})
            register("ds-new", {**blank, "name": "genshin-sr", "version": "v10", "pairing": "real"}, supersedes="ds-old")
            page.click("#refresh")
            page.wait_for_selector(".ds-version")
            rows = page.eval_on_selector_all(".ds-table tbody tr:not(.ds-detail)", "e => e.map(r => [...r.children].map(c => c.textContent))")
            self.assertEqual([row[0] for row in rows if len(row) == 1], ["genshin-sr2 个版本真实配对", "ui-crops1 个版本无配对"], "newest registration first; no plain result")
            versions = [row for row in rows if len(row) > 1]
            self.assertEqual([row[:2] for row in versions], [["v10", "真实配对"], ["v20（已替代）", "模拟低清"], ["未写版本", "无配对"]], "current versions first, then superseded versions")
            self.assertEqual(versions[1], ["v20（已替代）", "模拟低清", "genshin 5.1", "HONOR", "framedump", "12,000", "16", "9,600 / 1,200 / 1,200", "41.0 GB"])
            self.assertEqual(versions[0][4:], ["—"] * 5, "what is not known says nothing")
            self.assertEqual(page.get_attribute('tr[data-result="ds-old"] .ds-pairing', "class"), "tag warn ds-pairing")
            self.assertEqual(reads, [], "nothing is read until a row is opened")
            panel = 'tr[data-result="ds-old"] + .ds-detail'
            self.assertTrue(page.is_hidden(panel))
            page.click('tr[data-result="ds-old"]')
            page.wait_for_function(f"() => document.querySelectorAll('{panel} .ds-preview img').length === 2 && [...document.querySelectorAll('{panel} .ds-preview img')].every(i => i.naturalWidth === 64)", timeout=20000)
            page.wait_for_function(f"() => document.querySelector('{panel} .ds-file pre')?.textContent.includes('misaligned')")
            self.assertEqual(json.loads(page.text_content(f"{panel} .ds-file pre")), {"kept": 12000, "dropped": {"misaligned": 512}})
            self.assertIn("mbp1 /Volumes/data/genshin-sr-v2", page.text_content(f"{panel} .ds-location"))
            hrefs = page.eval_on_selector_all(f"{panel} .ds-links a", "e => e.map(a => a.getAttribute('href'))")
            for wanted in ("kind=research&id=sr-data", f"/dash/jobs", f"job={jid}", "result=ds-old"):
                self.assertTrue(any(wanted in href for href in hrefs), (wanted, hrefs))
            self.assertIn("split.json：没有登记", page.text_content(panel))
            self.assertEqual(sorted(reads), ["preview-1.png", "preview-2.png", "qa_report.json"])
            page.click(f"{panel} summary:has-text('manifest.json')")
            page.wait_for_function(f"() => document.querySelector('{panel}').textContent.includes('这里只显示开头 1.0 MB')", timeout=20000)
            self.assertIn("文件 1.6 MB", page.text_content(panel))
            page.click('tr[data-result="ds-old"]')
            self.assertTrue(page.is_hidden(panel))
            self.assertEqual(errors, [])

    def test_the_story_record_stays_on_screen_however_long_it_gets(self):
        """A longer record gets more hours to a bar, so the chart never backs the camera away into the fog, and its time
        labels do not run into each other. The labels are not in the 3D scene, so they alone stayed when it was lost."""
        import base64
        data = self.showcase_snapshot()
        at = data["at"]
        data["research"][0]["created_at"] = at - 400 * 3600
        data["jobs"] += [{**data["jobs"][0], "id": f"j-long-{i}", "started": at - i * 21600 - 900, "queued": at - i * 21600 - 900,
                          "claimed": at - i * 21600 - 800, "ended": at - i * 21600} for i in range(1, 60)]
        (Path(self.board.showcase_dir) / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self._browser_page() as page:
            page.goto("http://%s:%d/dash/showcase#3" % self.address)
            page.wait_for_function("() => window.__ready === true")
            self.assertEqual(page.text_content("#kicker"), "人和 Agent")
            self.assertFalse(page.evaluate("() => document.body.classList.contains('flat')"), "this test needs the 3D scene")
            page.wait_for_timeout(2000)   # the camera arrives
            left = page.evaluate("() => Math.ceil(document.getElementById('copy').getBoundingClientRect().right)")
            shot = page.screenshot(clip={"x": left, "y": 0, "width": 1280 - left, "height": 800})
            teal = page.evaluate("""async png => {
                const image = new Image(); image.src = 'data:image/png;base64,' + png; await image.decode();
                const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
                const g = canvas.getContext('2d', {willReadFrequently: true}); g.drawImage(image, 0, 0);
                const {data} = g.getImageData(0, 0, image.width, image.height); let n = 0;
                for (let i = 0; i < data.length; i += 4) if (data[i + 1] > 110 && data[i + 2] > 90 && data[i] < data[i + 1] * .6) n++;
                return n;
            }""", base64.b64encode(shot).decode())
            self.assertGreater(teal, 300, "the bars are on screen, not lost in the fog")
            labels = page.evaluate("""() => [...document.querySelectorAll('.tag.dim')].filter(e => e.offsetWidth && !e.classList.contains('off')).map(e => {
                const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; })""")
            self.assertGreaterEqual(len(labels), 3)
            self.assertLessEqual(len(labels), 8)
            crossing = [(a, b) for i, a in enumerate(labels) for b in labels[i + 1:] if a[2] > b[0] and a[0] < b[2] and a[3] > b[1] and a[1] < b[3]]
            self.assertEqual(crossing, [], "the time labels do not run into each other")

    def test_the_command_view_follows_this_node_live_and_holds_when_left(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.assertEqual(self.web("POST", "/research-state/research/sr-live", self.write(
            "live-task", {"title": "直播里的任务", "objective": "从本节点读出"}))[0], 201)
        import time
        for i, jid in enumerate(("aaaaaaaaaa01", "aaaaaaaaaa02", "aaaaaaaaaa03")):   # finished runs of the task, each a leaf
            Path(self.jobs.path(jid, "out")).mkdir(parents=True)
            self.jobs._write_meta(jid, {"id": jid, "bin": "fixture", "argv": ["-c", "pass"], "research_id": "sr-live", "actor_id": "agent-a",
                                        "label": f"run {i}", "started": time.time() - 900 + i * 60, "ended": time.time() - 870 + i * 60, "exit": 0})
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "true", "this node's own view opens live")
                self.assertIn("直播", page.text_content("#status"))
                self.assertTrue(page.is_visible('.tree[data-tree="sr-live"] .root'))
                self.assertEqual(page.get_attribute("#b-follow", "aria-pressed"), "true")
                page.mouse.move(800, 400)
                page.mouse.wheel(0, -200)
                self.assertEqual(page.get_attribute("#b-follow", "aria-pressed"), "false", "moving the map by hand stops following")
                page.keyboard.press("l")
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "false")
                self.assertEqual(page.get_attribute("#t-play", "aria-label"), "播放", "leaving live holds the moment")
                page.click("#t-live")
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "true")
                page.mouse.click(5, 300)  # the rail, so Space is not taken by a focused button
                page.keyboard.press(" ")
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "false")
                self.assertEqual(page.get_attribute("#t-play", "aria-label"), "播放", "pausing live holds now")
                self.assertLess(page.evaluate("() => Date.now() / 1000 - window.__command.S.now"), 30, "and does not jump back to replay the record")
                # Clicking a run on the map opens its task and brings that run's row into view, highlighted; it stays marked.
                leaf = '.leaf[data-key="aaaaaaaaaa01"]'
                page.evaluate("id => window.__command.select(id)", "sr-live")
                page.click(leaf)
                row = '#panel-body li[data-job="aaaaaaaaaa01"]'
                page.wait_for_selector(row + ".hit")
                self.assertEqual(page.eval_on_selector_all("#panel-body .hit", "e => e.length"), 1)
                self.assertIn("marked", page.get_attribute(leaf, "class"))
                page.keyboard.press("Escape")
                self.assertEqual(page.eval_on_selector_all(".marked", "e => e.length"), 0)
                self.assertTrue(page.is_visible('a.back[href="/dash"]'))
                # A chosen source that cannot be read is reported with a retry, never swapped for another one.
                page.route("**/dash/api/command", lambda route: route.abort())
                page.goto(f"{base}/dash/command?src=live")
                page.wait_for_selector("#retry")
                self.assertIn("读不到本节点的数据", page.text_content("#empty"))
                self.assertIn("src=live", page.url)
                page.unroute("**/dash/api/command")
                page.click("#retry")  # a plain click: nothing may sit over the error and its buttons
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "true")
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_an_agent_who_joins_shows_in_the_live_command_view_without_a_reload(self):
        """Live, the command view takes in a model whose only change is a new agent: the rail counts and lists it."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                before = len(self.web("GET", "/command")[1]["agents"])
                self.assertEqual(page.text_content("#agents-n"), f"{before} 个")
                self.jobs.agents.create({"name": "agent-late"})
                # Polls go on until one reads a model with the new agent (rebuilt after a few seconds); nothing else changed.
                for _ in range(4):
                    with page.expect_response(lambda r: r.url.endswith("/dash/api/command"), timeout=30000) as polled:
                        pass
                    if "agent-late" in polled.value.text():
                        break
                else:
                    self.fail("no poll read the new agent")
                page.wait_for_function(f"() => document.getElementById('agents-n').textContent === '{before + 1} 个'", timeout=3000)
                self.assertTrue(page.is_visible('#roster li[data-agent="agent-late"]'))
                self.assertEqual(page.get_attribute("#t-live", "aria-pressed"), "true")
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_pages_link_versioned_assets_the_browser_may_keep(self):
        def get(path):
            connection = http.client.HTTPConnection(*self.address, timeout=10)
            connection.request("GET", path, headers={"Cookie": self.view_cookie})
            response = connection.getresponse()
            body, caching = response.read(), [v for k, v in response.getheaders() if k.lower() == "cache-control"]
            connection.close()
            return response.status, caching, body

        status, caching, page = get("/dash/research")
        self.assertEqual((status, caching), (200, ["no-store"]))
        version = re.search(rb'/dash/static/research\.js\?v=([0-9a-f]{12})"', page)
        self.assertIsNotNone(version, "pages link their scripts with a content version")
        self.assertEqual(get(f"/dash/static/research.js?v={version.group(1).decode()}")[1], ["private, max-age=31536000, immutable"])
        self.assertEqual(get("/dash/static/research.js")[1], ["no-store"])
        self.assertEqual(get("/dash/static/research.js?v=000000000000")[1], ["no-store"])

    def test_web_writes_outlive_the_page_and_conflicts_are_read_back(self):
        # 2026-10-29 mbp1: 改派 was clicked, the page was left while the POST was still crossing the Cloudflare hop,
        # and the edge dropped it (b2f99486 stayed unowned). keepalive lets the browser finish a write after leaving.
        task = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        self.assertIn("keepalive: new TextEncoder().encode(data).length < 60000", task)
        self.assertIn("error.code !== 'research_revision_conflict'", task)
        self.assertIn("if (shown?.record.id === record.id) await ctx.reload();", task)

    def test_web_reassignment_is_read_back_from_the_api(self):
        """A real browser clicks 改派; the owner must then have changed on the server, not just in the select."""
        # CI installs playwright and chromium; only a machine without them may skip this.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for name in ("agent-a", "agent-b"):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-assign", self.write(
            "create-assign", {"title": "改派读回", "objective": "改派后从接口读回负责人"}))[0], 201)
        owner = lambda: self.cli("GET", "/research-state/research/sr-assign")[1]
        base = "http://%s:%d" % self.address
        status = ".task-owner [role=status]"
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research?kind=research&id=sr-assign")
                page.select_option("#task-owner", "agent-a")
                page.click("#task-assign")
                page.wait_for_function("s => /已改派|没有改派/.test(document.querySelector(s)?.textContent || '')", arg=status)
                self.assertEqual((owner()["data"]["owner"], owner()["revision"]), ("agent-a", 2))
                self.assertEqual(page.text_content(status), "已改派给 agent-a")
                self.assertIn("agent-a", page.text_content(".task-owner .who"))
                self.assertIn("agent-a", page.text_content(".research-choice[aria-current=true]"))

                # Someone else reassigns while this page is open: the click must fail visibly and show the server's owner.
                current = owner()
                self.assertEqual(self.web("POST", "/research-state/research/sr-assign", dict(
                    self.write("other-tab", {**current["data"], "owner": "agent-b"}), base_revision=current["revision"], reason="另一页改派"))[0], 201)
                page.select_option("#task-owner", index=0)
                page.click("#task-assign")
                page.wait_for_function("s => /没有改派/.test(document.querySelector(s)?.textContent || '')", arg=status)
                self.assertEqual(owner()["data"]["owner"], "agent-b")
                page.wait_for_function("() => document.getElementById('task-owner')?.value === 'agent-b'")

                # The write is still on its way (held here, as the mbp1 edge held it) when the page is left: it must land.
                held = []
                page.route("**/dash/api/research-state/research/*",
                           lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
                page.select_option("#task-owner", "agent-a")
                page.click("#task-assign")
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(20)
                page.goto(f"{base}/dash/research")
                for route in held:
                    route.continue_()
                for _ in range(100):
                    if owner()["data"]["owner"] == "agent-a":
                        break
                    page.wait_for_timeout(50)
                self.assertEqual(owner()["data"]["owner"], "agent-a")
            finally:
                browser.close()

    def test_a_comparison_is_judged_from_home_and_shows_its_sources_only_after(self):
        """An agent's A/B waits on the home page; a person picks one in the task page block, without seeing which
        source is which until the answer is saved. A video in a note plays in place."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import struct, zlib

        def png(rgb):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * 2) * 2)) + chunk(b"IEND", b""))
        jid = "cccccccccccc"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        (out / "native.png").write_bytes(png(b"\x10\x20\x30"))
        (out / "upscaled.png").write_bytes(png(b"\x30\x20\x10"))
        (out / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42")
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
        self.assertEqual(self.web("POST", "/research-state/note/n-clip", self.write("create-n-clip", {
            "research_id": "sr-cmp", "text": "录屏", "reply_to": None, "decision": None, "ask": False,
            "evidence_refs": [{"kind": "job", "node_id": self.node_id, "job_id": jid, "path": "clip.mp4"}]}))[0], 201)
        # The home payload's list (this fixture has no phones, so it is read from the board, not /dash/api/home).
        reviews = self.board._reviews
        self.assertEqual([(r["id"], r["items"], r["url"]) for r in reviews()],
                         [("ev-cmp", 2, "/dash/research?kind=research&id=sr-cmp#cmp-ev-cmp")])
        self.assertIn('renderReviews(h)', (NODE / "dash_static/home.js").read_text(encoding="utf-8"))
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(base + reviews()[0]["url"])
                block = "#task-compare-open #cmp-ev-cmp"
                page.wait_for_selector(block)
                # Both images decode, and nothing on the block names a source before the answer.
                page.wait_for_function("s => [...document.querySelectorAll(s + ' figcaption span')].every(e => e.textContent === '2×2')", arg=block)
                self.assertNotRegex(page.inner_text(block), "native|upscaled|r-cmp")
                self.assertTrue(page.is_disabled(block + " button[type=submit]"))
                page.click(block + " .chip-btn.letter >> text=B")
                page.click(block + " .compare-diff >> text=中")
                page.click(block + " button[type=submit]")
                done = "#task-compare-done #cmp-ev-cmp"
                page.wait_for_selector(done + " .compare-source")
                answers = self.cli("GET", "/research-state/feedback?request_id=ev-cmp")[1]["items"]
                self.assertEqual(len(answers), 1)
                saved = self.cli("GET", "/research-state/feedback/" + answers[0]["id"])[1]
                self.assertEqual(saved["data"]["evaluation"], {"schema": "phonefarm/ab-feedback@1", "preference": "B", "difference": "moderate",
                    "reviewed": {"A": True, "B": True}, "client_display": {"A": "decoded", "B": "decoded"}, "identity_revealed": False})
                self.assertEqual(saved["data"]["text"], "B 更好 · 差别中")
                self.assertEqual(page.inner_text(done + " .compare-verdict strong"), "B 更好 · 差别中")
                self.assertEqual(page.eval_on_selector_all(done + " .compare-item.picked", "e => e.map(x => x.dataset.slot)"), ["B"])
                self.assertEqual(page.eval_on_selector_all(done + " .compare-source a", "e => e.map(x => x.textContent)"),
                                 ["upscaled.png", "native.png"])
                self.assertEqual(reviews(), [])
                # The fixture bytes are only a container header, so the player may give up; the figure and its file link stay.
                self.assertIn("path=clip.mp4", page.get_attribute("#note-n-clip figure.video figcaption a", "href"))
            finally:
                browser.close()

    def test_a_comparison_opened_from_home_comes_first_and_a_failed_read_says_so(self):
        """去评 lands on the task page with 待你评 shown at once; the comparison is read before the task's other
        sections, scrolled below the top bar and outlined. If its read fails the section says so with 重试."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import struct, zlib

        def png(rgb):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * 2) * 2)) + chunk(b"IEND", b""))
        jid = "dddddddddddd"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        (out / "a.png").write_bytes(png(b"\x10\x20\x30"))
        (out / "b.png").write_bytes(png(b"\x30\x20\x10"))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-first", "title": "素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".png"} for name in ("a", "b")]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-first", self.write(
            "create-first", {"title": "先评后看", "objective": "去评先出来"}))[0], 201)
        for i in range(12):
            self.assertEqual(self.web("POST", f"/research-state/note/n-first-{i}", self.write(f"create-n-first-{i}", {
                "research_id": "sr-first", "text": f"进展 {i}", "reply_to": None, "decision": None}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-first", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-first", self.write("create-ev-first", {
            "research_id": "sr-first", "type": "evaluation", "title": "两张图", "need": "哪张更清楚？", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref("a"), ref("b")],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": 0, "media_kind": "image"}, {"ref": 1, "media_kind": "image"}], "pick": 1}}))[0], 201)
        url = "/dash/research?kind=research&id=sr-first#cmp-ev-first"
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 700})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                # The first read of the comparisons fails: the section says so instead of disappearing.
                lists = "**/dash/api/research-state/request?research_id=*"
                page.route(lists, lambda route, request: route.abort())
                page.goto(base + url)
                page.wait_for_function("() => /对比没读出来/.test(document.getElementById('task-compare-open')?.textContent || '')")
                page.unroute(lists)
                order = []
                page.on("request", lambda request: order.append(request.url))
                page.click("#task-compare-open button >> text=重试")
                page.wait_for_selector("#task-compare-open #cmp-ev-first")
                # A fresh visit: the comparison's reads go before the thread's, and it lands in view, outlined. The
                # thread is read only after that and must not move the comparison when it arrives.
                order.clear()
                page.goto(base + "/dash")
                page.goto(base + url)
                block = "#task-compare-open #cmp-ev-first"
                page.wait_for_selector(block + ".compare-target")
                top = page.evaluate("s => document.querySelector(s).getBoundingClientRect().top", block)
                self.assertTrue(56 <= top <= 120, f"block top {top}: below the sticky top bar, not hidden under it")
                page.wait_for_selector("#task-thread .discuss-entry", state="attached")   # older notes are folded
                page.wait_for_function("() => !document.querySelector('[aria-busy=true]')")
                # Once the thread is in, the block is still top-aligned: its top sits at its own scroll margin.
                after, margin = page.evaluate("s => { const e = document.querySelector(s); return [e.getBoundingClientRect().top, parseFloat(getComputedStyle(e).scrollMarginTop)]; }", block)
                self.assertTrue(56 <= after <= 120 and abs(after - margin) < 1, f"block top {top} → {after} once the thread loaded (scroll margin {margin})")
                reads = [u for u in order if "/research-state/request?" in u or "/research-state/note?" in u]
                self.assertTrue(reads and "/research-state/request?" in reads[0], reads[:3])
                first_note = next(i for i, u in enumerate(reads) if "/research-state/note?" in u)
                self.assertTrue(all("/research-state/note?" not in u for u in reads[:first_note]))
                self.assertGreater(first_note, 0)
            finally:
                browser.close()

    def test_a_tasks_comparisons_and_their_answers_arrive_in_one_read(self):
        """A task's comparisons come from one request list read (full records, newest answers): no read per comparison
        or per answer. In a chain each task's page reads and shows only its own; the answered one with its verdict."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import struct, zlib

        def png(rgb):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * 2) * 2)) + chunk(b"IEND", b""))
        jid = "abababababab"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        (out / "a.png").write_bytes(png(b"\x10\x20\x30"))
        (out / "b.png").write_bytes(png(b"\x30\x20\x10"))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-one-read", "title": "素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".png"} for name in ("a", "b")]})[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-one-read", "source_name": name}
        for rid, follows in (("sr-one", None), ("sr-two", "sr-one")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "一次读出 " + rid, "objective": "对比一次读完", "follows": follows}))[0], 201)
            self.assertEqual(self.cli("POST", f"/research-state/request/ev-{rid}", self.write("create-ev-" + rid, {
                "research_id": rid, "type": "evaluation", "title": "对比 " + rid, "need": "哪张更清楚？", "why": "fixture",
                "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref("a"), ref("b")],
                "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": 0, "media_kind": "image"}, {"ref": 1, "media_kind": "image"}], "pick": 1}}))[0], 201)
        assessment = {"schema": "phonefarm/compare-feedback@1", "picked": ["B"], "unable": False, "difference": "moderate",
                      "client_display": {"A": "decoded", "B": "decoded"}, "identity_revealed": False}
        self.assertEqual(self.web("POST", "/research-state/feedback/f-one-read", self.write("answer-ev-sr-one", {
            "request_id": "ev-sr-one", "request_revision": 1, "text": "B 更清楚", "evaluation": assessment}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                asked = []
                page.on("request", lambda request: asked.append(request.url.split("/dash/api", 1)[1]) if "/dash/api/research-state/" in request.url else None)
                for rid, shown in (("sr-one", "#task-compare-done #cmp-ev-sr-one .compare-verdict"), ("sr-two", "#task-compare-open #cmp-ev-sr-two")):
                    asked.clear()
                    page.goto(f"{base}/dash/research?kind=research&id={rid}")
                    page.wait_for_selector(shown, state="attached")
                    page.wait_for_selector("#task-thread .section-head")
                    self.assertEqual(page.eval_on_selector_all("article[data-request]", "e => e.map(x => x.id)"), ["cmp-ev-" + rid])
                    lists = [url for url in asked if url.startswith("/research-state/request?")]
                    self.assertEqual(len(lists), 1, lists)
                    self.assertIn(f"research_id={rid}", lists[0])
                    self.assertNotIn("sr-two" if rid == "sr-one" else "sr-one", lists[0])
                    self.assertIn("full=1", lists[0])
                    self.assertIn("feedback=latest", lists[0])
                    self.assertEqual([url for url in asked if url.startswith(("/research-state/request/ev-", "/research-state/feedback"))
                                      and "/media/" not in url], [], "no read per comparison or per answer")
                    if rid == "sr-one":
                        self.assertIn("B 更好", page.text_content(shown))
            finally:
                browser.close()

    def test_access_that_arrives_after_the_task_does_not_paint_it_again(self):
        """For a viewer, access read after the task is shown changes nothing that may be done, so the task is not
        painted again (which would read its comparisons and media a second time)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.assertEqual(self.web("POST", "/research-state/research/sr-acc", self.write(
            "create-acc", {"title": "权限晚到", "objective": "只画一次"}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors, held, reads = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: reads.append(request.url) if "/research-state/request?research_id=sr-acc" in request.url else None)
                page.route("**/dash/api/access", lambda route, request: held.append(route))   # access arrives after the task
                with page.expect_response(lambda r: "/research-state/request?research_id=sr-acc" in r.url):
                    page.goto(f"{base}/dash/research?kind=research&id=sr-acc")
                page.wait_for_selector("#task-directions")
                self.assertEqual(len(held), 1)
                self.assertEqual(page.text_content("#access-message"), "正在读取权限…")
                held[0].continue_()
                page.wait_for_function("() => document.getElementById('access-message').textContent !== '正在读取权限…'")
                page.wait_for_load_state("networkidle")
                self.assertEqual(len(reads), 1, reads)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_comparison_opens_in_a_viewer_that_switches_slots_in_place(self):
        """A picture opens an in-page, near full-screen viewer (no new page). Arrow keys and the A–D buttons switch the
        slot shown in the same box; only letters appear. Videos keep one clock across a switch. Esc closes it."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import base64, struct, zlib

        def png(rgb, width=64, height=32):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * width) * height)) + chunk(b"IEND", b""))
        jid = "ffffffffffff"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        methods = {"native": b"\x10\x20\x30", "bilinear": b"\x30\x20\x10", "sgsr": b"\x20\x30\x10", "ours": b"\x10\x30\x20"}
        for name, rgb in methods.items():
            (out / (name + ".png")).write_bytes(png(rgb))
        clip = base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQCEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggPs7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiECfQAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhxERJxo71UCZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAX14QDgibCBoLqBWpqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhxERJxo71UCWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDIuMDAwMDAwMDAwAB9DtnVCR+eBAKPWgQAAgPAFAJ0BKqAAWgAARwiFhYiFhIgCAgJ1qgP4AgaaE+CGqpNdxDqqTXcQ6qk13EOqpNdxDqqTXcQYAP7/TRL//FhX8WFfxYV/8WFf/PzO7cX85gCjmIEAZAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQDIABECAAEQEAAYABhYL/QACICBAAAAo5iBASwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEBkAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQH0ABECAAEQEAAYABhYL/QACICBAAAAo5iBAlgAEQIAARAQABgAGFgv9AAIgIEAAACjl4ECvADxAQABEBAUYABhYL/QACICBAAAo5iBAyAAEQIAARAQABgAGFgv9AAIgIEAAACjmIEDhAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQPoABECAAEQEAAYABhYL/QACICBAAAAo5iBBEwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEEsAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQUUABECAAEQEAAYABhYL/QACICBAAAAo5iBBXgAEQIAARAQABgAGFgv9AAIgIEAAACjmIEF3AARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQZAABECAAEQEAAYABhYL/QACICBAAAAo5iBBqQAEQIAARAQABgAGFgv9AAIgIEAAACjl4EHCADxAQABEBAUYABhYL/QACICBAAAo5iBB2wAEQIAARAQABgAGFgv9AAIgIEAAAAcU7trkbuPs4EAt4r3gQHxggGf8IED")   # a real 2 s 160×90 VP8 clip
        for name in ("clip-native", "clip-ours"):
            (out / (name + ".webm")).write_bytes(clip)
        sources = [*methods, "clip-native", "clip-ours"]
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-view", "title": "查看器素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + (".webm" if name.startswith("clip") else ".png")} for name in sources]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-view", self.write(
            "create-view", {"title": "放大对比", "objective": "同尺寸切换看差别"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-view", "source_name": name}
        for rid, names, kind in (("ev-pics", list(methods), "image"), ("ev-clips", ["clip-native", "clip-ours"], "video")):
            self.assertEqual(self.cli("POST", f"/research-state/request/{rid}", self.write("create-" + rid, {
                "research_id": "sr-view", "type": "evaluation", "title": "哪个更清楚 " + rid, "need": "选 1 个", "why": "fixture",
                "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref(name) for name in names],
                "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": kind} for i in range(len(names))], "pick": 1}}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(f"{base}/dash/research?kind=research&id=sr-view")
                pics = "#cmp-ev-pics"
                page.wait_for_function(f"() => [...document.querySelectorAll('{pics} .compare-media img')].length === 4 && [...document.querySelectorAll('{pics} .compare-media img')].every(x => x.complete && x.naturalWidth)")
                page.locator(f'{pics} .compare-item[data-slot="B"] img').click()
                viewer = "dialog.compare-viewer[open]"
                page.wait_for_selector(viewer)
                self.assertEqual((len(context.pages), page.url.split("?")[0]), (1, f"{base}/dash/research"), "opens inside this page")
                page.wait_for_function(f"() => [...document.querySelectorAll('{viewer} img')].every(x => x.complete && x.naturalWidth === 64)")
                box = page.evaluate(f"() => {{ const r = document.querySelector('{viewer}').getBoundingClientRect(); return [r.width, r.height]; }}")
                self.assertGreater(box[0], 1280 * 0.95)
                self.assertGreater(box[1], 800 * 0.95)
                shown = f"() => {{ const m = document.querySelector('{viewer} .viewer-media.shown'), r = m.getBoundingClientRect(); return [m.dataset.slot, r.x, r.y, r.width, r.height, document.querySelectorAll('{viewer} .viewer-media.shown').length]; }}"
                seen = [page.evaluate(shown)]
                for key in ("ArrowRight", "ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown"):
                    page.keyboard.press(key)
                    seen.append(page.evaluate(shown))
                page.click(f'{viewer} .viewer-slot[data-slot="D"]')
                seen.append(page.evaluate(shown))
                self.assertEqual([s[0] for s in seen], ["B", "C", "D", "C", "B", "C", "D"])
                self.assertEqual({tuple(s[1:]) for s in seen}, {tuple(seen[0][1:])}, "every slot shows in the same box, one at a time")
                text = page.text_content(viewer)
                self.assertFalse([name for name in [*sources, "r-view", ".png", ".webm"] if name in text], text)
                self.assertTrue(all(src.startswith("blob:") for src in page.eval_on_selector_all(f"{viewer} .viewer-media", "e => e.map(x => x.getAttribute('src'))")))
                page.keyboard.press("Escape")
                page.wait_for_selector("dialog.compare-viewer", state="detached")

                clips = "#cmp-ev-clips"
                page.click(f'{clips} .compare-item[data-slot="A"] .compare-zoom')
                page.wait_for_selector(viewer)
                page.wait_for_function(f"() => [...document.querySelectorAll('{viewer} video')].every(x => x.readyState >= 2)")
                page.evaluate(f"() => new Promise(done => {{ const v = document.querySelector('{viewer} video.shown'); v.addEventListener('seeked', done, {{once: true}}); v.currentTime = 1.2; }})")
                page.keyboard.press("ArrowRight")
                state = f"() => [...document.querySelectorAll('{viewer} video')].map(v => [v.dataset.slot, v.classList.contains('shown'), v.currentTime, v.paused])"
                page.wait_for_function(f"() => document.querySelector('{viewer} video.shown').dataset.slot === 'B'")
                (_, _, a_time, a_paused), (_, _, b_time, b_paused) = page.evaluate(state)
                self.assertAlmostEqual(b_time, 1.2, delta=0.05)
                self.assertTrue(a_paused and b_paused, "a paused clip stays paused at the same moment")
                page.evaluate(f"() => document.querySelector('{viewer} video.shown').play()")
                page.wait_for_function(f"() => [...document.querySelectorAll('{viewer} video')].every(v => !v.paused && v.currentTime > 1.3)")
                page.keyboard.press("ArrowLeft")
                page.wait_for_function(f"() => document.querySelector('{viewer} video.shown').dataset.slot === 'A'")
                (_, _, a_time, a_paused), (_, _, b_time, b_paused) = page.evaluate(state)
                self.assertFalse(a_paused or b_paused, "a playing clip keeps playing")
                self.assertLess(abs(a_time - b_time), 0.25, "and shows the same moment")
                page.keyboard.press("Escape")
                page.wait_for_selector("dialog.compare-viewer", state="detached")
            finally:
                browser.close()

    def test_a_video_comparison_downloads_every_clip_before_any_plays(self):
        """Clips are not streamed: each downloads in full, one read at a time, with progress on the block; nothing plays,
        zooms or switches until every clip is in. Then the grid and the viewer play the local copies, and switching
        reads nothing more over the network."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import base64
        clip = base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQCEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggPs7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiECfQAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhxERJxo71UCZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAX14QDgibCBoLqBWpqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhxERJxo71UCWfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDIuMDAwMDAwMDAwAB9DtnVCR+eBAKPWgQAAgPAFAJ0BKqAAWgAARwiFhYiFhIgCAgJ1qgP4AgaaE+CGqpNdxDqqTXcQ6qk13EOqpNdxDqqTXcQYAP7/TRL//FhX8WFfxYV/8WFf/PzO7cX85gCjmIEAZAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQDIABECAAEQEAAYABhYL/QACICBAAAAo5iBASwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEBkAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQH0ABECAAEQEAAYABhYL/QACICBAAAAo5iBAlgAEQIAARAQABgAGFgv9AAIgIEAAACjl4ECvADxAQABEBAUYABhYL/QACICBAAAo5iBAyAAEQIAARAQABgAGFgv9AAIgIEAAACjmIEDhAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQPoABECAAEQEAAYABhYL/QACICBAAAAo5iBBEwAEQIAARAQABgAGFgv9AAIgIEAAACjmIEEsAARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQUUABECAAEQEAAYABhYL/QACICBAAAAo5iBBXgAEQIAARAQABgAGFgv9AAIgIEAAACjmIEF3AARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQZAABECAAEQEAAYABhYL/QACICBAAAAo5iBBqQAEQIAARAQABgAGFgv9AAIgIEAAACjl4EHCADxAQABEBAUYABhYL/QACICBAAAo5iBB2wAEQIAARAQABgAGFgv9AAIgIEAAAAcU7trkbuPs4EAt4r3gQHxggGf8IED")   # a real 2 s 160×90 VP8 clip
        jid = "dddddddddddd"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        names = ["dl-native", "dl-ours"]
        for name in names:
            (out / (name + ".webm")).write_bytes(clip)
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-dl", "title": "下载素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".webm"} for name in names]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-dl", self.write(
            "create-dl", {"title": "视频盲评", "objective": "先下完再看"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-dl", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-dl", self.write("create-ev-dl", {
            "research_id": "sr-dl", "type": "evaluation", "title": "哪段更流畅", "need": "选 1 个", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref(name) for name in names],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": "video"} for i in range(2)], "pick": 1}}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors, held, reads = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def hold(route, request):
                    # Every read of a clip is kept back until the test lets it through, like a slow link.
                    reads.append((request.url.split("/media/")[1].split("?")[0], request.headers.get("range")))
                    held.append(route)
                page.route("**/research-state/request/ev-dl/media/*", hold)
                page.goto(f"{base}/dash/research?kind=research&id=sr-dl")
                block = "#cmp-ev-dl"
                page.wait_for_selector(f"{block} .compare-load:not([hidden])")

                def next_read():
                    for _ in range(100):
                        if held:
                            return held.pop()
                        page.wait_for_timeout(50)
                    self.fail(f"no read after {reads}")
                first = next_read()
                self.assertIn("正在下载 2 段视频", page.text_content(f"{block} .compare-load"))
                self.assertEqual(page.eval_on_selector_all(f"{block} video", "e => e.map(v => v.getAttribute('src'))"), [None, None],
                                 "no clip plays from the network")
                self.assertEqual(page.eval_on_selector_all(f"{block} .compare-zoom", "e => e.map(b => b.disabled)"), [True, True])
                self.assertTrue(page.is_disabled(f"{block} .compare-together"))
                order = [("A", "bytes=0-0"), ("B", "bytes=0-0"), ("A", "bytes=1-"), ("B", "bytes=1-")]
                route = first
                for i in range(len(order)):
                    page.wait_for_timeout(100)
                    self.assertEqual(reads, order[:i + 1], "with one read out, no other is asked for")
                    route.continue_()
                    if i + 1 < len(order):
                        route = next_read()
                page.wait_for_function(f"() => [...document.querySelectorAll('{block} video')].every(v => v.src.startsWith('blob:') && v.readyState >= 2)")
                self.assertEqual(page.text_content(f"{block} .compare-load span"), "已下载 2 段视频 · 0.0 MB", "the line stays, so nothing below moves")
                self.assertTrue(page.is_hidden(f"{block} .compare-load progress"))
                self.assertTrue(page.is_enabled(f"{block} .compare-together"))
                self.assertEqual(reads, order, "each clip's first byte, then the rest of each in turn")
                page.click(f'{block} .compare-item[data-slot="A"] .compare-zoom')
                viewer = "dialog.compare-viewer[open]"
                page.wait_for_function(f"() => [...document.querySelectorAll('{viewer} video')].every(v => v.src.startsWith('blob:') && v.readyState >= 2)")
                page.keyboard.press("ArrowRight")
                page.wait_for_function(f"() => document.querySelector('{viewer} video.shown').dataset.slot === 'B'")
                self.assertEqual(len(reads), 4, "switching reads nothing more")
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_download_the_link_cuts_carries_on_from_the_next_byte_by_itself(self):
        """Files come one at a time. A read cut off halfway, a 502, a 530 and a network error are each picked up a few
        seconds later with no click: the resume asks only for the bytes still missing, the line says what is happening,
        and the local copies are byte for byte the files, pictures as well as videos."""
        files, path = self._large_comparison("ev-cut", {"A": "video", "B": "video", "C": "image", "D": "image"})
        cut, cuts = 1 << 20, {"A": 1 << 20}
        total = f"{sum(map(len, files.values())) / 1048576:.1f}"
        block, line = "#cmp-ev-cut", "#cmp-ev-cut .compare-load span"
        reads, held, versions, errors = [], [], [], []
        faults = {"B": [502], "C": [530], "D": ["reset", "whole file"]}

        def relay(route, request):
            slot, asked = request.url.split("/media/")[1].split("?")[0], request.headers.get("range")
            reads.append((slot, asked))
            fault = faults[slot].pop(0) if asked != "bytes=0-0" and faults.get(slot) else None
            if asked == f"bytes={cut + 1}-":
                versions.append(request.headers.get("if-range"))
                held.append(route)
            elif fault == "reset":
                route.abort("connectionreset")
            elif fault == "whole file":   # a resume answered with the whole file must not be stitched onto what is held
                route.continue_(headers={k: v for k, v in request.all_headers().items() if k not in ("range", "if-range")})
            elif fault:
                route.fulfill(status=fault, body="")
            else:
                route.continue_()
        with self._reads_ending_early(cuts), self._browser_page() as page:
            page.add_init_script("""{ const create = URL.createObjectURL; window.blobs = {}; URL.createObjectURL = blob => { const url = create.call(URL, blob); window.blobs[url] = blob; return url; }; }""")
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/research-state/request/ev-cut/media/*", relay)
            page.goto(f"http://%s:%d{path}" % self.address)
            for _ in range(200):
                if held:
                    break
                page.wait_for_timeout(50)
            # Pictures come before videos: by the time video A is cut, both pictures are in and video B waits.
            first = reads[:4]
            self.assertCountEqual(first, [(slot, "bytes=0-0") for slot in "ABCD"])
            pictures = [("C", "bytes=1-"), ("C", "bytes=1-"), ("D", "bytes=1-"), ("D", "bytes=1-")]
            self.assertEqual(reads, [*first, *pictures, ("A", "bytes=1-"), ("A", f"bytes={cut + 1}-")], "with one file out, no other is asked for")
            page.wait_for_timeout(300)
            self.assertEqual(len(reads), 10, "with one file out, no other is asked for")
            self.assertTrue(versions[0], "a resume says which version of the file it continues")
            self.assertEqual(page.text_content(line), f"连接断了，几秒后接着下 · {(len(files['C']) + len(files['D']) + cut) / 1048576:.1f} / {total} MB")
            self.assertEqual(page.text_content(f'{block} .compare-item[data-slot="A"] figcaption span'), "1.0 / 3.0 MB")
            self.assertTrue(page.is_visible(f"{block} .compare-load progress"))
            self.assertTrue(page.is_hidden(f"{block} .compare-load button:has-text('重试')"))
            self.assertEqual(page.eval_on_selector_all(f"{block} video", "e => e.map(v => v.getAttribute('src'))"), [None, None])
            held[0].continue_()
            page.wait_for_function(f"() => [...document.querySelectorAll('{block} video')].every(v => v.src.startsWith('blob:') && v.readyState >= 2)"
                                   f" && [...document.querySelectorAll('{block} img')].every(i => i.complete && i.naturalWidth === 64)", timeout=30000)
            self.assertEqual(reads, [*first, *pictures, ("A", "bytes=1-"), ("A", f"bytes={cut + 1}-"), ("B", "bytes=1-"), ("B", "bytes=1-")])
            copies = page.evaluate(f"""async () => Object.fromEntries(await Promise.all([...document.querySelectorAll('{block} .compare-item')].map(async item => {{
                const blob = window.blobs[item.querySelector('video, img').src], hash = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
                return [item.dataset.slot, [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('')];
            }})))""")
            self.assertEqual(copies, {slot: hashlib.sha256(data).hexdigest() for slot, data in files.items()})
            self.assertEqual(page.text_content(line), f"已下载 2 段视频和 2 张图片 · {total} MB")
            self.assertTrue(page.is_hidden(f"{block} .compare-load progress"))
            self.assertEqual(page.eval_on_selector_all(f"{block} .compare-zoom", "e => e.map(b => b.disabled)"), [False] * 4)
            self.assertEqual(errors, [])

    def test_a_file_the_node_refuses_says_so_the_others_still_come_and_retry_resumes_it(self):
        """A refusal waiting will not cure is not called a broken link: the line names the file and the reason, the next
        file still comes, and 重试 asks only for the bytes still missing."""
        files, path = self._large_comparison("ev-no", {"A": "image", "B": "image"})
        cut, cuts = 1 << 20, {"A": 1 << 20}
        block, line = "#cmp-ev-no", "#cmp-ev-no .compare-load span"
        reads, refuse = [], [404]

        def relay(route, request):
            asked = request.headers.get("range")
            reads.append((request.url.split("/media/")[1].split("?")[0], asked))
            if asked == f"bytes={cut + 1}-" and refuse:
                route.fulfill(status=refuse.pop(), body="")
            else:
                route.continue_()
        with self._reads_ending_early(cuts), self._browser_page() as page:
            page.route("**/research-state/request/ev-no/media/*", relay)
            page.goto(f"http://%s:%d{path}" % self.address)
            page.wait_for_function(f"() => document.querySelector('{line}')?.textContent === 'A 没下载下来：HTTP 404'", timeout=20000)
            page.wait_for_function(f"() => document.querySelector('{block} .compare-item[data-slot=\"B\"] img').naturalWidth === 64")
            self.assertEqual(page.eval_on_selector(f'{block} .compare-item[data-slot="A"] img', "i => i.getAttribute('src')"), None)
            self.assertEqual(page.eval_on_selector_all(f"{block} .compare-zoom", "e => e.map(b => b.disabled)"), [True, True])
            page.click(f"{block} .compare-load button:has-text('重试')")
            page.wait_for_function(f"() => document.querySelector('{block} .compare-item[data-slot=\"A\"] img').naturalWidth === 64")
            self.assertEqual(reads, [("A", "bytes=0-0"), ("B", "bytes=0-0"), ("A", "bytes=1-"), ("A", f"bytes={cut + 1}-"), ("B", "bytes=1-"), ("A", f"bytes={cut + 1}-")])
            self.assertEqual(page.text_content(line), f"已下载 2 张图片 · {sum(map(len, files.values())) / 1048576:.1f} MB")
            self.assertTrue(page.is_hidden(f"{block} .compare-load button:has-text('重试')"))

    def test_a_read_that_goes_quiet_is_given_up_and_picked_up_again(self):
        """A dead link need not raise anything: a read with no bytes for half a minute counts as cut off."""
        _, path = self._large_comparison("ev-quiet", {"A": "image", "B": "image"})
        block, line = "#cmp-ev-quiet", "#cmp-ev-quiet .compare-load span"
        reads, held = [], []

        def relay(route, request):
            asked = request.headers.get("range")
            reads.append((request.url.split("/media/")[1].split("?")[0], asked))
            if asked == "bytes=1-" and not held:
                held.append(route)   # never answered
            else:
                route.continue_()
        with self._browser_page() as page:
            page.clock.install()
            page.route("**/research-state/request/ev-quiet/media/*", relay)
            page.goto(f"http://%s:%d{path}" % self.address)
            for _ in range(200):
                if held:
                    break
                page.wait_for_timeout(50)
            self.assertTrue(held)
            page.clock.fast_forward(29000)
            self.assertEqual(reads, [("A", "bytes=0-0"), ("B", "bytes=0-0"), ("A", "bytes=1-")], "not before it has been quiet for 30 seconds")
            page.clock.fast_forward(2000)
            page.wait_for_function(f"() => document.querySelector('{line}').textContent.startsWith('连接断了，几秒后接着下')")
            page.wait_for_function(f"() => [...document.querySelectorAll('{block} img')].every(i => i.complete && i.naturalWidth === 64)", timeout=20000)
            self.assertEqual(reads, [("A", "bytes=0-0"), ("B", "bytes=0-0"), ("A", "bytes=1-"), ("A", "bytes=1-"), ("B", "bytes=1-")])
            page.unroute_all(behavior="ignoreErrors")   # the read never answered

    def test_a_page_painted_again_during_a_download_keeps_it(self):
        """A repaint attaches the new block to the download already running: nothing is read again."""
        files, path = self._large_comparison("ev-again", {"A": "image", "B": "image"})
        block, line = "#cmp-ev-again", "#cmp-ev-again .compare-load span"
        reads, held = [], []

        def relay(route, request):
            asked = request.headers.get("range")
            reads.append((request.url.split("/media/")[1].split("?")[0], asked))
            if asked == "bytes=1-" and not held:
                held.append(route)
            else:
                route.continue_()
        with self._browser_page() as page:
            page.route("**/research-state/request/ev-again/media/*", relay)
            page.goto(f"http://%s:%d{path}" % self.address)
            for _ in range(200):
                if held:
                    break
                page.wait_for_timeout(50)
            self.assertTrue(held)
            page.evaluate(f"() => {{ document.querySelector('{block}').dataset.painted = 'before'; }}")
            page.click("#refresh-all")
            page.wait_for_selector(f"{block}:not([data-painted])")
            self.assertTrue(page.text_content(line).startswith("正在下载 2 张图片"), page.text_content(line))
            held[0].continue_()
            page.wait_for_function(f"() => [...document.querySelectorAll('{block} img')].every(i => i.complete && i.naturalWidth === 64)", timeout=20000)
            self.assertEqual(reads, [("A", "bytes=0-0"), ("B", "bytes=0-0"), ("A", "bytes=1-"), ("B", "bytes=1-")])
            self.assertEqual(page.text_content(line), f"已下载 2 张图片 · {sum(map(len, files.values())) / 1048576:.1f} MB")

    def test_a_closed_comparison_nobody_answered_is_one_line_until_it_is_opened(self):
        """Nobody will answer it, and its old question may hold clues a person judging the others should not see: the task
        page shows its title and 已关闭, and reads its question and files only if it is opened. One that was answered
        stays in full, closed or not."""
        clue = "A 是原生，B 是超分"
        _, path = self._large_comparison("ev-shut", {"A": "image", "B": "image"}, title="旧的一组", need=clue)
        shut = self.cli("GET", "/research-state/request/ev-shut")[1]["data"]
        self.assertEqual(self.cli("POST", "/research-state/request/ev-judged", self.write("create-ev-judged", {
            **shut, "title": "答过的一组", "need": "答过的题目"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/feedback/f-judged", self.write("answer-ev-judged", {
            "request_id": "ev-judged", "request_revision": 1, "text": "A 更清楚", "evaluation": {
                "schema": "phonefarm/compare-feedback@1", "picked": ["A"], "unable": False, "difference": "moderate",
                "client_display": {"A": "decoded", "B": "decoded"}, "identity_revealed": False}}))[0], 201)
        self.assertEqual(self.cli("POST", "/research-state/request/ev-waiting", self.write("create-ev-waiting", {
            **shut, "title": "等人评的一组", "need": "等人评的题目"}))[0], 201)
        for rid in ("ev-shut", "ev-judged"):
            current = self.cli("GET", f"/research-state/request/{rid}")[1]
            closing = {**self.write(f"close-{rid}", {**current["data"], "state": "closed", "close_reason": "重新出题"}), "base_revision": current["revision"], "reason": "重新出题"}
            self.assertEqual(self.cli("POST", f"/research-state/request/{rid}", closing)[0], 201)
        row, reads = "#cmp-ev-shut", []
        with self._browser_page() as page:
            page.on("request", lambda request: reads.append(request.url) if "/request/ev-shut/media/" in request.url else None)
            page.goto(f"http://%s:%d{path}" % self.address)
            pictures = lambda block: f"() => [...document.querySelectorAll('{block} .compare-media img')].length === 2 && [...document.querySelectorAll('{block} .compare-media img')].every(i => i.naturalWidth === 64)"
            page.wait_for_function(pictures("#cmp-ev-waiting"), timeout=20000)
            page.locator(row).scroll_into_view_if_needed()   # as near the screen as can be, and still nothing is read
            page.wait_for_function(pictures("#cmp-ev-judged"), timeout=20000)
            self.assertEqual(page.eval_on_selector_all("#task-compare-done > .compare", "e => e.map(x => x.id)"), ["cmp-ev-judged", "cmp-ev-shut"])
            self.assertFalse(clue in page.content(), "its question is not on the page until it is opened")
            self.assertEqual(reads, [], "its files are not read until it is opened")
            self.assertEqual(page.text_content(f"{row} > summary"), "旧的一组已关闭")
            self.assertLess(page.eval_on_selector(f"{row} > summary", "e => e.getBoundingClientRect().height"), 40, "one line")
            self.assertEqual(page.text_content("#cmp-ev-judged .compare-need"), "答过的题目")
            self.assertTrue(page.is_visible("#cmp-ev-judged .compare-verdict"))
            self.assertEqual(page.text_content("#cmp-ev-waiting .compare-need"), "等人评的题目")
            page.click(f"{row} > summary")
            page.wait_for_function(pictures(row), timeout=20000)
            self.assertEqual(page.text_content(f"{row} .compare-need"), clue)
            self.assertEqual(len(reads), 4, "each file's first byte and the rest of it")
            self.assertEqual(page.locator(f"{row} form").count(), 0, "still closed: nothing to answer")
            page.click(f"{row} > summary")
            self.assertTrue(page.is_hidden(f"{row} .compare-need"))
            page.click(f"{row} > summary")
            page.wait_for_function(f"() => document.querySelector('{row}').open")
            self.assertEqual(page.locator(f"{row} .compare-grid").count(), 1, "opened again, it is not built again")
            self.assertEqual(len(reads), 4)

    def test_a_ranking_is_answered_in_the_order_the_letters_are_clicked(self):
        """A comparison that asks for every item is a ranking: letters are clicked best to worst (a ranked letter takes
        itself and the places after it back), each neighbouring pair gets how much better, and the answer saves picked in
        click order with gaps. Answered, one line reads the places and gaps, each card shows its place and its source."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import struct, zlib

        def png(rgb, width=64, height=32):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * width) * height)) + chunk(b"IEND", b""))
        jid = "bbbbbbbbbbbb"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        methods = {"rk-native": b"\x10\x20\x30", "rk-bilinear": b"\x30\x20\x10", "rk-sgsr": b"\x20\x30\x10", "rk-ours": b"\x10\x30\x20"}
        for name, rgb in methods.items():
            (out / (name + ".png")).write_bytes(png(rgb))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-rank", "title": "排序素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".png"} for name in methods]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-rank", self.write(
            "create-rank", {"title": "排序盲评", "objective": "排出先后"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-rank", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-rank", self.write("create-ev-rank", {
            "research_id": "sr-rank", "type": "evaluation", "title": "从好到差排", "need": "四种做法排出先后", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref(name) for name in methods],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": "image"} for i in range(4)], "pick": 4}}))[0], 201)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/research?kind=research&id=sr-rank")
                form = "#cmp-ev-rank .compare-answer"
                page.wait_for_selector(form)
                status = lambda: page.text_content(f"{form} .compare-status")
                letter = lambda name: page.click(f'{form} .compare-picks .chip-btn.letter:text-is("{name}"), {form} .compare-picks .chip-btn.letter:text-matches("^{name} ")')
                places = lambda: page.eval_on_selector_all(f"{form} .compare-place", "e => e.map(x => x.textContent)")
                self.assertEqual(status(), "还没提交 · 从好到差依次点 4 个")
                for name in ("C", "A", "D"):
                    letter(name)
                self.assertEqual(places(), ["① C", "② A", "③ D"])
                self.assertEqual(status(), "还没提交 · 再点第 4 名（还剩 1 个）")
                letter("D")   # takes ③ back
                for name in ("B", "D"):
                    letter(name)
                self.assertEqual(places(), ["① C", "② A", "③ B", "④ D"])
                self.assertEqual(status(), "还没提交 · 再选 C 和 A 差多少")
                self.assertTrue(page.is_disabled(f'{form} button[type="submit"]'))
                gap = lambda i, key: page.click(f'{form} .compare-gap >> nth={i} >> [data-gap="{key}"]')
                gap(0, "small")
                gap(1, "none")
                self.assertEqual(status(), "还没提交 · 再选 B 和 D 差多少")
                gap(2, "large")
                self.assertEqual(status(), "还没提交 · 可以提交")
                page.click(f'{form} button[type="submit"]')
                page.wait_for_selector("#cmp-ev-rank .compare-verdict")
                saved = self.web("GET", "/research-state/request?research_id=sr-rank&full=1&feedback=latest")[1]["items"][0]["latest_feedback"]["data"]["evaluation"]
                self.assertEqual((saved["picked"], saved["gaps"], saved["difference"]), (["C", "A", "B", "D"], ["small", "none", "large"], None))
                self.assertIn("C ›小› A ≈ B ›大› D", page.text_content("#cmp-ev-rank .compare-verdict"))
                page.wait_for_function("() => document.querySelectorAll('#cmp-ev-rank .compare-source').length === 4")
                cards = page.eval_on_selector_all("#cmp-ev-rank .compare-item", """e => e.map(x => [x.dataset.slot, x.querySelector('.compare-rank')?.textContent,
                                                  x.querySelector('.compare-source a')?.textContent])""")
                self.assertEqual(cards, [["A", "②", "rk-native.png"], ["B", "③", "rk-bilinear.png"], ["C", "①", "rk-sgsr.png"], ["D", "④", "rk-ours.png"]])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_comparison_answer_unlocks_beside_submit_and_says_it_is_not_saved_until_it_is(self):
        """Locked, the answer form asks for the steer password beside 提交 instead of sending the person to the top of
        the page. Until the answer is saved the form says it is not submitted and what is missing; a wrong password
        keeps the choices; the right one saves the answer."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import struct, zlib

        def png(rgb, width=64, height=32):
            def chunk(kind, data):
                return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress((b"\x00" + rgb * width) * height)) + chunk(b"IEND", b""))
        jid = "cccccccccccc"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        names = {"ask-native": b"\x10\x20\x30", "ask-ours": b"\x30\x20\x10"}
        for name, rgb in names.items():
            (out / (name + ".png")).write_bytes(png(rgb))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-ask", "title": "作答素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": name + ".png"} for name in names]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-ask", self.write(
            "create-ask", {"title": "图片盲评", "objective": "提交了才算"}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-ask", "source_name": name}
        self.assertEqual(self.cli("POST", "/research-state/request/ev-ask", self.write("create-ev-ask", {
            "research_id": "sr-ask", "type": "evaluation", "title": "哪张更清楚", "need": "选 1 个", "why": "fixture",
            "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref(name) for name in names],
            "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": i, "media_kind": "image"} for i in range(2)], "pick": 1}}))[0], 201)
        import collections
        # The unlock route counts wrong passwords and sets its cookie as on plain LAN HTTP (Dash.__init__ is skipped here).
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                context.add_cookies([{"name": self.view_cookie.split("=", 1)[0], "value": self.view_cookie.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"{base}/dash/research?kind=research&id=sr-ask")
                form = "#cmp-ev-ask .compare-answer"
                page.wait_for_selector(form)
                status = lambda: page.text_content(f"{form} .compare-status")
                password = f'{form} input[type="password"]'
                send = f'{form} button[type="submit"]'
                self.assertEqual(status(), "还没提交 · 先选 1 个")
                page.click(f"{form} .chip-btn.letter >> text=B")
                self.assertEqual(status(), "还没提交 · 再选差别大小")
                self.assertTrue(page.is_disabled(send))
                page.click(f'{form} [data-diff="moderate"]')
                self.assertEqual(status(), "还没提交 · 填操作口令后提交")
                self.assertTrue(page.is_visible(password), "the password is asked for beside 提交")
                self.assertEqual(page.text_content(send), "解锁并提交")
                page.locator("#cmp-ev-ask").scroll_into_view_if_needed()
                top = page.evaluate("() => scrollY")
                page.fill(password, "not-the-password")
                page.click(send)
                page.wait_for_function(f"() => document.querySelector('{form} .compare-status').textContent.startsWith('没有提交：')")
                self.assertEqual(page.evaluate("() => scrollY"), top, "nobody is sent to the top of the page")
                self.assertTrue(page.is_hidden("#access-panel"))
                self.assertEqual(page.get_attribute(f"{form} .chip-btn.letter >> text=B", "aria-pressed"), "true", "the choice is kept")
                page.fill(password, STEER)
                self.assertEqual(status(), "还没提交 · 可以提交")
                page.click(send)
                page.wait_for_selector("#cmp-ev-ask .compare-verdict")
                saved = self.web("GET", "/research-state/request?research_id=sr-ask&full=1&feedback=latest")[1]["items"]
                self.assertEqual([(r["id"], r["latest_feedback"]["data"]["evaluation"]["picked"]) for r in saved], [("ev-ask", ["B"])])
                self.assertIn("B 更好", page.text_content("#cmp-ev-ask .compare-verdict"))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_a_comparison_stays_where_it_landed_when_videos_above_it_load(self):
        """去评 lands on the second of two comparisons. The first one's videos are held back until after the landing;
        when they load (portrait, unlike the browser's default box) the comparison does not move."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        import base64
        # A real 720×1440 VP8 clip: Chromium reads its size, so a slot that followed it would grow.
        clip = base64.b64decode("GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAjIEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEgTbuMU6uEHFO7a1Osggiy7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiEBpAAAAAAAAFlSua8OuAQAAAAAAADrXgQFzxYgWmfis4mL8bZyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAvrwgDgi7CCAtC6ggWgmoECElTDZ/xzc6BjwIBnyJpFo4dFTkNPREVSRIeNTGF2ZjYwLjE2LjEwMHNz1mPAi2PFiBaZ+KziYvxtZ8ihRaOHRU5DT0RFUkSHlExhdmM2MC4zMS4xMDIgbGlidnB4Z8ihRaOIRFVSQVRJT05Eh5MwMDowMDowMC4yMDAwMDAwMDAAH0O2dUcL54EAo0cFgQAAgPDbAJ0BKtACoAUARwiFhYiFhIgCAgJ1qgP4A/oCBrak9waBZJ9r25snOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOHsnOGQA/v1u8//jmTcwxP+Obf/xYTwOKMj/8VEAHFO7a5G7j7OBALeK94EB8YIBofCBAw==")
        jid = "eeeeeeeeeeee"
        out = Path(self.jobs.path(jid, "out"))
        out.mkdir(parents=True)
        self.jobs._write_meta(jid, {"id": jid, "bin": "custom", "argv": ["capture"], "started": 1, "exit": 0})
        for name in ("v1", "v2"):
            (out / (name + ".webm")).write_bytes(clip)
        import struct, zlib

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        # A large portrait picture too: an image's own size must not set its slot's size either.
        width, height = 1200, 2400
        (out / "tall.png").write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                                       + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x40\x60\x80" * width) * height)) + chunk(b"IEND", b""))
        self.assertEqual(self.cli("POST", "/results", {"schema": "phonefarm/result@1", "result_id": "r-vid", "title": "视频素材",
            "origin": {"evidence_origin": "synthetic"}, "limitations": ["fixture"], "sources": [
                {"name": name, "kind": "job_output", "job_id": jid, "path": path} for name, path in (("v1", "v1.webm"), ("v2", "v2.webm"), ("tall", "tall.png"))]})[0], 201)
        self.assertEqual(self.web("POST", "/research-state/research/sr-vid", self.write(
            "create-vid", {"title": "两段视频", "objective": "落点不被上方视频推走"}))[0], 201)
        for i in range(12):
            self.assertEqual(self.web("POST", f"/research-state/note/n-vid-{i}", self.write(f"create-n-vid-{i}", {
                "research_id": "sr-vid", "text": f"进展 {i}", "reply_to": None, "decision": None}))[0], 201)
        ref = lambda name: {"kind": "result_source", "node_id": self.node_id, "result_id": "r-vid", "source_name": name}
        for rid in ("ev-vid-1", "ev-vid-2"):
            self.assertEqual(self.cli("POST", f"/research-state/request/{rid}", self.write("create-" + rid, {
                "research_id": "sr-vid", "type": "evaluation", "title": "两段视频 " + rid, "need": "哪段更清楚？", "why": "fixture",
                "context": {"research_revision": 1, "directions": []}, "evidence_refs": [ref("v1"), ref("v2"), ref("tall")],
                "evaluation": {"schema": "phonefarm/compare@1", "items": [{"ref": 0, "media_kind": "video"}, {"ref": 1, "media_kind": "video"},
                                                                          {"ref": 2, "media_kind": "image"}], "pick": 1}}))[0], 201)
        url = "/dash/research?kind=research&id=sr-vid"
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 700})
                context.add_cookies([{"name": cookie.split("=", 1)[0], "value": cookie.split("=", 1)[1], "url": base}
                                     for cookie in (self.view_cookie, self.steer_cookie)])
                page = context.new_page()
                page.goto(base + url)
                page.wait_for_function("() => document.querySelectorAll('#task-compare-open .compare').length === 2")
                above, target = page.eval_on_selector_all("#task-compare-open .compare", "e => e.map(x => x.id)")
                gate = {"open": False, "held": []}

                def hold(route, request):
                    if gate["open"]:
                        route.continue_()
                    else:
                        gate["held"].append(route)
                page.route(f"**/research-state/request/{above[len('cmp-'):]}/media/*", hold)
                page.goto(base + "/dash")
                page.goto(f"{base}{url}#{target}")
                page.wait_for_selector(f"#{target}.compare-target")
                position = f"() => {{ const e = document.getElementById('{target}'); return [e.getBoundingClientRect().top, parseFloat(getComputedStyle(e).scrollMarginTop)]; }}"
                top, margin = page.evaluate(position)
                self.assertTrue(56 <= top <= 120 and abs(top - margin) < 1, f"landed at {top} (scroll margin {margin})")
                videos = f"#{above} video"
                page.wait_for_function(f"() => document.querySelector('#{above} .compare-load span').textContent.startsWith('正在下载 2 段视频')")
                self.assertEqual(page.eval_on_selector_all(videos, "e => e.map(x => [x.getAttribute('src'), x.readyState])"), [[None, 0], [None, 0]],
                                 "the videos above wait until after the landing")
                gate["open"] = True
                for route in gate["held"]:
                    route.continue_()
                page.wait_for_function(f"() => [...document.querySelectorAll('{videos}')].every(x => x.videoHeight === 1440)"
                                       f" && [...document.querySelectorAll('#{above} img')].every(x => x.complete && x.naturalHeight === 2400)")
                page.evaluate("() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
                after, margin = page.evaluate(position)
                self.assertTrue(abs(after - margin) < 1, f"comparison moved from {top} to {after} when the videos above it loaded")
                # Every slot is the same 16:9 box inside its column, whatever the clip's own size.
                for width, height, column in page.eval_on_selector_all("#task-compare-open .compare-media", "e => e.map(x => [x.getBoundingClientRect().width, x.getBoundingClientRect().height, x.closest('.compare-item').clientWidth])"):
                    self.assertLessEqual(width, column)
                    self.assertAlmostEqual(height, min(width * 9 / 16, 640, 0.7 * 700), delta=1)
            finally:
                browser.close()

    def test_an_older_link_to_the_report_on_the_task_page_opens_the_report_page(self):
        """The task page keeps only the delivery's conclusion and a link; a #task-report link (as older notes and
        messages carry) opens the report's own page."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.assertEqual(self.web("POST", "/research-state/research/sr-landing", self.write(
            "create-sr-landing", {"title": "落到报告", "objective": "交付报告链接"}))[0], 201)
        for i in range(12):
            self.assertEqual(self.web("POST", f"/research-state/note/n-landing-{i}", self.write(f"create-n-landing-{i}", {
                "research_id": "sr-landing", "text": f"进展 {i}", "reply_to": None, "decision": None}))[0], 201)
        self.assertEqual(self.pf(ADMIN, "research", "deliver", "sr-landing", "--summary", "# 结果\n\n帧率 +18%",
                                 "--artifact", f"job:{self.jid}", "--game-version", "5.1", "--device", "FixturePhone")[0], 0)
        base = "http://%s:%d" % self.address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 700})
                name, value = self.view_cookie.split("=", 1)
                context.add_cookies([{"name": name, "value": value, "url": base}])
                page = context.new_page()
                page.goto(base + "/dash/research?kind=research&id=sr-landing#task-report")
                page.wait_for_url("**/dash/research/report?id=sr-landing**")
                page.wait_for_function("() => document.getElementById('report')?.textContent.includes('帧率 +18%')")
            finally:
                browser.close()

    def test_research_copy_criteria_filters_and_navigation_follow_task_terms(self):
        page = (NODE / "dash_static/research.html").read_text(encoding="utf-8")
        script = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        criteria = (NODE / "dash_static/research-criteria.js").read_text(encoding="utf-8")
        shell = (NODE / "dash_static/shell.js").read_text(encoding="utf-8")
        for text in ("研究工作台", "研究与协助", "当前后台保存独立研究状态", "可查看研究；保存与答复需要操作口令",
                     "状态是作者声明；选择记录查看具体版本与依赖", "已读取当前版本。读取不会覆盖打开的草稿",
                     "此版本未声明数值判据；不会从目标文字补阈值"):
            self.assertNotIn(text, page + script + criteria)
        self.assertIn('<title>任务 · HGamey</title>', page)
        self.assertIn('<h1>任务</h1>', page)
        self.assertIn('class="access-panel" hidden', page)
        self.assertIn('<details class="research-filters"><summary>记录筛选</summary><form id="filters"', page)
        self.assertIn('requireWrite', script)
        engineering = page.split('<summary>工程详情</summary>', 1)[1]
        self.assertIn('id="criteria-detail"', engineering)
        self.assertIn('["/dash/jobs", "作业", "工程师用"]', shell)
        self.assertIn('["/dash/research", "task", "任务"]', shell)
        self.assertIn('["/dash/research#agents-directory", "agent", "Agent"]', shell)
        self.assertIn('id="objects-heading" tabindex="-1">任务</h2>', page)
        self.assertNotIn('id="detail-heading"', page)
        self.assertIn('id="object-title" tabindex="-1"', page)
        self.assertIn("$('object-title').focus()", script)
        self.assertIn("$('new-object').textContent = $('filter-kind').value === 'research' ? '＋ 新任务'", script)

    def test_unlock_resumes_original_task_creation_and_labels_are_tasks(self):
        home = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        gol = (NODE / "dash_static/gol.js").read_text(encoding="utf-8")
        research = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        task = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        page = (NODE / "dash_static/research.html").read_text(encoding="utf-8")
        self.assertIn('needSteer(openResearch)', home)
        self.assertIn('needSteer(() => openNew(1))', gol)
        self.assertIn('writeContinuation', research)
        self.assertIn('requireWrite(() => openDraft', research)
        self.assertIn('requireWrite(() => form.requestSubmit())', task)
        self.assertIn("research: '任务'", research)
        self.assertIn('<option value="research">任务</option>', page)

    def test_home_primary_action_creates_a_research_task_not_a_gol_goal(self):
        page = (NODE / "dash_static/home.html").read_text(encoding="utf-8")
        script = (NODE / "dash_static/home.js").read_text(encoding="utf-8")
        self.assertRegex(page, r'id="new-btn" class="btn primary"[^>]*>＋ 新任务</button>')
        self.assertNotIn('id="goal-btn"', page)
        self.assertIn('id="research-form"', page)
        self.assertIn('id="research-objective"', page)
        self.assertIn('id="research-game"', page)
        self.assertIn('id="research-device"', page)
        self.assertIn('/dash/api/research-state/research/', script)

    def test_the_home_page_shows_each_agent_its_check_in_its_status_and_what_it_owes(self):
        # A human sees every agent on the home page: when it last checked in, what it says it is doing,
        # what it still owes (guidance it has not answered comes first) and the research it owns.
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        [shown] = board._agents()
        self.assertAlmostEqual(shown.pop("activity_at"), time.time(), delta=5)   # registered just now
        self.assertEqual(shown, {"name": "agent-a", "note": None, "checkin_at": None, "status": None,
                                 "owes": ["post_status"], "research": [], "archived": None})
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", self.write(
            "web-task", {"title": "优化原神超分方向", "objective": "画质不明显下降时提高帧率"}))[0], 201)
        self.assertEqual(self.pf(token, "research", "claim", "sr-genshin")[0], 0)
        self.assertEqual(self.pf(token, "agents", "status", "agent-a", "规划超分方向")[0], 0)
        checked_in = self.duties(token)["at"]
        self.assertEqual(self.web("POST", "/research-state/note/n-guide", self.write(
            "web-guide", {"research_id": "sr-genshin", "text": "先做 1080p，别动 HUD", "reply_to": None, "decision": None}))[0], 201)
        [agent] = board._agents()
        self.assertAlmostEqual(agent["checkin_at"], checked_in, delta=5)
        self.assertEqual((agent["status"]["text"], agent["owes"], agent["research"]),
                         ("规划超分方向", ["answer_note", "plan_directions"], ["sr-genshin"]))
        # Reading the home page is not a check-in: the guidance stays new for the agent's next one.
        self.assertEqual([duty["unread"] for duty in self.duties(token)["duties"] if duty["kind"] == "answer_note"], [True])
        self.assertEqual(self.pf(token, "research", "note", "sr-genshin", "采纳", "--reply-to", "n-guide",
                                 "--decision", "adopt")[0], 0)
        self.assertEqual(board._agents()[0]["owes"], ["plan_directions"])

    def test_an_agent_cannot_conclude_its_research_while_a_human_note_is_unanswered(self):
        # Agents forget to read the page, which is where a human steers. Until the owner answers every human note in
        # its research it can neither conclude a direction nor deliver, and the CLI says what to do. Talking stays open.
        self.assertEqual(self.web("POST", "/research-state/research/sr-genshin", self.write(
            "web-task", {"title": "优化原神超分方向", "objective": "画质不明显下降时提高帧率"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        self.assertEqual(self.pf(token, "research", "claim", "sr-genshin")[0], 0)
        self.assertEqual(self.agent(token, "POST", "/research-state/direction/d-1080p", self.write(
            "d-create", {"research_id": "sr-genshin", "title": "1080p 超分", "hypothesis": "1080p 渲染再超分到 2K"}))[0], 201)
        self.assertEqual(self.web("POST", "/research-state/note/n-guide", self.write(
            "web-guide", {"research_id": "sr-genshin", "text": "先做 1080p，别动 HUD", "reply_to": None, "decision": None}))[0], 201)

        def conclude(operation):
            return self.agent(token, "POST", "/research-state/direction/d-1080p", {
                **self.write(operation, {"research_id": "sr-genshin", "title": "1080p 超分", "hypothesis": "1080p 渲染再超分到 2K",
                                         "state": "no_gain", "state_reason": "帧率没涨"}),
                "base_revision": 1, "reason": "量完了"})
        status, refused = conclude("d-done")
        self.assertEqual((status, refused["code"], refused["notes"]), (409, "research_guidance_unanswered", ["n-guide"]))
        code, _, err = self.pf(token, "research", "deliver", "sr-genshin", "--summary", "完成", "--artifact", f"job:{self.jid}")
        self.assertNotEqual(code, 0)
        self.assertIn("code=research_guidance_unanswered", err)
        self.assertIn("--reply-to <留言>", err)

        self.assertEqual(self.pf(token, "research", "note", "sr-genshin", "采纳：先做 1080p", "--reply-to", "n-guide",
                                 "--decision", "adopt")[0], 0)
        self.assertEqual(conclude("d-done-2")[0], 201)
        code, _, err = self.pf(token, "research", "deliver", "sr-genshin", "--summary", "完成", "--artifact", f"job:{self.jid}")
        self.assertEqual(code, 0, err)
        # A human closing a research on the page is not held back by notes nobody answered.
        self.assertEqual(self.web("POST", "/research-state/note/n-later", self.write(
            "web-later", {"research_id": "sr-genshin", "text": "再看看 HUD", "reply_to": None, "decision": None}))[0], 201)
        current = self.cli("GET", "/research-state/research/sr-genshin")[1]
        status, closed = self.web("POST", "/research-state/research/sr-genshin", {
            **self.write("web-close", {**current["data"], "state": "closed", "state_reason": "人工结束"}),
            "base_revision": current["revision"], "reason": "人工结束"})
        self.assertEqual(status, 201, closed)

    def test_home_keeps_lane_holder_visible_with_global_owner(self):
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        global_job = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "write",
                                                "resources": ["legacy-device"], "label": "glm 全局", "actor_id": "glm-researcher"}, b"", "global-owner")
        lane_job = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass", "HONOR"], "device_access": "write",
                                              "resources": ["device:HONOR"], "label": "kimi HONOR", "actor_id": "kimi-researcher"}, b"", "lane-owner")
        global_id = global_job["job"]["id"] if "job" in global_job else global_job["id"]
        lane_id = lane_job["job"]["id"] if "job" in lane_job else lane_job["id"]
        lock = {"status": "held", "held_by": f"job-{global_id}", "lease": {"entries": [
            {"serial": "HONOR", "job_id": lane_id, "label": f"job-{lane_id}", "state": "active"}]}}
        local = board._holder("HONOR", lock, {"HONOR"})
        global_holder = board._global_holder("HONOR", lock, {"HONOR"})
        self.assertEqual((local["kind"], local["job_id"], local["actor_id"]), ("job", lane_id, "kimi-researcher"))
        self.assertEqual((global_holder["kind"], global_holder["job_id"], global_holder["actor_id"]),
                         ("global", global_id, "glm-researcher"))
        registry = {"devices": [{"device_id": "HONOR", "aliases": ["HONOR"]}]}
        scoped = {"status": "unknown", "lease": {"device_scopes": {"devices": [{"device_id": "HONOR", "status": "clear"}]}}}
        self.assertFalse(board._device_busy("HONOR", scoped, registry))
        scoped["lease"]["device_scopes"]["devices"][0]["status"] = "held"
        self.assertTrue(board._device_busy("HONOR", scoped, registry))
        self.assertTrue(board._device_busy("UNKNOWN", {"status": "unknown", "lease": {}}, registry))

    def test_a_device_write_job_keeps_its_phone_between_its_locks(self):
        # A single-device write job reserves only its phone between its own locks (while cooling, say).
        # Other phones stay unreserved; a lock or lease, when there is one, still decides first.
        self.assertEqual(self.web("POST", "/research-state/research/sr-ab", self.write(
            "ab-task", {"title": "原神超分 A/B", "objective": "复现第 7 组"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        code, out, err = self.pf(token, "--research", "sr-ab", "--label", "selfsr 第 7 组", "--device-access", "write",
                                 "--detach", "fixture", "-c", "pass", "--serial", "S1", "6")
        self.assertEqual(code, 0, err)
        jid = out.strip()
        board = dash.Dash.__new__(dash.Dash)
        board.job_service = self.jobs
        phones, free = {"S1", "S2"}, {"status": "free", "held_by": None, "lease": {"entries": []}}
        self.assertEqual(board._reservations(), [])  # queued: nothing is reserved yet
        self.assertEqual(self.jobs.dispatch.queue.claim_next(4, admit=lambda row: row["job_id"] == jid)["job_id"], jid)
        owners = board._reservations()
        mine = board._holder("S1", free, phones) or board._reserved("S1", owners, phones)
        self.assertEqual(mine, {"kind": "job", "job_id": jid, "label": "selfsr 第 7 组", "actor_id": "agent-a",
                                "research_id": "sr-ab", "research_title": "原神超分 A/B", "state": mine["state"],
                                "reserved": True})
        self.assertIsNone(board._reserved("S2", owners, phones))
        held = {"status": "held", "held_by": f"job-{jid}-selfsr-native-1@S1", "lease": {"entries": []}}
        self.assertNotIn("reserved", board._holder("S1", held, phones))
        # Terminal jobs release their scheduling reservation without a manual release step.
        self.jobs.dispatch.queue.set_state(jid, "blocked")  # legacy rows are not active reservations either
        self.assertEqual(board._reservations(), [])
        self.jobs.dispatch.queue.set_state(jid, "done")
        self.assertEqual(board._reservations(), [])
        self.assertIsNone(board._reserved("S1", [], phones))

    def test_logging_in_returns_to_the_task_that_was_asked_for(self):
        """A link to a task, a job or a comparison opened without a session comes back to that same place after the
        view password, not to the overview; a next that leaves the dashboard is ignored."""
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        self.assertEqual(self.web("POST", "/research-state/research/sr-back", self.write(
            "create-sr-back", {"title": "登录后回到这里", "objective": "深链接"}))[0], 201)
        def login(query):
            connection = http.client.HTTPConnection(*self.address, timeout=10)
            connection.request("POST", "/dash/login?" + urlencode(query), body="pw=" + VIEW,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
            response = connection.getresponse(); response.read(); connection.close()
            return response.getheader("Location")
        self.assertEqual(login({"next": "/dash/jobs", "expected_node_id": self.node_id}),
                         "/dash/jobs?expected_node_id=" + self.node_id, "older login links keep their node pin")
        for nxt in ("//evil.example/dash", "https://evil.example/dash/jobs", "/dash/../evil", "/elsewhere"):
            self.assertEqual(login({"next": nxt}), "/dash", nxt)
        wanted = "/dash/research?kind=research&id=sr-back"
        with self._browser_page() as page:
            page.context.clear_cookies()
            page.goto(f"http://%s:%d{wanted}#task-report" % self.address)
            self.assertEqual(urlparse(page.url).path, "/dash/login")
            page.fill("input[name=pw]", VIEW)
            page.press("input[name=pw]", "Enter")
            page.wait_for_url("**/dash/research?**")
            here = urlparse(page.url)
            self.assertEqual(here.path + "?" + here.query, wanted)
            self.assertEqual(here.fragment, "task-report", "the #place survives both redirects")
            page.wait_for_function("() => document.body.innerText.includes('登录后回到这里')", timeout=20000)

    def test_maintenance_log_says_there_is_no_log_when_the_node_has_no_jobs(self):
        """With no job to follow, the log pane stops saying it is reading one."""
        state = {"now": time.time(), "jobs": [], "hist": [], "fleet_age_s": 3, "fleet_err": None, "fleet_skip": None,
                 "devices": [], "notes": [], "lock": {"status": "clear"}, "shot": None}
        with self._browser_page() as page:
            page.route("**/dash/api/state", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(state)))
            page.goto(f"http://%s:%d/dash/maint" % self.address)
            page.wait_for_function("() => document.getElementById('log').textContent === '这台节点上还没有任务日志'", timeout=15000)

    def test_a_page_that_does_not_exist_is_a_page_with_a_way_back(self):
        """A mistyped dashboard address shows a 404 page linking back; API paths keep their JSON 404."""
        def get(path):
            connection = http.client.HTTPConnection(*self.address, timeout=10)
            connection.request("GET", path, headers={"Cookie": self.view_cookie})
            response = connection.getresponse(); raw = response.read(); connection.close()
            return response.status, response.getheader("Content-Type"), raw.decode()
        status, kind, body = get("/dash/reserch")
        self.assertEqual((status, kind.split(";")[0]), (404, "text/html"))
        self.assertIn('<a href="/dash">回到总览</a>', body)
        status, kind, body = get("/dash/api/nothing-here")
        self.assertEqual((status, kind.split(";")[0], json.loads(body)), (404, "application/json", {"error": "not found"}))

    def test_a_job_query_link_opens_that_job_and_becomes_a_hash_link(self):
        """/dash/jobs?job=<id> is a hand-written form of #job=<id>: it shows the job and the address becomes the hash
        form; an invalid id gets the same format message as the hash form."""
        facts = "() => document.getElementById('job-info').textContent"
        with self._browser_page() as page:
            page.goto(f"http://%s:%d/dash/jobs?job={self.jid}" % self.address)
            page.wait_for_function(f"() => !document.getElementById('detail-content').hidden && ({facts})().includes('{self.jid}')", timeout=20000)
            self.assertTrue(page.url.endswith(f"/dash/jobs#job={self.jid}"), page.url)
            page.goto("http://%s:%d/dash/jobs?job=nope" % self.address)
            page.wait_for_function("() => document.getElementById('detail-message').textContent.includes('任务 ID 格式不正确')", timeout=20000)

    def test_a_job_link_opened_in_the_jobs_tab_shows_that_job(self):
        """Pasting /dash/jobs#job=B into a tab that shows job A changes only the hash; the page follows it."""
        other = self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none",
                                           "resources": [], "actor_id": "agent-a"}, b"", "core-flow-job-2")
        other = other["job"]["id"] if "job" in other else other["id"]
        facts = "() => document.getElementById('job-info').textContent"
        with self._browser_page() as page:
            page.goto(f"http://%s:%d/dash/jobs#job={self.jid}" % self.address)
            page.wait_for_function(f"() => !document.getElementById('detail-content').hidden && ({facts})().includes('{self.jid}')", timeout=20000)
            page.evaluate(f"location.hash = 'job={other}'")
            page.wait_for_function(f"() => !document.getElementById('detail-content').hidden && ({facts})().includes('{other}')", timeout=20000)
            self.assertNotIn(self.jid, page.evaluate(facts))
            self.assertIn(f"#job={other}", page.url)

    def _steered_page(self):
        context = self._browser_page()
        page = context.__enter__()
        page.context.add_cookies([{"name": self.steer_cookie.split("=", 1)[0], "value": self.steer_cookie.split("=", 1)[1],
                                   "url": "http://%s:%d" % self.address}])
        self.addCleanup(context.__exit__, None, None, None)
        return page

    def test_reading_the_shown_job_again_keeps_it_on_screen(self):
        """刷新任务 on the job already shown keeps its details and the log read so far on screen
        while the new read is in flight, instead of blanking the panel and starting the log from byte 0."""
        log = Path(self.jobs.path(self.jid, "log"))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(f"line {n:05d}\n" for n in range(9000)))   # more than one 64 KiB read
        page = self._steered_page()
        held, reads, offsets = [], [], []
        page.route(f"**/dash/api/jobs/{self.jid}", lambda route: (reads.append(1), held.append(route) if len(reads) == 2 else route.continue_()))
        page.on("request", lambda r: offsets.append(parse_qs(urlparse(r.url).query).get("offset", [""])[0]) if f"/jobs/{self.jid}/log" in r.url else None)
        page.goto(f"http://%s:%d/dash/jobs#job={self.jid}" % self.address)
        page.wait_for_function("() => !document.getElementById('detail-content').hidden && document.getElementById('job-log').textContent.length > 0", timeout=20000)
        page.click("#log-next")
        page.wait_for_function("() => document.getElementById('job-log').textContent.includes('line 08999')", timeout=20000)
        page.click("#refresh-detail")
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(20)
        self.assertEqual(len(held), 1)
        self.assertTrue(page.is_visible("#detail-content"), "the job stays on screen while it is read again")
        held[0].continue_()
        page.wait_for_function("() => !document.getElementById('refresh-detail').disabled", timeout=20000)
        page.wait_for_timeout(300)
        self.assertIn("line 08999", page.text_content("#job-log"), "the log is not rewound to its first 64 KiB")
        self.assertEqual(offsets[0], "0")
        self.assertNotIn("0", offsets[1:], "reading again carries on from the byte already read")

    def test_a_stop_request_follows_the_job_until_it_has_ended(self):
        """After 请求停止 the page keeps reading the job for a while, so 停止请求处理中 turns into how it ended
        without a click on 刷新任务."""
        page = self._steered_page()
        stopped, reads = [], []

        def job(route):
            response = route.fetch()
            data = response.json()
            reads.append(1)
            if not stopped or len(stopped) < 3:   # running, and still stopping on the first reads after the request
                data.update(state="running", phase="cancelling" if stopped else "running",
                            controls={"cancel_supported": True, "force_supported": False, "reason": None})
            else:
                data.update(state="finished", phase="finished", outcome="cancelled", exit=130)
            if stopped:
                stopped.append(1)
            route.fulfill(response=response, json=data)
        page.route(f"**/dash/api/jobs/{self.jid}", job)
        page.route(f"**/dash/api/jobs/{self.jid}/cancel", lambda route: (stopped.append(1), route.fulfill(json={"accepted": True})))
        page.goto(f"http://%s:%d/dash/jobs#job={self.jid}" % self.address)
        page.wait_for_function("() => !document.getElementById('cancel-job').disabled", timeout=20000)
        before = page.text_content("#job-state")
        page.click("#cancel-job")
        self.assertIn("运行", before)
        page.wait_for_function("() => /已取消|已停止/.test(document.getElementById('job-state').textContent)", timeout=20000)
        self.assertGreaterEqual(len(stopped), 4, "it read the job again on its own")

    def test_a_stopped_job_that_held_no_device_is_not_flagged_for_device_recovery(self):
        """A stopped host-only job (device_access none, no device in scope) reads 已停止; a stopped device job with no
        recovery report still reads 已停止 · 恢复需核查."""
        shape = {}

        def job(route):
            response = route.fetch()
            data = response.json()
            data.update(state="finished", phase="finished", outcome="cancelled", exit=130, recovery={"status": "not_reported"}, **shape)
            if isinstance(data.get("queue"), dict):
                data["queue"]["device_scope"] = shape["device_scope"]
            route.fulfill(response=response, json=data)
        with self._browser_page() as page:
            page.route(f"**/dash/api/jobs/{self.jid}", job)
            for access, scope, expected in (("none", {"scope": "none"}, "已停止"), ("write", {"scope": "global"}, "已停止 · 恢复需核查")):
                shape.clear()
                shape.update(device_access=access, device_binding=None, device_scope=scope)
                page.goto("about:blank")
                page.goto(f"http://%s:%d/dash/jobs#job={self.jid}" % self.address)
                page.wait_for_function("() => /已停止/.test(document.getElementById('job-state').textContent)", timeout=20000)
                self.assertEqual(page.text_content("#job-state").strip(), expected, access)

    def test_a_note_link_lands_below_the_top_bar_with_its_first_line_showing(self):
        """A #note- link to a note taller than the screen shows its author and first line just under the sticky top bar,
        on a desktop and on a phone; #task-report lands its heading below the bar too."""
        self.assertEqual(self.web("POST", "/research-state/research/sr-long", self.write(
            "create-sr-long", {"title": "长留言", "objective": "锚点"}))[0], 201)
        for n in range(3):
            self.assertEqual(self.web("POST", f"/research-state/note/n-short-{n}", self.write(
                f"short-{n}", {"research_id": "sr-long", "text": f"短留言 {n}", "reply_to": None, "decision": None}))[0], 201)
        long = "第一行在这里\n" + "\n".join(f"第 {n} 行" for n in range(80))
        self.assertEqual(self.web("POST", "/research-state/note/n-long", self.write(
            "long", {"research_id": "sr-long", "text": long, "reply_to": None, "decision": None}))[0], 201)
        for n in range(3, 8):
            self.assertEqual(self.web("POST", f"/research-state/note/n-short-{n}", self.write(
                f"short-{n}", {"research_id": "sr-long", "text": f"短留言 {n}", "reply_to": None, "decision": None}))[0], 201)
        with self._browser_page() as page:
            for width, height in ((1280, 800), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(f"http://%s:%d/dash/research?kind=research&id=sr-long#note-n-long" % self.address)
                page.wait_for_function("() => document.getElementById('note-n-long') && scrollY > 0", timeout=20000)
                page.wait_for_timeout(300)
                bar = page.eval_on_selector(".shell-top", "e => e.getBoundingClientRect().bottom")
                top = page.eval_on_selector("#note-n-long", "e => e.getBoundingClientRect().top")
                self.assertGreaterEqual(top, bar - 1, f"{width}px: the note starts below the top bar")
                self.assertLess(top, bar + 60, f"{width}px: and right under it")

    def test_choosing_a_ranking_gap_by_keyboard_keeps_the_focus_on_it(self):
        """The places and gaps are rebuilt on every choice; a gap chosen with the keyboard keeps focus on that choice."""
        _, path = self._large_comparison("ev-one", {"A": "image", "B": "image", "C": "image"})
        one = self.cli("GET", "/research-state/request/ev-one")[1]["data"]
        self.assertEqual(self.cli("POST", "/research-state/request/ev-rank3", self.write("create-ev-rank3", {
            **one, "title": "三个排先后", "evaluation": {**one["evaluation"], "pick": 3}}))[0], 201)
        block = "#cmp-ev-rank3"
        with self._browser_page() as page:
            page.goto(f"http://%s:%d{path}" % self.address)
            page.wait_for_selector(f"{block} .compare-picks button.letter", timeout=20000)
            for letter in ("B", "A", "C"):
                page.click(f"{block} .compare-picks button.letter:text-is('{letter}')")
            for group in range(2):
                gap = f"{block} .compare-gap >> nth={group}"
                page.locator(gap).locator("[data-gap]").first.focus()
                page.keyboard.press("Enter")
                self.assertEqual(page.evaluate("[document.activeElement.dataset.gap, document.activeElement.getAttribute('aria-pressed')]"),
                                 [page.locator(gap).locator("[data-gap]").first.get_attribute("data-gap"), "true"])

    def test_a_phone_shot_asked_for_by_its_time_may_be_kept_by_the_browser(self):
        """The overview names each phone's picture by when it was taken; that URL may be cached, so a refresh does not
        download every picture again. Without a matching time the answer stays no-store."""
        shots = self.tmp / "shots"
        shots.mkdir()
        (shots / "S1.jpg").write_bytes(b"\xff\xd8fixture")
        os.utime(shots / "S1.jpg", (1700000000, 1700000000))
        self.board.shot_dir = str(shots)
        self.board.monitor = mock.Mock()

        def shot(query):
            connection = http.client.HTTPConnection(*self.address, timeout=10)
            connection.request("GET", "/dash/api/shot?" + query, headers={"Cookie": self.view_cookie})
            response = connection.getresponse(); body = response.read(); connection.close()
            return response.status, response.getheader("Cache-Control"), body
        self.assertEqual(shot("serial=S1&t=1700000000"), (200, "private, max-age=86400", b"\xff\xd8fixture"))
        self.assertEqual(shot("serial=S1&t=1699999999")[1], "no-store", "a newer picture than the one named")
        self.assertEqual(shot("serial=S1")[1], "no-store")

    def test_maintenance_log_never_shows_one_job_under_another_jobs_heading(self):
        """When the followed job changes, the old job's log leaves at once and the new one is read right away."""
        job = lambda jid: {"id": jid, "state": "running", "label": "JOB-" + jid, "bin": "fixture", "argv": [], "log_bytes": 10}
        state = {"now": time.time(), "jobs": [job("aaaaaaaaaaaa")], "hist": [], "fleet_age_s": 3, "fleet_err": None,
                 "fleet_skip": None, "devices": [], "notes": [], "lock": {"status": "clear"}, "shot": None}
        held = []

        def log(route):
            jid = parse_qs(urlparse(route.request.url).query)["job"][0]
            if jid == "bbbbbbbbbbbb":
                held.append(route)
                return
            route.fulfill(json={"job": jid, "size": 10, "text": "LOG-" + jid})
        with self._browser_page() as page:
            page.route("**/dash/api/state", lambda route: route.fulfill(json=state))
            page.route("**/dash/api/log?*", log)
            page.goto(f"http://%s:%d/dash/maint" % self.address)
            page.wait_for_function("() => document.getElementById('log').textContent === 'LOG-aaaaaaaaaaaa'", timeout=15000)
            state["jobs"] = [job("bbbbbbbbbbbb")]
            page.wait_for_function("() => document.getElementById('log-title').textContent.includes('bbbbbbbbbbbb')", timeout=15000)
            self.assertNotIn("LOG-aaaaaaaaaaaa", page.text_content("#log"), "the old log left with its job")
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(20)
            self.assertTrue(held, "the new job's log is read without waiting for the next poll")
            held[0].fulfill(json={"job": "bbbbbbbbbbbb", "size": 10, "text": "LOG-bbbbbbbbbbbb"})
            page.wait_for_function("() => document.getElementById('log').textContent === 'LOG-bbbbbbbbbbbb'", timeout=15000)
            state["jobs"] = []
            page.wait_for_function("() => document.getElementById('log').textContent === '这台节点上还没有任务日志'", timeout=15000)

    def test_the_live_page_keeps_a_focused_phone_button_across_status_refreshes(self):
        """The live page reads the phones' status every 2 s; an unchanged list keeps the focused phone button."""
        status = {"devices": [{"serial": "S1", "model": "Phone One", "state": "device", "mode": "idle", "viewers": 0},
                              {"serial": "S2", "model": "Phone Two", "state": "device", "mode": "idle", "viewers": 0}], "policy": {}}
        reads = []
        with self._browser_page() as page:
            page.route("**/dash/api/live/status*", lambda route: (reads.append(1), route.fulfill(json=status)))
            page.goto(f"http://%s:%d/dash/live" % self.address)
            page.wait_for_selector('#devs button[data-s="S2"]', timeout=15000)
            page.focus('#devs button[data-s="S2"]')
            page.eval_on_selector('#devs button[data-s="S2"]', "b => { b.dataset.mark = 'kept'; }")
            seen = len(reads)
            while len(reads) < seen + 2:
                page.wait_for_timeout(200)
            page.wait_for_timeout(100)
            self.assertEqual(page.evaluate("document.activeElement.dataset.mark || document.activeElement.tagName"), "kept")

if __name__ == "__main__":
    unittest.main()
