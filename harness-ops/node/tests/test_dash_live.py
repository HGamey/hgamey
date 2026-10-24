"""Live viewers share PF observations and never bypass a busy device domain."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dash_live import CaptureError, LiveHub

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


if __name__ == "__main__":
    unittest.main()
