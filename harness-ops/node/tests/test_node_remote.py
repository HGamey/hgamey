"""Gateway contracts with authoritative fake PF records, never raw device I/O.

PF's actual lease, command serialization and crash recovery are tested separately.
This fixture tests the node's routing/authentication and preservation of PF facts.
"""
import base64
import copy
import hashlib
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
from unittest import mock
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash
import node_remote
from node_read import NodeRead
from node_service import Jobs, make_handler
from remote_owner import OwnerError

SERIAL = "emulator-5554"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6h2UAAAAASUVORK5CYII=")


class FakePF:
    """Persist only the backend contract; do not emulate a device or lease."""
    available, reason = True, None
    input_contract = "frame-bound-v1"
    actions = ["screenshot", "tap", "swipe", "text", "key", "start_app", "stop_app", "renew", "release"]

    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {
            "refs": [], "descriptors": {}, "requests": {}, "receipts": {}}
        self.calls, self.lose_reply = [], set()
        self.starting = set()
        self.closed = False

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def load(self):
        self.calls.append(("load",))
        return copy.deepcopy(self.data["refs"])

    def start(self, serial, op, ip, max_seconds, acquisition_id, device_binding=None):
        self.calls.append(("start", serial, acquisition_id))
        ident = "ref-" + str(len(self.data["refs"]) + 1)
        ref = {"schema": "hgamey/remote-owner-ref@1", "id": ident, "serial": serial,
               "operator": op, "ip": ip, "created_at": time.time(), "max_seconds": max_seconds,
               "acquisition_id": acquisition_id}
        if device_binding is not None:
            ref["device_binding"] = copy.deepcopy(device_binding)
        self.data["refs"].append(ref)
        self.data["descriptors"][ident] = {
            "schema": "phonefarm/remote-control@1", "session_id": "session-" + ident,
            "serial": serial, "phase": "starting" if acquisition_id in self.starting else "ready", "live_owner": True,
            "capabilities": self.actions, "input_contract": self.input_contract,
            "input_frame_id": None, "frame_max_age_seconds": 30,
            "recovery": {"required": False}}
        if device_binding is not None:
            self.data["descriptors"][ident].update(device_binding=device_binding, lease_scope="device")
        self.save()
        return copy.deepcopy(ref)

    def status(self, ref):
        self.calls.append(("status", ref["id"]))
        return copy.deepcopy(self.data["descriptors"][ref["id"]])

    def submit(self, ref, request):
        rid = request["request_id"]
        self.calls.append(("submit", ref["id"], rid))
        key = ref["id"] + ":" + rid
        if key in self.data["requests"]:
            if self.data["requests"][key] != request:
                return {"error": "request ID already has different content"}
            return copy.deepcopy(self.data["receipts"][key])
        self.data["requests"][key] = copy.deepcopy(request)
        receipt = {"schema": "phonefarm/remote-control-action-result@1",
                   "session_id": request["session_id"], "request_id": rid,
                   "status": "completed", "result": {}, "error": None}
        descriptor = self.data["descriptors"][ref["id"]]
        if request["action"] == "release":
            # The node must expose pending/closing, not declare device handback.
            descriptor["phase"] = "closing"
            receipt["status"] = "accepted"
        elif request["action"] == "screenshot":
            receipt["result"] = {"mime": "image/png", "relative_path": "images/" + rid + ".png",
                                 "bytes": len(PNG), "original_size": [1, 1],
                                 "width": 1, "height": 1, "source_width": 1, "source_height": 1,
                                 "frame_id": rid, "session_id": request["session_id"],
                                 "serial": ref["serial"], "rotation": 1, "captured_at": time.time()}
            descriptor["input_frame_id"] = rid
            descriptor["latest_image"] = copy.deepcopy(receipt["result"])
        elif request["action"] in ("tap", "swipe"):
            # PF supplies these facts. No geometry, age or lease algorithm is
            # emulated by this gateway fixture.
            receipt["result"] = {"input_issued": True, "frame_id": request["params"]["frame_id"]}
            descriptor["input_frame_id"] = None
        elif request["action"] == "logcat":
            receipt["result"] = {"stdout": "fixture log excerpt", "stdout_truncated": True}
        elif request["action"] == "diagnostics":
            receipt["result"] = {"exit": 0, "stdout": "fixture diagnostic excerpt", "stdout_truncated": True}
        if rid in self.lose_reply:
            receipt.update(status="unknown", result=None, error="owner completion unknown")
            descriptor.update(phase="recovery_required", live_owner=False, recovery={"required": True})
        self.data["receipts"][key] = receipt
        self.save()
        if rid in self.lose_reply:
            raise OwnerError("helper response lost; query original request")
        return copy.deepcopy(receipt)

    def receipt(self, ref, request_id):
        self.calls.append(("receipt", ref["id"], request_id))
        return copy.deepcopy(self.data["receipts"][ref["id"] + ":" + request_id])

    def image(self, ref, receipt):
        self.calls.append(("image", ref["id"], receipt["request_id"]))
        if receipt["status"] != "completed" or receipt.get("result", {}).get("mime") != "image/png":
            raise OwnerError("no completed image")
        return PNG, "image/png"

    def close(self):
        self.closed = True

    def mutations(self):
        return [call for call in self.calls if call[0] in ("start", "submit")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg, self.locks, self.leases = self.root / "cfg", self.root / "locks", self.root / "devices"
        self.leases.mkdir()
        self.backend_path = self.root / "pf-records.json"
        self.backend = FakePF(self.backend_path)
        self.remotes = []
        self.remote = self.new_remote(self.backend)
        from node_agents import NodeAgents
        self.agents = NodeAgents(str(self.root), 'a' * 32)
        for name in ('alice', 'bob'):
            self.agents.create({'name': name})
            setattr(self, name + '_token', self.agents.issue_token(name)['token'])
            setattr(self, name, {'id': 'agent:' + name, 'name': name})

    def new_remote(self, backend):
        remote = node_remote.Remote(lambda: {"status": "clear", "entries": []},
                                    adb="/fixture-no-real-adb", config_dir=str(self.cfg),
                                    backend=backend)
        # Host transport inventory only; no tool/phone process is spawned here.
        inventory = [{"serial": SERIAL, "transport": "adb", "state": "device", "model": "fixture"},
                     {"serial": "fixture-phone", "transport": "adb", "state": "device", "model": "fixture"}]
        patcher = mock.patch.object(remote, "_enumerate", return_value=inventory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remotes.append(remote)
        return remote

    def tearDown(self):
        for remote in self.remotes:
            remote.close()
        self.agents.close()
        self.tmp.cleanup()

    def acquire(self, request_id="acquire-1", op=None, remote=None, **params):
        return (remote or self.remote).do(op or self.alice, "fixture-ip", SERIAL, "acquire",
                                         {"request_id": request_id, **params})

    def action(self, sid, rid, action="tap", op=None, **params):
        return self.remote.do(op or self.alice, "fixture-ip", SERIAL, action,
                              {"session_id": sid, "request_id": rid, **params})


class RemoteTests(Base):
    def test_raw_remote_session_does_not_block_another_phone(self):
        first = self.acquire("phone-a")
        second = self.remote.do(self.bob, "fixture-ip", "fixture-phone", "acquire",
                                {"request_id": "phone-b"})
        self.assertEqual((first["phase"], second["phase"]), ("ready", "ready"))
        self.assertEqual([call[1] for call in self.backend.calls if call[0] == "start"],
                         [SERIAL, "fixture-phone"])
        devices = {item["serial"]: item for item in self.remote.devices()["devices"]}
        for serial, acquired in ((SERIAL, first), ("fixture-phone", second)):
            self.assertEqual(devices[serial]["holder"]["session_id"], acquired["session_id"])
            self.assertEqual(devices[serial]["holder"]["device_scope"]["scope"], "device")

    def test_another_phone_lock_snapshot_does_not_reject_remote_acquisition(self):
        snapshot = {"status": "held", "entries": [{"scope": "device", "device_id": "b",
                    "serial": "fixture-phone", "label": "other-phone-task"}]}
        self.remote.occupancy = node_remote.Occupancy(lambda: snapshot)
        acquired = self.acquire("phone-a")
        self.assertEqual(acquired["phase"], "ready")
        self.assertEqual([call[1] for call in self.backend.calls if call[0] == "start"], [SERIAL])
        devices = {item["serial"]: item for item in self.remote.devices()["devices"]}
        self.assertEqual(devices[SERIAL]["holder"]["session_id"], acquired["session_id"])
        self.assertEqual(devices["fixture-phone"]["holder"]["label"], "other-phone-task")

    def test_inventory_places_a_device_holder_only_on_matching_aliases(self):
        snapshot = {"status": "held", "entries": [{"scope": "device", "device_id": "a",
            "serial": "usb-a", "label": "live-a"}], "device_scopes": {"devices": [
                {"device_id": "a", "aliases": ["usb-a", SERIAL]}]}}
        leases = mock.Mock()
        leases.snapshot.side_effect = lambda **kw: snapshot
        self.remote.occupancy = node_remote.Occupancy(node_remote.storage_from_leases(leases))
        devices = {item["serial"]: item for item in self.remote.devices()["devices"]}
        self.assertEqual(devices[SERIAL]["holder"]["label"], "live-a")
        self.assertFalse(devices[SERIAL]["holder"]["global"])
        self.assertIsNone(devices["fixture-phone"]["holder"])
        snapshot["entries"] = [{"scope": "global", "label": "live-global"}]
        self.assertTrue(all(item["holder"]["global"] for item in self.remote.devices()["devices"]))

    def test_remote_holder_aliases_come_from_pf_registry(self):
        first = self.acquire()
        self.remote.occupancy = node_remote.Occupancy(lambda: {'status': 'held',
            'entries': [{'scope': 'device', 'device_id': 'a', 'serial': SERIAL, 'label': 'owner'}],
            'device_scopes': {'devices': [{'device_id': 'a', 'aliases': [SERIAL, 'fixture-phone']}]}})
        devices = {item['serial']: item for item in self.remote.devices()['devices']}
        self.assertEqual(devices['fixture-phone']['holder']['kind'], 'remote')
        self.assertEqual(devices['fixture-phone']['holder']['session_id'], first['session_id'])
        self.assertEqual(devices[SERIAL]['aliases'], ['fixture-phone'])

    def test_occupancy_follows_current_fd_status_and_preserves_old_records(self):
        self.locks.mkdir()
        old = self.locks / "devlock.d"
        old.write_text('old fence and owner evidence')
        snapshot = {"status": "held", "entries": [{"scope": "global", "label": "fixture-owner"}]}
        leases = mock.Mock()
        leases.snapshot.side_effect = lambda **kw: snapshot
        self.remote.occupancy = node_remote.Occupancy(node_remote.storage_from_leases(leases))
        self.assertEqual(self.remote.occupancy.holders()[0]["label"], "fixture-owner")
        self.backend.starting.add("held")
        self.assertEqual(self.acquire("held")["phase"], "starting")
        self.assertEqual(self.remote.devices()["devices"][0]["holder"]["label"], "fixture-owner")
        snapshot.update(status="unknown", error="fd status unavailable", entries=[])
        with self.assertRaises(node_remote.RemoteError) as unknown:
            self.remote.occupancy.holders()
        self.assertEqual(unknown.exception.status, 503)
        self.assertEqual(self.remote.devices()["lock_storage_error"], "fd status unavailable")
        self.assertEqual(self.acquire("unknown")["phase"], "ready")
        snapshot.update(status="clear", error=None)
        self.assertEqual(self.remote.occupancy.holders(), [])
        self.assertEqual(self.acquire("clear")["phase"], "ready")
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 3)
        self.assertEqual(old.read_text(), 'old fence and owner evidence')

    def test_center_agent_tokens_are_hashed_and_revocable(self):
        stored = (self.root / '_node' / 'agents' / 'registry.json').read_text()
        self.assertEqual((self.root / '_node' / 'agents' / 'registry.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.alice_token, stored)
        self.assertIn(hashlib.sha256(self.alice_token.encode()).hexdigest(), stored)
        altered = self.alice_token[:-1] + ('x' if self.alice_token[-1] != 'x' else 'y')
        self.assertIsNone(self.agents.verify(altered))
        self.assertIsNone(self.agents.verify('Bearer ' + self.alice_token))
        self.agents.update('alice', {'revision': self.agents.get('alice')['revision'], 'enabled': False})
        self.assertIsNone(self.agents.verify(self.alice_token))
        self.assertEqual(self.agents.verify(self.bob_token), 'bob')
        self.assertEqual(self.backend.mutations(), [])

    def test_missing_pf_capability_still_does_not_launch(self):
        self.backend.available, self.backend.reason = False, 'observation owner capability unavailable'
        with self.assertRaises(node_remote.RemoteError) as unsupported:
            self.acquire()
        self.assertEqual(unsupported.exception.status, 503)
        self.assertEqual(self.backend.mutations(), [])

    def test_stable_ids_query_without_replay_and_closing_does_not_release(self):
        with self.assertRaises(node_remote.RemoteError) as missing:
            self.remote.do(self.alice, "fixture-ip", SERIAL, "acquire", {})
        self.assertEqual(missing.exception.status, 400)
        acquired = self.acquire()
        sid = acquired["session_id"]
        self.assertTrue(self.acquire()["reused"])
        with self.assertRaises(node_remote.RemoteError) as changed:
            self.acquire(max_minutes=1)
        self.assertEqual(changed.exception.status, 409)
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 1)
        for params in ({"x": 1, "y": 2}, {"session_id": sid, "x": 1, "y": 2},
                       {"request_id": "no-session", "x": 1, "y": 2}):
            with self.assertRaises(node_remote.RemoteError) as missing_action:
                self.remote.do(self.alice, "fixture-ip", SERIAL, "tap", params)
            self.assertEqual(missing_action.exception.status, 400)
        result = self.action(sid, "tap-1", x=1, y=2, frame_id="fixture-frame")
        before = self.backend.mutations()
        self.assertEqual(self.remote.query(self.alice, SERIAL, session_id=sid, request_id="tap-1"), result)
        self.assertEqual(self.backend.mutations(), before)
        with self.assertRaises(node_remote.RemoteError) as other_operator:
            self.remote.query(self.bob, SERIAL, session_id=sid, request_id="tap-1")
        self.assertEqual(other_operator.exception.status, 403)
        pending = self.action(sid, "release-1", "release")
        self.assertEqual(pending["status"], "accepted")
        self.assertNotIn("released", pending)
        state = self.remote.query(self.alice, SERIAL, acquisition_id="acquire-1")
        self.assertEqual(state["phase"], "closing")
        self.backend.starting.add("bob-acquire")
        self.assertEqual(self.acquire("bob-acquire", self.bob)["phase"], "starting")
        self.assertEqual(self.remote.query(self.alice, SERIAL, acquisition_id="acquire-1")["phase"], "closing")
        self.assertEqual(len(self.remote.sessions), 2)
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 2)

    def test_unknown_submit_and_node_restart_keep_saved_reference_and_receipt(self):
        acquired = self.acquire()
        sid = acquired["session_id"]
        self.backend.lose_reply.add("lost-1")
        result = self.action(sid, "lost-1", x=1, y=2, frame_id="fixture-frame")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["query_required"])
        self.assertEqual(result["request_id"], "lost-1")
        self.remote.close()
        records = self.backend_path.read_bytes()
        restarted_backend = FakePF(self.backend_path)
        restarted = self.new_remote(restarted_backend)
        self.assertEqual(self.backend_path.read_bytes(), records)
        state = restarted.query(self.alice, SERIAL, acquisition_id="acquire-1")
        self.assertEqual(state["phase"], "recovery_required")
        self.assertTrue(state["recovery"]["required"])
        receipt = restarted.query(self.alice, SERIAL, session_id=sid, request_id="lost-1")
        self.assertEqual((receipt["status"], receipt["request_id"]), ("unknown", "lost-1"))
        self.assertEqual(restarted_backend.mutations(), [])
        self.assertEqual(self.acquire("after-restart", self.bob, restarted)["phase"], "ready")
        self.assertEqual(restarted_backend.mutations(), [("start", SERIAL, "after-restart")])
        self.assertEqual(restarted.query(self.alice, SERIAL, acquisition_id="acquire-1"), state)
        self.assertEqual(restarted.query(self.alice, SERIAL, session_id=sid, request_id="lost-1"), receipt)
        self.assertEqual(len(restarted_backend.data["requests"]), 1)
        self.assertEqual(len(restarted.sessions), 2)


class RemoteHttpTests(Base):
    def setUp(self):
        super().setUp()
        self.jobs = Jobs(str(self.root / "jobs"), {})
        self.jobs.agents = self.agents
        self.board = dash.Dash.__new__(dash.Dash)
        self.pw = "unit-test-view-password"
        self.board._pass = lambda: self.pw
        self.board._steer_pass = lambda: "unit-test-steer"
        self.admin = "unit-test-node-admin-not-production"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            object(), self.jobs, self.admin, self.board,
            NodeRead(self.jobs, self.jobs.data_root, str(self.root)), remote=self.remote))
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(2)
        self.jobs.directory.close()
        super().tearDown()

    def req(self, method, path, value=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            conn.request(method, path, body=json.dumps(value).encode() if value is not None else None,
                         headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def bearer(self, token):
        return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def test_control_page_shows_each_raw_remote_holder_on_its_phone(self):
        """With the view password alone the page lists each phone (by its own serial, selected by platform reference)
        with the session holding it, and asks for the steer password before any action; there is no operator login."""
        self.board.wake = threading.Event()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            if os.environ.get("CI"):
                raise
            self.skipTest("playwright is not installed")
        self.acquire("alice-phone")
        self.remote.do(self.bob, "fixture-ip", "fixture-phone", "acquire", {"request_id": "bob-phone"})
        view = f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"
        base = "http://%s:%d" % self.server.server_address
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                if os.environ.get("CI"):
                    raise
                self.skipTest(f"no browser for playwright: {error}")
            try:
                context = browser.new_context()
                context.add_cookies([{"name": view.split("=", 1)[0], "value": view.split("=", 1)[1], "url": base}])
                page = context.new_page()
                errors, requests = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(base + "/dash/control")
                page.wait_for_function("() => document.querySelectorAll('#devices .dev-held').length === 2")
                rows = {row.locator(".dev-serial").inner_text(): row.locator(".dev-sub").inner_text()
                        for row in page.locator("#devices li").all()}
                self.assertIn("alice", rows[SERIAL])
                self.assertNotIn("bob", rows[SERIAL])
                self.assertIn("bob", rows["fixture-phone"])
                self.assertNotIn("alice", rows["fixture-phone"])
                self.assertNotIn("全域占用", " ".join(rows.values()))
                self.assertEqual(page.text_content("#op-login label"), "掌舵密码")
                self.assertTrue(page.is_visible("#op-login"))
                page.locator("#devices button").filter(has_text=SERIAL).click()
                self.assertIn("先用掌舵密码解锁", page.text_content("#operation-info"))
                self.assertFalse([url for url in requests if "/remote/login" in url])
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_control_page_can_release_starting_without_enabling_device_actions(self):
        from playwright.sync_api import sync_playwright
        self.board.wake = threading.Event()
        self.board.live = mock.Mock()
        self.board.live.known.return_value = False
        self.board.live.status.return_value = {"devices": [], "policy": {}}
        start = self.backend.start

        def waiting(*args, **kwargs):
            self.backend.starting.add(args[4])
            return start(*args, **kwargs)

        view = f"{dash.COOKIE}={self.board._cookie_val(self.pw)}"
        steer = f"{dash.STEER_COOKIE}={self.board._cookie_val(self.board._steer_pass(), b'hgamey-steer-v1')}"
        base = "http://%s:%d" % self.server.server_address
        with sync_playwright() as playwright, mock.patch.object(self.backend, "start", side_effect=waiting):
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context()
                context.add_cookies([{"name": c.split("=", 1)[0], "value": c.split("=", 1)[1], "url": base}
                                     for c in (view, steer)])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/dash/control")
                page.locator("#devices button").filter(has_text=SERIAL).click()
                page.locator("#take").click()
                page.wait_for_function("() => document.querySelector('#lease-info').textContent.includes('正在建立会话')")
                self.assertTrue(page.locator("#release").is_enabled())
                self.assertTrue(page.locator("#renew").is_disabled())
                self.assertTrue(page.locator("#ops").evaluate("element => element.disabled"))
                self.assertTrue(all(button.is_disabled() for button in page.locator("[data-action], [data-key]").all()))
                page.locator("#release").click()
                page.wait_for_function("() => document.querySelector('#lease-info').textContent.includes('正在交回')")
                requests = list(self.backend.data["requests"].values())
                self.assertEqual(len(requests), 1)
                self.assertEqual((requests[0]["action"], requests[0]["session_id"]), ("release", "session-ref-1"))
                self.assertTrue(page.locator("#release").is_disabled())
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_logcat_mapping_preserves_receipt_and_owner_capability(self):
        self.backend.actions = [*self.backend.actions, "logcat"]
        sid = self.acquire()["session_id"]
        auth = self.bearer(self.alice_token)
        path = f"/remote/devices/{SERIAL}"
        status, _, raw = self.req("GET", path + f"/logcat?session_id={sid}&request_id=logs&lines=80&pid=42", headers=auth)
        receipt = json.loads(raw)
        self.assertEqual((status, receipt["result"]),
                         (200, {"stdout": "fixture log excerpt", "stdout_truncated": True}))
        self.assertEqual(self.backend.data["requests"]["ref-1:logs"]["params"], {"lines": 80, "pid": 42})
        status, _, _ = self.req("POST", path + "/logcat", {"session_id": sid, "request_id": "default-logs"}, auth)
        self.assertEqual(status, 200)
        self.assertEqual(self.backend.data["requests"]["ref-1:default-logs"]["params"], {"lines": 200})
        before = self.backend.mutations()
        status, _, raw = self.req("GET", path + f"/requests/logs?session_id={sid}", headers=auth)
        self.assertEqual((status, json.loads(raw)), (200, receipt))
        self.assertEqual(self.req("POST", path + "/logcat", {
            "session_id": sid, "request_id": "bad-logs", "lines": 0}, auth)[0], 400)
        self.backend.data["descriptors"]["ref-1"]["capabilities"] = []
        status, _, raw = self.req("POST", path + "/logcat", {"session_id": sid, "request_id": "old-owner"}, auth)
        self.assertEqual((status, json.loads(raw)["code"]), (409, "action_unavailable"))
        self.assertEqual(self.backend.mutations(), before)
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 1)
        self.assertIn("hgremote_logcat", node_remote.MCP_ACTION)

    def test_diagnostics_mapping_preserves_owner_receipt_and_parameters(self):
        self.backend.actions = [*self.backend.actions, "diagnostics"]
        sid = self.acquire()["session_id"]
        auth = self.bearer(self.alice_token)
        path = f"/remote/devices/{SERIAL}"
        status, _, raw = self.req("GET", path + f"/diagnostics?session_id={sid}&request_id=base", headers=auth)
        receipt = json.loads(raw)
        self.assertEqual((status, receipt["result"]),
                         (200, {"exit": 0, "stdout": "fixture diagnostic excerpt", "stdout_truncated": True}))
        self.assertEqual(self.backend.data["requests"]["ref-1:base"]["params"], {})
        status, _, _ = self.req("POST", path + "/diagnostics", {
            "session_id": sid, "request_id": "app", "package": "org.example.game"}, auth)
        self.assertEqual(status, 200)
        self.assertEqual(self.backend.data["requests"]["ref-1:app"]["params"], {"package": "org.example.game"})
        before = self.backend.mutations()
        self.assertEqual(self.req("POST", path + "/diagnostics", {
            "session_id": sid, "request_id": "bad", "package": "org.example;id"}, auth)[0], 400)
        self.backend.data["descriptors"]["ref-1"]["capabilities"] = []
        status, _, raw = self.req("POST", path + "/diagnostics", {"session_id": sid, "request_id": "old-owner"}, auth)
        self.assertEqual((status, json.loads(raw)["code"]), (409, "action_unavailable"))
        status, _, raw = self.req("GET", path + f"/requests/base?session_id={sid}", headers=auth)
        self.assertEqual((status, json.loads(raw)), (200, receipt))
        self.assertEqual(self.backend.mutations(), before)
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 1)
        self.assertIn("hgremote_diagnostics", node_remote.MCP_ACTION)

    def test_remote_device_lanes_keep_original_binding_and_scope(self):
        from test_device_scope import bound, DOMAIN

        self.backend.device_binding_available = True
        a, alias, b = bound("A", SERIAL), bound("A", "emulator-alias"), bound("B", "fixture-phone")
        resolver = mock.Mock(side_effect=lambda serial: {a["serial"]: a, alias["serial"]: alias, b["serial"]: b}[serial])
        self.remote.device_scopes = mock.Mock(resolve=resolver)
        current = {"status": "held", "storage": {"readiness": "ready", "layout_version": 2, "domain_id": DOMAIN},
            "device_scopes": {"schema": "phonefarm/device-scopes@1", "domain_id": DOMAIN,
                "registry_revision": 1, "global": {"status": "clear", "error": None},
                "queue_waiting": False, "unknown": False,
                "devices": [{"device_id": key, "status": "clear", "error": None} for key in ("A", "B")]}}
        self.remote.leases = mock.Mock(snapshot=lambda **kw: copy.deepcopy(current))
        auth = {**self.bearer(self.alice_token), "X-Expected-Node-ID": self.jobs.node_id}
        path = "/remote/devices/"
        code, _, raw = self.req("GET", path + "resolve?serial=" + SERIAL, headers=auth)
        self.assertEqual((code, json.loads(raw)), (200, {"node_id": self.jobs.node_id, "binding": a}))

        def acquire(key, value, serial=None):
            body = {"request_id": key, **({"device_binding": value} if value else {})}
            code, _, raw = self.req("POST", path + (serial or value["serial"]) + "/session", body, auth)
            return code, json.loads(raw)

        current["device_scopes"]["registry_revision"] = 2
        resolver.side_effect = AssertionError("saved binding must not be re-resolved at admission")
        code, first = acquire("lane-a", a); self.assertEqual(code, 200, first)
        current["device_scopes"]["devices"][0]["status"] = "held"
        code, second = acquire("lane-b", b); self.assertEqual(code, 200, second)
        self.backend.starting.update(("alias-a", "raw-b"))
        with mock.patch.object(self.remote, "_enumerate", return_value=[
                {"serial": value["serial"], "transport": "adb", "state": "device"} for value in (a, alias, b)]):
            code, pending_alias = acquire("alias-a", alias)
        self.assertEqual((code, pending_alias["phase"]), (202, "starting"))
        code, pending_raw = acquire("raw-b", None, b["serial"])
        self.assertEqual((code, pending_raw["phase"]), (202, "starting"))
        code, _, raw = self.req("GET", path.rstrip("/"), headers=auth)
        devices = {d["serial"]: d for d in json.loads(raw)["devices"]}
        self.assertEqual(devices[self.jobs.node_id + "/" + SERIAL]["holder"]["device_scope"], {"scope": "device", "binding": a})
        self.assertEqual(devices[self.jobs.node_id + "/" + b["serial"]]["holder"]["session_id"], second["session_id"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.backend.device_binding_available = False
        resolver.side_effect = AssertionError("old acquisition must not re-resolve")
        self.assertEqual(acquire("lane-a", a)[1]["session_id"], first["session_id"])
        self.assertEqual(acquire("lane-a", {**a, "registry_revision": 2})[0], 409)
        self.assertEqual(sum(call[0] == "start" for call in self.backend.calls), 4)
        # A saved binding also survives a gateway restart and missing new capability.
        restarted = self.new_remote(self.backend)
        self.assertEqual(restarted.query(self.alice, SERIAL, acquisition_id="lane-a")["device_binding"], a)
        for descriptor in self.backend.data["descriptors"].values():
            descriptor["phase"] = "closed"
        code, legacy = acquire("raw-a", None, SERIAL)
        self.assertEqual(code, 200, legacy)
        self.assertNotIn("device_binding", self.backend.data["refs"][-1])

    def test_frame_bound_http_cli_preserves_metadata_and_never_retries_unknown_input(self):
        auth = self.bearer(self.alice_token)
        path = f"/remote/devices/{SERIAL}"
        status, _, raw = self.req("POST", path + "/session", {"request_id": "frame-owner"}, auth)
        self.assertEqual(status, 200)
        acquired = json.loads(raw)
        sid = acquired["session_id"]
        self.assertEqual(acquired["input_contract"], "frame-bound-v1")
        self.assertEqual(acquired["frame_max_age_seconds"], 30)
        credential = self.root / "operator.credential"
        credential.write_text(self.alice_token)
        credential.chmod(0o600)

        def cli(*args):
            return subprocess.run([
                sys.executable, str(Path(__file__).resolve().parents[1] / "pf-remote"),
                "--url", f"http://127.0.0.1:{self.server.server_port}",
                "--node-id", self.jobs.node_id, "--token-file", str(credential), *args,
            ], env={"PATH": os.defpath, "NO_PROXY": "*", "no_proxy": "*"},
                capture_output=True, text=True, timeout=5)

        def action(rid, name, *args):
            return cli("action", SERIAL, name, "--session-id", sid, "--request-id", rid, *args)

        captured = action("frame-1", "screenshot", "--params", '{"max_side":1}',
                          "--out", str(self.root / "frame.png"))
        self.assertEqual(captured.returncode, 0, captured.stderr + captured.stdout)
        self.assertEqual((self.root / "frame.png").read_bytes(), PNG)
        image = json.loads(captured.stdout)["result"]
        self.assertEqual({key: image[key] for key in (
            "frame_id", "session_id", "serial", "rotation", "source_width", "source_height",
            "width", "height", "original_size")}, {
            "frame_id": "frame-1", "session_id": sid, "serial": SERIAL, "rotation": 1,
            "source_width": 1, "source_height": 1, "width": 1, "height": 1, "original_size": [1, 1]})
        self.assertIsInstance(image["captured_at"], (int, float))
        status, _, raw = self.req("GET", path + f"/session?session_id={sid}", headers=auth)
        self.assertEqual((status, json.loads(raw)["input_frame_id"]), (200, "frame-1"))

        tapped = action("frame-tap", "tap", "--frame-id", "frame-1", "--params", '{"x":0,"y":0}')
        self.assertEqual(tapped.returncode, 0, tapped.stderr + tapped.stdout)
        request = self.backend.data["requests"]["ref-1:frame-tap"]
        self.assertEqual(request["params"], {"x": 0, "y": 0, "frame_id": "frame-1"})
        before = self.backend.mutations()
        queried = cli("receipt", SERIAL, "frame-tap", "--session-id", sid)
        self.assertEqual(queried.returncode, 0, queried.stderr + queried.stdout)
        self.assertEqual(json.loads(queried.stdout), json.loads(tapped.stdout))
        conflict = action("conflicting-frame", "tap", "--frame-id", "frame-1",
                          "--params", '{"x":0,"y":0,"frame_id":"other-frame"}')
        self.assertNotEqual(conflict.returncode, 0)
        self.assertEqual(self.backend.mutations(), before)

        # A new helper's capability cannot retrofit a persisted old owner.
        descriptor = self.backend.data["descriptors"]["ref-1"]
        descriptor.pop("input_contract")
        self.backend.save()
        self.assertEqual(self.remote.capabilities()["input_contract"], "frame-bound-v1")
        status, _, raw = self.req("POST", path + "/tap", {
            "session_id": sid, "request_id": "old-owner-input", "frame_id": "frame-1", "x": 0, "y": 0}, auth)
        rejected = json.loads(raw)
        self.assertEqual((status, rejected["code"]), (409, "input_contract_unavailable"))
        self.assertEqual((rejected["request_id"], rejected["session_id"]), ("old-owner-input", sid))
        status, _, raw = self.req("GET", path + f"/requests/frame-tap?session_id={sid}", headers=auth)
        self.assertEqual((status, json.loads(raw)), (200, json.loads(tapped.stdout)))
        status, headers, data = self.req("GET", path + f"/requests/frame-1?session_id={sid}&image=1", headers=auth)
        self.assertEqual((status, data), (200, PNG))
        self.assertEqual(json.loads(headers["X-Meta"])["result"], image)
        self.assertEqual(self.backend.mutations(), before)
        descriptor["input_contract"] = "frame-bound-v1"
        self.backend.save()

        second = action("frame-2", "screenshot", "--params", '{"max_side":1}')
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.backend.lose_reply.add("uncertain-swipe")
        uncertain = action("uncertain-swipe", "swipe", "--frame-id", "frame-2", "--params",
                           '{"x1":0,"y1":0,"x2":0,"y2":0,"ms":1}')
        self.assertEqual(uncertain.returncode, 4, uncertain.stderr + uncertain.stdout)
        value = json.loads(uncertain.stdout)
        self.assertEqual((value["status"], value["request_id"]), ("unknown", "uncertain-swipe"))
        self.assertTrue(value["query_required"])
        self.assertEqual(self.backend.data["requests"]["ref-1:uncertain-swipe"]["params"], {
            "frame_id": "frame-2", "x1": 0, "y1": 0, "x2": 0, "y2": 0, "ms": 1})
        before = self.backend.mutations()
        found = cli("receipt", SERIAL, "uncertain-swipe", "--session-id", sid)
        self.assertEqual(found.returncode, 4, found.stderr + found.stdout)
        self.assertEqual(json.loads(found.stdout)["status"], "unknown")
        self.assertEqual(self.backend.mutations(), before)
        self.assertEqual([c[2] for c in self.backend.calls if c[0] == "submit"],
                         ["frame-1", "frame-tap", "frame-2", "uncertain-swipe"])

    def test_http_mcp_query_pending_and_unsupported_upload_without_body_read(self):
        auth = self.bearer(self.alice_token)
        self.assertEqual(self.req("GET", "/remote/devices", headers=self.bearer(self.admin))[0], 200)
        status, _, raw = self.req("GET", "/remote/devices", headers=auth)
        self.assertEqual(status, 200)
        devices = {item["serial"]: item for item in json.loads(raw)["devices"]}
        self.assertIn(self.jobs.node_id + "/" + SERIAL, devices)
        self.assertIn(self.jobs.node_id + "/fixture-phone", devices)
        status, _, raw = self.req("POST", f"/remote/devices/{SERIAL}/session", {"request_id": "http-acquire"}, auth)
        self.assertEqual(status, 200)
        sid = json.loads(raw)["session_id"]
        _, _, queried = self.req("GET", f"/remote/devices/{SERIAL}/session?acquisition_id=http-acquire", headers=auth)
        self.assertEqual(json.loads(queried)["session_id"], sid)
        status, headers, data = self.req("POST", f"/remote/devices/{SERIAL}/screenshot",
            {"session_id": sid, "request_id": "shot-1", "max_side": 0}, auth)
        self.assertEqual((status, headers["X-Device-W"], data), (200, "1", PNG))
        before = self.backend.mutations()
        status, _, data = self.req("GET", f"/remote/devices/{SERIAL}/requests/shot-1?session_id={sid}&image=1", headers=auth)
        self.assertEqual((status, data), (200, PNG))
        self.assertEqual(self.backend.mutations(), before)

        # A request with a declared upload body but no sent bytes must fail now,
        # rather than blocking to read or forward any legacy upload.
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            conn.putrequest("PUT", f"/remote/devices/{SERIAL}/install")
            conn.putheader("Authorization", "Bearer " + self.alice_token)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", "1024")
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 501)
            response.read()
        finally:
            conn.close()
        self.assertEqual(self.backend.mutations(), before)

        def rpc(method, params=None, token=None):
            status, _, body = self.req("POST", "/remote/mcp", {"jsonrpc": "2.0", "id": 7,
                "method": method, "params": params or {}}, self.bearer(token or self.alice_token))
            self.assertEqual(status, 200)
            return json.loads(body)

        self.assertEqual(rpc("initialize")["result"]["serverInfo"]["name"], "hgamey-remote")
        names = {tool["name"] for tool in rpc("tools/list")["result"]["tools"]}
        self.assertTrue({"hgremote_acquire", "hgremote_status", "hgremote_screenshot"} <= names)
        self.assertTrue({"hgremote_shell", "hgremote_install", "hgremote_push"}.isdisjoint(names))
        self.backend.starting.add("bob-attempt")
        result = rpc("tools/call", {"name": "hgremote_acquire", "arguments": {
            "serial": SERIAL, "request_id": "bob-attempt"}}, self.bob_token)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"])["phase"], "starting")
        before = self.backend.mutations()
        result = rpc("tools/call", {"name": "hgremote_status", "arguments": {
            "serial": SERIAL, "session_id": sid, "request_id": "shot-1"}})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"])["status"], "completed")
        self.assertEqual(self.backend.mutations(), before)

        status, _, body = self.req("DELETE", f"/remote/devices/{SERIAL}/session",
            {"session_id": sid, "request_id": "http-release"}, auth)
        self.assertEqual((status, json.loads(body)["status"]), (202, "accepted"))
        status, _, body = self.req("GET", f"/remote/devices/{SERIAL}/session?session_id={sid}", headers=auth)
        self.assertEqual((status, json.loads(body)["phase"]), (202, "closing"))

    def test_browser_uses_view_and_steer_and_revoked_agents_cannot_write(self):
        path = f'/dash/api/remote/devices/{SERIAL}'
        view = {'Cookie': f'{dash.COOKIE}={self.board._cookie_val(self.pw)}', 'Content-Type': 'application/json'}
        data = {'request_id': 'web-acquire'}
        self.assertEqual(self.req('POST', path + '/session', data, {'X-HG': '1'})[0], 401)
        self.assertEqual(self.req('POST', path + '/session', data, {**view, 'X-HG': '1'})[0], 403)
        web = {**view, 'X-HG': '1', 'Cookie': view['Cookie'] + '; hg_steer=' +
               self.board._cookie_val('unit-test-steer', b'hgamey-steer-v1')}
        self.assertEqual(self.req('POST', path + '/session', data, {**web, 'X-HG': ''})[0], 400)
        self.assertEqual(self.backend.mutations(), [])
        status, _, body = self.req('POST', path + '/session', data, web)
        self.assertEqual(status, 200, body)
        sid = json.loads(body)['session_id']
        payload = {'session_id': sid, 'request_id': 'web-back', 'key': 'back'}
        status, _, body = self.req('POST', path + '/key', payload, web)
        self.assertEqual((status, json.loads(body)['status']), (200, 'completed'))
        self.assertEqual(next(iter(self.backend.data['requests'].values()))['params'], {'key': 4})
        auth = self.bearer(self.alice_token)
        self.assertEqual(self.req('GET', '/remote/devices', headers=auth)[0], 200)
        before = self.backend.mutations()
        self.agents.update('alice', {'revision': self.agents.get('alice')['revision'], 'enabled': False})
        self.assertEqual(self.req('POST', f'/remote/devices/{SERIAL}/session', {'request_id': 'revoked'}, auth)[0], 401)
        self.assertEqual(self.backend.mutations(), before)
        self.assertEqual(self.req('GET', '/dash/control', headers={'Cookie': view['Cookie']})[0], 200)
        self.assertEqual(self.req('GET', '/dash/control')[0], 303)


if __name__ == "__main__":
    unittest.main()
