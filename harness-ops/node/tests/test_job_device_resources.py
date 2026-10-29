"""Production HDC defaults: one reservation per registered device identity."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import test_node_games as fixture
from test_device_scope import PROGRAM, bound
from job_dispatch import request_fingerprint
from node_service import Jobs


class JobDeviceResources(unittest.TestCase):
    def test_research_and_explicit_aliases_have_one_key_per_device(self):
        env = fixture.GameFlowTests()
        env.setUp()
        self.addCleanup(env.tearDown)
        program = env.root / "scope-pf"
        program.write_text("#!" + sys.executable + "\n" + PROGRAM)
        program.chmod(0o755)
        env.jobs.bins["phonefarm"] = (str(program), str(env.root))
        env.jobs.programs.refresh()
        serial, alias = "hdc:88X9K26901026869", "hdc:network-huawei"
        (env.root / "registry.json").write_text(json.dumps(
            {"revision": 1, "aliases": {serial: "HUAWEI", alias: "HUAWEI", "usb-a": "A"}}))
        env.jobs.research.write("research", "huawei", {
            "schema": "phonefarm/research-write@1", "operation_id": "create-huawei",
            "base_revision": None, "actor_label": None, "reason": None,
            "data": {"title": "huawei", "objective": "one device", "device_requirements": [serial]}}, "fixture")
        base = {"bin": "bundle_prepare", "argv": ["measure_default.sh", serial],
                "device_access": "write", "research_id": "huawei"}
        cases = [
            ({}, ["device:HUAWEI"]),
            ({"resources": ["device:" + serial]}, ["device:HUAWEI"]),
            ({"resources": ["device:" + alias]}, ["device:HUAWEI"]),
            ({"resources": ["device:" + serial, "device:" + alias, "capture:power"]},
             ["capture:power", "device:HUAWEI"]),
            ({"resources": ["device:HUAWEI"]}, ["device:HUAWEI"]),
            ({"device_access": "none", "resources": ["device:" + serial, "device:" + alias]},
             ["device:HUAWEI"]),
            ({"resources": ["device:" + serial, "device:" + alias, "device:usb-a"]},
             ["device:A", "device:HUAWEI"]),
        ]
        for number, (extra, expected) in enumerate(cases):
            with self.subTest(extra=extra):
                spec = {**base, **extra}
                code, result = env.call("POST", "/jobs", headers={
                    "X-Expected-Node-ID": env.jobs.node_id, "X-Request-ID": "resources-" + str(number),
                    "X-Job": json.dumps(spec)})
                self.assertEqual(code, 200, result)
                self.assertEqual(result["resources"], expected)
                self.assertEqual(result["queue"]["resources"], expected)
                row = env.jobs.dispatch.queue.get(result["id"])
                self.assertEqual(row["resources"], expected)
                self.assertEqual(row["spec"]["resources"], expected)
                if spec["device_access"] == "write" and len([r for r in expected if r.startswith("device:")]) == 1:
                    self.assertEqual(result["device_binding"]["device_id"], "HUAWEI")
                    self.assertIn(result["device_binding"]["serial"], (serial, alias))

    def test_old_hdc_resources_project_once_without_rewriting_or_breaking_replay(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = Jobs(root, {"phonefarm": ("/usr/bin/true", root)})
            self.addCleanup(jobs._read_root.close)
            jobs.dispatch.start = Mock()
            serial, domain = "hdc:88X9K26901026869", "fcfddb8ace1982a051086da58251fb2f"
            binding = {**bound("HUAWEI", serial), "domain_id": domain}
            spec = {"bin": "phonefarm", "argv": ["gray-layer", "session", "--serial", serial],
                    "label": None, "device_access": "write", "device_binding": binding,
                    "resources": ["device:" + domain + ":HUAWEI", "device:" + serial]}
            original = copy.deepcopy(spec)
            row, _ = jobs.dispatch.queue.reserve("old-hdc", request_fingerprint(spec, b"original"), spec, spec["resources"])
            jid = row["job_id"]
            self.assertEqual(row["resources"], ["device:HUAWEI"])
            jobs._read_root.mkdir(jid)
            meta = {**jobs.dispatch.base_meta(row), "resources": original["resources"],
                    "state": "finished", "phase": "finished", "outcome": "succeeded", "exit": 0}
            jobs._write_meta(jid, meta)
            jobs.dispatch.queue.set_state(jid, "done")
            meta_path = Path(jobs.path(jid, "meta.json"))
            original_meta = meta_path.read_bytes()
            view = jobs.status(jid)
            self.assertEqual(view["resources"], ["device:HUAWEI"])
            self.assertEqual(view["queue"]["resources"], ["device:HUAWEI"])
            supplied = {**spec, "device_binding": bound("HUAWEI", serial)}
            with unittest.mock.patch.object(jobs, "status", return_value={"id": jid}):
                self.assertEqual(jobs.start(supplied, b"original", "old-hdc"), {"id": jid})
            with jobs.dispatch.queue._transaction() as db:
                saved = db.execute("SELECT spec,resources,fingerprint FROM jobs WHERE job_id=?", (jid,)).fetchone()
            self.assertEqual(json.loads(saved["spec"]), original)
            self.assertEqual(json.loads(saved["resources"]), original["resources"])
            self.assertEqual(saved["fingerprint"], request_fingerprint(original, b"original"))
            self.assertEqual(meta_path.read_bytes(), original_meta)
