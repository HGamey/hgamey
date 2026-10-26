"""A copied service observes its updater without executing it or waking devices."""
import base64
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


NODE = Path(__file__).resolve().parents[1]
TOKEN = "isolated-update-status-token"
PASSWORD = "isolated-update-view-password"
SERVER = r'''
import json, os, sys, threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0, sys.argv[1])
import dash, node_update
from node_service import Jobs, make_handler, readonly_token
from node_read import NodeRead
from node_updates import NodeUpdates

install, root = Path(sys.argv[1]), Path(sys.argv[2])
def forbidden(*args, **kwargs):
    raise AssertionError("update status must not execute, inspect leases, schedule, or wake devices")
class NoDevices:
    handle = request = forbidden
class NoWake:
    set = forbidden
jobs = Jobs(str(root / "phonefarm"), {"update_node": (str(install / "update_node.sh"), str(install))})
node = NodeRead(jobs, jobs.data_root, str(install))
board = dash.Dash.__new__(dash.Dash)
board._pass = lambda: "isolated-update-view-password"
board._steer_pass = lambda: "unused-isolated-steer-password"
board.job_service = jobs
board.last_view, board.wake, board.monitor = 0, NoWake(), NoDevices()
# Route checks do not depend on the separately owned page content.
board._static = lambda h, name: board._send(h, 200, {"static": name})
jobs.dispatch.start = lambda: None  # No automatic workers in this read-only fixture.
base = make_handler(NoDevices(), jobs, "isolated-update-status-token", board, node)
jobs.dispatch.start = jobs.leases.snapshot = jobs.programs.refresh = forbidden
node_update.unresolved = forbidden  # History is neither scanned nor a status dependency.
calls = []
class Handler(base):
    def do_GET(self):
        calls.append(self.path)
        (root / "calls.json").write_text(json.dumps(calls))
        return super().do_GET()
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
custom = root / "custom-update.sh"
custom.write_text("#!/bin/sh\nexit 99\n")
custom.chmod(0o700)
custom_observer = NodeUpdates(jobs.update_layout, str(custom), install)
ready = {"port": server.server_port, "node_id": node.node_id, "layout": jobs.update_layout,
         "readonly_token": readonly_token("isolated-update-status-token"),
         "custom_updater": custom_observer.updater(), "pid": os.getpid()}
(root / "ready.tmp").write_text(json.dumps(ready))
(root / "ready.tmp").replace(root / "ready.json")
try:
    sys.stdin.buffer.read()  # The parent owns this process and closes its pipe.
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    jobs.directory.close()
    jobs.history.close()
    jobs.export_cache.close()
    jobs._read_root.close()
    (root / "closed.json").write_text(json.dumps({"server_thread_alive": thread.is_alive(),
                                                 "last_view": board.last_view, "pid": os.getpid()}))
'''


class NodeUpdatesTests(unittest.TestCase):
    def test_pinned_readonly_status_cli_and_startup_file_identity(self):
        with tempfile.TemporaryDirectory(prefix="node-updates-") as temporary:
            root = Path(temporary).resolve()
            install = root / "installed node"
            install.mkdir()
            # Only small code modules; no repository data or production settings.
            for source in NODE.glob("*.py"):
                shutil.copyfile(source, install / source.name)
            script = install / "update_node.sh"
            shutil.copy2(NODE / "update_node.sh", script)
            log = root / "server.log"
            with log.open("wb") as output:
                process = subprocess.Popen([sys.executable, "-c", SERVER, str(install), str(root)],
                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, cwd=root,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root),
                         "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
            try:
                until = time.monotonic() + 10
                while not (root / "ready.json").exists():
                    if process.poll() is not None or time.monotonic() >= until:
                        self.fail("isolated HTTP fixture did not start: " + log.read_text())
                    time.sleep(.02)
                ready = json.loads((root / "ready.json").read_text())
                pin = {"X-Expected-Node-ID": ready["node_id"]}
                view = {"Authorization": "Basic " + base64.b64encode((":" + PASSWORD).encode()).decode()}

                def request(path, headers=None, token=None):
                    conn = http.client.HTTPConnection("127.0.0.1", ready["port"], timeout=4)
                    try:
                        conn.request("GET", path, headers={"Authorization": "Bearer " + (token or ready["readonly_token"]),
                                                          **(headers or {})})
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        conn.close()

                def report():
                    code, value = request("/node-updates", pin)
                    self.assertEqual(code, 200, value)
                    return value

                self.assertEqual(request("/node-updates")[0], 400)
                self.assertEqual(request("/node-updates", {"X-Expected-Node-ID": "0" * 32})[0], 409)
                self.assertEqual(request("/node-updates", pin, token="incorrect")[0], 401)
                self.assertEqual(request("/node-updates?refresh=1", pin)[0], 400)
                value = report()
                self.assertEqual(value["schema"], "hgamey/node-updates@1")
                self.assertEqual(value["scope"], "host-only")
                self.assertEqual(value["layout"], ready["layout"])
                self.assertTrue(value["updater"]["available"], value)
                self.assertEqual(value["updater"]["identity_state"], "observed")
                self.assertEqual(value["maintenance"]["state"], "clear")
                self.assertFalse(value["maintenance"]["all_participants_verified"])
                self.assertFalse(ready["custom_updater"]["available"])
                self.assertEqual(ready["custom_updater"]["identity_state"], "unknown")
                code, browser = request("/dash/api/node-updates", {**view, **pin})
                self.assertEqual(code, 200, browser)
                self.assertEqual(browser["updater"], value["updater"])
                self.assertEqual(request("/dash/api/node-updates", view)[0], 400)
                self.assertEqual(request("/dash/api/node-updates", pin)[0], 401)
                for path in ("/dash/updates?expected_node_id=" + "0" * 32,
                             "/dash/static/updates.js", "/dash/static/updates.css"):
                    self.assertEqual(request(path, view)[0], 200)

                state = Path(ready["layout"]["state_dir"])
                state.mkdir(parents=True)
                marker = state / "maintenance.json"
                run_id = "a" * 32
                marker.write_text(json.dumps({"schema": "hgamey/node-update-maintenance@1", "run_id": run_id,
                                              "layout": ready["layout"], "at": time.time()}))
                self.assertEqual(report()["maintenance"]["run_id"], run_id)
                token_file = root / "readonly-token"
                token_file.write_text(ready["readonly_token"])
                token_file.chmod(0o600)
                cli = [sys.executable, str(NODE / "pf-node"), "--url", "http://127.0.0.1:" + str(ready["port"]),
                       "--node-id", ready["node_id"], "--token-file", str(token_file), "update-status", "--json"]
                result = subprocess.run(cli, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                received = json.loads(result.stdout)
                self.assertEqual(received["node_id"], ready["node_id"])
                self.assertEqual(received["schema"], value["schema"])
                self.assertEqual(received["scope"], "host-only")
                self.assertEqual(received["maintenance"]["state"], "blocked")
                self.assertEqual(json.loads((root / "calls.json").read_text())[-2:], ["/node", "/node-updates"])

                marker.write_text('{"run_id":"' + run_id + '","invalid":true}')
                invalid = report()["maintenance"]
                self.assertEqual(invalid["state"], "unknown")
                self.assertIsNone(invalid["run_id"])
                marker.unlink()
                replacement = install / "replacement.sh"
                replacement.write_bytes(script.read_bytes() + b"\n# isolated replacement\n")
                replacement.chmod(0o755)
                replacement.replace(script)
                changed = report()
                self.assertEqual(changed["maintenance"]["state"], "clear")
                self.assertFalse(changed["updater"]["available"])
                self.assertEqual(changed["updater"]["identity_state"], "stale")
                self.assertEqual(changed["updater"]["observed_at"], value["updater"]["observed_at"])
                self.assertEqual(report()["updater"], changed["updater"], "reading must not refresh the startup identity")
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    self.fail("owned HTTP fixture did not exit after EOF: " + log.read_text())
            self.assertEqual(process.returncode, 0, log.read_text())
            closed = json.loads((root / "closed.json").read_text())
            self.assertFalse(closed["server_thread_alive"])
            self.assertEqual(closed["last_view"], 0)
            self.assertEqual(closed["pid"], process.pid)


if __name__ == "__main__":
    unittest.main()
