"""Shared dashboard frames, captured only by PF's bounded observation owner.

HTTP viewers reuse cached JPEGs. Device queries, screenshots and ownership stay
inside the configured PF. There is no raw ADB/video fallback when it is busy,
unknown or too old. Video encoding can return after its lifetime is supervised.
"""
import collections
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs

from device_scope import binding, lease_problem
from device_registry import RegistryError


def _env_num(name, default, lo, hi):
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


LIVE_FPS = _env_num("HG_LIVE_FPS", 1, .2, 5)
FREE_GRACE_S = _env_num("HG_LIVE_FREE_GRACE_S", 5, 0, 120)
LINGER_S = 5
POLICY_EVERY_S = 1.0
STREAM_MAX_S = 600
KEEPALIVE_S = 5
MAX_VIEWERS_PER_DEVICE = 6
MAX_VIEWERS = 16
MAX_FRAME = 4 << 20
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
BOUNDARY = "hgframe"
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                  "Referrer-Policy": "no-referrer"}
Frame = collections.namedtuple("Frame", "seq jpeg at mode")


def lock_for(serial, lock, device_binding=None):
    """Advisory only. PF takes the actual declared lease after this check."""
    if not isinstance(lock, dict):
        return True, "设备锁状态未知"
    if device_binding is not None:
        reason = lease_problem({"device_access": "write", "device_binding": device_binding}, lock.get("lease") or {})
        return reason is not None, reason
    lease = lock.get("lease") or {}
    if lock.get("status") == "unknown" or lease.get("status") not in ("clear", "held", "blocked"):
        return True, "设备锁状态未知"
    if lease.get("status") == "blocked":
        return True, "设备域有未完成恢复"
    if lock.get("status") != "free" or lease.get("status") != "clear" or lease.get("entries"):
        return True, lock.get("held_by") or "设备域被占用"
    if lock.get("queue"):
        return True, "已有设备任务排队"
    return False, None


class CaptureError(Exception):
    def __init__(self, state, reason, *, retain=False):
        super().__init__(reason)
        self.state, self.retain = state, retain


class PFBackend:
    def __init__(self, monitor, executable, cwd, env, root):
        self.monitor, self.executable, self.cwd = monitor, executable, cwd
        self.env = env if env is not None else {k: v for k, v in os.environ.items()
                                               if not k.startswith("PF_JOB_") and k not in ("DEVLOCK_LABEL", "DEVLOCK_INSTANCE_ID")}
        self.root = Path(root)
        self.available, self.reason = False, "configured PF lacks observation capture_v1"
        self.device_binding_available = False
        try:
            result = subprocess.run([executable, "screen", "capabilities", "--json"], cwd=cwd, env=self.env,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            cap = json.loads(result.stdout) if len(result.stdout) <= 8192 else {}
            self.available = (result.returncode == 0 and cap.get("schema") == "phonefarm/screen-capabilities@1"
                              and cap.get("capture_v1") is True and cap.get("ownership") == "observation-v1")
            self.device_binding_available = self.available and cap.get("device_binding_v1") is True
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def devices(self):
        # Fleet observations already have their own owner and freshness record.
        # Reading the live list never probes another serial to discover aliases.
        snapshot = self.monitor.request(wait=0)
        items = snapshot.get("devices") or []
        return [{**item, "state": item.get("state", "unknown"),
                 "sampled_at": snapshot.get("sampled_at"), "stale": snapshot.get("stale", True)} for item in items
                if isinstance(item, dict) and isinstance(item.get("serial"), str)]

    def snapshot(self, serial, device_binding=None):
        if not self.available:
            raise CaptureError("unavailable", self.reason)
        if device_binding is not None and not self.device_binding_available:
            raise CaptureError("unavailable", "configured PF screen capture does not support device binding; no global fallback")
        self.root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="frame-", dir=self.root)).resolve()
        retain = False
        try:
            with (work / "stdout").open("w+b") as stdout, (work / "stderr").open("w+b") as stderr:
                argv = [self.executable, "screen", "capture", "--serial", serial,
                        "--out", str(work / "capture"), "--json"]
                if device_binding is not None:
                    argv += ["--device-binding", json.dumps(device_binding, separators=(",", ":"))]
                process = subprocess.Popen(argv, cwd=self.cwd, env=self.env,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                deadline = time.monotonic() + 25
                while process.poll() is None:
                    if time.monotonic() >= deadline or max(os.fstat(stdout.fileno()).st_size,
                                                          os.fstat(stderr.fileno()).st_size) > 128 << 10:
                        process.kill()
                        process.wait()
                        retain = True
                        raise CaptureError("error", "PF capture did not finish within its host budget; retained " + str(work), retain=True)
                    time.sleep(.05)
                stdout.seek(0)
                raw = stdout.read((128 << 10) + 1)
            if len(raw) > 128 << 10:
                raise CaptureError("error", "PF capture response exceeded its limit", retain=True)
            value = json.loads(raw)
            if value.get("schema") != "phonefarm/screen-capture@1":
                raise CaptureError("error", "PF capture returned an incompatible receipt", retain=True)
            if process.returncode != 0 or value.get("state") != "ready":
                state = "deferred" if value.get("state") == "deferred" else "error"
                raise CaptureError(state, value.get("reason") or "PF capture failed", retain=state != "deferred")
            path = work / "capture" / "capture.jpg"
            at = value.get("captured_at_ms")
            if (value.get("serial") != serial or value.get("ownership") != "observation-v1"
                    or value.get("jpeg_path") != str(path) or type(at) is not int or at <= 0):
                raise CaptureError("error", "PF capture identity or time is missing", retain=True)
            if device_binding is not None and (value.get("device_binding") != device_binding
                                              or value.get("lease_scope") != "device"):
                raise CaptureError("error", "PF capture did not preserve the original device binding", retain=True)
            with path.open("rb") as stream:
                jpeg = stream.read(MAX_FRAME + 1)
            if len(jpeg) > MAX_FRAME or not jpeg.startswith(b"\xff\xd8"):
                raise CaptureError("error", "PF capture JPEG is invalid or exceeds 4 MiB", retain=True)
            return jpeg, at / 1000
        except CaptureError as error:
            retain = retain or error.retain
            raise
        except (OSError, ValueError) as error:
            retain = True
            raise CaptureError("error", "PF capture record unavailable: " + str(error), retain=True) from error
        finally:
            if not retain:
                shutil.rmtree(work)


class LiveBusy(Exception):
    pass


class DeviceStream:
    def __init__(self, hub, serial, device_binding=None):
        self.hub, self.serial, self.device_binding = hub, serial, device_binding
        self.cond = threading.Condition()
        self.viewers, self.seq = 0, 0
        self.left_at = time.monotonic()
        self.frame = self.thread = None
        self.mode, self.label, self.error = "idle", None, None
        self.held_seen_at = None
        self.stamps = collections.deque(maxlen=64)

    def join(self):
        with self.cond:
            self.viewers += 1
            if self.thread is None:
                self.thread = threading.Thread(target=self._supervise, daemon=True, name=f"live-{self.serial}")
                self.thread.start()
            self.cond.notify_all()

    def leave(self):
        with self.cond:
            self.viewers -= 1
            if self.viewers == 0:
                self.left_at = time.monotonic()
            self.cond.notify_all()

    def wait_frame(self, after_seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq > after_seq, timeout=timeout)
            return self.frame

    def publish(self, jpeg, at):
        with self.cond:
            self.seq += 1
            self.frame = Frame(self.seq, jpeg, at, "live")
            self.stamps.append(time.monotonic())
            self.mode, self.error, self.label = "live", None, None
            self.cond.notify_all()

    def status(self):
        with self.cond:
            now = time.monotonic()
            return {"serial": self.serial, "mode": self.mode, "label": self.label, "viewers": self.viewers,
                    "running": self.thread is not None, "error": self.error,
                    "fps": round(sum(t >= now - 3 for t in self.stamps) / 3, 1),
                    "frame_at": self.frame.at if self.frame else None, "frame_seq": self.seq,
                    "policy": self.hub.policy(),
                    **({"device_binding": self.device_binding, "lease_scope": "device"}
                       if self.device_binding is not None else {})}

    def _decide(self, now):
        held, label = self.hub.held(self.serial, self.device_binding)
        if held:
            self.held_seen_at = now
            return "deferred", label
        if self.held_seen_at is not None and now - self.held_seen_at < self.hub.free_grace_s:
            return "deferred", "设备域刚释放，等待冷却"
        return "live", None

    def _supervise(self):
        next_sample = 0
        try:
            while True:
                with self.cond:
                    now = time.monotonic()
                    if self.viewers <= 0:
                        if now - self.left_at >= self.hub.linger_s:
                            break
                        self.mode = "idle"
                        self.cond.wait(.1)
                        continue
                try:
                    mode, label = self._decide(now)
                    with self.cond:
                        if mode != "live":
                            self.mode, self.label, self.error = mode, label, None
                    if mode == "live" and now >= next_sample:
                        with self.cond:
                            if self.viewers <= 0:
                                continue
                            self.mode, self.label = "starting", None
                        jpeg, at = (self.hub.backend.snapshot(self.serial, self.device_binding)
                                    if self.device_binding is not None else self.hub.backend.snapshot(self.serial))
                        self.publish(jpeg, at)
                        next_sample = time.monotonic() + self.hub.interval_s
                except CaptureError as error:
                    with self.cond:
                        self.mode, self.error = error.state, str(error)
                    next_sample = time.monotonic() + max(1, self.hub.interval_s)
                except Exception as error:
                    with self.cond:
                        self.mode, self.error = "unknown", str(error)[:300]
                    next_sample = time.monotonic() + max(1, self.hub.interval_s)
                with self.cond:
                    self.cond.wait(self.hub.policy_every_s)
        finally:
            with self.cond:
                self.thread, self.mode = None, "idle"
                self.cond.notify_all()
                if self.viewers > 0:
                    self.thread = threading.Thread(target=self._supervise, daemon=True, name=f"live-{self.serial}")
                    self.thread.start()


class LiveHub:
    def __init__(self, lock_fn, backend, *, free_grace_s=FREE_GRACE_S,
                 linger_s=LINGER_S, policy_every_s=POLICY_EVERY_S, interval_s=None, device_scopes=None):
        self.lock_fn, self.backend, self.device_scopes = lock_fn, backend, device_scopes
        self.free_grace_s, self.linger_s, self.policy_every_s = free_grace_s, linger_s, policy_every_s
        self.interval_s = 1 / LIVE_FPS if interval_s is None else interval_s
        self.streams, self.lock = {}, threading.Lock()

    def policy(self):
        return {"live_fps": 1 / self.interval_s, "held_fps": 0, "free_grace_s": self.free_grace_s,
                "stream_max_s": STREAM_MAX_S, "backend": "pf-observation", "video": False,
                "device_binding_v1": bool(getattr(self.backend, "device_binding_available", False)
                                          and self.device_scopes is not None)}

    def held(self, serial, device_binding=None):
        return lock_for(serial, self.lock_fn(), device_binding)

    def devices(self):
        return self.backend.devices()

    def known(self, serial):
        return bool(SERIAL_RE.fullmatch(serial or "")) and any(
            d["serial"] == serial and d["state"] == "device" for d in self.devices())

    @staticmethod
    def key(serial, device_binding=None):
        return (serial, json.dumps(device_binding, sort_keys=True)) if device_binding is not None else serial

    def open(self, serial, device_binding=None):
        if device_binding is not None:
            device_binding = binding(device_binding)
            if device_binding["serial"] != serial:
                raise ValueError("serial must match the original device binding")
        key = self.key(serial, device_binding)
        with self.lock:
            stream = self.streams.get(key)
            if (sum(s.viewers for s in self.streams.values()) >= MAX_VIEWERS
                    or sum(s.viewers for s in self.streams.values() if s.serial == serial) >= MAX_VIEWERS_PER_DEVICE):
                raise LiveBusy("观看人数已满")
            if stream is None:
                if device_binding is not None:
                    if not self.policy()["device_binding_v1"]:
                        raise CaptureError("unavailable", "configured PF screen capture does not support device binding; no global fallback")
                    self.device_scopes.admit(device_binding)
                stream = self.streams[key] = DeviceStream(self, serial, device_binding)
            stream.join()
            return stream

    def status(self, serial="", device_binding=None):
        with self.lock:
            streams = dict(self.streams)
        result = []
        devices = self.devices()
        if device_binding is not None and not any(d["serial"] == serial for d in devices):
            devices.append({"serial": serial, "state": "unknown", "stale": True, "sampled_at": None})
        for device in devices:
            value = device_binding if device["serial"] == serial else None
            key = self.key(device["serial"], value)
            state = (streams[key].status() if key in streams else
                     {"mode": "idle", "viewers": 0, "running": False,
                      **({"device_binding": value, "lease_scope": "device"} if value is not None else {})})
            result.append({**device, **state})
        return {"devices": result, "policy": self.policy()}


# ═════════════ HTTP（dash.py 认证后转进来） ═════════════

def handle(dash, h, sub, query):
    """dash.Dash.handle_get 在认证通过后调用；不是本模块的路由返回 False。"""
    if sub == ["live"]:
        dash._static(h, "live.html")
        return True
    if sub[:2] != ["api", "live"]:
        return False
    hub = dash.live
    q = parse_qs(query, keep_blank_values=True)
    serial = (q.get("serial") or [""])[0]
    try:
        value = None
        if "device_binding" in q:
            if len(q["device_binding"]) != 1:
                raise ValueError("device_binding requires one original binding")
            value = binding(json.loads(q["device_binding"][0]))
            if value["serial"] != serial:
                raise ValueError("serial must match the original device binding")
    except ValueError as error:
        dash._send(h, 400, {"error": str(error)})
        return True
    if sub == ["api", "live", "status"]:
        dash._send(h, 200, hub.status(serial, value))
        return True
    if sub[2:] not in (["stream"], ["frame"]):
        dash._send(h, 404, {"error": "not found"})
        return True
    if value is None and not hub.known(serial):
        dash._send(h, 404, {"error": "没有这台在线设备"})
        return True
    try:
        s = hub.open(serial, value)
    except LiveBusy as e:
        dash._send(h, 503, {"error": str(e)})
        return True
    except (CaptureError, RegistryError) as error:
        dash._send(h, error.status if isinstance(error, RegistryError) else 503, {"error": str(error)})
        return True
    try:
        if sub[2] == "frame":
            _frame(dash, h, s, q)
        else:
            _stream(dash, h, s)
    finally:
        s.leave()
    return True


def _frame(dash, h, s, q):
    try:
        after = int((q.get("after") or ["0"])[0])
    except ValueError:
        after = 0
    f = s.wait_frame(after, 8)
    if f is None:
        return dash._send(h, 503, {"error": s.error or "还没有画面"})
    dash._send(h, 200, f.jpeg, "image/jpeg", extra={"X-Frame-Seq": str(f.seq), "X-Frame-Mode": f.mode,
                                                   "X-Frame-At": f"{f.at:.3f}"})


def _stream(dash, h, s):
    h.close_connection = True
    h.send_response(200)
    for k, v in {**STREAM_HEADERS, "Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                 "X-Accel-Buffering": "no", "Connection": "close",
                 # 必须分块：cloudflared 只对 chunked（或 SSE 等）响应逐块转发，否则攒到连接关闭才发
                 "Transfer-Encoding": "chunked"}.items():
        h.send_header(k, v)
    h.end_headers()
    try:
        h.connection.settimeout(20)
    except OSError:
        pass
    start = time.monotonic()
    last_seq, last_sent = 0, 0.0
    try:
        while time.monotonic() - start < STREAM_MAX_S:
            f = s.wait_frame(last_seq, KEEPALIVE_S)
            now = time.monotonic()
            if f is None:
                # With no first frame there is no write to discover a dead
                # subscriber. End this attempt after one bounded wait; an
                # active viewer can reconnect, a hidden/closed one cannot leave
                # a 600-second phantom subscription behind.
                break
            if f.seq == last_seq and now - last_sent < KEEPALIVE_S:
                continue
            gap = 1.0 / LIVE_FPS - (now - last_sent)
            if gap > 0:
                time.sleep(gap)
                f = s.frame  # 睡醒后发最新一帧，慢的观看者自然丢中间帧
            part = (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(f.jpeg)}\r\n"
                    f"X-Frame-Seq: {f.seq}\r\nX-Frame-Mode: {f.mode}\r\nX-Frame-At: {f.at:.3f}\r\n\r\n").encode() + f.jpeg + b"\r\n"
            h.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            h.wfile.flush()
            last_seq, last_sent = f.seq, time.monotonic()
        h.wfile.write(b"0\r\n\r\n")
    except (OSError, ValueError):
        pass  # 观看者断开 / 写超时
