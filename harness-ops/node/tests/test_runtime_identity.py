"""A configured-file observation never replaces a job's own startup report."""
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
import job_runtime
from node_fleet import FleetState
from node_read import NodeRead
from node_service import Jobs, make_handler, readonly_token


PROGRAM = r'''
import json, os, sys
from pathlib import Path
identity = {
    "schema": "phonefarm/runtime-identity@1", "program": "game_opt_loop",
    "package_version": VERSION,
    "build": {"source_revision": VERSION, "source_dirty": False,
              "target": "host-fixture", "profile": "test"},
    "supported_protocols": ["phonefarm/runtime-identity@1", "phonefarm/program-start@1"]}
counter = Path(COUNTER)
if sys.argv[1:] == ["runtime-info", "--json"]:
    with counter.open("a") as stream:
        stream.write("probe " + VERSION + "\n")
    print(json.dumps(identity))
    raise SystemExit(0)
report = os.environ.pop("PF_RUNTIME_REPORT", None)
if "no-report" not in sys.argv and report:
    value = {"schema": "phonefarm/program-start@1", "job_id": os.environ["PF_JOB_ID"],
             "pid": os.getpid(), "identity": identity}
    temporary = Path(report + "." + str(os.getpid()))
    temporary.write_text(json.dumps(value))
    try:
        os.link(temporary, report)
    finally:
        temporary.unlink()
with counter.open("a") as stream:
    stream.write("execute " + VERSION + "\n")
(Path(os.environ["PF_JOB_OUT"]) / "business.json").write_text(json.dumps({"version": VERSION}))
'''


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.program, self.counter = self.root / "game-opt-fixture", self.root / "calls.txt"
        self.replace_program("A")
        self.jobs = Jobs(str(self.root), {"game_opt_loop": (str(self.program), str(self.root))})
        self.node = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.admin = "runtime-fixture-not-production"
        self.workers, self.requests = [], []
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
        base = make_handler(None, self.jobs, self.admin, node=self.node, fleet=self.fleet)
        requests = self.requests

        class Handler(base):
            def do_GET(self):
                requests.append(("GET", self.path))
                super().do_GET()

            def do_POST(self):
                requests.append(("POST", self.path))
                super().do_POST()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
                    worker.terminate()  # Only this fixture's still-owned Popen.
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

    def replace_program(self, version):
        temporary = self.program.with_suffix(".new")
        temporary.write_text(f"#!{sys.executable}\nVERSION={version!r}\nCOUNTER={str(self.counter)!r}\n" + PROGRAM)
        temporary.chmod(0o755)
        os.replace(temporary, self.program)

    def call(self, method, path, *, body=None, headers=None, readonly=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        values = {"Authorization": "Bearer " + (readonly_token(self.admin) if readonly else self.admin),
                  "X-Expected-Node-ID": self.node.node_id, **(headers or {})}
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def get(self, path):
        status, value = self.call("GET", path)
        self.assertEqual(status, 200, value)
        return value

    def run_job(self, request_id, *argv):
        spec = {"bin": "game_opt_loop", "argv": ["run", *argv], "device_access": "none", "resources": []}
        status, value = self.call("POST", "/jobs", readonly=False, body=b"", headers={
            "X-Request-ID": request_id, "X-Job": json.dumps(spec), "Content-Type": "application/gzip"})
        self.assertEqual(status, 200, value)
        jid = value["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = self.get("/jobs/" + jid)
            if value["state"] == "finished":
                self.assertEqual((value["exit"], value["outcome"], value["queue"]["state"]),
                                 (0, "succeeded", "done"), value)
                self.assertFalse(value["execution"]["worker_alive"])
                return value
            time.sleep(.02)
        runner = Path(self.jobs.path(jid, "runner.log"))
        self.fail(f"bounded host job did not finish: {value}; " + (runner.read_text() if runner.exists() else ""))

    def cli(self, *args):
        env = dict(os.environ, PF_NODE_URL=self.url, PF_NODE_TOKEN=self.admin,
                   PF_NODE_ID=self.node.node_id, HOME=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        env.pop("PF_NODE_TOKEN_FILE", None)
        proc = subprocess.run([sys.executable, str(NODE / "pf-node"), *args], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assert_report(self, job, version):
        execution = job["execution"]
        report = execution["program_identity"]
        self.assertEqual((report["state"], report["source"]), ("reported", "program_self_report"))
        self.assertEqual((report["record"]["job_id"], report["record"]["pid"]),
                         (job["id"], execution["child_pid"]))
        self.assertEqual(report["record"]["identity"]["package_version"], version)
        self.assertEqual(execution["service_at_launch"], {"node_id": self.node.node_id, "service": self.node.service})
        self.assertEqual(execution["worker_identity"]["entry_path"], str(NODE / "job_runtime.py"))
        self.assertIsInstance(execution["worker_identity"]["observed_at"], (int, float))
        self.assertTrue(execution["worker_identity"]["python_version"])
        launch = json.loads(Path(self.jobs.path(job["id"], "launch.json")).read_text())
        self.assertEqual(launch["service_at_launch"], execution["service_at_launch"])

    def test_cache_replacement_and_job_reports_stay_separate(self):
        initial = self.get("/node")
        self.assertIn("node.runtime-v1", initial["capabilities"])
        self.assertEqual(initial["service"]["source_kind"], "startup_checkout_observation")
        observed = initial["execution"]["programs"]["game_opt_loop"]
        self.assertEqual(observed["state"], "observed")
        self.assertEqual(observed["identity"]["package_version"], "A")
        self.assertEqual(self.counter.read_text().splitlines(), ["probe A"])
        first = self.run_job("runtime-A")
        self.assert_report(first, "A")
        frozen = {name: Path(self.jobs.path(first["id"], *name.split("/"))).read_bytes()
                  for name in ("meta.json", "launch.json", "out/.program-runtime.json")}

        # A pathname replacement invalidates the configuration cache, not the
        # already-recorded direct child identity or that job's business output.
        self.replace_program("B")
        for _ in range(2):
            stale = self.get("/node")["execution"]["programs"]["game_opt_loop"]
            self.assertEqual(stale["state"], "stale")
            self.assertEqual(stale["identity"], observed["identity"])
            self.assertEqual(stale["observed_at"], observed["observed_at"])
        self.assert_report(self.cli("status", first["id"]), "A")
        second = self.run_job("runtime-B")
        self.assert_report(second, "B")
        third = self.run_job("runtime-no-report", "no-report")
        self.assertEqual(third["execution"]["program_identity"]["state"], "unknown")
        self.assertIsNone(third["execution"]["program_identity"]["record"])
        self.assertEqual(json.loads(Path(self.jobs.path(third["id"], "out", "business.json")).read_text()),
                         {"version": "B"})

        legacy_id = "eeeeeeeeeeee"
        legacy_path = Path(self.jobs.path(legacy_id))
        (legacy_path / "out").mkdir(parents=True)
        (legacy_path / "log").write_text("historical evidence\n")
        self.jobs._write_meta(legacy_id, {"id": legacy_id, "bin": "game_opt_loop", "argv": ["old"],
                                        "started": 1, "ended": 2, "exit": 0})
        legacy_bytes = (legacy_path / "meta.json").read_bytes()
        self.assertNotIn("execution", self.cli("status", legacy_id))

        lost_id = "dddddddddddd"
        lost_path = Path(self.jobs.path(lost_id))
        lost_path.mkdir()
        self.jobs._write_meta(lost_id, {"id": lost_id, "bin": "game_opt_loop", "argv": [],
            "started": 1, "state": "running", "phase": "running",
            "execution": {"kind": "supervised-v1", "child_pid": None,
                "program_identity": {"state": "pending", "source": "program_self_report",
                                     "record": None, "reason": None}}})
        lost_bytes = (lost_path / "meta.json").read_bytes()
        lost = self.get("/jobs/" + lost_id)
        self.assertTrue(lost["lost"])
        self.assertEqual(lost["execution"]["program_identity"]["state"], "unknown")
        self.assertIsNone(lost["execution"]["program_identity"]["record"])
        self.assertEqual((lost_path / "meta.json").read_bytes(), lost_bytes)

        before_refresh = self.counter.read_text()
        status, denied = self.call("POST", "/node/runtime/refresh", body=b"{}",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual(status, 401, denied)
        self.assertEqual(self.counter.read_text(), before_refresh)
        self.requests.clear()
        refreshed = self.cli("node", "--refresh-runtime")
        self.assertEqual(self.requests, [("GET", "/node"), ("POST", "/node/runtime/refresh")])
        self.assertEqual(refreshed["service"], initial["service"])
        current = refreshed["execution"]["programs"]["game_opt_loop"]
        self.assertEqual((current["state"], current["identity"]["package_version"]), ("observed", "B"))
        self.assertEqual(self.counter.read_text().splitlines(),
                         ["probe A", "execute A", "execute B", "execute B", "probe B"])
        self.assertEqual(len(self.jobs.dispatch.queue.rows()), 3)
        self.assertEqual(len(self.workers), 3)
        for name, value in frozen.items():
            self.assertEqual(Path(self.jobs.path(first["id"], *name.split("/"))).read_bytes(), value)
        self.assertEqual((legacy_path / "meta.json").read_bytes(), legacy_bytes)

        before_reader_restart = self.counter.read_text()
        restarted = NodeRead(self.jobs, self.jobs.data_root, str(self.root))
        self.assertEqual(restarted.node_id, self.node.node_id)
        self.assertNotEqual(restarted.service["instance_id"], self.node.service["instance_id"])
        self.assertEqual(self.get("/jobs/" + first["id"])["execution"]["program_identity"],
                         first["execution"]["program_identity"])
        self.assertNotIn("execution", self.get("/jobs/" + legacy_id))
        self.assertEqual((legacy_path / "meta.json").read_bytes(), legacy_bytes)
        self.assertEqual(self.counter.read_text(), before_reader_restart,
                         "NodeRead must reuse Jobs' program cache without another runtime probe")


if __name__ == "__main__":
    unittest.main()
