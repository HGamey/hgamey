import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dash_live
from dash_live import LiveBusy, LiveHub, fit_size, lock_for, split_jpegs

JPEG = b"\xff\xd8frame\xff\xd9"


class FakeCapture:
    def __init__(self, backend, serial, on_frame):
        self.backend, self.size, self.dead, self.stopped = backend, (1080, 2400), False, False
        on_frame(JPEG)

    def alive(self):
        return not self.dead

    def stop(self):
        self.stopped = True


class FakeBackend:
    def __init__(self):
        self.captures, self.snaps = [], []
        self.lock = threading.Lock()

    def devices(self):
        return [{"serial": "emu", "state": "device", "model": "x"}]

    def display_size(self, serial):
        return (1080, 2400)

    def start_live(self, serial, on_frame):
        c = FakeCapture(self, serial, on_frame)
        with self.lock:
            self.captures.append(c)
        return c

    def snapshot(self, serial, side):
        with self.lock:
            self.snaps.append(time.monotonic())
        return JPEG

    def running(self):
        return [c for c in self.captures if not c.stopped]


def lease(serial=None, label="bench"):
    entries = [{"serial": serial, "label": label}] if serial else []
    return {"status": "held" if serial else "free", "held_by": label if serial else None,
            "lease": {"status": "held" if serial else "clear", "bridge_held": bool(serial), "entries": entries}}


def wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class LockPolicyTests(unittest.TestCase):
    def test_lock_attribution_is_per_device_and_strict_when_unsure(self):
        self.assertEqual(lock_for("a", lease("a", "eval-1")), (True, "eval-1"))
        self.assertEqual(lock_for("b", lease("a"))[0], False)       # 别的手机在测，不影响这台
        self.assertEqual(lock_for("b", lease()), (False, None))
        self.assertTrue(lock_for("b", None)[0])
        self.assertTrue(lock_for("b", {"status": "unknown", "lease": {"status": "unknown", "entries": []}})[0])
        self.assertTrue(lock_for("b", {"status": "held", "lease": {"status": "held", "entries": [{"serial": None}]}})[0])
        legacy = {"status": "held", "held_by": "someone", "lease": {"status": "clear", "entries": []}}
        self.assertTrue(lock_for("b", legacy)[0])                   # 归属不明的旧 devlock = 全机占用
        pf = {"status": "held", "held_by": "phonefarm:eval:a", "lease": {"status": "clear", "entries": []}}
        self.assertEqual(lock_for("a", pf), (True, "eval"))
        self.assertFalse(lock_for("b", pf)[0])
        tagged = {"status": "held", "held_by": "selfsr-plain-A1@a since 2026-10-24 08:45:34",
                  "lease": {"status": "clear", "entries": []}}
        self.assertEqual(lock_for("a", tagged), (True, "selfsr-plain-A1@a"))   # 标签带序列号: 只占那一台
        self.assertFalse(lock_for("b", tagged)[0])


class HubTests(unittest.TestCase):
    def make(self, lock=None, **kw):
        self.lock = lock if lock is not None else lease()
        self.backend = FakeBackend()
        kw.setdefault("linger_s", 0.3)
        kw.setdefault("policy_every_s", 0.05)
        kw.setdefault("free_grace_s", 0)
        return LiveHub(lambda: self.lock, self.backend, **kw)

    def test_one_capture_for_many_viewers_and_stops_after_last_leaves(self):
        hub = self.make()
        a, b = hub.open("emu"), hub.open("emu")
        self.assertIs(a, b)
        self.assertTrue(wait(lambda: a.frame is not None and a.mode == "live"))
        self.assertEqual(len(self.backend.captures), 1)
        a.leave()
        time.sleep(0.5)
        self.assertEqual(len(self.backend.running()), 1)            # 还有一个人在看
        b.leave()
        self.assertTrue(wait(lambda: a.thread is None))
        self.assertEqual(self.backend.running(), [])
        self.assertEqual(a.mode, "idle")

    def test_viewer_returning_within_linger_reuses_capture(self):
        hub = self.make(linger_s=1.0)
        s = hub.open("emu")
        self.assertTrue(wait(lambda: s.mode == "live"))
        s.leave()
        time.sleep(0.2)
        hub.open("emu")
        time.sleep(1.3)
        self.assertEqual(len(self.backend.captures), 1)
        s.leave()
        self.assertTrue(wait(lambda: s.thread is None))

    def test_dead_capture_is_restarted(self):
        hub = self.make()
        s = hub.open("emu")
        self.assertTrue(wait(lambda: len(self.backend.captures) == 1))
        self.backend.captures[0].dead = True
        self.assertTrue(wait(lambda: len(self.backend.captures) == 2))
        self.assertTrue(self.backend.captures[0].stopped)
        s.leave()
        self.assertTrue(wait(lambda: s.thread is None))
        self.assertEqual(self.backend.running(), [])

    def test_measurement_lock_stops_encoder_and_caps_screenshots(self):
        hub = self.make(held_fps=2)
        s = hub.open("emu")
        self.assertTrue(wait(lambda: s.mode == "live"))
        self.lock = lease("emu", "eval-7")
        self.assertTrue(wait(lambda: s.mode == "held"))
        self.assertEqual(self.backend.running(), [])               # 测量中不留编码器
        self.assertEqual(s.status()["label"], "eval-7")
        self.backend.snaps.clear()
        time.sleep(1.6)
        snaps = list(self.backend.snaps)
        self.assertLessEqual(len(snaps), 4)
        self.assertTrue(all(b - a >= 0.45 for a, b in zip(snaps, snaps[1:])))
        self.assertEqual(len(self.backend.captures), 1)            # 持锁期间没再起编码器
        self.lock = lease()
        self.assertTrue(wait(lambda: s.mode == "live"))
        s.leave()
        self.assertTrue(wait(lambda: s.thread is None))

    def test_paused_policy_and_release_grace(self):
        hub = self.make(lock=lease("emu"), held_fps=0, free_grace_s=0.6)
        s = hub.open("emu")
        self.assertTrue(wait(lambda: s.mode == "paused"))
        time.sleep(0.3)
        self.assertEqual((self.backend.snaps, self.backend.captures), ([], []))
        self.lock = lease()
        released = time.monotonic()
        self.assertTrue(wait(lambda: s.mode == "live"))
        self.assertGreaterEqual(time.monotonic() - released, 0.35)  # 释放后等宽限期（从最后一次看到持锁算起）才恢复编码
        s.leave()
        self.assertTrue(wait(lambda: s.thread is None))

    def test_lock_reader_failure_is_treated_as_held(self):
        hub = self.make(held_fps=1)
        hub.lock_fn = lambda: (_ for _ in ()).throw(OSError("lease"))
        s = hub.open("emu")
        self.assertTrue(wait(lambda: s.mode == "held"))
        self.assertEqual(self.backend.captures, [])
        s.leave()
        self.assertTrue(wait(lambda: s.thread is None))

    def test_viewer_limit(self):
        hub = self.make()
        opened = [hub.open("emu") for _ in range(dash_live.MAX_VIEWERS_PER_DEVICE)]
        with self.assertRaises(LiveBusy):
            hub.open("emu")
        for s in opened:
            s.leave()
        self.assertTrue(wait(lambda: opened[0].thread is None))

    def test_unknown_serial_is_rejected(self):
        hub = self.make()
        self.assertTrue(hub.known("emu"))
        self.assertFalse(hub.known("other"))
        self.assertFalse(hub.known("emu; rm -rf /"))


class FramingTests(unittest.TestCase):
    def test_split_jpegs_keeps_partial_tail(self):
        frames, rest = split_jpegs(b"junk" + JPEG + JPEG + b"\xff\xd8part")
        self.assertEqual((frames, rest), ([JPEG, JPEG], b"\xff\xd8part"))
        self.assertEqual(split_jpegs(b"noise"), ([], b""))

    def test_fit_size_is_16_aligned_and_keeps_orientation(self):
        self.assertEqual(fit_size(1080, 2400, 960), (432, 960))
        self.assertEqual(fit_size(2400, 1080, 960), (960, 432))
        w, h = fit_size(1220, 2712, 960)
        self.assertEqual((w % 16, h % 16), (0, 0))


if __name__ == "__main__":
    unittest.main()
