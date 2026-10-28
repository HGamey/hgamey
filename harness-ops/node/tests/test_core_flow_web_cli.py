"""The human-agent interaction chain must read back identically through the web page and the CLI.

Requests are shaped the way the shipped clients send them: dashboard calls carry only the
cookies and headers the page scripts set, token calls mirror pf-node. No workers or devices.
"""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
        root = str(Path(temporary.name) / "node")
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
