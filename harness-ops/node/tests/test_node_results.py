"""Result registration is metadata, not permission to claim successful measurement."""
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
from unittest.mock import patch
from urllib.parse import urlencode

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import dash
import job_runtime
from node_service import readonly_token
import test_node_games as game_fixture


def metric(value=2.413, pointer="/timing/gpu_ms"):
    return {"name": "gpu_render", "condition": "candidate", "value": value, "unit": "ms",
            "scope": "module", "method": "reported", "window": None, "denominator": None,
            "power_rail": None, "image_domain": None, "region": None,
            "source": {"name": "report", "pointer": pointer}, "unavailable_reason": None}


def registration(result_id, jid):
    return {"schema": "phonefarm/result@1", "result_id": result_id, "title": "Module timing fixture",
            "job_id": jid, "labels": {"game_id": "fixture-game", "game_version": None, "device": None,
                                       "research_id": "research-fixture", "direction_id": "low-render"},
            "origin": {"producer": "fixture", "producer_version": "1", "evidence_origin": "synthetic",
                       "observed_at": None}, "outcome": "unknown", "source_outcome": None,
            "sources": [{"name": "report", "kind": "job_output", "job_id": jid, "path": "report.json"}],
            "metrics": [metric()], "claims": [], "limitations": ["Synthetic host evidence only"], "supersedes": None}


def doom_report():
    return {"schema": "hgamey/lr4-026-original-native-owner-result@1", "at_utc": "2026-10-20T00:00:00Z",
            "status": "FIXED_SCENE_TWO_INDEPENDENT_PAIRS_MODULE_MS_GAIN_OBSERVED_OWNED016_RESTORED",
            "scope": "Fixed scene module timing; not display FPS.", "primary_statistics_job": "111111111111",
            "primary_statistics": {"path": "/old-host/analysis.json", "bytes": 12, "sha256": "a" * 64},
            "primary_display_summary_derived_from_equal_trial_means": [
                {"module": name, "original016_equal_trial_mean_ms": old, "trained026_equal_trial_mean_ms": new,
                 "change_percent": percent, "all_two_independent_pairs_line_lower": True}
                for name, old, new, percent in [("cpu_backend_excluding_swap_ns", 4.773, 3.273, -31.4),
                                               ("cpu_main_loop_wall_ns", 5.577, 3.607, -35.3),
                                               ("gpu_render_ns", 3.660, 2.413, -34.1)]],
            "image_summary": {"screenshot_domain": "Android compositor RGB8",
                              "trained_model_full_PSNR_dB": 38.539637, "trained_model_fixed_ROI_PSNR_dB": 37.953273,
                              "zero_mechanism_full_PSNR_dB": 38.527126, "zero_mechanism_fixed_ROI_PSNR_dB": 37.945651,
                              "scope": "Only this fixed state."},
            "limits": ["FreeDVFS six-second fixed-scene windows; two independent startups.",
                       "GPU elapsed includes submission gaps; do not add to CPU."],
            "glass_FPS_gain": None, "power_W_gain": None, "energy_J_per_software_present_gain": None,
            "CI": None, "adopt": False}


def gol_report():
    return {"schema": "game_opt_loop/optimize_report@1", "tool": "game_opt_loop", "tool_version": "fixture",
            "generated_at": "2026-10-20T00:00:00Z", "game": "Fixture game", "package": "com.example.fixture",
            "serial": "synthetic-device", "device_model": None, "plan_only": False, "plan": {},
            "layers": [{"layer": "shader", "used": True, "reason": "fixture", "eval_errors": ["compiler unavailable"]}],
            "archive": [
                {"id": name, "status": status, "kept": kept, "measurement_valid": valid, "layer": "gray",
                 "generation": 1, "why": "fixture", "change": {}, "metrics": {"power_watt": None, "psnr_db": None},
                 "verdict": {"reason": status, "per_metric": {"frame_p95_ms": {"a_mean": 8.0, "b_mean": 7.0}}},
                 "qualification_state": "unknown", "measurement_evidence": {}, "conditions_observed": None,
                 "power_source": None, "quality_reference": None}
                for name, status, kept, valid in [("candidate-a", "PASS", True, True),
                                                  ("candidate-b", "ABORT", False, False)]],
            "kept": ["candidate-a"], "headline": "Fixture mixed outcomes", "disclaimer": "Producer assertion only"}


class NodeResultTests(unittest.TestCase):
    def setUp(self):
        self.env = game_fixture.GameFlowTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.jobs, self.root = self.env.jobs, self.env.root
        self.jobs.bins["fixture"] = (sys.executable, str(self.root))
        self.workers = []
        launch = job_runtime.launch

        def tracked(*args, **kwargs):
            worker = launch(*args, **kwargs)
            self.workers.append(worker)
            return worker

        def cleanup_workers():
            for worker in self.workers:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.terminate()
                    worker.wait(timeout=5)

        self.addCleanup(cleanup_workers)
        tracker = patch.object(job_runtime, "launch", side_effect=tracked)
        tracker.start()
        self.addCleanup(tracker.stop)
        self.pin = self.jobs.games.node_id
        self.ro = readonly_token(self.env.admin)

    def request(self, method, path, value=None, *, token=None, headers=None):
        conn = http.client.HTTPConnection(*self.env.server.server_address, timeout=5)
        try:
            conn.request(method, path, body=None if value is None else json.dumps(value), headers={
                "Authorization": "Bearer " + (self.env.admin if token is None else token),
                "X-Expected-Node-ID": self.pin, "Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def api(self, method, path, value=None, **kwargs):
        status, _, raw = self.request(method, path, value, **kwargs)
        return status, json.loads(raw)

    def cli(self, *args):
        env = {**os.environ, "PF_NODE_URL": self.env.url, "PF_NODE_ID": self.pin,
               "PF_NODE_TOKEN": self.env.admin, "HOME": str(self.root)}
        env.pop("PF_NODE_TOKEN_FILE", None)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), "results", *args],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_job_registration_pagination_and_bound_source_download(self):
        raw = b'{ "timing": {"gpu_ms": 2.413}, "power_w": null }\n'
        code = "from pathlib import Path; import os; Path(os.environ['PF_JOB_OUT'],'report.json').write_bytes(" + repr(raw) + ")"
        _, job = self.env.call("POST", "/jobs", headers={"X-Request-ID": "results-source",
            "X-Job": json.dumps({"bin": "fixture", "argv": ["-c", code], "device_access": "none", "resources": []})})
        self.assertEqual(self.env.finish(job["id"])["exit"], 0)
        source = Path(self.jobs.path(job["id"], "out", "report.json"))
        manifest = registration("module-result", job["id"])
        file = self.root / "registration.json"
        file.write_text(json.dumps(manifest))
        submitted = self.cli("register", str(file))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        initial = json.loads(submitted.stdout)
        self.assertEqual(initial["metric_evidence"][0]["status"], "matched_source")
        self.assertEqual(initial["source_observations"][0]["status"], "available")
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(self.api("POST", "/results", manifest)[1]["sequence"], initial["sequence"])
        self.assertEqual(self.api("POST", "/results", {**manifest, "title": "changed"})[0], 409)
        wrong = copy.deepcopy(manifest)
        wrong["result_id"], wrong["metrics"][0]["value"] = "wrong-pointer-value", 99
        code, mismatch = self.api("POST", "/results", wrong)
        self.assertEqual((code, mismatch["code"]), (409, "result_value_mismatch"))
        self.assertEqual(self.api("GET", "/results/wrong-pointer-value")[0], 404)
        self.assertEqual(self.api("POST", "/results", manifest, token=self.ro)[0], 401)
        view = {"Authorization": "Basic " + base64.b64encode(b"viewer:test-view").decode(), "X-HG": "1"}
        self.assertEqual(self.api("POST", "/dash/api/results", manifest, headers=view)[0], 403)
        code, headers, downloaded = self.request("GET", "/results/module-result/sources/report", token=self.ro,
                                                  headers={"Range": "bytes=0-9"})
        self.assertEqual((code, downloaded), (206, raw[:10]))
        self.assertIn("Content-Range", headers)

        # A genuine legacy-shaped failed job is registrable without modern worker proof.
        legacy = "eeeeeeeeeeee"
        Path(self.jobs.path(legacy, "out")).mkdir(parents=True)
        self.jobs._write_meta(legacy, {"id": legacy, "bin": "custom", "argv": ["legacy-analysis"],
                                      "started": 1, "exit": 7, "lost": True})
        failed = registration("legacy-failed", legacy)
        failed.update(outcome="failed", source_outcome="unreadable old evidence")
        failed["sources"][0]["path"] = "missing.json"
        code, registered = self.api("POST", "/results", failed)
        self.assertEqual(code, 201, registered)
        self.assertEqual(registered["metric_evidence"][0]["status"], "unverified")
        self.assertEqual(registered["current"]["job"]["record"]["exit"], 7)
        first = self.cli("list", "--game-id", "fixture-game", "--limit", "1")
        self.assertEqual(first.returncode, 0, first.stderr)
        page = json.loads(first.stdout)
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["next_cursor"])
        # Concurrent different contents for one new ID produce exactly one immutable winner.
        barrier = threading.Barrier(2)
        def race(title):
            barrier.wait(timeout=2)
            return self.api("POST", "/results", {**manifest, "result_id": "later-result", "title": title})
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(race, ["first contender", "second contender"]))
        self.assertEqual(sorted(status for status, _ in replies), [201, 409])
        next_page = self.api("GET", "/results?" + urlencode({"game_id": "fixture-game", "limit": 1,
                                                           "cursor": page["next_cursor"]}))[1]
        self.assertEqual({page["items"][0]["result_id"], next_page["items"][0]["result_id"]},
                         {"module-result", "legacy-failed"})
        self.assertIsNone(next_page["next_cursor"])
        only_failed = self.api("GET", "/results?outcome=failed")[1]
        self.assertEqual([item["result_id"] for item in only_failed["items"]], ["legacy-failed"])
        source.write_bytes(raw.replace(b"2.413", b"9.999"))
        status, _, body = self.request("GET", "/results/module-result/sources/report", token=self.ro)
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["code"], "result_source_changed")
        self.assertNotIn(b"9.999", body)
        details = self.api("GET", "/dash/api/results/module-result", headers=view)[1]
        self.assertEqual(details["manifest"]["metrics"][0]["value"], 2.413)
        self.assertEqual(details["metric_evidence"][0]["status"], "matched_source")
        self.assertEqual(details["current"]["sources"][0]["status"], "changed")
        self.assertEqual(len(self.workers), 1, "metadata operations must not create execution jobs")
        self.assertEqual(len(self.jobs.dispatch.queue.rows()), 1)

    def test_small_imports_preserve_original_text_all_candidates_and_unknown_metrics(self):
        original = json.dumps(doom_report(), ensure_ascii=False, indent=3) + "\n\n"
        source = self.root / "doom-result.json"
        source.write_text(original)
        imported = self.cli("import", str(source), "--adapter", "doom-lr4-026-v1", "--import-id", "doom-fixture")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        receipt = json.loads(imported.stdout)
        self.assertEqual(receipt["result_ids"], ["doom-fixture"])
        again = self.cli("import", str(source), "--adapter", "doom-lr4-026-v1", "--import-id", "doom-fixture")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads(again.stdout), receipt)
        self.assertEqual(self.request("GET", "/result-imports/doom-fixture/source", token=self.ro)[2], original.encode())
        self.assertEqual(source.read_text(), original)
        result = self.api("GET", "/results/doom-fixture")[1]
        self.assertIsNone(result["manifest"]["job_id"], "historical source job ID must not bind the current node")
        metrics = result["manifest"]["metrics"]
        by_pointer = {item["source"]["pointer"]: item for item in metrics if item.get("source")}
        prefix = "/primary_display_summary_derived_from_equal_trial_means/"
        for index, value in enumerate((3.273, 3.607, 2.413)):
            self.assertEqual(by_pointer[prefix + str(index) + "/trained026_equal_trial_mean_ms"]["value"], value)
        self.assertEqual(by_pointer["/image_summary/trained_model_full_PSNR_dB"]["region"], "full_frame")
        self.assertEqual(by_pointer["/image_summary/trained_model_fixed_ROI_PSNR_dB"]["region"], "roi")
        for pointer in ("/glass_FPS_gain", "/power_W_gain", "/energy_J_per_software_present_gain"):
            self.assertIsNone(by_pointer[pointer]["value"])
        gol = json.dumps(gol_report(), separators=(",", ":")) + "\n"
        payload = {"schema": "phonefarm/result-import@1", "import_id": "gol-fixture", "adapter": "gol-optimize-v1",
                   "filename": "old-report.json", "content": gol, "labels": {"research_id": "legacy-study"}}
        code, receipt = self.api("POST", "/result-imports", payload)
        self.assertEqual(code, 201, receipt)
        self.assertEqual(len(receipt["result_ids"]), 3)
        parent = self.api("GET", "/results/gol-fixture")[1]
        self.assertIn("compiler unavailable", json.dumps(parent["manifest"]))
        children = [self.api("GET", "/results/" + rid)[1]["manifest"]
                    for rid in receipt["result_ids"] if rid != "gol-fixture"]
        self.assertEqual({child["source_outcome"] for child in children}, {"PASS", "ABORT"})
        self.assertNotEqual(next(child for child in children if child["source_outcome"] == "ABORT")["outcome"], "no_gain")
        self.assertTrue(all(any(item["value"] is None for item in child["metrics"]) for child in children))
        self.assertEqual(self.request("GET", "/result-imports/gol-fixture/source", token=self.ro)[2], gol.encode())
        self.assertEqual(self.api("POST", "/result-imports", {**payload, "content": gol + " "})[0], 409)
        self.assertEqual(self.jobs.dispatch.queue.rows(), [])
        self.assertEqual(self.workers, [])


if __name__ == "__main__":
    unittest.main()
