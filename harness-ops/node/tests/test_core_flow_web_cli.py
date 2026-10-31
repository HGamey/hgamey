"""The human-agent interaction chain must read back identically through the web page and the CLI.

Requests are shaped the way the shipped clients send them: dashboard calls carry only the
cookies and headers the page scripts set, token calls mirror pf-node. No workers or devices.
"""
import http.client
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
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


def forbidden(*args, **kwargs):
    raise AssertionError("the interaction chain must not touch devices or workers")


class NoDevices:
    handle = request = forbidden

    def snapshot(self):
        return {"schema_version": 1, "devices": [], "lock": {"status": "unknown"},
                "sampled_at": None, "age_s": None}


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

    def duties(self, token):
        code, out, err = self.pf(token, "checkin", "--json")
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

        changed_loop = list(agent_duties.LOOP)
        changed_loop[5] += "（已修改）"
        with mock.patch.object(agent_duties, "LOOP", changed_loop):
            changed_json = self.duties(tokens["agent-a"])
            self.assertEqual(changed_json["loop"][5], "[新] " + changed_loop[5])
            self.assertEqual([step for step in changed_json["loop"] if step.startswith("[新]")],
                             [changed_json["loop"][5]])
            self.assertFalse(any(step.startswith("[新]") for step in self.duties(tokens["agent-a"])["loop"]))

            changed_text = self.pf(tokens["agent-b"], "checkin")
            self.assertEqual(changed_text[0], 0, changed_text[2])
            loop_text = changed_text[1].split("每轮做法：", 1)[1]
            self.assertIn(f" 6. [新] {changed_loop[5]}", loop_text)
            self.assertEqual(loop_text.count("[新]"), 1)
            next_text = self.pf(tokens["agent-b"], "checkin")
            self.assertNotIn("[新]", next_text[1].split("每轮做法：", 1)[1])

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
        self.assertIn(COMPARE_DUTY, value["loop"])
        self.assertIn(COMPARE_HELP, self.pf(token, "research", "--help")[1])
        self.assertIn(COMPARE_HELP, (NODE.parent / "skills/hgamey-research/SKILL.md").read_text(encoding="utf-8"))

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
        self.assertNotIn("n-collab", [item.get("target") for item in self.duties(tokens["agent-a"])["duties"]])
        self.assertIn(COLLABORATION, self.duties(tokens["agent-b"])["loop"])
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

        # The new owner inherits the human's guidance: its check-in lists the note, and it cannot deliver until it
        # answers it itself, so a handover does not drop what the human asked for.
        self.assertIn("n-guide", [duty["target"] for duty in self.duties(token_b)["duties"] if duty["kind"] == "answer_note"])
        code, _, err = self.pf(token_b, "research", "deliver", "sr-genshin", "--summary", "1080p 自训练超分，帧率 +18%",
                               "--artifact", f"job:{self.jid}")
        self.assertNotEqual(code, 0)
        self.assertIn("code=research_guidance_unanswered", err)
        self.assertEqual(self.pf(token_b, "research", "note", "sr-genshin", "接手：照旧先做 1080p，不动 HUD",
                                 "--reply-to", "n-guide", "--decision", "adopt")[0], 0)

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
        self.assertEqual(tasks["sr-report"]["report_url"], "/dash/research?kind=research&id=sr-report#task-report")

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
                         ("sr-chain-root", "delivered", "agent-a", 2, "/dash/research?kind=research&id=sr-chain-root#task-report"))
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

    def test_a_chain_of_tasks_reads_as_one(self):
        """Tasks that follow one another (research.follows) list as their root with a count, read as one page with a
        strip of the chain and tagged entries, take notes on the chosen task, and draw one tree in the command view."""
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
                page.wait_for_selector(".chain-strip")
                self.assertEqual(page.eval_on_selector_all(".chain-seg", "e => e.map(x => x.dataset.seg || 'all')"), ["all", "sr-root", "sr-next"])
                page.wait_for_function("() => document.querySelectorAll('#task-thread .discuss-entry[data-task]').length === 2")
                self.assertEqual(page.eval_on_selector_all("#task-thread .discuss-entry .chain-tag", "e => e.map(x => x.textContent[0])"), ["①", "②"])
                page.click('.chain-seg[data-seg="sr-next"]')
                self.assertEqual(page.eval_on_selector_all("#task-thread .discuss-entry:not(.seg-out) .discuss-text", "e => e.map(x => x.textContent)"), ["进展 sr-next"])
                page.fill("#task-note-text", "写到第二段")
                page.click("#task-note-submit")
                page.wait_for_function("() => [...document.querySelectorAll('.discuss-text')].some(e => e.textContent === '写到第二段')")
                saved = [n for n in self.web("GET", "/research-state/note?research_id=sr-next&limit=5")[1]["items"] if n["text"] == "写到第二段"]
                self.assertEqual(len(saved), 1, "a note written with ② selected belongs to ②")
                page.goto(f"{base}/dash/command")
                page.wait_for_function("() => window.__ready === true")
                self.assertEqual(page.evaluate("() => [...window.__command.world.trees.keys()].filter(id => id.startsWith('sr-'))"), ["sr-root"])
            finally:
                browser.close()

    def test_a_long_chain_reads_what_is_on_screen_first(self):
        """A chain's page reads what is on screen: below the fold, runs and notes wait until scrolled to, each task's
        runs are asked for once, an older delivery is read when it is opened, and the folded agent directory is not
        read at all."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        for rid, follows in (("sr-a", None), ("sr-b", "sr-a"), ("sr-c", "sr-b")):
            self.assertEqual(self.web("POST", f"/research-state/research/{rid}", self.write(
                "create-" + rid, {"title": "长链 " + rid, "objective": "首屏只读看得到的", "follows": follows}))[0], 201)
        # A note on a delivered task reopens it, so the note comes first.
        self.assertEqual(self.web("POST", "/research-state/note/n-long", self.write("create-n-long", {
            "research_id": "sr-a", "text": "最早的进展", "reply_to": None, "decision": None}))[0], 201)
        for rid in ("sr-a", "sr-b"):
            self.assertEqual(self.pf(ADMIN, "research", "deliver", rid, "--summary", "# 交付 " + rid, "--artifact", f"job:{self.jid}",
                                     "--game-version", "5.1", "--device", "FixturePhone")[0], 0)
        for i in range(20):
            self.assertEqual(self.web("POST", f"/research-state/direction/d-long-{i}", self.write(f"create-d-long-{i}", {
                "research_id": "sr-c", "title": f"方向 {i}", "hypothesis": "把运行和留言推到屏幕下面"}))[0], 201)
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
                settled = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
                page.goto(f"{base}/dash/research?kind=research&id=sr-c")
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 20")
                page.wait_for_selector('.task-report[data-task="sr-b"]', state="attached")
                page.evaluate(settled)
                self.assertGreater(page.evaluate("() => document.getElementById('task-runs').getBoundingClientRect().top - innerHeight"), 100,
                                   "the fixture puts runs and notes below the fold")
                self.assertEqual(reads("/research-state/note?"), [], "notes below the fold are not read yet")
                self.assertEqual(page.eval_on_selector_all("#task-runs .run-list, #task-thread .discuss-entry", "e => e.length"), 0)
                self.assertEqual(reads("/research-state/research/sr-a"), [], "the older delivery is not read while folded")
                page.wait_for_function("() => { scrollTo(0, document.documentElement.scrollHeight); return document.querySelectorAll('#task-thread .discuss-entry').length === 1; }")
                self.assertEqual(sorted(url.split("research=")[1].split("&")[0] for url in reads("/jobs?research=")), ["sr-a", "sr-b", "sr-c"],
                                 "directions and runs share one read of each task's runs")
                self.assertEqual(len(reads("/research-state/note?")), 3)
                page.click(".chain-seg:not([data-seg])")
                page.click(".report-older summary")
                page.wait_for_selector(".report-older .task-report")
                self.assertIn("交付 sr-a", page.text_content(".report-older"))
                self.assertEqual(len(reads("/research-state/research/sr-a")), 1)
                self.assertEqual(reads("/duties") + reads("/jobs?limit=50"), [], "the folded agent directory is not read")
            finally:
                browser.close()

    def test_a_chain_grows_along_what_each_direction_is_based_on(self):
        """Directions hang under the direction (and run) they are based on, on the task page and in the command view.
        The chain's tasks are a tree in the task list under the root; 探索过程 is indented by based_on with 「基于 …」,
        the line to the newest standing direction is marked and a rejected dead end folds. In the command view a
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
                self.assertEqual(page.eval_on_selector_all(tree + " .chain-seg", "e => e.map(x => [x.dataset.seg || 'all', x.classList.contains('nested')])"),
                                 [["all", False], ["lin-root", False], ["lin-next", True], ["lin-rep", True], ["lin-side", True]])
                self.assertEqual(page.get_attribute('.chain-seg[data-seg="lin-rep"]', "style"), "--depth: 2;")
                # 探索过程: each direction under the one it builds on, the run it builds on named, the main line marked.
                page.wait_for_function("() => document.querySelectorAll('#task-directions .research-direction').length === 5")
                rows = page.eval_on_selector_all("#task-directions .research-direction", """e => e.map(x => [x.dataset.direction,
                  getComputedStyle(x).getPropertyValue('--depth').trim(), x.classList.contains('on-main'), x.offsetParent !== null])""")
                self.assertEqual(rows, [["d-a", "0", True, True], ["d-b", "1", True, True], ["d-c", "2", True, True], ["d-d", "3", True, True],
                                        ["d-x", "1", False, False]])
                self.assertIn("1 条没往下走的分支：方向 X：试过放弃（已排除）", page.text_content("#task-directions .direction-fold"))
                based = lambda did: page.text_content(f'#task-directions [data-direction="{did}"] .direction-base')
                self.assertEqual(based("d-b"), "基于 「方向 A：基线」")
                self.assertTrue(based("d-c").startswith("基于 「方向 B：在 A 上改」 里的 ") and based("d-c").endswith(" 那次运行"), based("d-c"))
                self.assertTrue(page.get_attribute('[data-direction="d-c"] .direction-base a', "href").endswith(f"#job={picked}"))
                self.assertEqual(sorted(page.eval_on_selector_all('[data-direction="d-b"] .run-row', "e => e.map(x => [x.dataset.job, x.classList.contains('picked')])")),
                                 sorted([[picked, True], [other, False]]), "the run a later direction builds on is marked in its list")
                page.click("#task-directions .direction-fold")
                self.assertTrue(page.is_visible('[data-direction="d-x"]'), "the fold opens in place")
                page.click('.chain-seg[data-seg="lin-next"]')
                self.assertEqual(page.eval_on_selector_all("#task-directions .research-direction:not(.seg-out)", "e => e.map(x => x.dataset.direction)"), ["d-c"])
                self.assertIn("方向 B：在 A 上改", based("d-c"), "on its own, a step still names what it builds on")
                self.assertEqual(page.evaluate("() => getComputedStyle(document.querySelector('[data-direction=\"d-c\"]')).getPropertyValue('--depth').trim()"), "0",
                                 "and is not indented under directions that are not shown")
                page.click(".chain-seg:not([data-seg])")
                self.assertEqual(page.evaluate("() => getComputedStyle(document.querySelector('[data-direction=\"d-c\"]')).getPropertyValue('--depth').trim()"), "2")
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
        # Comparisons waiting for a person sit right under the owner; answered ones follow the thread.
        # A chain's task tree sits in the task list under its root's row; the delivery report (the newest one in a chain)
        # fills the last place once read.
        self.assertIn('container.append(...[ownerSection(record, ctx), waiting, directions, runs, thread, judged, reports]', script)
        self.assertIn("reports.replaceWith(...found)", script)
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
                page.wait_for_selector("#task-thread .discuss-entry")
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

    def test_a_chains_comparisons_and_their_answers_arrive_in_one_read(self):
        """A chain's comparisons come from one request list read (every task, full records, newest answers): no read
        per comparison or per answer. Each shows under its task in chain order; the answered one with its verdict."""
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
                page.goto(f"{base}/dash/research?kind=research&id=sr-one")
                page.wait_for_selector("#task-compare-open #cmp-ev-sr-two")
                page.wait_for_selector("#task-compare-done #cmp-ev-sr-one .compare-verdict", state="attached")
                self.assertIn("B 更好", page.text_content("#task-compare-done #cmp-ev-sr-one .compare-verdict"))
                lists = [url for url in asked if url.startswith("/research-state/request?")]
                self.assertEqual(len(lists), 1, lists)
                self.assertIn("research_id=sr-one&research_id=sr-two", lists[0])
                self.assertIn("full=1", lists[0])
                self.assertIn("feedback=latest", lists[0])
                self.assertEqual([url for url in asked if url.startswith(("/research-state/request/ev-", "/research-state/feedback"))
                                  and "/media/" not in url], [], "no read per comparison or per answer")
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
                self.assertTrue(all("/media/" in src for src in page.eval_on_selector_all(f"{viewer} .viewer-media", "e => e.map(x => x.getAttribute('src'))")))
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
                page.wait_for_function(f"() => {{ const v = [...document.querySelectorAll('{videos}')]; return v.length === 2 && v.every(x => x.currentSrc); }}")
                self.assertEqual(page.eval_on_selector_all(videos, "e => e.map(x => x.readyState)"), [0, 0], "the videos above wait until after the landing")
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

    def test_a_report_link_from_home_lands_on_the_report_once_the_thread_is_in(self):
        """#task-report is the last section: the page lands on it after the long thread above has loaded."""
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
                page.wait_for_selector("#task-thread .discuss-entry")
                page.wait_for_function("() => Math.abs(document.getElementById('task-report').getBoundingClientRect().top - 72) < 50")
            finally:
                browser.close()

    def test_research_copy_criteria_filters_and_navigation_follow_task_terms(self):
        page = (NODE / "dash_static/research.html").read_text(encoding="utf-8")
        script = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        criteria = (NODE / "dash_static/research-criteria.js").read_text(encoding="utf-8")
        shell = (NODE / "dash_static/shell.js").read_text(encoding="utf-8")
        for text in ("研究工作台", "研究与协助", "当前后台保存独立研究状态", "可查看研究；保存与答复需要掌舵权限",
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
        self.assertIn('["/dash/jobs", "runs", "运行"]', shell)
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
        self.assertIn('id="goal-btn"', page)
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
        self.assertEqual(board._agents(), [{"name": "agent-a", "checkin_at": None, "status": None,
                                            "owes": ["post_status"], "research": []}])
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
        # A device-write job reserves the node's phones from claim until someone releases it. Between its
        # own locks (an experiment cooling the phone, say) its phone still shows it and the others show the
        # reservation; a lock or lease, when there is one, still decides first.
        self.assertEqual(self.web("POST", "/research-state/research/sr-ab", self.write(
            "ab-task", {"title": "原神超分 A/B", "objective": "复现第 7 组"}))[0], 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        token = self.cli("POST", "/agents/agent-a/token", {})[1]["token"]
        code, out, err = self.pf(token, "--research", "sr-ab", "--label", "selfsr 第 7 组", "--device-access", "write",
                                 "--detach", "fixture", "-c", "pass", "S1", "6")
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
        self.assertEqual(board._reserved("S2", owners, phones)["kind"], "global")
        held = {"status": "held", "held_by": f"job-{jid}-selfsr-native-1@S1", "lease": {"entries": []}}
        self.assertNotIn("reserved", board._holder("S1", held, phones))
        # Terminal jobs release their scheduling reservation without a manual release step.
        self.jobs.dispatch.queue.set_state(jid, "blocked")  # legacy rows are not active reservations either
        self.assertEqual(board._reservations(), [])
        self.jobs.dispatch.queue.set_state(jid, "done")
        self.assertEqual(board._reservations(), [])
        self.assertIsNone(board._reserved("S1", [], phones))

if __name__ == "__main__":
    unittest.main()
