"""Old session metadata remains readable after retiring the lock domain ID."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from node_session import _source_binding, _recovery_spec, _check_attempt, SessionError
from node_deployments import NodeDeployments
from remote_owner import PFRemote
from job_dispatch import request_fingerprint
from node_games import snapshot_body
from node_service import Jobs
from job_queue import RequestConflict
from node_remote import Remote, MAX_MINUTES_DEFAULT


class BindingHistory(unittest.TestCase):
    def setUp(self):
        self.current = {"schema": "phonefarm/device-binding@1", "registry_revision": 1,
                        "device_id": "a", "serial": "usb-a"}
        self.old = {**self.current, "domain_id": "a" * 32}

    def test_gray_recovery_reads_old_binding_without_rewriting_original(self):
        source = {"spec": {"device_binding": self.old}}
        descriptor = {"device_binding": copy.deepcopy(self.old), "lease_scope": "device", "serial": "usb-a"}
        original = copy.deepcopy((source, descriptor))
        self.assertEqual(_source_binding(source, descriptor), self.current)
        self.assertEqual((source, descriptor), original)
        descriptor["device_binding"]["serial"] = "other-phone"
        with self.assertRaises(SessionError):
            _source_binding(source, descriptor)

    def test_game_and_remote_reports_match_old_saved_device_identity(self):
        request = {"schema": "phonefarm/game-deploy-request@1", "node_id": "node", "game_id": "game",
                   "version": "1", "serial": "usb-a", "mode": "install"}
        report = {**request, "schema": "phonefarm/game-deployment@1", "device_binding": self.current,
                  "lease_scope": "device", **{key: {} for key in
                      ("installed", "resources_deployed", "application_access", "login", "scene_ready", "recovery")}}
        gateway = object.__new__(NodeDeployments)
        gateway.jobs = Mock()
        gateway.jobs.center.remote.return_value = None
        gateway.jobs._read_root.read_json.return_value = report
        self.assertEqual(gateway._report("job", request, self.old), (report, None))
        owner = object.__new__(PFRemote)
        owner.processes = {}
        owner._path = Mock(return_value=Path("/fixture"))
        descriptor = {"schema": "phonefarm/remote-control@1", "serial": "usb-a", "phase": "closed",
                      "device_binding": self.current, "lease_scope": "device"}
        owner._call = Mock(return_value=descriptor)
        ref = {"id": "session", "serial": "usb-a", "device_binding": self.old}
        self.assertEqual(owner.status(ref), descriptor)
        self.assertEqual(ref["device_binding"], self.old)

    def test_old_recovery_attempt_keeps_its_original_spec(self):
        jobs = Mock()
        jobs.path.return_value = "/source/session"
        source = {"job_id": "source", "spec": {"device_binding": self.old},
                  "resources": ["device:" + "a" * 32 + ":a"]}
        jobs.dispatch.queue.get.return_value = source
        descriptor = {"device_binding": self.old, "lease_scope": "device",
                      "serial": "usb-a", "session_path": "session"}
        spec = {**_recovery_spec(jobs, "source", descriptor, "source"),
                "device_binding": self.old, "resources": source["resources"]}
        child = {"spec": spec, "resources": source["resources"]}
        original = copy.deepcopy(child)
        _check_attempt(jobs, source, descriptor, child)
        self.assertEqual(child, original)

    def test_old_deployment_fingerprint_uses_saved_scope_metadata(self):
        gateway = object.__new__(NodeDeployments)
        gateway.jobs = Mock()
        gateway.jobs.center.remote.return_value = None
        gateway.games = Mock()
        gateway.games._resource.return_value = "game:game:1"
        request = {"schema": "phonefarm/game-deploy-request@1", "node_id": "node", "game_id": "game",
                   "version": "1", "serial": "usb-a", "mode": "check"}
        spec = gateway._spec(request, device_binding=self.current)
        spec.update(device_binding=self.old, resources=["device:" + "a" * 32 + ":a"])
        row = {"job_id": "source", "spec": spec,
               "fingerprint": request_fingerprint(spec, snapshot_body(request))}
        gateway.jobs.dispatch.queue.get.return_value = row
        gateway.jobs._read_root.read_json.return_value = request
        gateway._request = Mock(return_value=(request, []))
        original = copy.deepcopy(row)
        self.assertEqual(gateway._bound("source"), (row, request))
        self.assertEqual(row, original)

    def test_recovery_admission_inherits_saved_binding_and_resources(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = Jobs(root, {"phonefarm": ("/usr/bin/true", root)})
            try:
                source = {"job_id": "0123456789ab", "spec": {"device_binding": self.old},
                          "resources": ["device:" + "a" * 32 + ":a"]}
                jobs.dispatch.queue.get = Mock(return_value=source)
                jobs.dispatch.submit = Mock(side_effect=lambda spec, *_args, **_kw: spec)
                jobs.validate_bound_recovery = Mock()
                spec = {"bin": "phonefarm", "argv": ["gray-layer", "session-recover", "--session", "/source", "--serial", "usb-a"],
                        "device_access": "recovery", "recovery_for": source["job_id"], "device_binding": self.current}
                saved = jobs.start(spec, b"", "old-recovery-id")
                self.assertEqual(saved["device_binding"], self.old)
                self.assertEqual(saved["resources"], ["device:a"])
                self.assertEqual(source["resources"], ["device:" + "a" * 32 + ":a"])
            finally:
                jobs._read_root.close()

    def test_replaying_old_request_uses_original_fingerprint_without_rewriting(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = Jobs(root, {"phonefarm": ("/usr/bin/true", root)})
            try:
                spec = {"bin": "phonefarm", "argv": ["gray-layer", "session", "--serial", "usb-a"],
                        "label": None, "device_access": "write", "device_binding": self.old,
                        "resources": ["device:" + "a" * 32 + ":a"]}
                row, _ = jobs.dispatch.queue.reserve("original", request_fingerprint(spec, b"body"), spec, spec["resources"])
                jobs.dispatch.start = Mock()
                jobs.status = Mock(return_value={"id": row["job_id"]})
                supplied = {k: v for k, v in spec.items() if k != "resources"}
                self.assertEqual(jobs.start(supplied, b"body", "original"), {"id": row["job_id"]})
                self.assertEqual(jobs.dispatch.queue.get(row["job_id"])["spec"], spec)
                with self.assertRaises(RequestConflict):
                    jobs.start({**supplied, "label": "different"}, b"body", "original")
                with self.assertRaises(RequestConflict):
                    jobs.start(supplied, b"changed body", "original")
            finally:
                jobs._read_root.close()

    def test_remote_acquisition_id_reuses_saved_binding(self):
        remote = object.__new__(Remote)
        remote._require_backend = Mock()
        remote._lock = threading.Lock()
        ref = {"acquisition_id": "original", "max_seconds": MAX_MINUTES_DEFAULT * 60,
               "device_binding": self.old}
        remote.sessions = {"session": SimpleNamespace(operator={"id": "operator"}, serial="usb-a", ref=ref)}
        remote._refresh = Mock(return_value={"session_id": "session"})
        self.assertEqual(remote.acquire({"id": "operator"}, "usb-a", "127.0.0.1",
                                       {"request_id": "original", "device_binding": self.old}),
                         {"reused": True, "session_id": "session"})
