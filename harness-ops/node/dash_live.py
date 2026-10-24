"""实时画面 /dash/live：每台手机一路共享的近实时画面，只读，不往手机写任何输入。

采集方式（为什么这么选）：
  - 空闲（没人持测量锁）：`adb exec-out screenrecord --output-format=h264` 出 H.264 → 宿主 ffmpeg 解码成 MJPEG
    → 本模块按帧切开，只留最新一帧。手机端只用硬件编码器，宿主做解码，延迟 ~0.1–0.3 s。
    浏览器侧是普通 HTTP 分块流（multipart/x-mixed-replace，每帧带 Content-Length），
    经 Cloudflare 隧道、Chrome/Safari（含手机）都能用；流不通时页面自动退回逐帧拉取 /frame。
    没选 scrcpy（节点没装，且要推 server jar 到手机）、没选浏览器端 WebCodecs/MSE（Safari 支持不齐、关键帧/重连复杂）。
  - screenrecord 只在画面变化时出帧，静止画面不耗编码器；它有 180 s 上限，退出后由监管线程重启。
  - 被杀掉的 adb 客户端不会立刻带走手机上的 screenrecord（静止画面时它能一直挂着），
    所以启动时用 `echo $$; exec screenrecord` 拿到手机端 pid，停止时按 pid（且 comm 仍是 screenrecord）精确 kill。

测量纪律（显式策略，可配置）：
  - 只有有人看时才采集；同一台手机不论几个人看只有一路采集；最后一个观看者离开 LINGER_S 秒后停止、杀进程。
  - 每 POLICY_EVERY_S 秒按设备锁判一次（与 /dash 同源：phonefarm lease status + tools/devlock，见 lock_for）：
      * 本机被测量占用（lease 条目的 serial 是它；或旧 devlock 被不明归属者持有；或锁状态未知/阻塞——一律从严）
        → 立刻停掉 screenrecord（编码器会占 GPU/功耗），改为 `screencap` 原始帧低频抓取：
          HG_LIVE_HELD_FPS（默认 1，上限 2；0 = 暂停，只显示最后一帧），长边 HG_LIVE_HELD_SIDE（默认 480）。
          页面显示「测量中」角标和持锁者。
      * 锁释放后再等 HG_LIVE_FREE_GRACE_S 秒（默认 5，防止下一个测试紧接着拿锁）才恢复全速。
    从拿锁到我们停掉 screenrecord 最多约 POLICY_EVERY_S + lease 读数的时间（~1–2 s）。
  - 单个观看连接最长 STREAM_MAX_S 秒，页面只在前台可见且近期有人操作时自动续上。

不新增任何写能力：只有 GET，认证沿用 /dash 的查看密码（dash.Dash.authed）。
"""
import collections
import os
import re
import select
import shutil
import subprocess
import threading
import time
from urllib.parse import parse_qs


def _env_num(name, default, lo, hi):
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


LIVE_FPS = _env_num("HG_LIVE_FPS", 15, 1, 30)              # 空闲时每个观看者最多收多少帧/秒
LIVE_SIDE = int(_env_num("HG_LIVE_SIDE", 960, 320, 1920))   # 空闲时编码长边像素
LIVE_BITRATE = os.environ.get("HG_LIVE_BITRATE", "4M")
LIVE_JPEG_Q = int(_env_num("HG_LIVE_JPEG_Q", 7, 2, 20))     # ffmpeg mjpeg -q:v，越大越糊越小
HELD_FPS = _env_num("HG_LIVE_HELD_FPS", 1, 0, 2)            # 测量中的上限；0 = 暂停
HELD_SIDE = int(_env_num("HG_LIVE_HELD_SIDE", 480, 160, 1080))
FREE_GRACE_S = _env_num("HG_LIVE_FREE_GRACE_S", 5, 0, 120)
LINGER_S = 5              # 最后一个观看者走后多久停采集（逐帧拉取的间隔要小于它）
POLICY_EVERY_S = 1.0
SCREEN_OFF = "手机屏幕已熄灭，没有新画面（远程调试控制台按「唤醒」键点亮）"
ORIENT_EVERY_S = 5.0      # 空闲直播时多久查一次横竖屏（变了就按新尺寸重启 screenrecord）
STREAM_MAX_S = 600
KEEPALIVE_S = 5           # 静止画面也每隔这么久重发一帧：既探测断开，也防隧道空闲超时
MAX_VIEWERS_PER_DEVICE = 6
MAX_VIEWERS = 16
MAX_FRAME = 4 << 20
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
BOUNDARY = "hgframe"
AUD = b"\x00\x00\x00\x01\x09\xf0"   # H.264 访问单元分隔符
AU_IDLE_S = 0.03
STREAM_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                  "Referrer-Policy": "no-referrer"}

Frame = collections.namedtuple("Frame", "seq jpeg at mode")


def _log(s):
    print(time.strftime("%F %T"), "dash_live", s, file=__import__("sys").stderr, flush=True)


# ═════════════ 锁策略（纯函数，可单测） ═════════════

def lock_for(serial, lock):
    """→ (held, label)。lock 是 FleetState.snapshot()["lock"] 的形状。拿不准一律当被占用。"""
    if not isinstance(lock, dict):
        return True, "设备锁状态未知"
    lease = lock.get("lease") or {}
    entries = [e for e in lease.get("entries") or [] if isinstance(e, dict)]
    for e in entries:
        if e.get("serial") == serial:
            return True, e.get("label") or e.get("job_id") or "lease"
    if lock.get("status") == "unknown" or lease.get("status") in ("blocked", "unknown"):
        return True, "设备锁状态未知"
    if any(not e.get("serial") for e in entries):
        return True, "设备锁归属未知"
    if lock.get("status") == "held" and not entries:
        # 旧 tools/devlock：只有 owner 文本。phonefarm 写的是 phonefarm:<label>:<serial>，能认出就按设备分；
        # 其余无法归属的旧锁视为全机占用。
        who = lock.get("held_by") or ""
        # 约定: 只碰一台手机的 tools/devlock 持有者把序列号写进标签 「<标签>@<序列号>」 (selfsr_ab 这样做)
        tag = who.split(" ", 1)[0]
        if "@" in tag:
            return (tag.rsplit("@", 1)[1] == serial), tag
        if who.startswith("phonefarm:") and ":" in who[10:]:
            return (who.rsplit(":", 1)[1] == serial), who.split(":")[1]
        return True, who or "devlock"
    return False, None


# ═════════════ 采集后端（真机 adb + ffmpeg；单测替换成假的） ═════════════

def _even16(x):
    return max(16, int(round(x / 16.0)) * 16)


def fit_size(w, h, side):
    """按长边缩放并取 16 的倍数（硬件编码器友好）。"""
    k = min(1.0, side / float(max(w, h)))
    return _even16(w * k), _even16(h * k)


def split_jpegs(buf):
    """从 MJPEG 字节流切出完整 JPEG：返回 (帧列表, 剩余)。熵编码段里 0xFF 有填充，FFD9 只会是结束标记。"""
    frames = []
    while True:
        s = buf.find(b"\xff\xd8")
        if s < 0:
            return frames, b""
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            rest = buf[s:]
            return frames, (rest if len(rest) <= MAX_FRAME else b"")
        frames.append(buf[s:e + 2])
        buf = buf[e + 2:]


def _kill(p):
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, 9)
        except OSError:
            try:
                p.kill()
            except OSError:
                pass
    if p:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


class AdbBackend:
    def __init__(self, adb=None, ffmpeg=None):
        self.adb = adb or shutil.which("adb") or "/opt/homebrew/bin/adb"
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

    def _run(self, serial, args, timeout=5):
        return subprocess.run([self.adb, "-s", serial, *args], capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def devices(self):
        r = subprocess.run([self.adb, "devices", "-l"], capture_output=True, text=True, timeout=5,
                           stdin=subprocess.DEVNULL)
        out = []
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and SERIAL_RE.fullmatch(parts[0]):
                kv = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
                out.append({"serial": parts[0], "state": parts[1], "model": kv.get("model")})
        return out

    def display_size(self, serial):
        """当前方向下的显示尺寸（横屏时宽 > 高）。"""
        r = self._run(serial, ["shell", "dumpsys window displays | grep -m1 ' cur='"])
        m = re.search(rb" cur=(\d+)x(\d+)", r.stdout)
        if not m:
            r = self._run(serial, ["shell", "wm size"])
            m = re.search(rb"(\d+)x(\d+)\s*$", r.stdout.strip())
        if not m:
            raise RuntimeError("读不到屏幕尺寸（设备离线？）")
        return int(m.group(1)), int(m.group(2))

    def hw_id(self, serial):
        """同一台手机可能 USB 和 Wi-Fi 两路同时接入（序列号不同），按 ro.serialno 认成一台。"""
        r = self._run(serial, ["shell", "getprop ro.serialno"])
        return r.stdout.decode(errors="replace").strip() or None

    def screen_on(self, serial):
        """屏幕是否点亮；读不出来返回 None（不下结论）。"""
        r = self._run(serial, ["shell", "dumpsys power | grep -m1 mWakefulness="])
        m = re.search(rb"mWakefulness=(\w+)", r.stdout)
        return None if not m else m.group(1) == b"Awake"

    def start_live(self, serial, on_frame):
        return LiveCapture(self, serial, on_frame)

    def snapshot(self, serial, side):
        """测量中用：一次 screencap 原始帧（手机端不压缩），宿主转成小 JPEG。"""
        r = self._run(serial, ["exec-out", "screencap"], timeout=10)
        raw = r.stdout
        if r.returncode != 0 or len(raw) < 16:
            raise RuntimeError("screencap 失败（设备离线？）")
        w, h, fmt = (int.from_bytes(raw[i:i + 4], "little") for i in (0, 4, 8))
        body = w * h * 4
        if not (0 < w < 10000 and 0 < h < 10000) or len(raw) - body not in (12, 16):
            raise RuntimeError("screencap 格式不认识")
        pix = {1: "rgba", 2: "rgb0", 5: "bgra"}.get(fmt, "rgba")
        tw, th = fit_size(w, h, side)
        f = subprocess.run([self.ffmpeg, "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}",
                            "-i", "pipe:0", "-vf", f"scale={tw}:{th}", "-frames:v", "1", "-q:v", "8",
                            "-f", "mjpeg", "pipe:1"], input=raw[len(raw) - body:], capture_output=True, timeout=10)
        if f.returncode != 0 or not f.stdout.startswith(b"\xff\xd8"):
            raise RuntimeError("截图转码失败")
        return f.stdout


class LiveCapture:
    """一路 screenrecord | ffmpeg。stop() 保证宿主两进程退出、手机端 screenrecord 被杀。"""

    def __init__(self, backend, serial, on_frame):
        self.backend, self.serial, self.on_frame = backend, serial, on_frame
        self.device_pid = None
        self.adb = self.ff = None
        self._done = threading.Event()
        w, h = backend.display_size(serial)
        self.size = (w, h)
        tw, th = fit_size(w, h, LIVE_SIDE)
        cmd = (f"echo $$; exec screenrecord --output-format=h264 --size {tw}x{th} "
               f"--bit-rate {LIVE_BITRATE} --time-limit 180 -")
        try:
            self.adb = subprocess.Popen([backend.adb, "-s", serial, "exec-out", cmd], stdout=subprocess.PIPE,
                                        stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                        bufsize=0)  # 无缓冲：读 pid 行不会多吞后面的 H.264 字节
            if not select.select([self.adb.stdout], [], [], 8)[0]:
                raise RuntimeError("screenrecord 启动超时")
            line = self.adb.stdout.readline(32)
            if not line.strip().isdigit():
                raise RuntimeError("screenrecord 没起来（设备离线？）")
            self.device_pid = int(line)
            self.ff = subprocess.Popen([backend.ffmpeg, "-loglevel", "error", "-flags", "low_delay",
                                        "-probesize", "32", "-analyzeduration", "0", "-f", "h264", "-i", "pipe:0",
                                        # 不要 -fflags nobuffer：它会丢掉探测时读到的首个 IDR。
                                        # 编码器单线程：mjpeg 帧线程编码会把每帧压到下一帧来了才吐出。
                                        "-threads", "1", "-q:v", str(LIVE_JPEG_Q), "-flush_packets", "1",
                                        "-f", "mjpeg", "pipe:1"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       start_new_session=True)
        except Exception:
            self.stop()
            raise
        threading.Thread(target=self._pump, daemon=True, name=f"live-pump-{serial}").start()
        threading.Thread(target=self._read, daemon=True, name=f"live-read-{serial}").start()

    def _pump(self):
        """adb → ffmpeg。H.264 裸流解析器要看到下一帧的起始码才吐出当前帧，静止画面时最后一帧会卡在 ffmpeg 里；
        所以输入停顿 AU_IDLE_S 后补一个访问单元分隔符（AUD），让解码器立即输出。screenrecord 每帧一次 write，
        停顿时正处在帧边界；万一切在帧中间，只会坏一帧，下一个关键帧（≤10 s）恢复。"""
        fd, dirty = self.adb.stdout.fileno(), False
        try:
            while True:
                if not select.select([fd], [], [], AU_IDLE_S)[0]:
                    if dirty:
                        self.ff.stdin.write(AUD)
                        self.ff.stdin.flush()
                        dirty = False
                    continue
                d = os.read(fd, 65536)
                if not d:
                    break
                self.ff.stdin.write(d)
                self.ff.stdin.flush()
                dirty = True
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.ff.stdin.close()
            except (OSError, ValueError):
                pass

    def _read(self):
        buf = b""
        try:
            while True:
                d = self.ff.stdout.read1(65536)
                if not d:
                    break
                frames, buf = split_jpegs(buf + d)
                if frames:
                    self.on_frame(frames[-1])
        except (OSError, ValueError):
            pass
        finally:
            self._done.set()

    def alive(self):
        return not self._done.is_set() and self.adb.poll() is None

    def stop(self):
        _kill(self.adb)
        _kill(self.ff)
        if self.device_pid:
            pid, self.device_pid = self.device_pid, None
            try:  # 只杀我们起的那个，且它仍然是 screenrecord
                self.backend._run(self.serial, ["shell", f'[ "$(cat /proc/{pid}/comm 2>/dev/null)" = screenrecord ] '
                                                         f'&& kill {pid}; true'], timeout=4)
            except (OSError, subprocess.SubprocessError):
                pass
        for s in (self.adb and self.adb.stdout, self.ff and self.ff.stdout):
            try:
                s and s.close()
            except OSError:
                pass


# ═════════════ 每台设备一路：观看计数 + 监管线程 ═════════════

class LiveBusy(Exception):
    pass


class DeviceStream:
    def __init__(self, hub, serial):
        self.hub, self.serial = hub, serial
        self.cond = threading.Condition()
        self.viewers = 0
        self.left_at = time.monotonic()
        self.frame = None
        self.seq = 0
        self.mode = "idle"          # idle | live | held | paused | error
        self.label = None
        self.error = None
        self.thread = None
        self.capture = None
        self.held_seen_at = None
        self.stamps = collections.deque(maxlen=64)
        self.restarts = 0

    # ── 观看者 ──
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

    def publish(self, jpeg, mode=None):
        with self.cond:
            if self.error == SCREEN_OFF:
                self.error = None
            self.seq += 1
            self.frame = Frame(self.seq, jpeg, time.time(), mode or self.mode)
            self.stamps.append(time.monotonic())
            self.cond.notify_all()

    def status(self):
        with self.cond:
            now = time.monotonic()
            recent = [t for t in self.stamps if now - t <= 3]
            return {"serial": self.serial, "mode": self.mode, "label": self.label, "viewers": self.viewers,
                    "running": self.thread is not None, "error": self.error,
                    "fps": round(len(recent) / 3.0, 1), "frame_at": self.frame.at if self.frame else None,
                    "frame_seq": self.seq, "restarts": self.restarts,
                    "policy": {"live_fps": LIVE_FPS, "held_fps": HELD_FPS, "held_side": HELD_SIDE,
                               "free_grace_s": FREE_GRACE_S}}

    # ── 监管 ──
    def _stop_capture(self):
        c, self.capture = self.capture, None
        if c:
            c.stop()

    def _decide(self, now):
        held, label = self.hub.held(self.serial)
        if held:
            self.held_seen_at = now
            return ("held" if self.hub.held_fps > 0 else "paused"), label
        if self.held_seen_at is not None and now - self.held_seen_at < self.hub.free_grace_s:
            return ("held" if self.hub.held_fps > 0 else "paused"), "刚释放，等待确认无人接着测"
        return "live", None

    def _supervise(self):
        next_policy = next_try = next_snap = next_orient = 0.0
        backoff = 1.0
        mode = None
        try:
            while True:
                with self.cond:
                    now = time.monotonic()
                    if self.viewers <= 0 and now - self.left_at >= self.hub.linger_s:
                        break
                if now >= next_policy:
                    try:
                        mode, label = self._decide(now)
                    except Exception as e:  # noqa: BLE001  读锁出错 → 从严
                        mode, label = ("held" if self.hub.held_fps > 0 else "paused"), f"读锁出错: {e}"
                    next_policy = now + self.hub.policy_every_s
                    with self.cond:
                        if mode != self.mode and mode != "live":
                            self.error = None
                        self.mode, self.label = (mode if mode != "live" or self.capture else self.mode), label
                if mode != "live":
                    self._stop_capture()  # 测量中绝不保留编码器
                if mode == "live":
                    if self.capture and not self.capture.alive():
                        self._stop_capture()
                        self.restarts += 1
                        next_try = max(next_try, now + 0.3)
                    if self.capture and now >= next_orient:
                        next_orient = now + ORIENT_EVERY_S
                        try:
                            if self.hub.backend.display_size(self.serial) != self.capture.size:
                                self._stop_capture()  # 横竖屏变了，按新尺寸重启
                        except Exception:  # noqa: BLE001
                            pass
                        # screenrecord 只在画面变化时出帧，熄屏时一帧都没有：说清楚，免得看着像坏了
                        stale = not self.frame or time.time() - self.frame.at > ORIENT_EVERY_S
                        on = getattr(self.hub.backend, "screen_on", lambda _s: True)
                        try:
                            off = stale and on(self.serial) is False
                        except Exception:  # noqa: BLE001
                            off = False
                        with self.cond:
                            if off:
                                self.error = SCREEN_OFF
                            elif self.error == SCREEN_OFF:
                                self.error = None
                    if not self.capture and now >= next_try:
                        try:
                            self.capture = self.hub.backend.start_live(self.serial, lambda j: self.publish(j, "live"))
                            next_orient = now + ORIENT_EVERY_S
                            backoff = 1.0
                            with self.cond:
                                self.mode, self.error = "live", None
                        except Exception as e:  # noqa: BLE001
                            next_try = now + backoff
                            backoff = min(backoff * 2, 15.0)
                            with self.cond:
                                self.mode, self.error = "error", str(e)[:200]
                elif mode == "held" and now >= next_snap:
                    next_snap = now + 1.0 / self.hub.held_fps
                    try:
                        self.publish(self.hub.backend.snapshot(self.serial, self.hub.held_side), "held")
                        with self.cond:
                            self.error = None
                    except Exception as e:  # noqa: BLE001
                        next_snap = now + backoff
                        backoff = min(backoff * 2, 15.0)
                        with self.cond:
                            self.error = str(e)[:200]
                with self.cond:
                    self.cond.wait(0.2)
        finally:
            self._stop_capture()
            with self.cond:
                self.thread = None
                self.mode = "idle"
                self.cond.notify_all()
                # 停下期间又来了人：重新起一个监管线程
                if self.viewers > 0:
                    self.thread = threading.Thread(target=self._supervise, daemon=True, name=f"live-{self.serial}")
                    self.thread.start()


class LiveHub:
    def __init__(self, lock_fn, backend=None, *, held_fps=None, held_side=None, free_grace_s=None,
                 linger_s=LINGER_S, policy_every_s=POLICY_EVERY_S):
        self.lock_fn = lock_fn
        self.backend = backend or AdbBackend()
        self.held_fps = HELD_FPS if held_fps is None else held_fps
        self.held_side = held_side or HELD_SIDE
        self.free_grace_s = FREE_GRACE_S if free_grace_s is None else free_grace_s
        self.linger_s, self.policy_every_s = linger_s, policy_every_s
        self.streams = {}
        self.lock = threading.Lock()
        self._dev_cache = (0.0, [])
        self._hw = {}

    def aliases(self, serial):
        """同一台物理手机的所有在线序列号（含自己）。读不到硬件序列号的只算它自己。"""
        def hw(s):
            if s not in self._hw:
                try:
                    self._hw[s] = getattr(self.backend, "hw_id", lambda _s: None)(s)
                except (OSError, subprocess.SubprocessError):
                    return None  # 不缓存失败，下次再读
            return self._hw[s]
        online = {d["serial"] for d in self.devices()}
        for s in [k for k in self._hw if k not in online]:
            del self._hw[s]  # 掉线的序列号（比如 Wi-Fi 地址换了手机）下次重读
        mine = hw(serial)
        if not mine:
            return [serial]
        return [serial] + [d["serial"] for d in self.devices()
                           if d["serial"] != serial and d["state"] == "device" and hw(d["serial"]) == mine]

    def held(self, serial):
        lock = self.lock_fn()
        free_label = None
        for s in self.aliases(serial):  # 任何一路序列号被测量占用，整台手机都算占用
            held, label = lock_for(s, lock)
            if held:
                return held, label
            free_label = free_label or label
        return False, free_label

    def devices(self):
        t, devs = self._dev_cache
        if time.monotonic() - t > 3:
            try:
                devs = self.backend.devices()
            except (OSError, subprocess.SubprocessError):
                devs = []
            self._dev_cache = (time.monotonic(), devs)
        return devs

    def known(self, serial):
        return bool(SERIAL_RE.fullmatch(serial or "")) and any(
            d["serial"] == serial and d["state"] == "device" for d in self.devices())

    def stream(self, serial):
        with self.lock:
            s = self.streams.get(serial)
            if s is None:
                s = self.streams[serial] = DeviceStream(self, serial)
            return s

    def open(self, serial):
        """登记一个观看者；用完必须 close。超出上限抛 LiveBusy。"""
        with self.lock:
            total = sum(s.viewers for s in self.streams.values())
            s = self.streams.get(serial)
            if total >= MAX_VIEWERS or (s and s.viewers >= MAX_VIEWERS_PER_DEVICE):
                raise LiveBusy("观看人数已满")
        s = self.stream(serial)
        s.join()
        return s

    def status(self):
        with self.lock:
            streams = dict(self.streams)
        devs = self.devices()
        out = []
        for d in devs:
            st = streams[d["serial"]].status() if d["serial"] in streams else {"serial": d["serial"], "mode": "idle",
                                                                               "viewers": 0, "running": False}
            out.append({**d, **st})
        return {"devices": out, "policy": {"live_fps": LIVE_FPS, "live_side": LIVE_SIDE, "held_fps": self.held_fps,
                                           "held_side": self.held_side, "free_grace_s": self.free_grace_s,
                                           "stream_max_s": STREAM_MAX_S}}


# ═════════════ HTTP（dash.py 认证后转进来） ═════════════

def handle(dash, h, sub, query):
    """dash.Dash.handle_get 在认证通过后调用；不是本模块的路由返回 False。"""
    if sub == ["live"]:
        dash._static(h, "live.html")
        return True
    if sub[:2] != ["api", "live"]:
        return False
    hub = dash.live
    q = parse_qs(query)
    serial = (q.get("serial") or [""])[0]
    if sub == ["api", "live", "status"]:
        dash._send(h, 200, hub.status())
        return True
    if sub[2:] not in (["stream"], ["frame"]):
        dash._send(h, 404, {"error": "not found"})
        return True
    if not hub.known(serial):
        dash._send(h, 404, {"error": "没有这台在线设备"})
        return True
    try:
        s = hub.open(serial)
    except LiveBusy as e:
        dash._send(h, 503, {"error": str(e)})
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
                if s.thread is None and s.viewers <= 0:
                    break
                continue
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
