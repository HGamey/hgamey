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
import time
import unittest
from unittest import mock
from urllib.parse import urlencode, urlsplit

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
from node_fleet import FleetState
from node_read import decode_cursor, encode_cursor
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

    def cli(self, *args, token=None):
        env = {**os.environ, "PF_NODE_URL": self.env.url, "PF_NODE_ID": self.pin,
               "PF_NODE_TOKEN": self.env.admin if token is None else token, "HOME": str(self.env.root)}
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

    def test_criteria_declared_before_candidate_and_assessed_at_fixed_revision(self):
        legacy = {"title": "Synthetic scoped study", "objective": "Module ms with explicit quality tolerance"}
        original_receipt = self.create("research", "study", legacy)
        original = self.read("research", "study")
        self.assertNotIn("criteria", original["data"])
        self.assertNotIn("criteria_bindings", original["derived"])
        endpoint = "/research-state/research/study/assessment"
        code, absent = self.call("GET", endpoint + "?revision=1")
        self.assertEqual((code, absent["code"]), (409, "research_criteria_required"))

        def register(identity, timing, quality):
            code, record = self.call("POST", "/results", {
                "schema": "phonefarm/result@1", "result_id": identity,
                "title": "Synthetic registered point values only", "outcome": "unknown",
                "origin": {"producer": "fixture", "evidence_origin": "synthetic"},
                "metrics": [
                    {"name": "module_time", "condition": identity, "value": timing, "unit": "ms",
                     "scope": "module", "method": "reported"},
                    {"name": "psnr", "condition": identity, "value": quality, "unit": "dB",
                     "scope": "image", "method": "reported", "region": "full_frame"},
                    {"name": "power", "condition": identity, "value": None, "unit": "W",
                     "scope": "power", "method": "unknown", "unavailable_reason": "No power was sampled"}],
                "claims": ["Synthetic arithmetic, no device or scientific qualification"]})
            self.assertEqual(code, 201, record)
            return record

        baseline = register("criteria-native", 4, 40)
        criteria = {"schema": "phonefarm/research-criteria@1", "baseline_role": "original_game",
            "comparison": {"schema": "phonefarm/result-compare@1", "node_id": self.pin,
                "columns": [{"id": "native", "label": "Declared original", "result_id": "criteria-native"},
                            {"id": "low", "label": "Pending lower-render candidate", "result_id": "criteria-later"}],
                "rows": [{"id": row, "label": label, "metric_indexes": [index, index]}
                         for index, row, label in ((0, "timing", "Module elapsed"),
                                                  (1, "quality", "Declared quality"), (2, "power", "Power"))],
                "context": {"scope": "Synthetic values, not measured performance",
                            "assumptions": ["Explicit row selections"],
                            "interventions": ["Declared lower rendering resolution"]}},
            "axes": [
                {"id": "time", "row_id": "timing", "label": "Module time", "unit": "ms", "scope": "module",
                 "role": "objective", "prefer": "minimize", "required": True,
                 "rule": {"kind": "absolute_improvement_min", "threshold": .5}},
                {"id": "image", "row_id": "quality", "label": "Acceptable quality", "unit": "dB", "scope": "image",
                 "role": "constraint", "prefer": "maximize", "required": True,
                 "rule": {"kind": "degradation_max", "threshold": 3}},
                {"id": "power", "row_id": "power", "label": "Optional power", "unit": "W", "scope": "power",
                 "role": "observe", "prefer": "minimize", "required": False, "rule": None}],
            "pareto_axes": ["time", "image"]}
        code, _ = self.write("research", "study", "freeze-before-candidate",
                             {**original["data"], "criteria": criteria}, 1, "Freeze before candidate registration")
        self.assertEqual(code, 201)
        frozen = self.read("research", "study", 2)
        bindings = frozen["derived"]["criteria_bindings"]
        self.assertEqual(bindings, [
            {"column_id": "native", "node_id": self.pin, "result_id": "criteria-native",
             "status": "registration_bound", "result_sequence": baseline["sequence"],
             "result_registered_at": baseline["registered_at"]},
            {"column_id": "low", "node_id": self.pin, "result_id": "criteria-later",
             "status": "declared_pending", "result_sequence": None, "result_registered_at": None}])
        self.create("direction", "planned", {**self.direction("A separately scheduled experiment"),
            "next_experiment": {"title": "Use the frozen goal", "plan": "No job is started by this test",
                                "research_revision": 2}})
        planned = self.read("direction", "planned")
        code, pending = self.call("GET", endpoint + "?revision=2")
        self.assertEqual(code, 200, pending)
        self.assertEqual(pending["comparison"]["columns"][1]["error"]["code"], "result_not_found")
        self.assertEqual(pending["candidates"][1]["requirements"]["status"], "undetermined")
        missing_result = next(item for item in pending["suggestions"] if item["kind"] == "verify_measurement")
        self.assertIn({"column_id": "low", "axis_id": None, "reason": "result_not_found"}, missing_result["affected"])

        # Registration is later than the criteria commit; reading does not rewrite
        # the declaration into evidence that the candidate was known in advance.
        candidate = register("criteria-later", 3, 38)
        readonly = readonly_token(self.env.admin)
        code, assessed = self.call("GET", endpoint + "?revision=2", token=readonly)
        self.assertEqual(code, 200, assessed)
        self.assertEqual((assessed["research_id"], assessed["revision"], assessed["criteria"]),
                         ("study", 2, criteria))
        self.assertEqual((assessed["basis"], assessed["comparability"]),
                         ("registered_point_values", "not_established"))
        self.assertEqual(assessed["criteria_bindings"], bindings)
        low = assessed["candidates"][1]
        self.assertEqual(low["column_id"], "low")
        axes = {axis["axis_id"]: axis for axis in low["axes"]}
        self.assertEqual((axes["time"]["value"], axes["time"]["observed"], axes["time"]["status"]),
                         (3, 1, "satisfied"))
        self.assertEqual((axes["image"]["value"], axes["image"]["observed"], axes["image"]["status"]),
                         (38, 2, "satisfied"))
        self.assertEqual((axes["power"]["status"], axes["power"]["reason"], axes["power"]["observed"]),
                         ("unknown", "value_unavailable", None))
        suggestions = {item["kind"]: item for item in assessed["suggestions"]}
        self.assertEqual(set(suggestions), {"reuse_baseline", "module_timing", "matched_images",
                                           "aligned_power", "asynchronous_assistance"})
        self.assertIn({"column_id": "low", "axis_id": "power", "reason": "value_unavailable"},
                      suggestions["aligned_power"]["affected"])
        self.assertIn({"column_id": "low", "axis_id": "image", "reason": "image_domain:unrecorded"},
                      suggestions["matched_images"]["affected"])
        self.assertTrue(all(isinstance(item["next_experiment"], str) for item in suggestions.values()))
        self.assertEqual(self.read("direction", "planned"), planned, "suggestions must not rewrite or unblock a direction")
        self.assertEqual(low["requirements"], {"status": "satisfied", "required_axes": ["time", "image"],
                                              "unknown_axes": [], "failed_axes": []})
        self.assertEqual((assessed["pareto"]["status"], assessed["pareto"]["scope"]),
                         ("computed", "complete_value_subset"))
        self.assertEqual(assessed["pareto"]["undetermined_column_ids"], [])
        self.assertEqual(assessed["pareto"]["excluded_axis_ids"], ["power"])
        self.assertEqual([(item["column_id"], item["status"], item["dominated_by"])
                          for item in assessed["pareto"]["columns"]],
                         [("native", "non_dominated", []), ("low", "non_dominated", [])])

        revised = copy.deepcopy(frozen["data"])
        revised["criteria"]["axes"][1]["rule"]["threshold"] = 1
        revised["criteria"]["pareto_axes"].append("power")
        code, _ = self.write("research", "study", "tighten-after-registration", revised, 2,
                             "Tighten declared quality tolerance and include power in the selected projection")
        self.assertEqual(code, 201)
        code, stricter = self.call("GET", endpoint + "?revision=3", token=readonly)
        self.assertEqual(code, 200, stricter)
        self.assertEqual(stricter["candidates"][1]["requirements"]["failed_axes"], ["image"])
        self.assertEqual(stricter["candidates"][1]["requirements"]["status"], "not_satisfied")
        self.assertEqual(stricter["pareto"]["undetermined_column_ids"], ["native", "low"])
        for column in stricter["pareto"]["columns"]:
            self.assertEqual((column["status"], column["dominated_by"]), ("undetermined", []))
            self.assertIn({"axis_id": "power", "reason": "value_unavailable"}, column["reasons"])
        self.assertEqual(stricter["criteria_bindings"], bindings)
        self.assertEqual(self.read("research", "study", 3)["derived"]["criteria_bindings"], bindings)
        self.assertEqual(self.read("research", "study", 2)["data"], frozen["data"])
        self.assertEqual(self.read("direction", "planned"), planned)

        before_reads = self.events()
        code, replay = self.call("POST", "/research-state/research/study",
                                self.envelope("create-study", legacy))
        self.assertEqual((code, replay), (201, original_receipt))
        self.assertEqual(self.read("research", "study", 1)["data"], original["data"])
        code, no_pin = self.env.call("GET", endpoint + "?revision=2", token=readonly)
        self.assertEqual((code, no_pin["code"]), (400, "target_required"))
        self.assertEqual(self.call("GET", endpoint, token=readonly)[0], 400)
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1"}
        code, viewed = self.call("GET", "/dash/api" + endpoint + "?revision=2", headers=view)
        self.assertEqual(code, 200, viewed)
        self.assertEqual(viewed["criteria"], criteria)

        calls = []
        handler = self.env.server.RequestHandlerClass
        original_get, original_post = handler.do_GET, handler.do_POST

        def tracked_get(request):
            calls.append(("GET", request.path))
            return original_get(request)

        def tracked_post(request):
            calls.append(("POST", request.path))
            return original_post(request)

        with mock.patch.object(handler, "do_GET", tracked_get), mock.patch.object(handler, "do_POST", tracked_post):
            response = self.cli("assess", "study", "--revision", "2", token=readonly)
        self.assertEqual(response.returncode, 0, response.stderr)
        prior = json.loads(response.stdout)
        self.assertEqual(calls, [("GET", "/node"), ("GET", endpoint + "?revision=2")])
        self.assertEqual((prior["revision"], prior["current_revision"], prior["criteria"]), (2, 3, criteria))
        self.assertEqual(prior["criteria_bindings"], bindings)
        self.assertEqual(prior["candidates"], assessed["candidates"])
        self.assertEqual(prior["pareto"], assessed["pareto"])
        self.assertEqual(self.events(), before_reads)
        for record in (baseline, candidate):
            self.assertEqual(self.call("GET", "/results/" + record["result_id"])[1], record)
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])
        self.assertIsNone(self.env.jobs.dispatch.thread)
        self.assertIsNone(self.env.board.monitor._thread)

    def test_watch_cli_timeout_commit_wakeup_and_persisted_cursor(self):
        self.create("research", "study", {"title": "Watched study", "objective": "Observe committed metadata only"})
        code, initial = self.call("GET", "/research-events?research_id=study&after=0")
        self.assertEqual(code, 200, initial)
        cursor, stream = initial["next_cursor"], initial["stream_id"]
        self.assertEqual(initial["wait_result"], "events")
        self.assertEqual(initial["next_after"], initial["head"])
        # Existing unpinned numeric reads remain possible; portable positions and
        # active waits require the explicit target, including for read-only users.
        self.assertEqual(self.env.call("GET", "/research-events?after=0")[0], 200)
        code, unbound = self.env.call("GET", "/research-events?" + urlencode({"cursor": cursor}))
        self.assertEqual((code, unbound["code"]), (400, "target_required"))

        event_gets = []
        handler = self.env.server.RequestHandlerClass
        original_get = handler.do_GET

        def record_get(request):
            if urlsplit(request.path).path == "/research-events":
                event_gets.append(request.path)
            return original_get(request)

        start = time.monotonic()
        with mock.patch.object(handler, "do_GET", record_get):
            empty = self.cli("events", "--cursor", cursor, "--wait-seconds", "1")
        elapsed = time.monotonic() - start
        self.assertEqual(empty.returncode, 0, empty.stderr)
        timeout = json.loads(empty.stdout)
        self.assertEqual((timeout["items"], timeout["wait_result"], timeout["next_cursor"]), ([], "timeout", cursor))
        self.assertEqual(len(event_gets), 1, "CLI must not turn empty wait into an automatic polling loop")
        self.assertGreaterEqual(elapsed, .7)
        self.assertLess(elapsed, 5, "one-second empty wait must return within a bounded request")

        # Observe entry to the actual Condition.wait; submit only once its last
        # empty read has completed, without relying on a scheduling sleep.
        entered = threading.Event()
        condition = self.env.jobs.research.event_changed
        original_wait = condition.wait

        def observed_wait(seconds=None):
            entered.set()
            return original_wait(seconds)

        with mock.patch.object(condition, "wait", observed_wait), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.call, "GET", "/research-events?" + urlencode({"cursor": cursor, "wait_seconds": 5}),
                                     token=readonly_token(self.env.admin))
            self.assertTrue(entered.wait(3), "watch did not enter its bounded wait")
            committed = self.create("direction", "a", self.direction("A metadata change"))
            code, wake = future.result(timeout=3)
        self.assertEqual(code, 200, wake)
        self.assertEqual((wake["wait_result"], wake["stream_id"], wake["research_id"]), ("events", stream, "study"))
        self.assertEqual([item["operation_id"] for item in wake["items"]], ["create-a"])
        self.assertEqual(wake["next_after"], committed["event_seq"])
        retained = wake["next_cursor"]

        # A later event in another scope advances head, never this consumer's
        # position. Reopening the same store preserves both stream and anchor.
        self.create("research", "other", {"title": "Other study", "objective": "Separate event filter"})
        self.restart()
        code, resumed_empty = self.call("GET", "/research-events?" + urlencode({"cursor": retained}))
        self.assertEqual(code, 200, resumed_empty)
        self.assertEqual((resumed_empty["stream_id"], resumed_empty["research_id"], resumed_empty["items"]),
                         (stream, "study", []))
        self.assertEqual((resumed_empty["wait_result"], resumed_empty["next_cursor"]), ("immediate", retained))
        self.assertGreater(resumed_empty["head"], resumed_empty["next_after"])
        self.create("direction", "b", self.direction("After restart"))
        resumed = self.cli("events", "--cursor", retained, "--limit", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        page = json.loads(resumed.stdout)
        self.assertEqual([item["operation_id"] for item in page["items"]], ["create-b"])
        self.assertEqual(page["stream_id"], stream)

        anchor = decode_cursor(page["next_cursor"])
        mutations = [
            ({**anchor, "stream_id": "0" * 32 if stream != "0" * 32 else "1" * 32}, "research_cursor_stream_changed"),
            ({**anchor, "after": page["head"] + 1}, "research_cursor_ahead"),
            ({**anchor, "operation_id": "not-the-original-event"}, "research_cursor_history_changed"),
        ]
        for changed, expected in mutations:
            with self.subTest(rejection=expected):
                code, error = self.call("GET", "/research-events?" + urlencode({"cursor": encode_cursor(changed)}))
                self.assertEqual((code, error["code"]), (409, expected))
        code, scope_error = self.call("GET", "/research-events?" + urlencode({"cursor": retained, "research_id": "other"}))
        self.assertEqual((code, scope_error["code"]), (409, "research_cursor_scope_changed"))
        self.assertEqual(self.call("GET", "/research-events?" + urlencode({"cursor": retained, "after": 0}))[0], 400)
        code, original_again = self.call("GET", "/research-events?" + urlencode({"cursor": retained}))
        self.assertEqual(code, 200, original_again)
        self.assertEqual(original_again["items"], page["items"], "rejected positions must not advance the original consumer")
        self.assertEqual([item["operation_id"] for item in self.events()], ["create-study", "create-a", "create-b"])
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])
        self.assertIsNone(self.env.board.monitor._thread, "metadata watch must not activate device sampling")

    def test_watch_capacity_does_not_block_reads_or_the_commit_that_releases_waiters(self):
        self.create("research", "study", {"title": "Capacity fixture", "objective": "Only waiting requests consume slots"})
        code, initial = self.call("GET", "/research-events?research_id=study")
        self.assertEqual(code, 200, initial)
        cursor = initial["next_cursor"]
        # Only this loopback fixture needs enough listen backlog for 16 concurrent
        # requests. It does not change production admission or semaphore limits.
        self.env.server.socket.listen(32)
        condition = self.env.jobs.research.event_changed
        original_wait = condition.wait
        entered, guard, waiting = threading.Event(), threading.Lock(), set()

        def observed_wait(seconds=None):
            ident = threading.get_ident()
            with guard:
                waiting.add(ident)
                if len(waiting) == 16:
                    entered.set()
            try:
                return original_wait(seconds)
            finally:
                with guard:
                    waiting.remove(ident)

        path = "/research-events?" + urlencode({"cursor": cursor, "wait_seconds": 8})
        with mock.patch.object(condition, "wait", observed_wait), ThreadPoolExecutor(max_workers=16) as executor:
            pending = [executor.submit(self.call, "GET", path, token=readonly_token(self.env.admin)) for _ in range(16)]
            try:
                self.assertTrue(entered.wait(5), "all 16 accepted waiters must reach Condition.wait")
                code, full = self.call("GET", path, token=readonly_token(self.env.admin))
                self.assertEqual((code, full["code"], full["retry_after_seconds"]), (429, "research_watch_busy", 15))
                code, immediate = self.call("GET", "/research-events?" + urlencode({"cursor": cursor}))
                self.assertEqual((code, immediate["wait_result"], immediate["items"]), (200, "immediate", []))
                # An already-available page also bypasses the waiting capacity.
                code, existing = self.call("GET", "/research-events?research_id=study&after=0&wait_seconds=1")
                self.assertEqual((code, existing["wait_result"]), (200, "events"))
                self.assertEqual(self.read("research", "study")["revision"], 1)
            finally:
                # A real committed write releases all waiters even if an assertion
                # failed, so teardown need not leave up to 16 deadline sleepers.
                committed = self.create("direction", "capacity-release", self.direction("Wake admitted readers"))
            for future in pending:
                code, page = future.result(timeout=3)
                self.assertEqual((code, page["wait_result"]), (200, "events"))
                self.assertEqual([item["operation_id"] for item in page["items"]], ["create-capacity-release"])
                self.assertEqual(page["next_after"], committed["event_seq"])
        self.assertEqual(waiting, set())
        self.assertEqual([item["operation_id"] for item in self.events()], ["create-study", "create-capacity-release"])
        self.assertEqual(self.env.jobs.dispatch.queue.rows(), [])
        self.assertIsNone(self.env.board.monitor._thread)


if __name__ == "__main__":
    unittest.main()
