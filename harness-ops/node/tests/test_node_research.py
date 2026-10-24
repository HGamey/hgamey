"""Research metadata is durable collaboration state, not an execution scheduler."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
from node_fleet import FleetState
from node_service import Jobs, make_handler, readonly_token
import test_node_games as game_fixture


class NodeResearchTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.pin = self.env.jobs.games.node_id

    def call(self, method, path, data=None, *, headers=None, token=None):
        return self.env.call(method, path, data, token=token,
                             headers={"X-Expected-Node-ID": self.pin, **(headers or {})})

    @staticmethod
    def envelope(operation, data, revision=None, reason=None):
        return {"schema": "phonefarm/research-write@1", "operation_id": operation,
                "base_revision": revision, "data": data, "actor_label": "fixture-agent", "reason": reason}

    def write(self, kind, identity, operation, data, revision=None, reason=None, **kwargs):
        return self.call("POST", f"/research-state/{kind}/{identity}",
                         self.envelope(operation, data, revision, reason), **kwargs)

    def read(self, kind, identity, revision=None):
        suffix = "" if revision is None else "?revision=" + str(revision)
        code, value = self.call("GET", f"/research-state/{kind}/{identity}" + suffix,
                                token=readonly_token(self.env.admin))
        self.assertEqual(code, 200, value)
        return value

    def create(self, kind, identity, data):
        code, value = self.write(kind, identity, "create-" + identity, data)
        self.assertEqual(code, 201, value)
        return value

    def events(self):
        code, value = self.call("GET", "/research-events?research_id=study&limit=500",
                                token=readonly_token(self.env.admin))
        self.assertEqual(code, 200, value)
        return value["items"]

    def cli(self, *args):
        env = {**os.environ, "PF_NODE_URL": self.env.url, "PF_NODE_ID": self.pin,
               "PF_NODE_TOKEN": self.env.admin, "HOME": str(self.env.root)}
        env.pop("PF_NODE_TOKEN_FILE", None)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "research", *args],
                              env=env, capture_output=True, text=True, timeout=10)

    @staticmethod
    def direction(title):
        return {"research_id": "study", "title": title, "hypothesis": "A separately evaluated hypothesis"}

    @staticmethod
    def dependency(kind, identity, revision):
        return {"id": "needs-" + identity, "kind": kind, "target_id": identity,
                "target_revision": revision, "blocking": True, "reason": "Explicit prerequisite",
                "resolution": None}

    def restart(self):
        """Reopen the same node data with a new service and store, retaining no object cache."""
        env = self.env
        env.server.shutdown()
        env.server.server_close()
        env.thread.join(timeout=2)
        self.assertFalse(env.thread.is_alive())
        env.jobs.directory.close()
        env.jobs.games.close()
        env.board.monitor.close()
        env.jobs = Jobs(str(env.root), {}, data_dir=str(env.root))
        env.board.job_service = env.jobs
        env.board.monitor = FleetState(str(env.root), str(env.root / "locks"), str(env.root / "shots"))
        env.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, env.jobs, env.admin, env.board))
        env.thread = threading.Thread(target=env.server.serve_forever, daemon=True)
        env.thread.start()
        env.url = "http://127.0.0.1:" + str(env.server.server_port)
        self.assertEqual(env.jobs.games.node_id, self.pin)

    def test_cas_operation_replay_cycle_rollback_and_restart_history(self):
        source = self.env.root / "research-data.json"
        source.write_text(json.dumps({"title": "Fixture study", "objective": "Compare explicitly scoped evidence"}))
        created = self.cli("create", "research", "study", str(source), "--operation-id", "study-create")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)["object"], {"kind": "research", "id": "study", "revision": 1})
        self.create("direction", "a", self.direction("Direction A"))
        self.create("direction", "b", self.direction("Direction B"))
        original = self.read("direction", "a")["data"]
        contenders = [self.envelope("a-contender-" + str(n), {**original, "priority": n}, 1,
                                    "Independent proposed priority") for n in (1, 2)]
        barrier = threading.Barrier(2)

        def attempt(payload):
            barrier.wait(timeout=3)
            return self.call("POST", "/research-state/direction/a", payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(attempt, contenders))
        self.assertEqual(sorted(code for code, _ in replies), [201, 409])
        winner_index = next(i for i, reply in enumerate(replies) if reply[0] == 201)
        winner, receipt = contenders[winner_index], replies[winner_index][1]
        conflict = replies[1 - winner_index][1]
        self.assertEqual((conflict["code"], conflict["current_revision"]), ("research_revision_conflict", 2))
        a2 = self.read("direction", "a")
        code, _ = self.write("direction", "a", "a-next", {**a2["data"], "priority_reason": "Later decision"},
                             2, "Keep the later explicit decision")
        self.assertEqual(code, 201)
        before_replay = self.events()
        self.assertEqual(self.call("POST", "/research-state/direction/a", winner), (201, receipt))
        changed = {**winner, "data": {**winner["data"], "priority": 999}}
        code, value = self.call("POST", "/research-state/direction/a", changed)
        self.assertEqual((code, value["code"]), (409, "research_operation_conflict"))
        self.assertEqual(self.events(), before_replay)

        b1 = self.read("direction", "b")
        self.assertEqual(b1["revision"], 1, "A's revisions must not consume B's CAS revision")
        code, _ = self.write("direction", "b", "b-ready", {**b1["data"], "state": "ready"}, 1, "B can proceed")
        self.assertEqual(code, 201)
        a3 = self.read("direction", "a")
        code, _ = self.write("direction", "a", "a-needs-b", {**a3["data"],
                             "dependencies": [self.dependency("direction", "b", 2)]}, 3, "A needs B")
        self.assertEqual(code, 201)
        b2 = self.read("direction", "b")
        before_cycle = self.events()
        code, failure = self.write("direction", "b", "cycle-rejected", {**b2["data"],
                                   "dependencies": [self.dependency("direction", "a", 4)]}, 2, "Would form a cycle")
        self.assertEqual((code, failure["code"]), (409, "research_dependency_cycle"))
        self.assertEqual(self.events(), before_cycle)
        self.assertEqual(self.read("direction", "b"), b2)
        self.assertEqual(self.call("GET", "/research-operations/cycle-rejected")[0], 404)
        self.assertTrue(self.read("direction", "a")["derived"]["metadata_blocked"])
        self.assertFalse(b2["derived"]["metadata_blocked"])

        code, history = self.call("GET", "/research-state/direction/a/history?limit=2")
        self.assertEqual(code, 200, history)
        self.assertEqual([entry["revision"] for entry in history["items"]], [4, 3])
        self.restart()
        lookup = self.cli("operation", winner["operation_id"])
        self.assertEqual(lookup.returncode, 0, lookup.stderr)
        self.assertEqual(json.loads(lookup.stdout), receipt)
        code, tail = self.call("GET", "/research-state/direction/a/history?limit=2&cursor=" + history["next_cursor"])
        self.assertEqual(code, 200, tail)
        self.assertEqual([entry["revision"] for entry in tail["items"]], [2, 1])
        old = self.read("direction", "a", 1)
        self.assertEqual((old["revision"], old["current_revision"], old["data"]), (1, 4, original))
        self.assertEqual(self.events(), before_cycle)
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])

    def test_feedback_preserves_context_until_explicit_direction_resolution(self):
        self.create("research", "study", {"title": "Fixture study", "objective": "Original evaluation criteria"})
        self.create("direction", "a", self.direction("Waiting direction"))
        self.create("direction", "b", self.direction("Independent direction"))
        result = self.env.jobs.results.register({"schema": "phonefarm/result@1", "result_id": "reference",
            "title": "Synthetic metadata reference, no image was inspected", "outcome": "unknown",
            "sources": [{"name": "image", "kind": "external_reference", "locator": "fixture://declared-image"}]})
        self.assertEqual(result["source_observations"][0]["status"], "external")
        self.assertIsNone(result["source_observations"][0]["sha256"])
        ref = {"kind": "result_source", "node_id": self.pin, "result_id": "reference", "source_name": "image"}
        question = {"research_id": "study", "type": "evaluation", "title": "Review the stated limitation",
                    "need": "Text feedback only", "why": "The referenced bytes have not been inspected",
                    "context": {"research_revision": 1, "directions": [{"id": "a", "revision": 1}]},
                    "evidence_refs": [ref], "due_at": 0}
        self.create("request", "question", question)
        request1 = self.read("request", "question")
        self.assertEqual(request1["data"]["state"], "open")
        self.assertTrue(request1["derived"]["overdue"], "past due only adds a hint")
        self.assertEqual(len(request1["evidence_bindings"]), 1)
        a1, b1 = self.read("direction", "a"), self.read("direction", "b")
        code, _ = self.write("direction", "a", "wait-for-question", {**a1["data"], "state": "waiting_human",
            "state_reason": "Explicitly needs this evaluation", "dependencies": [self.dependency("request", "question", 1)]},
            1, "Record the pending prerequisite")
        self.assertEqual(code, 201)
        code, _ = self.write("direction", "b", "independent-ready", {**b1["data"], "state": "ready"}, 1, "Continue B")
        self.assertEqual(code, 201)

        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1"}
        steer = {**view, "Cookie": dash.STEER_COOKIE + "=" + self.env.board._cookie_val("test-steer", b"hgamey-steer-v1")}
        reply = self.envelope("reply-one", {"request_id": "question", "request_revision": 1,
                                            "text": "Keep the limitation explicit; no image judgment is claimed."})
        reply["actor_label"] = "self-reported-human-label"
        code, reply_receipt = self.call("POST", "/dash/api/research-state/feedback/reply-one", reply, headers=steer)
        self.assertEqual(code, 201, reply_receipt)
        self.assertEqual(self.read("feedback", "reply-one")["audit"]["auth_channel"], "dashboard_steer")
        observed = self.read("request", "question")
        self.assertEqual((observed["revision"], observed["derived"]["feedback_count"]), (1, 1))
        self.assertEqual(observed["derived"]["affected_directions"], [{"id": "a", "revision": 2,
                         "dependency_id": "needs-question", "blocking": True, "resolved": False}])
        code, _ = self.write("request", "question", "close-question", {**observed["data"], "state": "closed",
                             "close_reason": "Answer recorded, still awaiting agent assessment"}, 1, "Close this question")
        self.assertEqual(code, 201)
        study = self.read("research", "study")
        code, _ = self.write("research", "study", "revised-criteria", {**study["data"], "objective": "Revised criteria"},
                             1, "Criteria changed after the original question")
        self.assertEqual(code, 201)
        self.create("feedback", "late-reply", {"request_id": "question", "request_revision": 1,
                                                "text": "A late reply to the original question."})
        late = self.read("feedback", "late-reply")
        submission = copy.deepcopy(late["derived"])
        self.assertEqual((submission["request_state_at_submit"], submission["request_revision_at_submit"]), ("closed", 2))
        self.assertTrue(submission["context_changed_at_submit"]["research"])
        self.assertEqual(late["data"]["request_revision"], 1)
        self.assertEqual(self.read("request", "question")["data"]["context"], question["context"])
        self.create("request", "new-question", {**question, "supersedes": "question",
            "context": {"research_revision": 2, "directions": [{"id": "a", "revision": 2}]}})
        self.assertEqual(self.read("request", "new-question")["derived"]["feedback_count"], 0)
        self.assertEqual(self.read("request", "question")["derived"]["feedback_count"], 2)
        a2, b2 = self.read("direction", "a"), self.read("direction", "b")
        self.assertTrue(a2["derived"]["metadata_blocked"])
        self.assertEqual(len(a2["derived"]["unresolved_dependencies"]), 1)
        self.assertEqual((b2["revision"], b2["data"]["state"], b2["derived"]["metadata_blocked"]), (2, "ready", False))

        before_denied = self.events()
        forbidden = self.envelope("not-authorized", self.direction("Should not be saved"))
        path = "/research-state/direction/not-saved"
        self.assertEqual(self.call("POST", path, forbidden, token=readonly_token(self.env.admin))[0], 401)
        self.assertEqual(self.call("POST", "/dash/api" + path, forbidden, headers=view)[0], 403)
        self.assertEqual(self.env.call("POST", path, forbidden)[0], 400, "new writes require an explicit target")
        self.assertEqual(self.call("POST", path, forbidden, headers={"X-Expected-Node-ID": "f" * 32})[0], 409)
        self.assertEqual(self.call("GET", "/research-operations/not-authorized")[0], 404)
        self.assertEqual(self.events(), before_denied)

        resolved = copy.deepcopy(a2["data"])
        resolved["dependencies"][0]["resolution"] = {"decision": "satisfied", "reason": "Explicitly reviewed the response",
                                                       "feedback_ids": ["reply-one"]}
        resolved["feedback_decisions"] = [{"feedback_id": "reply-one", "decision": "adopt", "reason": "Retain its limitation"}]
        code, _ = self.write("direction", "a", "resolve-question", resolved, 2, "Adopt feedback explicitly")
        self.assertEqual(code, 201)
        self.assertFalse(self.read("direction", "a")["derived"]["metadata_blocked"])
        self.assertIsNone(self.read("direction", "a", 2)["data"]["dependencies"][0]["resolution"])
        self.assertEqual(self.read("feedback", "late-reply")["derived"], submission,
                         "later direction changes must not rewrite feedback submission facts")
        operation = self.cli("operation", "reply-one")
        self.assertEqual(operation.returncode, 0, operation.stderr)
        self.assertEqual(json.loads(operation.stdout), reply_receipt)
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])


if __name__ == "__main__":
    unittest.main()
