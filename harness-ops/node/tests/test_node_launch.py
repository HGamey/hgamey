"""The stock launcher keeps one startup selection across service replacement."""
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


NODE = Path(__file__).resolve().parents[1]
TOKEN = "isolated-node-launch-credential-not-production"


class NodeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-launch-")
        self.root = Path(self.temp.name).resolve()
        self.home, self.config = self.root / "home with spaces", self.root / "config with spaces"
        self.install, self.data = self.root / "installed node", self.root / "original data"
        for directory in (self.home, self.config, self.install):
            directory.mkdir()
        # Only executable source is copied: no checkout data, credentials or jobs.
        for source in NODE.glob("*.py"):
            shutil.copyfile(source, self.install / source.name)
        shutil.copyfile(NODE / "run_node.sh", self.install / "run_node.sh")
        self.token_file = self.config / "node.token"
        self.token_file.write_text(TOKEN + "\n")
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                    "HG_CONFIG_DIR": str(self.config), "HG_NODE_PYTHON": sys.executable,
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.processes = []

    def tearDown(self):
        try:
            for process, _ in self.processes:
                self.stop(process)
        finally:
            self.temp.cleanup()

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()  # The owned launcher forwards TERM to its owned service.
            try:
                process.wait(timeout=9)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise AssertionError("launcher did not complete its bounded child cleanup")

    def launch(self, *args):
        log = self.root / f"launcher-{len(self.processes)}.log"
        with log.open("wb") as output:
            process = subprocess.Popen(["/bin/sh", str(self.install / "run_node.sh"),
                                        "--no-tunnel", *args], cwd=self.root, env=self.env,
                                       stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append((process, log))
        return process, log

    def profile(self, args, *, token=None, name="launch.json"):
        path = self.config / name
        path.write_text(json.dumps({"schema": "hgamey/node-launch@1", "service_args": args,
                                    "node_token_file": str(token or self.token_file)}))
        return path

    def diagnostic(self, log):
        service = self.home / "node" / "node_service.log"
        return log.read_text()[-6000:] + (service.read_text()[-6000:] if service.exists() else "")

    @staticmethod
    def records(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def running(pid):
        try:
            # An exited orphan may briefly await the host init's reap on Linux.
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except FileNotFoundError:
            pass
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def call(self, port, path, *, method="GET", node_id=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        headers = {"Authorization": "Bearer " + TOKEN}
        if node_id:
            headers["X-Expected-Node-ID"] = node_id
        try:
            connection.request(method, path, body=b"{}" if method == "POST" else None,
                               headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_identity(self, process, log, port, *, previous=None):
        deadline = time.monotonic() + 15
        last = None
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), self.diagnostic(log))
            try:
                status, last = self.call(port, "/node")
                if status == 200 and last["service"]["instance_id"] != previous:
                    return last
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.05)
        self.fail(f"service did not become ready: {last!r}\n{self.diagnostic(log)}")

    def test_http_restart_keeps_frozen_paths_identity_and_old_job(self):
        pf = self.root / "closed fake phonefarm"
        pf.mkdir()
        calls = self.root / "pf-calls.jsonl"
        stub = pf / "stub.py"
        stub.write_text(f"CALLS={str(calls)!r}\nROOT={str(pf)!r}\n" + r'''
import json, os, select, sys, time
from pathlib import Path
def record(**value):
    with open(CALLS, "a") as output:
        output.write(json.dumps(dict(pid=os.getpid(), parent=os.getppid(), **value)) + "\n")
args = sys.argv[1:]
record(event="start", argv=args)
if args == ["serve", "--root", ROOT]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], .2)
        if ready:
            if os.read(sys.stdin.fileno(), 4096) == b"":
                record(event="serve_exit", reason="stdin_eof")
                raise SystemExit(0)
            record(event="unexpected_rpc")
            raise SystemExit(91)
    record(event="serve_exit", reason="fixture_deadline")
    raise SystemExit(92)
if args == ["runtime-info", "--json"]:
    print(json.dumps({"schema":"phonefarm/runtime-identity@1", "program":"phonefarm",
        "package_version":"fixture", "build":{"source_revision":None,"source_dirty":None,
        "target":"fixture","profile":"test"}, "supported_protocols":[]}))
    raise SystemExit(0)
if args in (["remote-control", "capabilities", "--json"],
            ["screen", "capabilities", "--json"], ["game-deploy", "capabilities", "--json"]):
    print(json.dumps({"schema":"fixture/unsupported-capability@1"}))
    raise SystemExit(2)
record(event="forbidden_call")
raise SystemExit(93)
''')
        program = pf / "phonefarm"
        program.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                           shlex.quote(str(stub)) + ' "$@"\n')
        program.chmod(0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = ["--phonefarm", str(pf), "--game-opt-loop", str(self.root / "missing gol"),
                "--data-dir", str(self.data), "--port", str(port), "--max-running", "3"]
        profile = self.profile(args)
        jid = "a0123456789b"
        job = self.data / "jobs" / jid
        (job / "out").mkdir(parents=True)
        originals = {job / "meta.json": json.dumps({"id": jid, "bin": "legacy",
            "argv": ["offline-fixture"], "label": "original small evidence", "started": 1, "exit": 7}).encode(),
            job / "log": b"old log\n", job / "out" / "evidence.txt": b"synthetic evidence\n"}
        for path, data in originals.items():
            path.write_bytes(data)
        declaration = self.install / "runtime-build.json"
        build = {"schema": "hgamey/source-build@1", "repository": "fixture/node-launch",
                 "revision": "a" * 40, "dirty": False}
        declaration.write_text(json.dumps(build))
        process, log = self.launch("--config", str(profile))
        first = self.wait_identity(process, log, port)
        self.assertEqual(first["storage"], {"data_root": str(self.data), "jobs_root": str(self.data / "jobs")})
        self.assertEqual(first["execution"]["max_running"], 3)
        self.assertEqual(first["service"]["runtime"]["build"], build)
        self.assertEqual(first["service"]["runtime"]["entry_path"], str(self.install / "node_service.py"))
        identity_bytes = (self.data / "_node" / "id").read_bytes()
        status, old = self.call(port, f"/jobs/{jid}", node_id=first["node_id"])
        self.assertEqual((status, old["exit"]), (200, 7))

        # Simulate a code-only install update, plus a deliberate future launch selection.
        service = self.install / "node_service.py"
        with service.open("a") as output:
            output.write("\n# Fixture code revision B, loaded only by the next service process.\n")
        build["revision"] = "b" * 40
        declaration.write_text(json.dumps(build))
        changed = json.loads(profile.read_text())
        changed["service_args"][changed["service_args"].index("--data-dir") + 1] = str(self.root / "other data")
        profile.write_text(json.dumps(changed))
        changed_profile = profile.read_bytes()
        self.token_file.write_text("changed-future-launch-token-not-current-service\n")
        status, restarted = self.call(port, "/admin/restart", method="POST", node_id=first["node_id"])
        self.assertEqual((status, restarted["restarting"]), (200, True))
        second = self.wait_identity(process, log, port, previous=first["service"]["instance_id"])
        self.assertEqual(second["node_id"], first["node_id"])
        self.assertEqual(second["storage"], first["storage"])
        self.assertEqual(second["service"]["runtime"]["build"], build)
        self.assertEqual((self.data / "_node" / "id").read_bytes(), identity_bytes)
        self.assertEqual(profile.read_bytes(), changed_profile)
        self.assertFalse((self.root / "other data").exists())
        status, old = self.call(port, f"/jobs/{jid}", node_id=first["node_id"])
        self.assertEqual((status, old["exit"], old["argv"]), (200, 7, ["offline-fixture"]))
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.stop(process)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM)
        service_pids = [int(pid) for pid in re.findall(r"service started pid=(\d+)", log.read_text())]
        self.assertEqual(len(service_pids), 2, self.diagnostic(log))
        self.assertNotEqual(*service_pids)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = self.records(calls)
            exits = [r for r in records if r["event"] == "serve_exit"]
            if len(exits) == 2 and all(not self.running(pid) for pid in service_pids + [r["pid"] for r in exits]):
                break
            time.sleep(.05)
        self.assertEqual(len(exits), 2, records)
        self.assertEqual({r["reason"] for r in exits}, {"stdin_eof"})
        self.assertEqual({r["parent"] for r in records if r.get("argv", [])[:1] == ["serve"]}, set(service_pids))
        self.assertFalse(any(r["event"] in {"forbidden_call", "unexpected_rpc"} for r in records), records)
        self.assertFalse(any(self.running(pid) for pid in service_pids + [r["pid"] for r in exits]))
        self.assertNotIn(TOKEN, self.diagnostic(log))
        self.assertFalse((self.home / "node" / "cloudflared.log").exists())

    def test_rejections_and_direct_legacy_arguments_use_the_shared_parser(self):
        output = self.root / "service-invocations.jsonl"
        # Retain the real parser without invoking its main or constructing Jobs.
        shutil.copyfile(self.install / "node_service.py", self.install / "service_parser.py")
        (self.install / "node_service.py").write_text(
            "import json, os, sys\nfrom service_parser import argument_parser\n"
            "if __name__ == '__main__':\n"
            "    parsed = argument_parser().parse_args()\n"
            f"    with open({str(output)!r}, 'a') as out:\n"
            "        out.write(json.dumps({'argv':sys.argv[1:], 'parsed':vars(parsed),\n"
            "            'cwd':os.getcwd(), 'python':sys.executable,\n"
            f"            'token_matches':os.environ.get('PHONEFARM_NODE_TOKEN') == {TOKEN!r}}}) + '\\n')\n"
            "    raise SystemExit(2)\n")
        valid = self.profile([])
        malformed = self.config / "malformed.json"
        malformed.write_text('{"schema":')
        bad_token = self.config / "short.token"
        bad_token.write_text("not-a-valid-credential")
        invalid_credential = self.profile([], token=bad_token, name="invalid-credential.json")
        for args in (("--config", str(malformed)), ("--config", str(valid), "--port", "9999"),
                     ("--max-running", "0"), ("--config", str(invalid_credential))):
            with self.subTest(rejected=args):
                process, log = self.launch(*args)
                self.assertEqual(process.wait(timeout=10), 2, self.diagnostic(log))
                self.assertNotIn("service started", log.read_text())
                self.assertNotIn(TOKEN, log.read_text())
                self.assertFalse(output.exists())
        direct = ["--data-dir", str(self.data), "--worker-prefix", str(self.root / "fixed worker"),
                  "--host-capacity", str(self.config / "capacity file.json"), "--max-running", "4"]
        for args in (direct, []):
            process, log = self.launch(*args)
            self.assertEqual(process.wait(timeout=10), 2, self.diagnostic(log))
            self.assertEqual(len(re.findall(r"service started pid=\d+", log.read_text())), 1)
        direct_call, legacy = self.records(output)
        self.assertEqual(direct_call["argv"], direct)
        self.assertEqual(direct_call["parsed"]["worker_prefix"], str(self.root / "fixed worker"))
        self.assertEqual(direct_call["parsed"]["host_capacity"], str(self.config / "capacity file.json"))
        self.assertEqual(legacy["argv"], [])
        self.assertEqual(legacy["parsed"]["phonefarm"], str(self.home / "node" / "phonefarm"))
        self.assertEqual(legacy["parsed"]["game_opt_loop"], str(self.home / "node" / "game_opt_loop"))
        self.assertIsNone(legacy["parsed"]["data_dir"])
        self.assertEqual(legacy["parsed"]["max_running"], 2)
        for call in (direct_call, legacy):
            self.assertTrue(call["token_matches"])
            self.assertEqual(call["cwd"], str(self.home / "node"))
            self.assertEqual(Path(call["python"]).resolve(), Path(sys.executable).resolve())


if __name__ == "__main__":
    unittest.main()
