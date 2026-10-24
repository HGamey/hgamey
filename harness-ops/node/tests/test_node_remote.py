import base64
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
import node_remote
from node_fleet import FleetState
from node_read import NodeRead
from node_service import Jobs, make_handler

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1080).to_bytes(4, "big") + (2400).to_bytes(4, "big")
       + b"\x08\x02\x00\x00\x00" + b"\x00" * 16)
# 假 adb：记录调用；按参数模拟输出、超时和大输出。
FAKE_ADB = r'''#!/usr/bin/env python3
import os, sys, time
log = os.environ["FAKE_ADB_LOG"]
open(log, "a").write(repr(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if a[:1] == ["devices"]:
    print("List of devices attached")
    print("emulator-5554          device product:sdk model:sdk_gphone64 transport_id:1")
    print("91253241019A           device usb:1 model:NX809J transport_id:2")
    print("PHYS1                  device usb:2 model:Other transport_id:3")
    sys.exit(0)
cmd = a[3] if len(a) > 3 else ""
if a[2:3] == ["exec-out"]:
    sys.stdout.buffer.write(open(os.environ["FAKE_PNG"], "rb").read()); sys.exit(0)
if cmd == "wm size":
    print("Physical size: 1080x2400"); sys.exit(0)
if cmd.startswith("big"):
    sys.stdout.write("x" * (1 << 20)); sys.exit(0)
if cmd.startswith("sleep"):
    time.sleep(5); sys.exit(0)
print("ok " + " ".join(a[2:])); sys.exit(0)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg, self.locks, self.leases = root / "cfg", root / "locks", root / "locks" / "devices"
        self.leases.mkdir(parents=True)
        adb = root / "adb"
        adb.write_text(FAKE_ADB)
        adb.chmod(0o755)
        (root / "shot.png").write_bytes(PNG)
        os.environ["FAKE_ADB_LOG"] = str(root / "adb.log")
        os.environ["FAKE_PNG"] = str(root / "shot.png")
        self.adb_log = root / "adb.log"
        self.remote = node_remote.Remote(lambda: (str(self.leases), str(self.locks)), adb=str(adb),
                                         config_dir=str(self.cfg), reaper=False)
        self.alice_token, _ = self.remote.operators.add("alice")
        self.bob_token, _ = self.remote.operators.add("bob")
        self.alice = self.remote.operators.verify(self.alice_token)
        self.bob = self.remote.operators.verify(self.bob_token)

    def tearDown(self):
        for s in list(self.remote.sessions.values()):
            self.remote._drop(s)
        self.tmp.cleanup()

    def legacy_devlock(self, label="selfsr-plain-A1", nested_serial=None):
        d = self.locks / "devlock.d"
        d.mkdir()
        (d / "owner").write_text(f"{label} since 2026-10-24 08:45:34\n")
        if nested_serial:
            n = self.locks / "phonefarm-lease.d"
            n.mkdir()
            owner = f"phonefarm:eval:{nested_serial} since 2026-10-24T08:45:35+00:00 token=1-2\n"
            (n / "owner").write_text(owner)
            (n / "phonefarm-lease.json").write_text(json.dumps(
                {"format": 1, "producer": "phonefarm", "pid": 1, "token": "1-2", "serial": nested_serial,
                 "label": "eval", "owner": owner}))

    def audit(self):
        return [json.loads(x) for x in (self.cfg / "remote.audit.jsonl").read_text().splitlines()]


class RemoteTests(Base):
    def test_tokens_are_hashed_and_revocable(self):
        stored = (self.cfg / "remote.operators.json").read_text()
        self.assertNotIn(self.alice_token, stored)
        self.assertIn(hashlib.sha256(self.alice_token.encode()).hexdigest(), stored)
        self.assertEqual(oct((self.cfg / "remote.operators.json").stat().st_mode & 0o777), "0o600")
        self.assertIsNone(self.remote.operators.verify(self.alice_token[:-1] + "x"))
        self.assertIsNone(self.remote.operators.verify("Bearer " + self.alice_token))
        self.remote.operators.revoke("alice")
        self.assertIsNone(self.remote.operators.verify(self.alice_token))
        self.assertEqual(self.remote.operators.verify(self.bob_token)["name"], "bob")

    def test_owner_devices_excluded_by_default_and_enable_is_explicit(self):
        p = self.remote.policy
        self.assertTrue(p.enabled("emulator-5554"))
        for s in ("91253241019A", "AVXB6R5C19000481", "192.168.3.53:5555", "PHYS1"):
            self.assertFalse(p.enabled(s), s)
        with self.assertRaises(node_remote.RemoteError) as e:
            self.remote.do(self.alice, "1.2.3.4", "91253241019A", "acquire", {})
        self.assertEqual(e.exception.status, 403)
        p.enable("91253241019A")
        self.assertTrue(p.enabled("91253241019A"))
        self.assertFalse(p.enabled("192.168.3.53:5555"), "an alias is not enabled implicitly")
        p.disable("91253241019A")
        self.assertFalse(p.enabled("91253241019A"))

    def test_experiment_holder_refuses_with_who_and_since(self):
        self.remote.policy.enable("PHYS1")
        self.legacy_devlock()
        with self.assertRaises(node_remote.RemoteError) as e:
            self.remote.do(self.alice, "1.2.3.4", "PHYS1", "acquire", {})
        self.assertEqual(e.exception.status, 409)
        self.assertIn("selfsr-plain-A1", e.exception.payload["error"])
        self.assertIn("2026-10-24 08:45:34", e.exception.payload["error"])
        # Emulators are configured to ignore label-only host locks, but not a lease naming them.
        self.assertFalse(self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})["reused"])
        rows = self.audit()
        self.assertEqual((rows[0]["op"], rows[0]["serial"], rows[0]["result"]["ok"]), ("alice", "PHYS1", False))
        self.assertEqual(rows[0]["ip"], "1.2.3.4")

    def test_lease_naming_device_blocks_even_ignored_global_and_midsession(self):
        self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})
        self.remote.do(self.alice, "ip", "emulator-5554", "tap", {"x": 1, "y": 2})
        self.legacy_devlock(nested_serial="emulator-5554")
        with self.assertRaises(node_remote.RemoteError) as e:
            self.remote.do(self.alice, "ip", "emulator-5554", "tap", {"x": 1, "y": 2})
        self.assertEqual(e.exception.status, 409)
        self.assertIn("eval", e.exception.payload["error"])

    def test_per_device_kernel_lock_is_shared_with_phonefarm(self):
        path = self.leases / (hashlib.sha256(b"emulator-5554").hexdigest() + ".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with self.assertRaises(node_remote.RemoteError) as e:
                self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})
            self.assertIn("phonefarm lease", e.exception.payload["error"])
        finally:
            os.close(fd)
        self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})
        fd = os.open(path, os.O_RDWR)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # phonefarm cannot lease it now
        os.close(fd)
        self.remote.do(self.alice, "ip", "emulator-5554", "release", {})
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)

    def test_sessions_exclude_other_operators_and_expire(self):
        self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {"max_minutes": 5})
        for action, params in (("acquire", {}), ("tap", {"x": 1, "y": 1}), ("release", {})):
            with self.assertRaises(node_remote.RemoteError) as e:
                self.remote.do(self.bob, "ip", "emulator-5554", action, params)
            self.assertIn("alice", e.exception.payload["error"])
        self.remote.sessions["emulator-5554"].beat -= node_remote.SESSION_TTL + 1
        self.remote.reap()
        self.assertNotIn("emulator-5554", self.remote.sessions)
        self.assertEqual(self.audit()[-1]["action"], "auto-release")
        self.remote.do(self.bob, "ip", "emulator-5554", "acquire", {})
        with self.assertRaises(node_remote.RemoteError):
            self.remote.do(self.alice, "ip", "emulator-5554", "shell", {"cmd": "id"})

    def test_actions_need_session_and_are_capped_and_audited(self):
        with self.assertRaises(node_remote.RemoteError) as e:
            self.remote.do(self.alice, "ip", "emulator-5554", "shell", {"cmd": "id"})
        self.assertTrue(e.exception.payload.get("need_session"))
        self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})
        out = self.remote.do(self.alice, "ip", "emulator-5554", "shell", {"cmd": "big"})
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["stdout"]), node_remote.OUTPUT_CAP)
        out = self.remote.do(self.alice, "ip", "emulator-5554", "shell", {"cmd": "sleep", "timeout": 1})
        self.assertTrue(out["timed_out"])
        for bad in ({"cmd": ""}, {"cmd": "id", "timeout": 999}):
            with self.assertRaises(node_remote.RemoteError):
                self.remote.do(self.alice, "ip", "emulator-5554", "shell", bad)
        with self.assertRaises(node_remote.RemoteError):
            self.remote.do(self.alice, "ip", "emulator-5554", "text", {"text": "a\nb"})
        self.remote.do(self.alice, "ip", "emulator-5554", "text", {"text": "it's a b"})
        calls = self.adb_log.read_text()
        self.assertIn("'-s', 'emulator-5554', 'shell', \"input text 'it'\\\\''s%sa%sb'\"", calls)
        with self.assertRaises(node_remote.RemoteError):
            self.remote.do(self.alice, "ip", "emulator-5554", "pull", {"path": "relative/x"})
        meta, data, _ = self.remote.do(self.alice, "ip", "emulator-5554", "screenshot", {"max": 0})
        self.assertEqual((meta["size"], data), ([1080, 2400], PNG))
        row = [r for r in self.audit() if r["action"] == "shell" and r["result"]["ok"]][0]
        self.assertEqual((row["op"], row["args"]["cmd"], row["result"]["truncated"]), ("alice", "big", True))
        self.assertNotIn("91253241019A", calls.replace("devices", ""))

    def test_rate_limit(self):
        self.remote.rate = node_remote.RateLimit(capacity=3, refill=0.001)
        self.remote.do(self.alice, "ip", "emulator-5554", "acquire", {})
        self.remote.do(self.alice, "ip", "emulator-5554", "tap", {"x": 1, "y": 1})
        with self.assertRaises(node_remote.RemoteError) as e:
            self.remote.do(self.alice, "ip", "emulator-5554", "shell", {"cmd": "id"})
        self.assertEqual(e.exception.status, 429)


class RemoteHttpTests(Base):
    """经过 node_service 的路由：令牌、MCP、网页 CSRF。"""

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.jobs = Jobs(str(root), {})
        self.board = dash.Dash.__new__(dash.Dash)
        self.pw = "unit-test-dash-password"
        self.board._pass = lambda: self.pw
        self.board.monitor = FleetState(str(root), str(root / "fl"), str(root / "shots"))
        self.admin = "unit-test-admin-credential-not-production"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            object(), self.jobs, self.admin, self.board, NodeRead(self.jobs, self.jobs.data_root, str(root)),
            remote=self.remote))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.jobs.directory.close()
        self.board.monitor.close()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        conn.close()
        return out

    def bearer(self, token, **extra):
        return {"Authorization": "Bearer " + token, "Content-Type": "application/json", **extra}

    def test_http_and_mcp_with_operator_token_only(self):
        self.assertEqual(self.req("GET", "/remote/devices", headers=self.bearer(self.admin))[0], 401)
        status, _, body = self.req("GET", "/remote/devices", headers=self.bearer(self.alice_token))
        devices = {d["serial"]: d for d in json.loads(body)["devices"]}
        self.assertTrue(devices["emulator-5554"]["enabled"])
        self.assertFalse(devices["91253241019A"]["enabled"])
        self.assertEqual(self.req("POST", "/remote/devices/emulator-5554/session", b"{}",
                                  self.bearer(self.alice_token))[0], 200)
        status, h, body = self.req("GET", "/remote/devices/emulator-5554/screenshot?max=0",
                                   headers=self.bearer(self.alice_token))
        self.assertEqual((status, h["X-Device-W"], body), (200, "1080", PNG))
        status, _, body = self.req("PUT", "/remote/devices/emulator-5554/install", b"PK\x03\x04apk",
                                   self.bearer(self.alice_token, **{"Content-Type": "application/octet-stream"}))
        self.assertEqual(json.loads(body)["exit"], 0)
        self.assertEqual(self.req("PUT", "/remote/devices/emulator-5554/install", b"x" * 10, self.bearer(
            self.alice_token, **{"Content-Type": "application/octet-stream", "X-Forwarded-Proto": "http"}))[0], 400)
        rpc = lambda m, p=None: json.loads(self.req("POST", "/remote/mcp", json.dumps(
            {"jsonrpc": "2.0", "id": 7, "method": m, "params": p or {}}).encode(), self.bearer(self.bob_token))[2])
        self.assertEqual(rpc("initialize")["result"]["serverInfo"]["name"], "hgamey-remote")
        names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"hgremote_acquire", "hgremote_shell", "hgremote_screenshot"} <= names)
        r = rpc("tools/call", {"name": "hgremote_acquire", "arguments": {"serial": "emulator-5554"}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("alice", r["content"][0]["text"])
        self.req("DELETE", "/remote/devices/emulator-5554/session", b"{}", self.bearer(self.alice_token))
        rpc("tools/call", {"name": "hgremote_acquire", "arguments": {"serial": "emulator-5554"}})
        r = rpc("tools/call", {"name": "hgremote_screenshot", "arguments": {"serial": "emulator-5554", "max": 0}})["result"]
        self.assertEqual(base64.b64decode(r["content"][0]["data"]), PNG)
        r = rpc("tools/call", {"name": "hgremote_push", "arguments": {
            "serial": "emulator-5554", "path": "/data/local/tmp/a.txt", "data_base64": base64.b64encode(b"hi").decode()}})
        self.assertFalse(r["result"]["isError"] if "result" in r else r["isError"])
        ops = {(x["op"], x["action"]) for x in self.audit()}
        self.assertTrue({("alice", "install"), ("bob", "screenshot"), ("bob", "push")} <= ops)

    def test_browser_needs_view_password_operator_cookie_and_csrf_header(self):
        view = {"Cookie": f"{dash.COOKIE}={self.board._cookie_val(self.pw)}", "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/dash/api/remote/login", json.dumps({"token": self.alice_token}).encode(),
                                  {"Content-Type": "application/json", "X-HG": "1"})[0], 401)
        self.assertEqual(self.req("POST", "/dash/api/remote/login", json.dumps({"token": self.alice_token}).encode(),
                                  view)[0], 400)
        status, h, _ = self.req("POST", "/dash/api/remote/login", json.dumps({"token": self.alice_token}).encode(),
                                {**view, "X-HG": "1"})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly; Secure; SameSite=Strict", h["Set-Cookie"])
        sid = h["Set-Cookie"].split(";")[0]
        web = {**view, "Cookie": view["Cookie"] + "; " + sid, "X-HG": "1"}
        self.assertEqual(self.req("POST", "/dash/api/remote/devices/emulator-5554/session", b"{}",
                                  {**web, "X-HG": ""})[0], 400)
        self.assertEqual(self.req("POST", "/dash/api/remote/devices/emulator-5554/session", b"{}", web)[0], 200)
        status, _, body = self.req("POST", "/dash/api/remote/devices/emulator-5554/key", b'{"key":"back"}', web)
        self.assertEqual((status, json.loads(body)["exit"]), (200, 0))
        self.remote.operators.revoke("alice")
        self.assertEqual(self.req("POST", "/dash/api/remote/devices/emulator-5554/key", b'{"key":"back"}', web)[0], 401)
        status, h, _ = self.req("GET", "/dash/control", headers={"Cookie": view["Cookie"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.req("GET", "/dash/control")[0], 303)


if __name__ == "__main__":
    unittest.main()
