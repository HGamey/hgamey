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

    node = federation_fixture.NodeResultSearchTests.node

    def publish(self, env, rid):
        baseline, candidate = (10, 8) if env.label == "alpha" else (20, 15)
        def metric(value, condition):
            return {"name": "same-module-name", "value": value, "condition": condition,
                    "unit": "ms", "scope": "module", "method": "reported"}
        return self.hub.jobs.results.register({
            "schema": "phonefarm/result@1", "result_id": rid, "title": env.label + " " + rid, "job_id": env.source_job,
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
                                "result_id": "result-" + prefix,
                                "baseline_column_id": prefix + "-baseline"})
        return {"schema": "phonefarm/result-cross-compare@1", "node_id": self.hub.jobs.games.node_id,
                "columns": columns,
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

    def detail(self, target, rid=None, **kwargs):
        return self.request('/results/' + (rid or 'result-' + target['name']), {}, **kwargs)

    def compare(self, spec):
        return self.request("/result-cross-compare", {"spec": json.dumps(spec)})

    def cli(self, *args):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(self.hub.root),
               "PF_NODE_URL": self.hub.url, "PF_NODE_ID": self.hub.jobs.games.node_id,
               "PF_NODE_TOKEN": readonly_token(self.hub.admin), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "results", *args],
                              env=env, capture_output=True, text=True, timeout=15)

    def assert_sources(self):
        federation_fixture.NodeResultSearchTests.assert_read_boundaries(self)

    def test_cli_details_four_explicit_baselines_and_offline_sources(self):
        originals = {env.label: self.hub.jobs.results.get('result-' + env.label) for env in (self.alpha, self.beta)}
        shown = self.cli('show', 'result-alpha')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        detail = json.loads(shown.stdout)
        self.assertEqual(detail, originals['alpha'])
        self.assertEqual(detail['provenance']['execution_node_id'], self.alpha.jobs.node_id)
        self.assertEqual([call['path'] for call in self.hub.calls], ['/node', '/results/result-alpha'])
        self.assert_sources()

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
            self.assertFalse(any(call["path"].startswith("/results") for call in env.calls))
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
            self.assertTrue(column["links"]["dashboard"].startswith("/dash/results?"))
            self.assertEqual(urlparse(column["links"]["api"]).path, "/results/result-" + env.label)

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

        self.beta.connector.close()
        self.hub.calls.clear()
        offline = self.cli('compare', str(path))
        self.assertEqual(offline.returncode, 0, offline.stderr)
        archived = json.loads(offline.stdout)
        self.assertFalse(archived['partial'])
        self.assertEqual(archived['spec'], spec)
        self.assertEqual([column['status'] for column in archived['columns']], ['available'] * 4)
        self.assertEqual([cell['delta']['value'] for cell in archived['rows'][0]['cells']], [0, -2, 0, -5])
        self.assertNotIn(readonly_token(self.beta.admin), offline.stdout)
        self.assert_sources()

    def test_missing_center_result_permissions_and_unavailable_evidence_remain_explicit(self):
        self.assertEqual(self.detail(self.targets[0], authenticated=False)[0], 401)
        self.assertEqual(self.detail(self.targets[1], 'missing')[0], 404)
        spec = self.spec()
        spec['columns'][2]['result_id'] = 'missing'
        code, value = self.compare(spec)
        self.assertEqual(code, 200)
        self.assertTrue(value['partial'])
        self.assertEqual([column['status'] for column in value['columns']],
                         ['available', 'available', 'unavailable', 'available'])
        self.assertEqual(value['rows'][0]['cells'][1]['delta']['value'], -2)
        self.assertIsNone(value['rows'][0]['cells'][3]['delta']['value'])
        self.assertEqual(value['rows'][0]['cells'][3]['delta']['reason'], 'result_unavailable')
        self.assert_sources()


if __name__ == '__main__':
    unittest.main()
