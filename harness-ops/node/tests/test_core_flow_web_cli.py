"""The human-agent interaction chain must read back identically through the web page and the CLI.

Requests are shaped the way the shipped clients send them: dashboard calls carry only the
cookies and headers the page scripts set, token calls mirror pf-node. No workers or devices.
"""
import http.client
import json
import os
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
from node_service import Jobs, make_handler

ADMIN = "core-flow-fixture-admin-token"
VIEW = "core-flow-view-password"
STEER = "core-flow-steer-password"


def forbidden(*args, **kwargs):
    raise AssertionError("the interaction chain must not touch devices or workers")


class NoDevices:
    handle = request = forbidden


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
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

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

    def test_task_guidance_and_discussion_meet_on_both_surfaces(self):
        status, _ = self.web("POST", "/research-state/research/sr-genshin",
                             self.write("web-task", {"title": "优化原神超分方向", "objective": "画质不明显下降时提高帧率"}))
        self.assertEqual(status, 201)
        self.assertEqual(self.cli("POST", "/agents", {"name": "agent-a"})[0], 201)
        self.assertEqual(self.cli("POST", "/agents/agent-a/status", {"text": "探索超分插帧"})[0], 200)
        status, _ = self.cli("POST", "/research-state/request/ask-1", self.write("cli-ask", {
            "research_id": "sr-genshin", "type": "guidance", "title": "目标分辨率", "need": "确认目标分辨率",
            "why": "决定方向排序", "context": {"research_revision": 1, "directions": []}}))
        self.assertEqual(status, 201)

        status, listing = self.web("GET", "/research-state/request?state=open")
        self.assertEqual((status, [item["id"] for item in listing["items"]]), (200, ["ask-1"]))
        status, _ = self.web("POST", "/research-state/feedback/fb-1",
                             self.write("web-answer", {"request_id": "ask-1", "request_revision": 1, "text": "先做 1080p"}))
        self.assertEqual(status, 201)
        status, feedback = self.cli("GET", "/research-state/feedback/fb-1")
        self.assertEqual((status, feedback["audit"]["auth_channel"]), (200, "dashboard_steer"))
        status, agents = self.web("GET", "/agents")
        self.assertEqual((status, agents["agents"][0]["status"]["text"]), (200, "探索超分插帧"))

        # The task page reads the discussion with the page session only, exactly like discuss.js.
        status, thread = self.web("GET", f"/jobs/{self.jid}/discuss")
        self.assertEqual(status, 200, thread)
        self.assertEqual(thread["entries"], [])
        # A page write is always the human, whatever label the body claims.
        status, posted = self.web("POST", f"/jobs/{self.jid}/discuss", {"text": "先试插帧", "from": "agent-a"})
        self.assertEqual(status, 201)
        self.assertEqual((posted["entry"]["from"], posted["entry"]["auth_channel"]), ("human", "dashboard_steer"))
        status, posted = self.cli("POST", f"/jobs/{self.jid}/discuss", {"text": "收到，开始插帧 A/B", "from": "agent-a"})
        self.assertEqual(status, 201)
        self.assertEqual((posted["entry"]["from"], posted["entry"]["auth_channel"]), ("agent-a", "admin_token"))
        # A token holder cannot speak as the human.
        self.assertEqual(self.cli("POST", f"/jobs/{self.jid}/discuss", {"text": "批准", "from": "human"})[0], 400)

        for read in (self.cli, self.web):
            status, thread = read("GET", f"/jobs/{self.jid}/discuss")
            self.assertEqual(status, 200)
            self.assertEqual([(e["from"], e["auth_channel"]) for e in thread["entries"]],
                             [("human", "dashboard_steer"), ("agent-a", "admin_token")])

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
        status, posted = self.agent(token, "POST", f"/jobs/{self.jid}/discuss", {"text": "收到"})
        self.assertEqual((status, posted["entry"]["from"], posted["entry"]["auth_channel"]), (201, "agent-a", "agent_token"))
        self.assertEqual(self.agent(token, "POST", f"/jobs/{self.jid}/discuss", {"text": "x", "from": "agent-b"})[0], 400)
        status, _ = self.agent(token, "POST", "/research-state/research/sr-a", self.write("agent-task", {"title": "t", "objective": "o"}))
        self.assertEqual(status, 201)
        audit = self.cli("GET", "/research-state/research/sr-a")[1]["audit"]
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
        # Finished but not yet released, the job still holds the phones, and says so.
        self.jobs.dispatch.queue.set_state(jid, "blocked")
        self.assertEqual(board._reserved("S1", board._reservations(), phones)["state"], "blocked")
        self.jobs.dispatch.queue.set_state(jid, "done")  # what a release leaves (this job never ran, so it cannot be released)
        self.assertEqual(board._reservations(), [])
        self.assertIsNone(board._reserved("S1", [], phones))

    def test_page_discussion_writes_keep_steer_and_csrf_guards(self):
        status, body = self.web("POST", f"/jobs/{self.jid}/discuss", {"text": "x"}, steer=False)
        self.assertEqual((status, body.get("need_steer")), (403, True))
        self.assertEqual(self.web("POST", f"/jobs/{self.jid}/discuss", {"text": "x"}, hg=False)[0], 400)
        self.assertEqual(self.request("GET", f"/dash/api/jobs/{self.jid}/discuss")[0], 401)
        status, body = self.web("POST", "/jobs/000000000000/discuss", {"text": "x"})
        self.assertEqual((status, body.get("code")), (404, "job_not_found"))
        status, thread = self.cli("GET", f"/jobs/{self.jid}/discuss")
        self.assertEqual((status, thread["entries"]), (200, []))


if __name__ == "__main__":
    unittest.main()
