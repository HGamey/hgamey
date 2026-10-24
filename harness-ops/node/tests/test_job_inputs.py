"""Small same-node input references through real HTTP, CLI and harmless workers."""
import copy
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
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import job_runtime
from node_fleet import FleetState
from node_service import Jobs, make_handler, readonly_token


class JobInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = Jobs(str(self.root), {"fixture": (sys.executable, str(self.root))})
        self.admin = "inputs-fixture-not-production"
        self.workers = []
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

    def test_reference_cli_binding_and_repeated_request_execute_once(self):
        status, node = self.call("GET", "/node")
        self.assertEqual(status, 200)
        self.assertIn("jobs.inputs-v1", node["capabilities"])
        source, manifest, data, original = self.source()
        manifest_path = self.root / "inputs.json"
        manifest_path.write_text(json.dumps(manifest))
        counter = self.root / "consumer-executions.txt"
        code = ("import os,json; from pathlib import Path; "
                "config=json.loads(Path('resolved/request.json').read_text()); "
                "bound=Path(config['nested']['requests'][0]['model']); "
                "result={'cwd':str(Path.cwd()),'input_dir':os.environ['PF_JOB_IN'],"
                "'bound_path':str(bound),'data':list(bound.read_bytes()),"
                "'keep':config['nested']['requests'][0]['keep']}; "
                "(Path(os.environ['PF_JOB_OUT'])/'result.json').write_text(json.dumps(result)); "
                f"counter=Path({str(counter)!r}); "
                "stream=counter.open('a'); stream.write('executed\\n'); stream.close()")
        args = ("--inputs", str(manifest_path), "--request-id", "bound-consumer-once", "--device-access", "none",
                "--detach", "fixture", "-c", code)
        submitted = self.cli(*args)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        jid = submitted.stdout.strip()
        finished = self.finished(jid)
        self.assertEqual(finished["exit"], 0, finished)
        repeat = self.cli(*args)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(repeat.stdout.strip(), jid)
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
                                  "bound_path": str(root / "data/model.bin"), "data": list(data), "keep": "unchanged"})
        self.assertEqual((root / "originals/request.json").read_bytes(), original)
        self.assertEqual(Path(self.jobs.path(source, "out", "request.json")).read_bytes(), original)
        self.assertEqual(Path(self.jobs.path(source, "out", "model.bin")).read_bytes(), data)
        self.assertNotIn("sha256", record["preparation"]["bindings"][0], "derived JSON must not inherit the original hash")

    def test_unavailable_sources_and_colliding_upload_never_start_consumer(self):
        source, manifest, data, original = self.source()
        sentinel = self.root / "must-not-execute"
        command = ["-c", f"from pathlib import Path; Path({str(sentinel)!r}).write_text('executed')"]

        def refused(name, declaration, body=b""):
            job = self.submit("refused-" + name, {"bin": "fixture", "argv": command,
                              "device_access": "none", "inputs": declaration}, body)
            terminal = self.finished(job["id"])
            self.assertEqual(terminal["outcome"], "failed", terminal)
            self.assertIsNone(terminal["execution"].get("child_pid"))
            self.assertEqual(terminal["execution"]["owned_process_group"]["status"], "not_started")
            self.assertFalse(Path(self.jobs.path(job["id"], "launch.json")).exists())
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
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            member = tarfile.TarInfo("originals/request.json")
            member.size = len(uploaded)
            archive.addfile(member, io.BytesIO(uploaded))
        conflict, _ = refused("uploaded-target", manifest, payload.getvalue())
        self.assertEqual(Path(self.jobs.path(conflict["id"], "in", "originals/request.json")).read_bytes(), uploaded)
        self.assertEqual(Path(self.jobs.path(source, "out", "request.json")).read_bytes(), original)
        self.assertFalse(sentinel.exists())
        self.assertEqual(len(self.workers), 1, "refused preparations must not launch workers")


if __name__ == "__main__":
    unittest.main()
