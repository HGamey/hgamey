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
                    "research_id": "sr-genshin", "research_title": "优化原神超分方向", "state": "running"}
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
