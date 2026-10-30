"""Registered agent identities: same persistence pattern as the node directory."""
import http.client
import tempfile
import json
import os
import shlex
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import threading
import unittest
from unittest import mock
from pathlib import Path

NODE = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(NODE))

from node_service import Jobs, make_handler
from node_agents import AgentError, NodeAgents

ADMIN = "agent-registry-fixture-not-production"


class HostBindTests(unittest.TestCase):
    """--host 参数：默认 127.0.0.1，显式 0.0.0.0 或内网地址对外。"""

    def test_default_loopback_and_explicit_all_interfaces(self):
        from node_service import argument_parser
        ap = argument_parser()
        # 默认回环
        args = ap.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        # 显式 0.0.0.0
        args = ap.parse_args(["--host", "0.0.0.0"])
        self.assertEqual(args.host, "0.0.0.0")
        # 显式内网地址
        args = ap.parse_args(["--host", "192.168.1.12"])
        self.assertEqual(args.host, "192.168.1.12")


class AgentRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jobs = Jobs(str(self.root / "node"), {"fixture": (sys.executable, str(self.root))})
        self.addCleanup(self.jobs._read_root.close)
        handler = make_handler(None, self.jobs, ADMIN)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def call(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        values = {"Authorization": "Bearer " + ADMIN,
                  "X-Expected-Node-ID": self.jobs.games.node_id,
                  "Content-Type": "application/json",
                  **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_register_list_get_update_and_errors(self):
        # 注册
        status, created = self.call("POST", "/agents", body=json.dumps({
            "name": "test-agent-1", "note": "first test agent", "tags": ["test", "ci"]}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(created["name"], "test-agent-1")
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["node_id"], self.jobs.games.node_id)

        # 重名拒绝
        status, error = self.call("POST", "/agents", body=json.dumps({"name": "test-agent-1"}).encode())
        self.assertEqual((status, error["code"]), (409, "agent_error"))

        # 列表
        status, listed = self.call("GET", "/agents")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["agents"]), 1)
        self.assertEqual(listed["agents"][0]["name"], "test-agent-1")

        # 单个查询
        status, got = self.call("GET", "/agents/test-agent-1")
        self.assertEqual(status, 200, got)
        self.assertEqual(got["note"], "first test agent")

        # 更新
        status, updated = self.call("POST", "/agents/test-agent-1", body=json.dumps({
            "revision": 1, "note": "updated note"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(updated["note"], "updated note")
        self.assertEqual(updated["revision"], 2)

        # 错误 revision 拒绝
        status, error = self.call("POST", "/agents/test-agent-1", body=json.dumps({
            "revision": 1, "note": "wrong revision"}).encode())
        self.assertEqual((status, error["code"]), (409, "agent_error"))

        # 不存在
        status, error = self.call("GET", "/agents/no-such-agent")
        self.assertEqual((status, error["code"]), (404, "agent_error"))

    def test_name_validation(self):
        for bad in ["", "UPPER", "a b", "a/b", "a" * 65]:
            status, error = self.call("POST", "/agents", body=json.dumps({"name": bad}).encode())
            self.assertEqual(status, 400, f"expected 400 for {bad!r}")

    def test_status_post_and_history(self):
        # 注册后上报状态
        self.call("POST", "/agents", body=json.dumps({"name": "status-agent"}).encode())
        status, value = self.call("POST", "/agents/status-agent/status", body=json.dumps({
            "text": "正在优化原神省电", "detail": "第 3 代：测大核封顶"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(value["status"]["text"], "正在优化原神省电")
        # 再上报一条，历史累积
        self.call("POST", "/agents/status-agent/status", body=json.dumps({"text": "等待人工确认"}).encode())
        _, got = self.call("GET", "/agents/status-agent")
        self.assertEqual(got["status"]["text"], "等待人工确认")
        self.assertEqual(len(got["status_history"]), 2)
        # 未注册 agent 上报 404
        status, error = self.call("POST", "/agents/no-such/status", body=json.dumps({"text": "x"}).encode())
        self.assertEqual((status, error["code"]), (404, "agent_error"))
        # 空文本 400
        status, _ = self.call("POST", "/agents/status-agent/status", body=json.dumps({"text": ""}).encode())
        self.assertEqual(status, 400)

    def test_persistence_across_restart(self):
        # 注册后关闭再开，注册表保留
        self.call("POST", "/agents", body=json.dumps({"name": "persistent-agent"}).encode())
        self.jobs.agents.close()
        self.jobs.agents = NodeAgents(self.jobs.data_root, self.jobs.games.node_id)
        self.assertEqual(self.jobs.agents.get("persistent-agent")["name"], "persistent-agent")

    def invite(self, **fields):
        status, value = self.call("POST", "/agent-invites", body=json.dumps(fields))
        self.assertEqual(status, 201, value)
        return value

    def join(self, code, name, **fields):
        # A new agent has neither a token nor a node ID.
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", "/join", json.dumps({"invite": code, "name": name, **fields}),
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_invite_join_checkin_and_hash_only_persistence(self):
        invited = self.invite(note="research team")
        self.assertEqual(invited["expires_at"] - invited["created_at"], 24 * 3600)
        # The code survives a node restart, but never appears on disk or in a list.
        self.jobs.agents.close()
        self.jobs.agents = NodeAgents(self.jobs.data_root, self.jobs.games.node_id)
        status, listed = self.call("GET", "/agent-invites")
        self.assertEqual(status, 200, listed)
        self.assertEqual([item["id"] for item in listed["invites"]], [invited["id"]])
        self.assertNotIn(invited["code"], json.dumps(listed))
        self.assertNotIn("sha256", json.dumps(listed))
        status, joined = self.join(invited["code"], "invite-agent", note="my model")
        self.assertEqual(status, 201, joined)
        self.assertEqual(joined["node_id"], self.jobs.games.node_id)
        self.assertEqual(joined["agent"]["note"], "my model")
        status, checked = self.call("POST", "/agents/invite-agent/checkin", body="{}",
                                   headers={"Authorization": "Bearer " + joined["token"]})
        self.assertEqual(status, 200, checked)
        self.assertEqual(checked["agent"], "invite-agent")
        self.assertEqual(self.call("GET", "/agent-invites")[1]["invites"], [])
        stored = (Path(self.jobs.data_root) / "_node/agents/registry.json").read_text()
        self.assertNotIn(invited["code"], stored)
        self.assertNotIn(joined["token"], stored)

    def test_used_expired_taken_and_invalid_invites_have_distinct_errors(self):
        used = self.invite()
        self.assertEqual(self.join(used["code"], "taken")[0], 201)
        status, replay = self.join(used["code"], "other")
        self.assertEqual(status, 409, replay)
        self.assertIn("used", replay["error"])
        fresh = self.invite(hours=2)
        self.assertEqual(fresh["expires_at"] - fresh["created_at"], 2 * 3600)
        status, taken = self.join(fresh["code"], "taken")
        self.assertEqual(status, 409, taken)
        self.assertIn("already registered", taken["error"])
        status, invalid_name = self.join(fresh["code"], "UPPER")
        self.assertEqual(status, 400, invalid_name)
        # A name error does not consume the code.
        self.assertEqual(self.join(fresh["code"], "available")[0], 201)
        expired = self.invite()
        with mock.patch("node_agents.time.time", return_value=expired["expires_at"] + 1):
            status, error = self.join(expired["code"], "late")
        self.assertEqual(status, 410, error)
        self.assertIn("expired", error["error"])
        status, error = self.join("not-an-invite", "unknown")
        self.assertEqual(status, 404, error)
        self.assertIn("not found", error["error"])

    def test_only_admin_can_issue_list_and_revoke_invites(self):
        first = self.invite()
        _, joined = self.join(first["code"], "member")
        second = self.invite()
        for method, path, body in [("POST", "/agent-invites", "{}"),
                                   ("GET", "/agent-invites", None),
                                   ("DELETE", "/agent-invites/" + second["id"], None)]:
            for token, expected in [("", 401), (joined["token"], 403)]:
                status, error = self.call(method, path, body=body, headers={"Authorization": "Bearer " + token})
                self.assertEqual(status, expected, error)
        self.assertEqual(self.call("DELETE", "/agent-invites/" + second["id"])[0], 200)
        self.assertEqual(self.join(second["code"], "revoked")[0], 404)
        self.assertEqual(self.call("GET", "/agent-invites")[1]["invites"], [])

    def test_simultaneous_joins_consume_invite_once(self):
        invited = self.invite()
        ready = threading.Barrier(2)
        def attempt(name):
            ready.wait(timeout=5)
            return self.join(invited["code"], name)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["first", "second"]))
        self.assertEqual(sorted(status for status, _ in results), [201, 409], results)
        self.assertEqual(len(self.jobs.agents.list()), 1)
        winner = next(value for status, value in results if status == 201)
        self.assertEqual(self.jobs.agents.verify(winner["token"]), winner["agent"]["name"])

    def pf(self, *args, **environment):
        env = {key: value for key, value in os.environ.items() if not key.startswith("PF_NODE_")}
        env.update(environment)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), *args], env=env,
                              capture_output=True, text=True, timeout=15)

    def test_cli_join_needs_only_url_writes_private_token_and_checks_in(self):
        url = "http://%s:%s" % self.server.server_address
        invited = self.pf("agents", "invite", "--note", "join fixture", PF_NODE_URL=url,
                          PF_NODE_ID=self.jobs.games.node_id, PF_NODE_TOKEN=ADMIN)
        self.assertEqual(invited.returncode, 0, invited.stderr)
        code = invited.stdout.splitlines()[0].split()[-1]
        for fragment in [url, self.jobs.games.node_id, "join --invite " + code, "每一轮：", "./bin/pf-node checkin"]:
            self.assertIn(fragment, invited.stdout)
        unwritable = self.pf("join", "--invite", code, "--name", "cli-agent", "--token-file", str(self.root),
                             PF_NODE_URL=url)
        self.assertNotEqual(unwritable.returncode, 0)
        self.assertEqual(self.jobs.agents.list(), [])
        token_file = self.root / "agent token"
        token_file.write_text("replace this token")
        token_file.chmod(0o644)
        joined = self.pf("join", "--invite", code, "--name", "cli-agent", "--token-file", str(token_file),
                         PF_NODE_URL=url)
        self.assertEqual(joined.returncode, 0, joined.stderr)
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        token = token_file.read_text().strip()
        self.assertEqual(self.jobs.agents.verify(token), "cli-agent")
        self.assertNotIn(token, joined.stdout + joined.stderr)
        exports = dict(shlex.split(line)[1].split("=", 1) for line in joined.stdout.splitlines()
                       if line.startswith("export "))
        self.assertEqual(exports, {"PF_NODE_URL": url, "PF_NODE_ID": self.jobs.games.node_id,
                                   "PF_NODE_TOKEN_FILE": str(token_file)})
        checked = self.pf("checkin", "--json", **exports)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["agent"], "cli-agent")
        # A rejected join leaves an existing token intact and reports the server's reason.
        replay = self.pf("join", "--invite", code, "--name", "again", "--token-file", str(token_file), PF_NODE_URL=url)
        self.assertNotEqual(replay.returncode, 0)
        self.assertIn("used", replay.stderr)
        self.assertEqual(token_file.read_text().strip(), token)

    def test_onboarding_help_is_available_without_credentials(self):
        for command in [("agents", "--help"), ("join", "--help")]:
            help_result = self.pf(*command)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--invite", help_result.stdout)

    def test_invite_expiry_must_be_a_positive_duration(self):
        for hours in [0, -1, "never", None, float("nan"), float("inf")]:
            status, error = self.call("POST", "/agent-invites", body=json.dumps({"hours": hours}))
            self.assertEqual(status, 400, error)
        self.assertEqual(self.call("GET", "/agent-invites")[1]["invites"], [])


if __name__ == "__main__":
    unittest.main()
