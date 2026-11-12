"""Archiving agents and getting one a new token from the Agent page.

An agent with no activity for three days (check-in, status, a job it submitted, a research write; registration counts)
is folded away under 已归档 on the home, Agent and 结构 pages, without anything written; any activity brings it back.
One archived by hand comes back the same way. Disabling stops its token and archives it; its registration stays.
A new token replaces the old one at once: the page first shows what the agent is doing, and one that looks to be
running needs its name typed. Every change needs the steer password and is recorded."""
import collections
import json
import time
import unittest
from pathlib import Path
from unittest import mock

import test_core_flow_web_cli as core   # puts the node directory on sys.path
import test_structure_page as structure
import node_agents

DAY = 86400


class AgentArchiveTests(unittest.TestCase):
    setUp = core.CoreFlowTests.setUp
    _browser_page = core.CoreFlowTests._browser_page
    request, cli, web, agent = core.CoreFlowTests.request, core.CoreFlowTests.cli, core.CoreFlowTests.web, core.CoreFlowTests.agent
    write = staticmethod(core.CoreFlowTests.write)
    fixture, open = structure.StructurePageTests.fixture, structure.StructurePageTests.open

    def make(self, name, days_ago):
        """An agent registered `days_ago` that has done nothing since."""
        with mock.patch.object(node_agents.time, "time", return_value=time.time() - days_ago * DAY):
            self.assertEqual(self.cli("POST", "/agents", {"name": name})[0], 201)

    def listed(self):
        return {a["name"]: a for a in self.web("GET", "/agents")[1]["agents"]}

    def token(self, name):
        code, value = self.cli("POST", f"/agents/{name}/token")
        self.assertEqual(code, 201, value)
        return value["token"]

    def test_three_days_without_activity_folds_an_agent_away_and_activity_brings_it_back(self):
        self.fixture()   # the 结构 page's computers and phones, and agent-a and agent-b registered now
        self.make("fresh", 2.9)
        self.make("stale", 3.1)
        agents = self.listed()
        self.assertEqual((agents["fresh"]["archived"], agents["stale"]["archived"]), (None, "idle"))
        self.assertEqual(agents["stale"]["activity"]["source"], "registered")
        folded = {a["name"]: a["archived"] for a in self.board._agents()}
        self.assertEqual((folded["fresh"], folded["stale"]), (None, "idle"))

        # Shown on the pages: folded under 已归档 on the home page and the Agent page, hidden on 结构 until asked for.
        # (This fixture has no phones or goal runs for the home payload; its agent list is the one read above.)
        home = {"now": time.time(), "steer": False, "questions": [], "reviews": [], "tasks": [], "slots": [], "elsewhere": [], "fleet_age_s": 0,
                "agents": self.board._agents()}
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/dash/api/home", lambda route: route.fulfill(json=home))
            page.goto("http://%s:%d/dash" % self.address)
            page.wait_for_selector('#agents li[data-agent="fresh"]')
            self.assertEqual(page.text_content("#agents .agent-archived > summary"), "已归档 1 个")
            self.assertTrue(page.is_hidden('#agents .agent-archived li[data-agent="stale"]'))
            self.assertEqual(page.locator('#agents > li[data-agent="stale"]').count(), 0)
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            page.wait_for_selector('#agents-list tr[data-agent-row="fresh"]')
            self.assertEqual(page.text_content("#agents-archived > summary"), "已归档 1 个")
            self.assertTrue(page.is_hidden('#agents-archived tr[data-agent-row="stale"]'))
            self.open(page, 1280)
            page.wait_for_selector('.st-card[data-id="a:fresh"]')
            self.assertEqual(page.text_content("#archived-agents"), "显示已归档的 1 个 Agent")
            self.assertEqual(page.locator('.st-card[data-id="a:stale"]').count(), 0)
            page.click("#archived-agents")
            page.wait_for_selector('.st-card[data-id="a:stale"]')
            self.assertEqual(page.text_content("#archived-agents"), "收起已归档的 1 个 Agent")
            self.assertEqual(errors, [])

        # Any activity brings it back: a status, a job, a research write.
        self.assertEqual(self.agent(self.token("stale"), "POST", "/agents/stale/status", {"text": "回来了"})[0], 200)
        self.assertIsNone(self.listed()["stale"]["archived"])
        self.make("idle-job", 4)
        self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": [],
                                   "actor_id": "idle-job"}, b"", "idle-job-1")
        self.assertEqual(self.listed()["idle-job"]["activity"]["source"], "job")
        self.assertIsNone(self.listed()["idle-job"]["archived"])
        self.make("idle-note", 4)
        self.assertEqual(self.web("POST", "/research-state/research/sr-act", self.write(
            "create-sr-act", {"title": "活动", "objective": "写入"}))[0], 201)
        code, _ = self.agent(self.token("idle-note"), "POST", "/research-state/note/n-act", self.write(
            "note-act", {"research_id": "sr-act", "text": "进展", "reply_to": None, "decision": None}))
        self.assertEqual(code, 201)
        self.assertEqual((self.listed()["idle-note"]["archived"], self.listed()["idle-note"]["activity"]["source"]), (None, "research"))

    def test_archived_by_hand_comes_back_on_new_activity_and_changes_need_the_steer_password(self):
        self.make("worker", 0)
        revision = self.listed()["worker"]["revision"]
        for action, body in (("archive", {"revision": revision, "archived": True}), ("disable", {"revision": revision}),
                             ("enable", {"revision": revision}), ("token", {})):
            self.assertEqual(self.web("POST", f"/agents/worker/{action}", body, steer=False)[0], 403, action)
        self.assertEqual(self.listed()["worker"]["revision"], revision, "nothing changed without steer")
        code, value = self.web("POST", "/agents/worker/archive", {"revision": revision, "archived": True})
        self.assertEqual((code, value["revision"]), (200, revision + 1))
        self.assertEqual(self.listed()["worker"]["archived"], "manual")
        self.assertEqual(self.web("POST", "/agents/worker/archive", {"revision": revision, "archived": False})[0], 409, "stale revision")
        token = self.token("worker")   # archiving is display only: its token works
        time.sleep(0.01)
        self.assertEqual(self.agent(token, "POST", "/agents/worker/status", {"text": "在干活"})[0], 200)
        self.assertIsNone(self.listed()["worker"]["archived"], "active after it was archived: back in the list")
        # Archived again and brought back by hand.
        revision = self.listed()["worker"]["revision"]
        self.assertEqual(self.web("POST", "/agents/worker/archive", {"revision": revision, "archived": True})[0], 200)
        self.assertEqual(self.web("POST", "/agents/worker/archive", {"revision": revision + 1, "archived": False})[0], 200)
        self.assertIsNone(self.listed()["worker"]["archived"])
        audit = [json.loads(line) for line in (Path(self.jobs.data_root) / "_node/agents/agents.audit.jsonl").read_text().splitlines()]
        self.assertEqual([(a["action"], a["agent"], a["ip"]) for a in audit],
                         [("archive", "worker", "127.0.0.1"), ("archive", "worker", "127.0.0.1"), ("unarchive", "worker", "127.0.0.1")])

    def test_disabling_stops_the_token_keeps_the_registration_and_enabling_needs_a_new_token(self):
        self.make("retired", 0)
        old = self.token("retired")
        self.assertEqual(self.agent(old, "GET", "/whoami")[0], 200)
        revision = self.listed()["retired"]["revision"]
        code, value = self.web("POST", "/agents/retired/disable", {"revision": revision})
        self.assertEqual((code, value["enabled"], value["credential_present"]), (200, False, False))
        self.assertEqual(self.agent(old, "GET", "/whoami")[0], 401)
        self.assertEqual(self.listed()["retired"]["archived"], "disabled")
        self.assertEqual(self.web("POST", "/agents/retired/token", {})[0], 409, "a disabled agent gets no token")
        self.assertEqual(self.web("POST", "/agents/retired/enable", {"revision": value["revision"]})[0], 200)
        self.assertEqual(self.agent(old, "GET", "/whoami")[0], 401, "the old token stays void after enabling")
        code, issued = self.web("POST", "/agents/retired/token", {})
        self.assertEqual((code, issued["node_id"]), (201, self.node_id))
        self.assertEqual(self.agent(issued["token"], "GET", "/whoami")[0], 200)

    def test_a_new_token_from_the_page_replaces_the_old_and_a_running_agent_needs_its_name(self):
        self.make("quiet", 1)
        self.make("busy", 0)
        quiet_old, busy_old = self.token("quiet"), self.token("busy")
        # quiet's last activity was a day ago; busy has a job that has not finished.
        with mock.patch.object(node_agents.time, "time", return_value=time.time() - DAY):
            self.assertEqual(self.agent(quiet_old, "POST", "/agents/quiet/status", {"text": "昨天"})[0], 200)
        self.jobs.dispatch.submit({"bin": "fixture", "argv": ["-c", "pass"], "device_access": "none", "resources": [],
                                   "actor_id": "busy"}, b"", "busy-job-1")
        detail = self.web("GET", "/agents/busy")[1]
        self.assertEqual(len(detail["work"]["jobs"]), 1)
        self.board.fails, self.board._lan_plain = collections.defaultdict(list), True
        with self._browser_page() as page:
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://%s:%d/dash/research#agents-directory" % self.address)
            # Without the steer password the changes are not offered: 管理 unlocks them first.
            page.click("#agents-manage")
            page.wait_for_selector("#agent-dialog #agent-steer")
            page.fill("#agent-steer", core.STEER)
            page.click("#agent-unlock")
            page.click('#agents-list tr[data-agent-row="quiet"] [data-act="token"]')
            page.wait_for_selector("#agent-dialog .agent-warning")
            self.assertNotIn("is-running", page.get_attribute("#agent-dialog .agent-warning", "class"))
            self.assertIn("它现在用的标识立即失效", page.text_content("#agent-dialog .agent-warning"))
            self.assertEqual(page.locator("#agent-confirm-name").count(), 0, "not running: a plain confirmation")
            page.click("#agent-go")
            page.wait_for_selector("#agent-dialog #agent-token")
            token = page.input_value("#agent-token")
            connect = page.input_value("#agent-connect")
            for line in (f"节点 ID：{self.node_id}", "Agent：quiet", f"标识（token）：{token}", "export PF_NODE_TOKEN_FILE=", "export PF_NODE_ID="):
                self.assertIn(line, connect)
            self.assertEqual(self.agent(quiet_old, "GET", "/whoami")[0], 401)
            self.assertEqual(self.agent(token, "GET", "/whoami")[0], 200)
            self.assertEqual(page.evaluate("JSON.stringify(localStorage) + JSON.stringify(sessionStorage)").count(token), 0, "kept nowhere")
            page.click("#agent-dialog [data-close]")
            page.wait_for_selector("#agent-dialog #agent-token", state="detached")   # gone once closed

            # A running agent: the warning stands out and its name must be typed.
            page.click('#agents-list tr[data-agent-row="busy"] [data-act="token"]')
            page.wait_for_selector("#agent-dialog .agent-warning.is-running")
            self.assertIn("未结束的作业：", page.text_content("#agent-dialog .agent-warning"))
            self.assertTrue(page.is_disabled("#agent-go"))
            page.fill("#agent-confirm-name", "bus")
            self.assertTrue(page.is_disabled("#agent-go"))
            page.click("#agent-go", force=True)
            self.assertEqual(self.agent(busy_old, "GET", "/whoami")[0], 200, "not confirmed: nothing replaced")
            page.fill("#agent-confirm-name", "busy")
            page.click("#agent-go")
            page.wait_for_selector("#agent-dialog #agent-token")
            self.assertEqual(self.agent(busy_old, "GET", "/whoami")[0], 401)
            self.assertEqual(errors, [])
        audit = (Path(self.jobs.data_root) / "_node/agents/agents.audit.jsonl").read_text()
        self.assertEqual([json.loads(line)["action"] for line in audit.splitlines()], ["token", "token"])
        self.assertNotIn(token, audit)


if __name__ == "__main__":
    unittest.main()
