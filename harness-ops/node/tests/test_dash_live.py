"""Live viewers share PF observations and never bypass a busy device domain."""
import sys
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import Mock
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash_live import CaptureError, LiveHub, PFBackend, handle
from node_read import NodeRead
from node_service import Jobs, make_handler, readonly_token

JPEG = b"\xff\xd8frame\xff\xd9"
CAPTURED_AT = 1_700_000_000.125


def clear():
    return {"status": "free", "queue": [],
            "lease": {"status": "clear", "entries": []}}


def until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        wake.wait(.005)
    return predicate()


class FakeBackend:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or [(JPEG, CAPTURED_AT)]
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.lock = threading.Lock()
        self.calls, self.active, self.max_active = [], 0, 0

    def devices(self):
        return [{"serial": "emu", "state": "device"}]

    def snapshot(self, serial):
        with self.lock:
            index = len(self.calls)
            self.calls.append(serial)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if not self.release.wait(2):
                raise AssertionError("fixture sample was not released")
            result = self.outcomes[min(index, len(self.outcomes) - 1)]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            with self.lock:
                self.active -= 1


class HubTests(unittest.TestCase):
    def hub(self, lock_fn, backend):
        return LiveHub(lock_fn, backend, free_grace_s=0, linger_s=.15,
                       policy_every_s=.01, interval_s=.01)

    def stop(self, stream, backend, *gates):
        # Even a failed assertion must leave no fixture collector thread behind.
        backend.release.set()
        for gate in gates:
            gate.set()
        with stream.cond:
            while stream.viewers:
                stream.leave()
            stream.hub.linger_s = 0
            stream.cond.notify_all()
            thread = stream.thread
        if thread:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "fixture collector did not stop")

    def test_viewers_share_sample_and_last_leave_prevents_next_capture(self):
        backend = FakeBackend()
        backend.release.clear()
        checked_again, finish_check = threading.Event(), threading.Event()
        checks = 0

        def lock_state():
            nonlocal checks
            checks += 1
            if checks > 1:
                checked_again.set()
                if not finish_check.wait(2):
                    raise AssertionError("fixture advisory check was not released")
            return clear()

        hub = self.hub(lock_state, backend)
        first = hub.open("emu")
        self.addCleanup(self.stop, first, backend, finish_check)
        self.assertTrue(backend.entered.wait(1))
        second = hub.open("emu")
        self.assertIs(first, second)
        self.assertEqual(backend.calls, ["emu"])
        first.leave()
        self.assertEqual(second.viewers, 1)
        backend.release.set()
        frame = second.wait_frame(0, 1)
        self.assertIsNotNone(frame)
        self.assertEqual((frame.jpeg, frame.at, frame.seq), (JPEG, CAPTURED_AT, 1))

        # The last viewer can leave while an advisory check is in progress.
        # Linger retains the cache/thread, not permission to start another sample.
        self.assertTrue(checked_again.wait(1))
        second.leave()
        finish_check.set()
        thread = second.thread
        if thread:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(backend.calls, ["emu"])
        self.assertEqual(backend.max_active, 1)
        self.assertEqual(second.frame, frame)
        self.assertEqual(second.status()["frame_at"], CAPTURED_AT)
        self.assertEqual(second.viewers, 0)

    def test_held_unknown_recovery_and_queue_never_call_backend(self):
        states = {
            # A different alias/serial is still in the current global domain.
            "other_alias": {"status": "held", "held_by": "owner@USB-ALIAS",
                            "lease": {"status": "held", "entries": [{"serial": "USB-ALIAS"}]}},
            "unknown": {"status": "unknown", "lease": {"status": "unknown"}},
            "recovery": {"status": "unknown", "lease": {"status": "blocked"}},
            "queue": {**clear(), "queue": [{"label": "waiting-writer"}]},
            "unreadable": OSError("lease snapshot unavailable"),
        }
        for name, state in states.items():
            with self.subTest(name=name):
                backend, checked = FakeBackend(), threading.Event()

                def lock_state():
                    checked.set()
                    if isinstance(state, Exception):
                        raise state
                    return state

                stream = self.hub(lock_state, backend).open("emu")
                try:
                    self.assertTrue(checked.wait(1))
                    self.assertTrue(until(lambda: stream.mode in ("deferred", "unknown")))
                    self.assertEqual(backend.calls, [])
                    self.assertIsNone(stream.frame)
                    self.assertEqual(stream.seq, 0)
                finally:
                    self.stop(stream, backend)

    def test_pf_deferred_after_clear_keeps_original_frame_and_time(self):
        backend = FakeBackend([(JPEG, CAPTURED_AT),
                               CaptureError("deferred", "writer acquired the observation domain")])
        stream = self.hub(clear, backend).open("emu")
        self.addCleanup(self.stop, stream, backend)
        frame = stream.wait_frame(0, 1)
        self.assertIsNotNone(frame)
        self.assertTrue(until(lambda: stream.mode == "deferred"))
        self.assertEqual(backend.calls, ["emu", "emu"])
        self.assertEqual(backend.max_active, 1)
        self.assertEqual(stream.frame, frame)
        self.assertEqual(stream.seq, 1)
        self.assertEqual(stream.status()["frame_at"], CAPTURED_AT)
        self.assertIn("writer acquired", stream.status()["error"])

    def test_bound_capture_keeps_scope_cache_and_legacy_global(self):
        domain = "a" * 32
        bound = {"schema": "phonefarm/device-binding@1", "domain_id": domain,
                 "registry_revision": 1, "device_id": "B", "serial": "USB-B"}
        lease = {"status": "held", "entries": [{"label": "A task"}],
                 "storage": {"readiness": "ready", "layout_version": 2, "domain_id": domain},
                 "device_scopes": {"schema": "phonefarm/device-scopes@1", "domain_id": domain,
                    "registry_revision": 1, "unknown": False, "queue_waiting": False,
                    "global": {"status": "clear", "error": None}, "devices": [
                        {"device_id": "A", "status": "held", "error": None},
                        {"device_id": "B", "status": "clear", "error": None}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / "phonefarm"
            exe.write_text(f"#!{sys.executable}\n" + '''import json, pathlib, sys
a = sys.argv[1:]
if a == ["screen", "capabilities", "--json"]:
    print(json.dumps({"schema":"phonefarm/screen-capabilities@1", "capture_v1":True,
                      "ownership":"observation-v1", "device_binding_v1":True}))
else:
    binding = json.loads(a[a.index("--device-binding") + 1])
    out = pathlib.Path(a[a.index("--out") + 1]); out.mkdir()
    image = out / "capture.jpg"; image.write_bytes(b"\\xff\\xd8frame\\xff\\xd9")
    with (pathlib.Path(__file__).parent / "calls").open("a") as log:
        log.write(json.dumps(binding) + "\\n")
    print(json.dumps({"schema":"phonefarm/screen-capture@1", "state":"ready",
        "serial": a[a.index("--serial") + 1], "ownership":"observation-v1",
        "jpeg_path":str(image), "captured_at_ms":1700000000125,
        "device_binding":binding, "lease_scope":"device"}))
''')
            exe.chmod(0o755)
            backend = PFBackend(SimpleNamespace(request=lambda **kw: {"devices": None}),
                                str(exe), directory, None, root / "frames")
            scopes = SimpleNamespace(admit=Mock())
            hub = LiveHub(lambda: {"status": "held", "lease": lease}, backend,
                          device_scopes=scopes, free_grace_s=0, linger_s=60,
                          policy_every_s=.01, interval_s=60)
            responses = []
            dash = SimpleNamespace(live=hub, _send=lambda h, *args, **kw: responses.append((args, kw)))
            query = urlencode({"serial": "USB-B", "device_binding": json.dumps(bound)})
            try:
                handle(dash, None, ["api", "live", "status"], query)
                self.assertEqual(responses[-1][0][1]["devices"][0]["state"], "unknown")
                scopes.admit.assert_not_called()
                self.assertFalse(hub.streams)
                handle(dash, None, ["api", "live", "frame"], query)
                self.assertEqual(responses[-1][0][:2], (200, JPEG))
                original = hub.streams[hub.key("USB-B", bound)]
                self.assertEqual((original.frame.seq, original.frame.at), (1, CAPTURED_AT))
                handle(dash, None, ["api", "live", "status"], query)
                self.assertEqual(responses[-1][0][1]["devices"][0]["device_binding"], bound)
                scopes.admit.assert_called_once_with(bound)
                self.assertEqual([json.loads(x) for x in (root / "calls").read_text().splitlines()], [bound])

                legacy = hub.open("USB-B")
                alias = hub.open("USB-A-alias", {**bound, "device_id": "A", "serial": "USB-A-alias"})
                self.assertTrue(until(lambda: legacy.mode == alias.mode == "deferred"))
                self.assertIsNone(legacy.frame)
                self.assertIsNone(alias.frame)
                self.assertNotIn("device_binding", legacy.status())
                revised = {**bound, "registry_revision": 2}
                lease["device_scopes"]["registry_revision"] = 2
                newer = hub.open("USB-B", revised)
                self.assertIsNot(newer, original)
                self.assertIsNotNone(newer.wait_frame(0, 1))
                self.assertEqual(original.frame.at, CAPTURED_AT)
                self.assertEqual([json.loads(x) for x in (root / "calls").read_text().splitlines()], [bound, revised])

                # Node-token clients use the same stream/cache without a dash login.
                jobs = Jobs(str(root), {})
                node = NodeRead(jobs, jobs.data_root, directory)
                board = SimpleNamespace(live=hub, _send=lambda h, *a, **kw: h._send(*a, **kw))
                admin = "isolated-live-token"
                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, jobs, admin, board, node))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def request(operation, *, token=readonly_token(admin), pin=node.node_id):
                    connection = http.client.HTTPConnection(*server.server_address, timeout=4)
                    headers = {"Authorization": "Bearer " + token}
                    if pin is not None:
                        headers["X-Expected-Node-ID"] = pin
                    connection.request("GET", "/live/" + operation + "?" + query, headers=headers)
                    response = connection.getresponse()
                    result = response.status, dict(response.getheaders()), response.read()
                    connection.close()
                    return result

                try:
                    self.assertEqual(request("status", token="wrong")[0], 401)
                    self.assertEqual(request("status", pin=None)[0], 400)
                    self.assertEqual(request("status", pin="f" * 32)[0], 409)
                    code, _, body = request("status")
                    self.assertEqual(code, 200)
                    self.assertEqual(json.loads(body)["devices"][0]["device_binding"], bound)
                    code, headers, body = request("frame")
                    self.assertEqual((code, body), (200, JPEG))
                    self.assertEqual(headers["X-Frame-At"], f"{CAPTURED_AT:.3f}")
                    self.assertEqual(len((root / "calls").read_text().splitlines()), 2)
                    server.RequestHandlerClass = make_handler(None, jobs, admin, node=node)
                    self.assertEqual(request("status")[0], 503)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                    jobs.directory.close()
            finally:
                hub.linger_s = 0
                for stream in hub.streams.values():
                    with stream.cond:
                        while stream.viewers:
                            stream.leave()
                        stream.cond.notify_all()
                        thread = stream.thread
                    if thread:
                        thread.join(2)
                        self.assertFalse(thread.is_alive(), "fixture collector did not stop")


if __name__ == "__main__":
    unittest.main()
