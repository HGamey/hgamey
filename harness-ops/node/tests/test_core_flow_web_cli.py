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
from http.server import ThreadingHTTPServer

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
from http_server_fixture import start_http_server
from node_service import Jobs, make_handler
from agent_duties import NOTE_STYLE, DELIVERY_STYLE

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
        self.assertIn('container.append(...[ownerSection(record, ctx), directions, runs, thread, deliverableSection(record)]', script)

    def test_owner_agents_refresh_with_detail_and_access(self):
        task = (NODE / "dash_static/research-task.js").read_text(encoding="utf-8")
        research = (NODE / "dash_static/research.js").read_text(encoding="utf-8")
        self.assertNotIn('if (agents === null)', task, "owner options must not be pinned to the first agent query")
        self.assertIn('agents = (await api(\'/agents\')', task)
        self.assertIn('renderDetail(detail)', research, "unlock must repaint the current task and its owner controls")

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
