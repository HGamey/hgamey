"""Federation reads frozen source indexes; it neither copies artifacts nor starts jobs."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlparse

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from node_service import readonly_token
import test_node_games as game_fixture


class NodeResultSearchTests(unittest.TestCase):
    def setUp(self):
        self.environments = []
        self.addCleanup(self.cleanup)
        self.hub = self.node("hub")
        self.alpha, self.beta = self.node("alpha"), self.node("beta")
        self.targets = []
        for env in (self.alpha, self.beta):
            self.publish(env, "older-" + env.label)
            self.publish(env, "same-result")
            entry = self.hub.jobs.directory.create({"name": env.label, "url": env.url,
                                                    "token": readonly_token(env.admin)})
            # Fixture precondition: the persistent directory already has a known
            # identity. The operation under test must not refresh or rebind it.
            self.assertTrue(self.hub.jobs.directory._pin(entry, env.jobs.games.node_id))
            self.targets.append({"name": env.label, "node_id": env.jobs.games.node_id,
                                 "revision": entry["revision"]})

    def node(self, label):
        env = game_fixture.GameFlowTests()
        env.setUp()
        self.environments.append(env)
        env.label, env.calls, env.fault = label, [], None
        env.entered, env.release = threading.Event(), threading.Event()
        original = env.server.RequestHandlerClass

        class Observed(original):
            def do_GET(self):
                parsed = urlparse(self.path)
                env.calls.append({"method": "GET", "path": parsed.path,
                                  "query": parse_qs(parsed.query),
                                  "pin": self.headers.get("X-Expected-Node-ID")})
                if env.fault == "identity" and parsed.path == "/node":
                    return self._send(200, {"schema_version": 1, "node_id": "f" * 32,
                                           "capabilities": ["results.index-v1", "node.target-v1"]})
                if env.fault == "redirect" and parsed.path == "/node":
                    self.send_response(307)
                    self.send_header("Location", env.redirect_url)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if env.fault == "wait" and parsed.path == "/results":
                    env.entered.set()
                    if not env.release.wait(5):
                        return self._send(503, {"error": "fixture release was not signaled"})
                return super().do_GET()

        env.server.RequestHandlerClass = Observed
        return env

    def cleanup(self):
        for env in self.environments:
            env.release.set()
        # A reply may precede the reader's finally block by a scheduling tick.
        # Wait for only this fixture's reader slots, not unrelated test threads.
        for env in self.environments:
            search = getattr(env.jobs, "result_search", None)
            if search:
                acquired = 0
                try:
                    for _ in range(4):
                        self.assertTrue(search._slots.acquire(timeout=2), "fixture reader did not exit")
                        acquired += 1
                finally:
                    for _ in range(acquired):
                        search._slots.release()
        for env in reversed(self.environments):
            env.tearDown()
            self.assertFalse(env.thread.is_alive())

    def publish(self, env, rid):
        return env.jobs.results.register({"schema": "phonefarm/result@1", "result_id": rid,
                                          "title": env.label + " " + rid,
                                          "labels": {"game_id": "fixture-game"}, "outcome": "unknown"})

    def request(self, targets, *, cursor=None, dash=False, authenticated=True):
        values = {"targets": json.dumps(targets), "game_id": "fixture-game", "limit": 1}
        if cursor is not None:
            values["cursor"] = cursor
        headers = {"X-Expected-Node-ID": self.hub.jobs.games.node_id}
        if authenticated:
            headers["Authorization"] = ("Basic " + base64.b64encode(b"viewer:test-view").decode()
                                        if dash else "Bearer " + readonly_token(self.hub.admin))
        connection = http.client.HTTPConnection(*self.hub.server.server_address, timeout=12)
        try:
            path = ("/dash/api/result-search" if dash else "/result-search") + "?" + urlencode(values)
            connection.request("GET", path, headers=headers)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def cli(self, *args):
        env = {**os.environ, "PF_NODE_URL": self.hub.url, "PF_NODE_ID": self.hub.jobs.games.node_id,
               "PF_NODE_TOKEN": readonly_token(self.hub.admin), "HOME": str(self.hub.root)}
        env.pop("PF_NODE_TOKEN_FILE", None)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "results", "search", *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def assert_read_boundaries(self):
        for env in (self.alpha, self.beta):
            self.assertTrue(env.calls)
            self.assertTrue(all(call["method"] == "GET" and call["path"] in ("/node", "/results")
                                for call in env.calls), env.calls)
            self.assertTrue(all(call["pin"] == env.jobs.games.node_id for call in env.calls), env.calls)
        for env in self.environments:
            self.assertEqual(env.jobs.dispatch.queue.rows(), [])
        self.assertTrue(all(call["method"] == "GET" for call in self.hub.calls))

    def test_cli_freezes_sources_independent_pages_and_partial_offline_results(self):
        first = self.cli("--node", "alpha", "--node", "beta", "--game-id", "fixture-game", "--limit", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        page = json.loads(first.stdout)
        self.assertFalse(page["partial"])
        self.assertEqual([call["path"] for call in self.hub.calls], ["/node", "/nodes", "/result-search"])
        selected = json.loads(self.hub.calls[-1]["query"]["targets"][0])
        self.assertEqual(selected, self.targets)
        groups = {item["source"]["name"]: item for item in page["nodes"]}
        self.assertEqual(set(groups), {"alpha", "beta"})
        self.assertNotEqual(groups["alpha"]["source"]["node_id"], groups["beta"]["source"]["node_id"])
        for env in (self.alpha, self.beta):
            row = groups[env.label]
            self.assertEqual((row["status"], row["items"][0]["result_id"]), ("ok", "same-result"))
            self.assertEqual(row["items"][0]["title"], env.label + " same-result")
            link = urlparse(row["items"][0]["detail_url"])
            self.assertEqual(link.netloc, urlparse(env.url).netloc)
            self.assertEqual((link.path, link.fragment), ("/dash/results", "result=same-result"))
            self.assertEqual(parse_qs(link.query), {"expected_node_id": [env.jobs.games.node_id]})
        self.assertNotIn(self.hub.admin, first.stdout)
        self.assertNotIn(readonly_token(self.hub.admin), first.stdout)

        self.publish(self.alpha, "later-alpha")
        original_cursor = groups["alpha"]["next_cursor"]
        self.assertIsInstance(original_cursor, str)
        self.hub.calls.clear()
        continued = self.cli("--node", "alpha", "--source-node-id", self.alpha.jobs.games.node_id,
                             "--revision", "1", "--cursor", original_cursor,
                             "--game-id", "fixture-game", "--limit", "1")
        self.assertEqual(continued.returncode, 0, continued.stderr)
        tail = json.loads(continued.stdout)["nodes"][0]
        self.assertEqual([call["path"] for call in self.hub.calls], ["/node", "/result-search"])
        self.assertEqual(json.loads(self.hub.calls[-1]["query"]["targets"][0]), self.targets[:1])
        self.assertEqual(tail["requested_cursor"], original_cursor)
        self.assertEqual([item["result_id"] for item in tail["items"]], ["older-alpha"])
        self.assertIsNone(tail["next_cursor"])

        self.beta.server.shutdown()
        self.beta.server.server_close()
        self.beta.thread.join(timeout=2)
        self.assertFalse(self.beta.thread.is_alive())
        partial = self.cli("--node", "alpha", "--node", "beta", "--game-id", "fixture-game", "--limit", "1")
        self.assertEqual(partial.returncode, 2, partial.stderr)
        value = json.loads(partial.stdout)
        self.assertTrue(value["partial"])
        groups = {item["source"]["name"]: item for item in value["nodes"]}
        self.assertEqual(groups["alpha"]["items"][0]["result_id"], "later-alpha")
        self.assertEqual(groups["beta"]["status"], "unavailable")
        self.assertIsNone(groups["beta"]["items"])
        self.assertIsNone(groups["beta"]["next_cursor"])
        self.assert_read_boundaries()

    def test_inflight_revision_identity_redirect_and_read_permissions(self):
        before = [len(env.calls) for env in (self.alpha, self.beta)]
        self.assertEqual(self.request(self.targets, authenticated=False)[0], 401)
        self.assertEqual([len(env.calls) for env in (self.alpha, self.beta)], before)

        self.alpha.fault = "wait"
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.request, self.targets[:1], dash=True)
            try:
                self.assertTrue(self.alpha.entered.wait(3), "source index was not requested")
                changed = self.hub.jobs.directory.update("alpha", {"revision": 1, "note": "changed while reading"})
                self.assertEqual(changed["revision"], 2)
            finally:
                self.alpha.release.set()
            code, result = future.result(timeout=10)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["nodes"][0]["status"], "configuration_changed")
        self.assertIsNone(result["nodes"][0]["items"], "late content must not be published")
        alpha_calls = len(self.alpha.calls)
        self.assertEqual(self.request(self.targets[:1])[1]["nodes"][0]["status"], "configuration_changed")
        self.assertEqual(len(self.alpha.calls), alpha_calls, "stale revision must not contact the new selection")

        self.beta.fault = "identity"
        beta_calls = len(self.beta.calls)
        code, result = self.request(self.targets[1:])
        self.assertEqual((code, result["nodes"][0]["status"]), (200, "identity_mismatch"))
        self.assertEqual([call["path"] for call in self.beta.calls[beta_calls:]], ["/node"])
        self.assertIsNone(result["nodes"][0]["items"])
        self.assertEqual(self.hub.jobs.directory.get("beta")["node_id"], self.beta.jobs.games.node_id)

        self.beta.fault, self.beta.redirect_url = "redirect", self.alpha.url + "/must-not-follow"
        beta_calls = len(self.beta.calls)
        code, result = self.request(self.targets[1:])
        self.assertEqual((code, result["nodes"][0]["status"]), (200, "unsupported"))
        self.assertEqual([call["path"] for call in self.beta.calls[beta_calls:]], ["/node"])
        self.assertEqual(len(self.alpha.calls), alpha_calls, "redirect must not forward source credentials")
        self.assertNotIn(readonly_token(self.beta.admin), json.dumps(result))
        self.assert_read_boundaries()


if __name__ == "__main__":
    unittest.main()
