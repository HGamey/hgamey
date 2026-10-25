"""Frozen remote details and explicit baselines; no original downloads or jobs."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
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
import test_node_result_search as federation_fixture


class ResultCrossCompareTests(unittest.TestCase):
    # Borrow fixture helpers without inheriting or rediscovering its old tests.
    setUp = federation_fixture.NodeResultSearchTests.setUp
    cleanup = federation_fixture.NodeResultSearchTests.cleanup

    def node(self, label):
        env = federation_fixture.NodeResultSearchTests.node(self, label)
        env.detail_fault, env.waiting_count = None, 0
        env.waiting_lock, env.all_entered = threading.Lock(), threading.Event()
        inherited = env.server.RequestHandlerClass
        product_get = inherited.__bases__[0].do_GET

        class DetailObserved(inherited):
            def do_GET(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith("/results/"):
                    return super().do_GET()
                env.calls.append({"method": "GET", "path": parsed.path,
                                  "query": parse_qs(parsed.query),
                                  "pin": self.headers.get("X-Expected-Node-ID")})
                if env.detail_fault == "plain404":
                    return self._send(404, {"error": "private fixture error must not be relayed"})
                if env.detail_fault == "redirect":
                    self.send_response(307)
                    self.send_header("Location", env.redirect_url)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if env.detail_fault in {"identity", "manifest_identity"}:
                    detail = env.jobs.results.get("same-result")
                    if env.detail_fault == "identity":
                        detail["node_id"] = "f" * 32
                    else:
                        detail["manifest"]["result_id"] = "different-result"
                    return self._send(200, detail)
                if env.detail_fault == "wait":
                    with env.waiting_lock:
                        env.waiting_count += 1
                        if env.waiting_count == 4:
                            env.all_entered.set()
                    env.entered.set()
                    if not env.release.wait(5):
                        return self._send(503, {"error": "fixture release was not signaled"})
                return product_get(self)

        env.server.RequestHandlerClass = DetailObserved
        return env

    def publish(self, env, rid):
        baseline, candidate = (10, 8) if env.label == "alpha" else (20, 15)
        def metric(value, condition):
            return {"name": "same-module-name", "value": value, "condition": condition,
                    "unit": "ms", "scope": "module", "method": "reported"}
        return env.jobs.results.register({
            "schema": "phonefarm/result@1", "result_id": rid, "title": env.label + " " + rid,
            "labels": {"game_id": "fixture-game", "device": env.label}, "outcome": "unknown",
            "origin": {"evidence_origin": "synthetic"},
            "metrics": [metric(baseline, "baseline"), metric(candidate, "candidate"),
                        {"name": "power", "value": None, "unit": "W", "scope": "power",
                         "unavailable_reason": "fixture has no power measurement"}],
            "limitations": ["registered values only; no simultaneous measurement"]})

    def spec(self):
        columns = []
        for target in self.targets:
            prefix = target["name"]
            for condition in ("baseline", "candidate"):
                columns.append({"id": prefix + "-" + condition, "label": prefix + " " + condition,
                                "node_id": target["node_id"], "result_id": "same-result",
                                "baseline_column_id": prefix + "-baseline"})
        return {"schema": "phonefarm/result-cross-compare@1", "node_id": self.hub.jobs.games.node_id,
                "targets": copy.deepcopy(self.targets), "columns": columns,
                "rows": [{"id": "module", "label": "explicit module indexes", "metric_indexes": [0, 1, 0, 1]},
                         {"id": "missing", "label": "not selected / null / absent index", "metric_indexes": [None, 2, None, 99]}]}

    def request(self, endpoint, values, *, dash=False, authenticated=True):
        headers = {"X-Expected-Node-ID": self.hub.jobs.games.node_id}
        if authenticated:
            headers["Authorization"] = ("Basic " + base64.b64encode(b"viewer:test-view").decode()
                                        if dash else "Bearer " + readonly_token(self.hub.admin))
        connection = http.client.HTTPConnection(*self.hub.server.server_address, timeout=12)
        try:
            connection.request("GET", ("/dash/api" if dash else "") + endpoint + "?" + urlencode(values), headers=headers)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            connection.close()

    def detail(self, target, rid="same-result", **kwargs):
        return self.request("/remote-result", {"target": json.dumps(target), "result_id": rid}, **kwargs)

    def compare(self, spec):
        return self.request("/result-cross-compare", {"spec": json.dumps(spec)})

    def cli(self, *args):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(self.hub.root),
               "PF_NODE_URL": self.hub.url, "PF_NODE_ID": self.hub.jobs.games.node_id,
               "PF_NODE_TOKEN": readonly_token(self.hub.admin), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "results", *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def assert_sources(self, *, allow_index=False):
        allowed = {"/node", "/results/same-result", "/results/missing"}
        if allow_index:
            allowed.add("/results")
        for env in (self.alpha, self.beta):
            self.assertTrue(all(call["method"] == "GET" and call["path"] in allowed for call in env.calls), env.calls)
            self.assertTrue(all(call["pin"] == env.jobs.games.node_id for call in env.calls), env.calls)
        for env in self.environments:
            self.assertEqual(env.jobs.dispatch.queue.rows(), [])

    def test_cli_details_four_explicit_baselines_and_partial_source_failure(self):
        originals = {env.label: env.jobs.results.get("same-result") for env in (self.alpha, self.beta)}
        target = self.targets[0]
        shown = self.cli("show", "same-result", "--node", target["name"],
                         "--source-node-id", target["node_id"], "--revision", str(target["revision"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        detail = json.loads(shown.stdout)
        self.assertEqual((detail["node_id"], detail["result_id"], detail["status"]),
                         (self.hub.jobs.games.node_id, "same-result", "ok"))
        self.assertEqual(detail["source"], {**target, "url": self.alpha.url})
        for key in ("manifest", "sequence", "registered_at", "provenance", "metric_evidence", "source_observations"):
            self.assertEqual(detail["result"][key], originals["alpha"][key])
        self.assertEqual([call["path"] for call in self.hub.calls], ["/node", "/remote-result"])
        self.assertEqual([call["path"] for call in self.alpha.calls], ["/node", "/results/same-result"])
        self.assertEqual(self.beta.calls, [])

        spec = self.spec()
        path = self.hub.root / "cross.json"
        path.write_text(json.dumps(spec))
        for env in self.environments:
            env.calls.clear()
        compared = self.cli("compare", str(path))
        self.assertEqual(compared.returncode, 0, compared.stderr)
        value = json.loads(compared.stdout)
        self.assertEqual(value["spec"], spec)
        self.assertEqual((value["basis"], value["comparability"], value["partial"]),
                         ("registered_values", "not_established", False))
        self.assertNotIn("baseline_column_id", value)
        self.assertEqual([call["path"] for call in self.hub.calls], ["/node", "/result-cross-compare"])
        for env in (self.alpha, self.beta):
            self.assertEqual([call["path"] for call in env.calls], ["/node", "/results/same-result"])
        cells = value["rows"][0]["cells"]
        self.assertEqual([cell["metric_index"] for cell in cells], [0, 1, 0, 1])
        self.assertEqual([cell["metric"]["condition"] for cell in cells], ["baseline", "candidate"] * 2)
        self.assertEqual([cell["delta"]["value"] for cell in cells], [0, -2, 0, -5])
        self.assertEqual([cell["delta"]["percent"] for cell in cells], [0, -20, 0, -25])
        self.assertEqual([cell["baseline_column_id"] for cell in cells],
                         ["alpha-baseline", "alpha-baseline", "beta-baseline", "beta-baseline"])
        self.assertEqual([cell["cross_node"] for cell in cells], [False] * 4)
        self.assertEqual([cell["status"] for cell in value["rows"][1]["cells"]],
                         ["not_selected", "available", "not_selected", "invalid_index"])
        self.assertIsNone(value["rows"][1]["cells"][1]["metric"]["value"])
        for index, column in enumerate(value["columns"]):
            env = self.alpha if index < 2 else self.beta
            self.assertEqual((column["node_id"], column["source_status"], column["status"]),
                             (env.jobs.games.node_id, "ok", "available"))
            self.assertEqual(column["result"]["manifest"], originals[env.label]["manifest"])
            self.assertIsInstance(column["read_at"], (int, float))
            link = urlparse(column["links"]["dashboard"])
            self.assertEqual((link.netloc, link.path, link.fragment),
                             (urlparse(env.url).netloc, "/dash/results", "result=same-result"))
            self.assertEqual(parse_qs(link.query), {"expected_node_id": [env.jobs.games.node_id]})

        explicit = copy.deepcopy(spec)
        explicit["columns"][1]["baseline_column_id"] = None
        explicit["columns"][3]["baseline_column_id"] = "alpha-baseline"
        code, changed = self.compare(explicit)
        self.assertEqual(code, 200)
        self.assertEqual(changed["spec"], explicit)
        self.assertFalse(changed["partial"])
        unpaired, cross_node = changed["rows"][0]["cells"][1], changed["rows"][0]["cells"][3]
        self.assertIsNone(unpaired["baseline_column_id"])
        self.assertIsNone(unpaired["cross_node"])
        self.assertIsNone(unpaired["delta"]["value"])
        self.assertEqual((unpaired["delta"]["reason"], unpaired["delta"]["percent_reason"]),
                         ("baseline_not_selected", "baseline_not_selected"))
        self.assertEqual(cross_node["baseline_column_id"], "alpha-baseline")
        self.assertTrue(cross_node["cross_node"])
        self.assertEqual((cross_node["delta"]["value"], cross_node["delta"]["percent"]), (5, 50))

        self.beta.server.shutdown()
        self.beta.server.server_close()
        self.beta.thread.join(timeout=2)
        self.assertFalse(self.beta.thread.is_alive())
        self.hub.calls.clear()
        failed = self.cli("compare", str(path))
        self.assertEqual(failed.returncode, 2, failed.stderr)
        partial = json.loads(failed.stdout)
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["spec"], spec)
        self.assertEqual([column["status"] for column in partial["columns"]], ["available"] * 2 + ["unavailable"] * 2)
        self.assertEqual([column["source_status"] for column in partial["columns"]][2:], ["unavailable"] * 2)
        self.assertEqual(partial["rows"][0]["cells"][1]["delta"]["value"], -2)
        self.assertIsNone(partial["rows"][0]["cells"][3]["delta"]["value"])
        self.assertEqual([call["path"] for call in self.hub.calls], ["/node", "/result-cross-compare"])
        self.assertNotIn(readonly_token(self.alpha.admin), failed.stdout)
        self.assert_sources()

    def test_detail_failures_inflight_revision_and_shared_reader_slots(self):
        alpha, beta = self.targets
        self.assertEqual(self.detail(alpha, authenticated=False)[0], 401)
        self.assertEqual(self.alpha.calls, [])
        code, result = self.detail(beta, "missing")
        self.assertEqual((code, result["status"], result["result"]), (200, "not_found", None))
        self.beta.detail_fault = "plain404"
        code, result = self.detail(beta)
        self.assertEqual((code, result["status"], result["result"]), (200, "unsupported", None))
        self.assertNotIn("private fixture", json.dumps(result))
        self.beta.detail_fault = None
        self.beta.fault = "identity"
        before = len(self.beta.calls)
        self.assertEqual(self.detail(beta)[1]["status"], "identity_mismatch")
        self.assertEqual([call["path"] for call in self.beta.calls[before:]], ["/node"])
        self.beta.fault = None
        for fault, expected in (("identity", "identity_mismatch"), ("manifest_identity", "unsupported")):
            self.beta.detail_fault = fault
            code, result = self.detail(beta)
            self.assertEqual((code, result["status"], result["result"]), (200, expected, None))
        self.beta.detail_fault, self.beta.redirect_url = "redirect", self.alpha.url + "/must-not-follow"
        before = len(self.alpha.calls)
        self.assertEqual(self.detail(beta)[1]["status"], "unsupported")
        self.assertEqual(len(self.alpha.calls), before)
        self.beta.detail_fault = None

        self.alpha.detail_fault = "wait"
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.detail, alpha, dash=True)
            try:
                self.assertTrue(self.alpha.entered.wait(3))
                changed = self.hub.jobs.directory.update("alpha", {"revision": 1, "note": "changed during detail IO"})
                self.assertEqual(changed["revision"], 2)
            finally:
                self.alpha.release.set()
            code, result = future.result(timeout=10)
        self.assertEqual((code, result["status"], result["result"]), (200, "configuration_changed", None))
        before = len(self.alpha.calls)
        self.assertEqual(self.detail(alpha)[1]["status"], "configuration_changed")
        self.assertEqual(len(self.alpha.calls), before)

        current = {**alpha, "revision": 2}
        self.alpha.waiting_count = 0
        self.alpha.entered.clear()
        self.alpha.all_entered.clear()
        self.alpha.release.clear()
        # Earlier HTTP replies can precede their reader's finally by one tick.
        # Observe the actual pool becoming idle before filling its four slots.
        acquired = 0
        try:
            for _ in range(4):
                self.assertTrue(self.hub.jobs.result_search._slots.acquire(timeout=2))
                acquired += 1
        finally:
            for _ in range(acquired):
                self.hub.jobs.result_search._slots.release()
        single = self.spec()
        single["targets"], single["columns"] = [current], single["columns"][:2]
        for row in single["rows"]:
            row["metric_indexes"] = row["metric_indexes"][:2]
        with ThreadPoolExecutor(max_workers=4) as executor:
            pending = [executor.submit(self.detail, current) for _ in range(4)]
            try:
                self.assertTrue(self.alpha.all_entered.wait(3), "four real detail readers did not occupy the shared slots")
                calls = [len(env.calls) for env in (self.alpha, self.beta)]
                search_code, search = federation_fixture.NodeResultSearchTests.request(self, [current])
                self.assertEqual((search_code, search["nodes"][0]["status"]), (200, "busy"))
                self.assertEqual(self.detail(beta)[1]["status"], "busy")
                code, compared = self.compare(single)
                self.assertEqual(code, 200)
                self.assertTrue(compared["partial"])
                self.assertEqual([column["source_status"] for column in compared["columns"]], ["busy", "busy"])
                self.assertEqual([len(env.calls) for env in (self.alpha, self.beta)], calls)
                returned = json.dumps(compared, sort_keys=True)
            finally:
                self.alpha.release.set()
            for future in pending:
                self.assertEqual(future.result(timeout=10)[1]["status"], "ok")
        self.assertEqual(json.dumps(compared, sort_keys=True), returned)
        self.assert_sources(allow_index=True)


if __name__ == "__main__":
    unittest.main()
