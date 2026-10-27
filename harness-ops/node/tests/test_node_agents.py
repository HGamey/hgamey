"""Registered agent identities: same persistence pattern as the node directory."""
import http.client
import tempfile
import json
from http.server import ThreadingHTTPServer
import threading
import unittest
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

    def test_persistence_across_restart(self):
        # 注册后关闭再开，注册表保留
        self.call("POST", "/agents", body=json.dumps({"name": "persistent-agent"}).encode())
        self.jobs.agents.close()
        self.jobs.agents = NodeAgents(self.jobs.data_root, self.jobs.games.node_id)
        self.assertEqual(self.jobs.agents.get("persistent-agent")["name"], "persistent-agent")


if __name__ == "__main__":
    unittest.main()
