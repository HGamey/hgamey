import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


NODE = Path(__file__).resolve().parents[1]
NODE_ID = "d" * 32
sys.path.insert(0, str(NODE))
import job_runtime
from node_service import Jobs


@unittest.skipUnless(Path("/bin/zsh").exists(), "deployment wrapper requires zsh")
class NodeDeployCLI(unittest.TestCase):
    def test_upload_credentials_use_selected_config_root_and_preserve_failed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "isolated home"
            (home / "node").mkdir(parents=True)
            default = home / ".config" / "hgamey"
            default.mkdir(parents=True, mode=0o700)
            originals = {name: ("old-" + name).encode() for name in ("dash.pass", "steer.pass")}
            for name, value in originals.items():
                (default / name).write_bytes(value)
            selected = root / "selected config with spaces"
            blocked = root / "config is a file"
            blocked.write_bytes(b"keep this ordinary file")
            env = {"HOME": str(home), "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath}
            for label, target, succeeds in (("explicit", selected, True),
                                              ("default", default, True),
                                              ("failed", blocked, False)):
                with self.subTest(case=label):
                    upload = root / (label + " upload")
                    secrets = upload / "secrets"
                    secrets.mkdir(parents=True)
                    payloads = {name: (f"synthetic-{label}-{name}-credential\n").encode()
                                for name in originals}
                    for name, value in payloads.items():
                        (secrets / name).write_bytes(value)
                    if label == "explicit":
                        workers, launch = [], job_runtime.launch

                        def track_worker(*args, **kwargs):
                            worker = launch(*args, **kwargs)
                            workers.append(worker)
                            return worker

                        with patch.dict(os.environ, {**env, "HG_CONFIG_DIR": str(target)}, clear=True), \
                                patch("job_dispatch.job_runtime.launch", side_effect=track_worker):
                            jobs = Jobs(str(home / "node"), {"update_node": ("/bin/zsh", str(home / "node"))})
                            # The installed profile filters the service environment;
                            # use the fixture's own group, with no privileged entry.
                            jobs.pf_env = {**env, "LANG": "C.UTF-8",
                                           "PHONEFARM_SHARED_GID": str(os.getegid())}
                            self.assertNotIn("HG_CONFIG_DIR", jobs.pf_env)
                            try:
                                value = jobs.start({"bin": "update_node",
                                    "argv": [str(NODE / "update_node.sh"), str(upload)],
                                    "device_access": "none", "resources": []}, b"", "config-upload")
                                jid = value["id"]
                                deadline = time.monotonic() + 10
                                while jobs.dispatch.thread is not None and time.monotonic() < deadline:
                                    time.sleep(.02)
                                self.assertIsNone(jobs.dispatch.thread, "update dispatcher did not finish")
                                self.assertEqual(len(workers), 1)
                                meta = jobs.status(jid)
                                self.assertEqual((meta["state"], meta["phase"]), ("finished", "finished"))
                                self.assertEqual(jobs.dispatch.queue.get(jid)["state"], "done")
                                returncode = meta["exit"]
                                output_text = Path(jobs.path(jid, "log")).read_text()
                            finally:
                                for worker in workers:
                                    try:
                                        worker.wait(timeout=5)
                                    except subprocess.TimeoutExpired:
                                        worker.terminate()  # Only this fixture's owned worker.
                                        worker.wait(timeout=5)
                                if jobs.dispatch.thread is not None:
                                    jobs.dispatch.thread.join(timeout=5)
                                jobs._read_root.close()
                    else:
                        result = subprocess.run(["/bin/zsh", str(NODE / "update_node.sh"), str(upload)],
                            cwd=home / "node", env={**env, **({"HG_CONFIG_DIR": str(target)}
                                                           if label != "default" else {})},
                            capture_output=True, text=True, timeout=10)
                        returncode, output_text = result.returncode, result.stdout + result.stderr
                    if succeeds:
                        self.assertEqual(returncode, 0, output_text)
                        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                        for name, value in payloads.items():
                            self.assertEqual((target / name).read_bytes(), value)
                            self.assertEqual((target / name).stat().st_mode & 0o777, 0o600)
                            self.assertFalse((secrets / name).exists())
                        if label == "explicit":
                            for name, value in originals.items():
                                self.assertEqual((default / name).read_bytes(), value)
                    else:
                        self.assertNotEqual(returncode, 0)
                        self.assertEqual(blocked.read_bytes(), b"keep this ordinary file")
                        for name, value in payloads.items():
                            self.assertEqual((secrets / name).read_bytes(), value)
                            self.assertEqual((default / name).read_bytes(),
                                             f"synthetic-default-{name}-credential\n".encode())
                    self.assertFalse((home / "node" / ".deploy.lock").exists())
                    for value in payloads.values():
                        self.assertNotIn(value.decode().strip(), output_text)

    def test_wrapper_preserves_target_and_single_restart_never_retries_unknown(self):
        # Only this temporary sibling is executed by the copied wrapper. It
        # records invocation facts and never performs an update or restart.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "pf-node-deploy"
            wrapper.write_bytes((NODE / "pf-node-deploy").read_bytes())
            recorder = root / "pf-node"
            recorder.write_text(f"#!{sys.executable}\n" + """
import json, os, sys
with open(os.environ['FIXTURE_CALLS'], 'a') as output:
    output.write(json.dumps({'argv':sys.argv[1:], 'url':os.environ['PF_NODE_URL'],
                             'node_id':os.environ['PF_NODE_ID']})+'\\n')
sys.exit(int(os.environ.get('FIXTURE_UPDATE_EXIT','0')) if 'update_node' in sys.argv else 1)
""")
            recorder.chmod(0o700)
            log = root / "calls.jsonl"
            env = {"PATH": os.defpath, "PF_NODE_URL": "https://fixture.invalid/service",
                   "PF_NODE_ID": NODE_ID, "PF_NODE_TOKEN": "fixture-only",
                   "FIXTURE_CALLS": str(log)}
            result = subprocess.run(["/bin/zsh", str(wrapper), "--restart", "harness-ops@fixture"],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)  # The restart substitute reports unknown.
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([call["argv"] for call in calls], [
                ["--label", "deploy", "update_node", "--git", "harness-ops@fixture"], ["admin-restart"]])
            self.assertTrue(all(call["url"] == env["PF_NODE_URL"] and call["node_id"] == NODE_ID for call in calls))
            log.unlink()
            failed = subprocess.run(["/bin/zsh", str(wrapper), "--restart"],
                                    env={**env, "FIXTURE_UPDATE_EXIT": "7"}, capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(failed.returncode, 7)
            self.assertEqual(len(log.read_text().splitlines()), 1)

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path, self.headers.get("X-Expected-Node-ID")))
                data = json.dumps({"node_id": NODE_ID, "capabilities": ["node.target-v1"],
                                   "host": {"hostname": "fixture"}, "storage": {"data_root": "/fixture"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                requests.append((self.command, self.path, self.headers.get("X-Expected-Node-ID")))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.close_connection = True  # Receipt lost; no actual restart is performed.

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        env = {"PATH": os.defpath, "NO_PROXY": "*", "no_proxy": "*", "PF_NODE_URL": endpoint,
               "PF_NODE_ID": NODE_ID, "PF_NODE_TOKEN": "fixture-only"}
        result = subprocess.run([sys.executable, str(NODE / "pf-node"), "admin-restart"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(requests, [("GET", "/node", NODE_ID), ("POST", "/admin/restart", NODE_ID)])
        self.assertIn("重启结果未确认", result.stderr)
        self.assertIn("--url " + endpoint, result.stderr)
        self.assertIn("--node-id " + NODE_ID, result.stderr)
        self.assertNotIn("fixture-only", result.stderr)


if __name__ == "__main__":
    unittest.main()
