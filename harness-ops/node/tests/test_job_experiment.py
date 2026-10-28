"""A job with bin `experiment` runs a script of the experiments repository at a pinned commit.

The repository is a local bare one. The node fetches the commit into its mirror, unpacks it into the
job's input directory and runs the entry with the node's program paths in its environment. Jobs are
submitted with the real pf-node CLI. No devices; harmless host workers only.
"""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from node_fleet import FleetState  # noqa: E402
from node_service import Jobs, make_handler  # noqa: E402

ADMIN = "experiment-fixture-admin-token-0123456789"
PROBE = """import json, os, sys
keys = ("PF_EXPERIMENT_COMMIT", "PHONEFARM_BIN", "PF_ROOT", "GAME_OPT_LOOP_BIN", "DEVLOCK_LABEL", "PF_JOB_RESEARCH")
value = {"cwd": os.getcwd(), "argv": sys.argv[1:], **{key: os.environ.get(key) for key in keys}}
open(os.path.join(os.environ["PF_JOB_OUT"], "probe.json"), "w").write(json.dumps(value))
print("probe ok")
"""


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ExperimentJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        source = self.tmp / "experiments"
        (source / "probe").mkdir(parents=True)
        (source / "probe" / "run.py").write_text(PROBE)
        (source / "probe" / "run.sh").write_text('echo "sh $1" > "$PF_JOB_OUT/sh.txt"\n')
        git("init", "-q", "-b", "main", str(source))
        git("add", ".", cwd=source)
        git("commit", "-qm", "probe", cwd=source)
        self.commit = git("rev-parse", "HEAD", cwd=source)
        self.remote = self.tmp / "experiments.git"
        git("clone", "-q", "--bare", str(source), str(self.remote))
        root = str(self.tmp / "node")
        bins = {"experiment": (sys.executable, root),
                "phonefarm": ("/opt/fixture/phonefarm/phonefarm", "/opt/fixture/phonefarm"),
                "game_opt_loop": ("/opt/fixture/gol/game_opt_loop", "/opt/fixture/gol")}
        self.jobs = Jobs(root, bins, experiment_url=str(self.remote))
        lease = patch.object(self.jobs.leases, "snapshot", return_value={
            "schema_version": 1, "status": "clear", "entries": [], "error": None})
        lease.start()
        self.addCleanup(lease.stop)
        fleet = FleetState(root, str(self.tmp / "locks"), str(self.tmp / "shots"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, self.jobs, ADMIN, fleet=fleet))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
            if self.jobs.dispatch.thread:
                self.jobs.dispatch.thread.join(timeout=10)
            for close in (self.jobs.games.close, self.jobs.directory.close, fleet.close, self.jobs._read_root.close):
                close()
        self.addCleanup(stop)
        self.address = server.server_address
        self.node_id = self.jobs.games.node_id

    def call(self, method, path, body=None):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        headers = {"Authorization": f"Bearer {ADMIN}", "X-Expected-Node-ID": self.node_id}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw) if raw else None

    def pf(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PF_")}
        env.update(PF_NODE_URL="http://%s:%d" % self.address, PF_NODE_TOKEN=ADMIN, PF_NODE_ID=self.node_id)
        done = subprocess.run([sys.executable, str(NODE / "pf-node"), *args], env=env, capture_output=True,
                              text=True, timeout=120, cwd=self.tmp)
        return done.returncode, done.stdout.strip(), done.stderr

    def finished(self, jid):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            value = self.jobs.status(jid)
            if value["state"] == "finished":
                return value
            time.sleep(.05)
        self.fail(f"experiment job did not finish: {self.jobs.status(jid)}")

    def test_pinned_commit_runs_with_the_node_programs_and_the_research(self):
        identity = self.call("GET", "/node")[1]
        self.assertIn("jobs.experiment-v1", identity["capabilities"])
        self.assertEqual(identity["execution"]["experiments"], {"url": str(self.remote)})
        write = {"schema": "phonefarm/research-write@1", "operation_id": "exp-task", "base_revision": None,
                 "data": {"title": "超分 A/B", "objective": "验证实验任务"}, "actor_label": None, "reason": None}
        self.assertEqual(self.call("POST", "/research-state/research/sr-ab", write)[0], 201)

        code, jid, err = self.pf("--experiment", "main", "--research", "sr-ab", "--device-access", "none", "--detach",
                                 "experiment", "probe/run.py", "--rounds", "2")
        self.assertEqual(code, 0, err)
        status = self.finished(jid)
        self.assertEqual((status["exit"], status["experiment"], status["research_id"]),
                         (0, {"commit": self.commit}, "sr-ab"), status)
        probe = json.loads(Path(self.jobs.path(jid, "out", "probe.json")).read_text())
        self.assertEqual(probe, {
            "cwd": os.path.realpath(self.jobs.path(jid, "in", "experiment")), "argv": ["--rounds", "2"],
            "PF_EXPERIMENT_COMMIT": self.commit, "PHONEFARM_BIN": "/opt/fixture/phonefarm/phonefarm",
            "PF_ROOT": "/opt/fixture/phonefarm", "GAME_OPT_LOOP_BIN": "/opt/fixture/gol/game_opt_loop",
            "DEVLOCK_LABEL": f"job-{jid}", "PF_JOB_RESEARCH": "sr-ab"})

        # A second job reuses the mirror; a shell entry runs under bash.
        code, jid, err = self.pf("--experiment", self.commit, "--device-access", "none", "--detach",
                                 "experiment", "probe/run.sh", "hello")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.finished(jid)["exit"], 0)
        self.assertEqual(Path(self.jobs.path(jid, "out", "sh.txt")).read_text(), "sh hello\n")

    def test_specs_outside_the_contract_are_refused(self):
        base = {"bin": "experiment", "argv": ["probe/run.py"], "device_access": "none", "experiment": {"commit": self.commit}}
        for change in ({"experiment": None}, {"argv": ["../escape.py"]}, {"argv": ["/tmp/escape.py"]},
                       {"argv": ["probe/run.txt"]}, {"experiment": {"commit": self.commit[:12]}},
                       {"experiment": {"commit": self.commit, "repo": "elsewhere"}}, {"exe": "@in/x"}):
            spec = {key: value for key, value in {**base, **change}.items() if value is not None}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.jobs.start(spec, b"")
        with self.assertRaises(ValueError):
            self.jobs.start({"bin": "phonefarm", "argv": ["x"], "device_access": "none", "experiment": {"commit": self.commit}}, b"")
        self.assertNotEqual(self.pf("--experiment", "main", "--device-access", "none", "--detach", "phonefarm", "x")[0], 0)
        self.assertNotEqual(self.pf("--device-access", "none", "--detach", "experiment", "probe/run.py")[0], 0)
        code, _, err = self.pf("--experiment", "no-such-branch", "--device-access", "none", "--detach", "experiment", "probe/run.py")
        self.assertNotEqual(code, 0)
        self.assertIn("no-such-branch", err)

    def test_a_commit_the_repository_lacks_fails_preparation_visibly(self):
        missing = "0" * 40
        job = self.jobs.start({"bin": "experiment", "argv": ["probe/run.py"], "device_access": "none",
                               "experiment": {"commit": missing}}, b"")
        status = self.finished(job["job"]["id"] if "job" in job else job["id"])
        self.assertNotEqual(status["exit"], 0)
        self.assertEqual(status["outcome"], "failed", status)


if __name__ == "__main__":
    unittest.main()
