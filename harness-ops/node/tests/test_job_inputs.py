"""Small same-node input references through real HTTP, CLI and harmless workers."""
import copy
import errno
import gzip
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import job_runtime
import job_input_archive
import job_preparation
from node_fleet import FleetState
from node_service import Jobs, make_handler, readonly_token


def tar_bytes(members, *, raw=False):
    """Tiny synthetic inputs; callers may supply a TarInfo for malformed kinds."""
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in members:
            member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            if member.isreg():
                member.size = len(data)
            archive.addfile(member, io.BytesIO(data) if member.isreg() else None)
    value = payload.getvalue()
    return value if raw else gzip.compress(value, mtime=0)


class JobInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(str(self.root), {"fixture": (sys.executable, str(self.root))})
        self.admin = "inputs-fixture-not-production"
        self.workers = []
        self.case_ok, self.checks = False, {}
        launch = job_runtime.launch

        def tracked_launch(*args, **kwargs):
            worker = launch(*args, **kwargs)
            self.workers.append(worker)
            return worker

        self.launch_patch = patch.object(job_runtime, "launch", side_effect=tracked_launch)
        self.launch_patch.start()
        self.lease_patch = patch.object(self.jobs.leases, "snapshot", return_value={
            "schema_version": 1, "status": "clear", "entries": [], "error": None})
        self.lease_patch.start()
        self.fleet = FleetState(str(self.root), str(self.root / "isolated-locks"), str(self.root / "shots"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            None, self.jobs, self.admin, fleet=self.fleet))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        try:
            for worker in self.workers:
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.terminate()  # This fixture's still-live Popen handle only.
                    worker.wait(timeout=5)
            if self.jobs.dispatch.thread:
                self.jobs.dispatch.thread.join(timeout=5)
            qa = NODE.parents[1] / "qa"
            rows = self.jobs.dispatch.queue.rows()
            if rows and qa.is_dir():
                observed = []
                for row in rows:
                    item = {"job_id": row["job_id"], "request_id": row["request_id"]}
                    try:
                        item["status"] = self.jobs.status(row["job_id"])
                    except (OSError, ValueError, KeyError) as error:
                        item["status_error"] = f"{type(error).__name__}: {error}"
                    item["receipts"] = {}
                    for name in ("inputs.json", "input-write-limits.json"):
                        try:
                            item["receipts"][name] = self.jobs._read_root.read_json(row["job_id"] + "/" + name)
                        except FileNotFoundError:
                            item["receipts"][name] = None
                        except (OSError, ValueError) as error:
                            item["receipts"][name] = {"read_error": f"{type(error).__name__}: {error}"}
                    observed.append(item)
                summary = {"test": self._testMethodName, "assertions_completed": self.case_ok,
                           "scope": "isolated HTTP/CLI and harmless host workers; synthetic small inputs",
                           "checks": self.checks, "jobs": observed,
                           "workers": [{"pid": worker.pid, "returncode": worker.returncode} for worker in self.workers],
                           "device_transport_calls": 0}
                path = qa / f"job-input-write-limits-{self._testMethodName}-{os.getpid()}-{time.time_ns()}.json"
                path.write_text(json.dumps(summary, indent=2) + "\n")
                print(f"job input write limits evidence: {path}", file=sys.stderr)
            self.jobs.games.close()
            self.jobs.directory.close()
            self.fleet.close()
            self.jobs._read_root.close()
        finally:
            self.launch_patch.stop()
            self.lease_patch.stop()
            self.temp.cleanup()

    def call(self, method, path, *, body=None, headers=None, readonly=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        values = {"Authorization": "Bearer " + (readonly_token(self.admin) if readonly else self.admin),
                  **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def submit(self, request_id, spec, body=b""):
        status, value = self.call("POST", "/jobs", body=body, headers={
            "X-Job": json.dumps(spec), "X-Request-ID": request_id,
            "Content-Type": "application/gzip"})
        self.assertEqual(status, 200, value)
        return value

    def finished(self, jid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.jobs.status(jid)
            if value["state"] == "finished":
                return value
            time.sleep(.03)
        log = Path(self.jobs.path(jid, "runner.log"))
        self.fail(f"bounded fixture did not finish: {value}; " + (log.read_text() if log.exists() else ""))

    def cli(self, *args):
        env = dict(os.environ, PF_NODE_URL=self.url, PF_NODE_TOKEN=self.admin,
                   PF_NODE_ID=self.jobs.games.node_id, HOME=str(self.root),
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PF_NODE_TOKEN_FILE", None)
        env.pop("DEVLOCK_LABEL", None)
        return subprocess.run([sys.executable, str(NODE / "pf-node"), *args], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=15)

    def source(self):
        data = b"small synthetic model bytes\x00\xff\n"
        original = b'{ "nested": {"requests": [{"model": "old-host-path", "keep": "unchanged"}]}}\n'
        code = ("import os; from pathlib import Path; out=Path(os.environ['PF_JOB_OUT']); "
                f"(out/'model.bin').write_bytes({data!r}); (out/'request.json').write_bytes({original!r})")
        job = self.submit("source-small-artifacts", {"bin": "fixture", "argv": ["-c", code], "device_access": "none"})
        finished = self.finished(job["id"])
        self.assertEqual(finished["exit"], 0, finished)
        self.assertIs(finished["execution"]["worker_alive"], False)
        self.assertEqual(finished["execution"]["owned_process_group"]["status"], "absent")
        self.assertEqual(finished["queue"]["state"], "done")
        references = {}
        for name in ("model.bin", "request.json"):
            status, reference = self.call("GET", f"/jobs/{job['id']}/input-reference?" + urlencode({"path": name}), readonly=True)
            self.assertEqual(status, 200, reference)
            self.assertEqual(reference["schema"], "phonefarm/job-input-reference@1")
            self.assertEqual(reference["source"]["job_id"], job["id"])
            references[name] = reference
        self.assertEqual(references["model.bin"]["source"]["sha256"], hashlib.sha256(data).hexdigest())
        manifest = {"schema": "phonefarm/job-inputs@1", "node_id": references["model.bin"]["node_id"],
                    "cwd": "input", "files": [
                        {"name": "config", "target": "originals/request.json", "source": references["request.json"]["source"]},
                        {"name": "model", "target": "data/model.bin", "source": references["model.bin"]["source"]}],
                    "bindings": [{"source": "config", "target": "resolved/request.json",
                                  "values": [{"pointer": "/nested/requests/0/model", "input": "model"}]}]}
        return job["id"], manifest, data, original

    def input_status(self, jid):
        status, value = self.call("GET", f"/jobs/{jid}/inputs", readonly=True)
        self.assertEqual(status, 200, value)
        self.assertEqual((value["schema"], value["job_id"]), ("phonefarm/job-input-status@1", jid))
        return value

    def peer_node(self, label):
        """A second isolated node as a registered cross-node source."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        peer = Jobs(str(Path(temp.name)), {"fixture": (sys.executable, str(Path(temp.name)))})
        admin = label + "-peer-fixture-not-production"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, peer, admin))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_peer():
            server.shutdown()
            server.server_close()
            thread.join()
            peer.directory.close()
            peer._read_root.close()

        self.addCleanup(close_peer)

        def call(method, path, *, body=None, headers=None, use_admin=True):
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            values = {"Authorization": "Bearer " + (admin if use_admin else readonly_token(admin)),
                      **(headers or {})}
            try:
                connection.request(method, path, body=body, headers=values)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        def submit(request_id, spec, body=b""):
            status, value = call("POST", "/jobs", body=body, headers={
                "X-Job": json.dumps(spec), "X-Request-ID": request_id,
                "Content-Type": "application/gzip"})
            self.assertEqual(status, 200, value)
            return value

        return peer, admin, f"http://127.0.0.1:{server.server_port}", call, submit

    def test_cross_node_input_reference_prepares_from_registered_source(self):
        original = b'{ "cross": {"node": "source-of-truth", "value": 7}}\n'
        peer, peer_admin, peer_url, peer_call, peer_submit = self.peer_node("lab-b")
        self.assertNotEqual(peer.games.node_id, self.jobs.games.node_id)
        code = ("import os; from pathlib import Path; "
                f"(Path(os.environ['PF_JOB_OUT']) / 'report.json').write_bytes({original!r})")
        job = peer_submit("peer-source-job", {"bin": "fixture", "argv": ["-c", code], "device_access": "none"})
        deadline = time.monotonic() + 10
        while peer.status(job["id"])["state"] != "finished" and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertEqual(peer.status(job["id"])["exit"], 0, peer.status(job["id"]))
        status, reference = peer_call("GET", f"/jobs/{job['id']}/input-reference?path=report.json")
        self.assertEqual(status, 200, reference)

        # 登记带凭据的只读来源；来源 node_id 与消费节点不同。
        entry = self.jobs.directory.create({"name": "lab-b", "url": peer_url,
                                            "token": readonly_token(peer_admin)})
        self.assertTrue(self.jobs.directory._pin(entry, peer.games.node_id))
        node = self.call("GET", "/node")[1]
        self.assertIn("jobs.cross-node-inputs-v1", node["capabilities"])

        manifest = {"schema": "phonefarm/job-inputs@1", "node_id": self.jobs.games.node_id,
                    "cwd": "default", "files": [
                        {"name": "report", "target": "originals/report.json",
                         "source": {**reference["source"], "node": "lab-b"}}],
                    "bindings": []}
        consumer = ("import os; from pathlib import Path; inp=os.environ['PF_JOB_IN']; "
                    "out=Path(os.environ['PF_JOB_OUT']); "
                    "out.joinpath('copy.json').write_bytes(Path(inp,'originals/report.json').read_bytes())")
        submitted = self.submit("cross-node-consumer", {"bin": "fixture", "argv": ["-c", consumer],
                                                        "device_access": "none", "inputs": manifest})
        terminal = self.finished(submitted["id"])
        self.assertEqual(terminal["exit"], 0, terminal)
        self.assertEqual(Path(self.jobs.path(submitted["id"], "out", "copy.json")).read_bytes(), original)
        receipt = self.input_status(submitted["id"])["preparation"]
        self.assertEqual(receipt["status"], "ready", receipt)
        self.assertEqual(receipt["files"][0]["source"]["node"], "lab-b")
        self.assertNotIn(peer_admin, json.dumps(receipt))
        # 同节点原件不被触碰。
        self.assertEqual(Path(peer.path(job["id"], "out", "report.json")).read_bytes(), original)

        def refused(name, declaration, error_code):
            bad = self.submit("cross-refused-" + name, {"bin": "fixture", "argv": ["-c", "import sys; sys.exit(2)"],
                                                        "device_access": "none", "inputs": declaration})
            terminal = self.finished(bad["id"])
            self.assertEqual(terminal["outcome"], "failed", terminal)
            self.assertIsNone(terminal["execution"].get("child_pid"))
            self.assertFalse(Path(self.jobs.path(bad["id"], "launch.json")).exists())
            budget = terminal["input_preparation"]
            self.assertEqual(budget["status"], "failed", budget)
            self.assertEqual(budget["error"]["code"], error_code, budget)
            self.assertEqual(self.input_status(bad["id"])["preparation"]["status"], "failed")

        wrong_hash = copy.deepcopy(manifest)
        wrong_hash["files"][0]["source"]["sha256"] = "0" * 64
        refused("hash", wrong_hash, "input_mismatch")
        wrong_size = copy.deepcopy(manifest)
        wrong_size["files"][0]["source"]["size"] += 1
        refused("size", wrong_size, "input_mismatch")
        unknown_entry = copy.deepcopy(manifest)
        unknown_entry["files"][0]["source"]["node"] = "no-such-entry"
        refused("entry", unknown_entry, "source_not_found")

        # 移除凭据：准备期拒绝，消费命令不执行。
        self.jobs.directory.update("lab-b", {"revision": entry["revision"], "token": None})
        refused("credential", manifest, "source_unavailable")

    def test_reference_cli_binding_and_repeated_request_execute_once(self):
        status, node = self.call("GET", "/node")
        self.assertEqual(status, 200)
        self.assertIn("jobs.inputs-v1", node["capabilities"])
        self.assertIn("jobs.input-limits-v1", node["capabilities"])
        self.assertEqual(node["execution"]["input_limits"], job_input_archive.POLICY)
        source, manifest, data, original = self.source()
        manifest_path = self.root / "inputs.json"
        manifest_path.write_text(json.dumps(manifest))
        upload = self.root / "small-upload.txt"
        upload_data = b"small CLI tar payload\n"
        upload.write_bytes(upload_data)
        counter = self.root / "consumer-executions.txt"
        code = ("import os,json,sys; from pathlib import Path; "
                "config=json.loads(Path('resolved/request.json').read_text()); "
                "bound=Path(config['nested']['requests'][0]['model']); "
                "result={'cwd':str(Path.cwd()),'input_dir':os.environ['PF_JOB_IN'],"
                "'bound_path':str(bound),'data':list(bound.read_bytes()),"
                "'uploaded':list(Path(sys.argv[1]).read_bytes()),"
                "'keep':config['nested']['requests'][0]['keep']}; "
                "(Path(os.environ['PF_JOB_OUT'])/'result.json').write_text(json.dumps(result)); "
                f"counter=Path({str(counter)!r}); "
                "stream=counter.open('a'); stream.write('executed\\n'); stream.close()")
        args = ("--inputs", str(manifest_path), "--request-id", "bound-consumer-once", "--device-access", "none",
                "--detach", "fixture", "-c", code, str(upload))
        submitted = self.cli(*args)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        jid = submitted.stdout.strip()
        finished = self.finished(jid)
        self.assertEqual(finished["exit"], 0, finished)
        preparation = finished["input_preparation"]
        # A frozen ID must not be re-prepared under a later, smaller policy.
        with patch.dict(job_input_archive.POLICY, expanded_bytes=1, entries=1):
            repeat = self.cli(*args)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(repeat.stdout.strip(), jid)
        self.assertEqual(self.jobs.status(jid)["input_preparation"], preparation)
        self.assertEqual(counter.read_text(), "executed\n")
        self.assertEqual(len(self.jobs.dispatch.queue.rows()), 2)
        record = self.input_status(jid)
        self.assertEqual(record["declared"], manifest)
        self.assertEqual(record["preparation"]["status"], "ready")
        self.assertIsNone(record["preparation"]["error"])
        cli_status = self.cli("inputs", jid)
        self.assertEqual(cli_status.returncode, 0, cli_status.stderr)
        self.assertEqual(json.loads(cli_status.stdout), record)
        root = Path(self.jobs.path(jid, "in"))
        result = json.loads(Path(self.jobs.path(jid, "out", "result.json")).read_text())
        self.assertEqual(result, {"cwd": str(root.resolve()), "input_dir": str(root),
                                  "bound_path": str(root / "data/model.bin"), "data": list(data),
                                  "uploaded": list(upload_data), "keep": "unchanged"})
        derived = (root / "resolved/request.json").read_bytes()
        expected_bytes = len(upload_data) + len(original) + len(data) + len(derived)
        self.assertEqual(preparation["schema"], "phonefarm/job-input-write-limits@1")
        self.assertEqual(preparation["status"], "ready")
        self.assertIsNone(preparation["error"])
        self.assertEqual(preparation["limits"], job_input_archive.POLICY)
        self.assertEqual(preparation["declared_bytes"], expected_bytes)
        self.assertEqual(preparation["written_bytes"], expected_bytes)
        self.assertGreater(preparation["tar_stream_bytes"], len(upload_data))
        self.assertGreaterEqual(preparation["entries"], 4)
        self.assertEqual(preparation["space"]["basis"], "f_bavail")
        self.assertGreaterEqual(preparation["space"]["available_bytes"], preparation["space"]["required_bytes"])
        self.assertEqual((root / "0/small-upload.txt").read_bytes(), upload_data)
        self.assertEqual(sum(path.stat().st_size for path in root.rglob("*") if path.is_file()), expected_bytes)
        self.assertEqual((root / "originals/request.json").read_bytes(), original)
        self.assertEqual(Path(self.jobs.path(source, "out", "request.json")).read_bytes(), original)
        self.assertEqual(Path(self.jobs.path(source, "out", "model.bin")).read_bytes(), data)
        self.assertNotIn("sha256", record["preparation"]["bindings"][0], "derived JSON must not inherit the original hash")
        self.checks.update(consumer_executions=1, source_bytes_unchanged=True,
                           prepared_bytes=expected_bytes, same_id_after_policy_change=True)
        self.case_ok = True

    def test_unavailable_sources_and_colliding_upload_never_start_consumer(self):
        source, manifest, data, original = self.source()
        sentinel = self.root / "must-not-execute"
        command = ["-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('executed')"]

        def refused(name, declaration, body=b"", *, error_code=None, error_status=None):
            job = self.submit("refused-" + name, {"bin": "fixture", "argv": command,
                              "device_access": "none", "inputs": declaration}, body)
            terminal = self.finished(job["id"])
            self.assertEqual(terminal["outcome"], "failed", terminal)
            self.assertIsNone(terminal["execution"].get("child_pid"))
            self.assertEqual(terminal["execution"]["owned_process_group"]["status"], "not_started")
            self.assertFalse(Path(self.jobs.path(job["id"], "launch.json")).exists())
            budget = terminal["input_preparation"]
            self.assertEqual(budget["schema"], "phonefarm/job-input-write-limits@1")
            self.assertEqual(budget["status"], "failed", budget)
            if error_code is not None:
                self.assertEqual(budget["error"]["code"], error_code, budget)
            if error_status is not None:
                self.assertEqual(budget["error"]["status"], error_status, budget)
            receipt = self.input_status(job["id"])
            self.assertEqual(receipt["preparation"]["status"], "failed", receipt)
            self.assertIsInstance(receipt["preparation"]["error"], str)
            self.assertTrue(receipt["preparation"]["error"])
            self.assertFalse(sentinel.exists())
            return job, receipt

        # Raw finished/exit=0 does not substitute for verified host completion.
        saved = self.jobs._read_meta(source)
        unknown = copy.deepcopy(saved)
        unknown["execution"]["owned_process_group"]["status"] = "unknown"
        self.jobs._write_meta(source, unknown)
        try:
            status, error = self.call("GET", f"/jobs/{source}/input-reference?path=model.bin")
            self.assertEqual((status, error["code"]), (409, "source_unavailable"))
            refused("unknown-host", manifest)
        finally:
            self.jobs._write_meta(source, saved)

        missing = copy.deepcopy(manifest)
        missing["files"][1]["source"]["path"] = "missing.bin"
        status, error = self.call("GET", f"/jobs/{source}/input-reference?path=missing.bin")
        self.assertEqual((status, error["code"]), (404, "source_not_found"))
        missing_job, missing_receipt = refused("missing", missing)
        # Repairing the source cannot silently re-prepare an already failed ID.
        missing_path = Path(self.jobs.path(source, "out", "missing.bin"))
        missing_path.write_bytes(data)
        repeat = self.submit("refused-missing", {"bin": "fixture", "argv": command,
                             "device_access": "none", "inputs": missing})
        self.assertEqual(repeat["id"], missing_job["id"])
        self.assertEqual(self.input_status(repeat["id"]), missing_receipt)

        mismatch = copy.deepcopy(manifest)
        mismatch["files"][1]["source"]["sha256"] = "0" * 64
        refused("hash-mismatch", mismatch)

        uploaded = b"pre-existing uploaded input must remain unchanged\n"
        conflict, _ = refused("uploaded-target", manifest,
                              tar_bytes([("originals/request.json", uploaded)]),
                              error_code="target_conflict", error_status=409)
        self.assertEqual(list(Path(self.jobs.path(conflict["id"], "in")).iterdir()), [],
                         "merged namespace must be checked before any tar or typed input write")

        # Originals fit; the actual derived JSON must still count in the same
        # budget, and this static failure must leave the entire input empty.
        with patch.dict(job_input_archive.POLICY, expanded_bytes=len(data) + len(original)):
            derived, _ = refused("derived-budget", manifest,
                                 error_code="input_budget_exceeded", error_status=413)
        self.assertEqual(list(Path(self.jobs.path(derived["id"], "in")).iterdir()), [])
        self.assertEqual(self.jobs.status(derived["id"])["input_preparation"]["written_bytes"], 0)

        with patch.object(job_preparation.os, "fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
            low, _ = refused("low-space", manifest,
                             error_code="input_space_insufficient", error_status=507)
        low_record = self.jobs.status(low["id"])["input_preparation"]
        self.assertEqual(low_record["written_bytes"], 0)
        self.assertEqual(low_record["space"]["available_bytes"], 0)
        self.assertEqual(list(Path(self.jobs.path(low["id"], "in")).iterdir()), [])

        kept, broken = b"written prefix retained\n", b"only this controlled input write fails\n"
        real_write, injected = os.write, []

        def no_space(fd, value):
            if bytes(value) == broken:
                injected.append(fd)
                raise OSError(errno.ENOSPC, "fixture input write: no space")
            return real_write(fd, value)

        with patch.object(job_preparation.os, "write", side_effect=no_space):
            full, _ = refused("write-enospc", manifest,
                              tar_bytes([("kept.bin", kept), ("failed.bin", broken)]),
                              error_code="input_write_space_failed", error_status=507)
        self.assertEqual(len(injected), 1)
        self.assertEqual(Path(self.jobs.path(full["id"], "in", "kept.bin")).read_bytes(), kept)
        self.assertEqual(self.jobs.status(full["id"])["input_preparation"]["written_bytes"], len(kept))
        failed_record = self.jobs.status(full["id"])["input_preparation"]
        self.assertEqual(failed_record["error"]["details"]["errno"], errno.ENOSPC)
        repeat = self.submit("refused-write-enospc", {"bin": "fixture", "argv": command,
                             "device_access": "none", "inputs": manifest},
                             tar_bytes([("kept.bin", kept), ("failed.bin", broken)]))
        self.assertEqual(repeat["id"], full["id"])
        self.assertEqual(self.jobs.status(full["id"])["input_preparation"], failed_record)

        # Losing the first receipt must remain distinguishable from a legacy
        # job without this protocol, even when later finalization succeeds.
        publish = self.jobs._read_root.publish_json_once
        publication_failures = []

        def fail_initial_receipt(rel, value):
            if rel.endswith("/input-write-limits.json") and not publication_failures:
                publication_failures.append(rel)
                raise OSError(errno.ENOSPC, "fixture initial input receipt publication")
            return publish(rel, value)

        spec = {"bin": "fixture", "argv": command, "device_access": "none", "inputs": manifest}
        with patch.object(self.jobs._read_root, "publish_json_once", side_effect=fail_initial_receipt):
            missing_receipt = self.submit("refused-initial-receipt", spec)
            terminal = self.finished(missing_receipt["id"])
            repeat = self.submit("refused-initial-receipt", spec)
        self.assertEqual(len(publication_failures), 1)
        self.assertEqual(terminal["outcome"], "failed", terminal)
        self.assertIsNone(terminal["execution"].get("child_pid"))
        self.assertEqual(terminal["execution"]["owned_process_group"]["status"], "not_started")
        self.assertEqual(terminal["input_preparation"]["status"], "unknown")
        self.assertEqual(terminal["input_preparation"]["error"]["code"], "input_receipt_unavailable")
        self.assertEqual(repeat["id"], missing_receipt["id"])
        self.assertEqual(repeat["input_preparation"], terminal["input_preparation"])
        self.assertNotIn("input_preparation_expected", self.jobs.dispatch.queue.get(repeat["id"])["spec"])
        for name in ("input-write-limits.json", "inputs.json", "launch.json"):
            self.assertFalse(Path(self.jobs.path(repeat["id"], name)).exists(), name)
        self.assertEqual(list(Path(self.jobs.path(repeat["id"], "in")).iterdir()), [])
        self.assertEqual(Path(self.jobs.path(source, "out", "request.json")).read_bytes(), original)
        self.assertEqual(Path(self.jobs.path(source, "out", "model.bin")).read_bytes(), data)
        self.assertFalse(sentinel.exists())
        self.assertEqual(len(self.workers), 1, "refused preparations must not launch workers")
        self.checks.update(consumer_executions=0, source_worker_count=1, source_bytes_unchanged=True,
                           enospc_prefix_bytes=len(kept), failed_id_not_replayed=True,
                           initial_receipt_failure_unknown=True)
        self.case_ok = True

    def test_archive_preflight_bounds_and_namespace_use_small_inputs(self):
        single = tar_bytes([("a", b"1234")])
        raw = tar_bytes([("a", b"1234")], raw=True)
        pax = tarfile.TarInfo("pax.txt")
        pax.pax_headers = {"comment": "metadata counts toward the complete stream" * 40}
        pax_raw = tar_bytes([(pax, b"x")], raw=True)
        link = tarfile.TarInfo("linked")
        link.type, link.linkname = tarfile.SYMTYPE, "a"
        hardlink = tarfile.TarInfo("hardlinked")
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "a"
        sparse = tarfile.TarInfo("sparse")
        sparse.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.size": "16"}
        cases = [
            ("compressed", single, {"compressed_bytes": len(single) - 1}, (), "input_budget_exceeded", 413),
            ("payload", single, {"expanded_bytes": 3}, (), "input_budget_exceeded", 413),
            ("entries", tar_bytes([("a", b""), ("b", b"")]), {"entries": 1}, (), "input_budget_exceeded", 413),
            ("implicit-directories", tar_bytes([("a/b/c", b"")]), {"entries": 2}, (), "input_budget_exceeded", 413),
            ("pax-metadata", gzip.compress(pax_raw), {"metadata_bytes": 32}, (), "input_budget_exceeded", 413),
            ("pax-stream", gzip.compress(pax_raw), {"tar_stream_bytes": len(pax_raw) - 1}, (), "input_budget_exceeded", 413),
            ("trailing-stream", gzip.compress(raw + b"\0" * 1024), {"tar_stream_bytes": len(raw)}, (), "input_budget_exceeded", 413),
            ("gzip-members", single + gzip.compress(b"\0" * 1024), {"tar_stream_bytes": len(raw)}, (), "input_budget_exceeded", 413),
            ("symlink", tar_bytes([("a", b"1"), (link, b"")]), {}, (), "invalid_input_archive", 400),
            ("hardlink", tar_bytes([("a", b"1"), (hardlink, b"")]), {}, (), "invalid_input_archive", 400),
            ("sparse", tar_bytes([(sparse, b"x")]), {}, (), "invalid_input_archive", 400),
            ("duplicate", tar_bytes([("a", b"1"), ("a", b"2")]), {}, (), "invalid_input_archive", 400),
            ("file-parent", tar_bytes([("a", b"1"), ("a/b", b"2")]), {}, (), "invalid_input_archive", 400),
            ("typed-parent", single, {}, ("a/b",), "target_conflict", 409),
        ]
        for name, body, limits, targets, code, status in cases:
            with self.subTest(name=name), patch.dict(job_input_archive.POLICY, limits):
                with self.assertRaises(job_input_archive.Error) as caught:
                    job_input_archive.inspect(body, typed_targets=targets)
                self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
                self.assertIsInstance(caught.exception.details, dict)
        inspected = job_input_archive.inspect(single)
        self.assertEqual(inspected.payload_bytes, 4)
        self.assertEqual(inspected.stream_bytes, len(raw))
        self.assertEqual(inspected.entries, 1)
        self.assertEqual(self.jobs.dispatch.queue.rows(), [])
        self.assertEqual(self.workers, [])


if __name__ == "__main__":
    unittest.main()
