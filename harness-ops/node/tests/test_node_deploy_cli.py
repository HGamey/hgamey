import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


NODE = Path(__file__).resolve().parents[1]
NODE_ID = "d" * 32


@unittest.skipUnless(Path("/bin/zsh").exists(), "deployment wrapper requires zsh")
class NodeDeployCLI(unittest.TestCase):
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
