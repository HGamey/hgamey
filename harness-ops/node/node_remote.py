#!/usr/bin/env python3
"""远程操作手机：同事（和他们的 agent）凭个人操作员令牌，经隧道远程调试允许的设备。

身份：每人一个操作员令牌 hgop_<id>_<secret>（节点上 CLI 签发，只存 SHA-256），与查看密码、管理 token 无关。
  浏览器：/dash/control 先用查看密码进看板，再填自己的操作员令牌换一个 HttpOnly 会话 cookie。
  agent ：Authorization: Bearer hgop_...，HTTP 接口在 /remote/...，MCP（streamable HTTP，JSON 响应）在 /remote/mcp。

设备互斥（远程操作员唯一的一套规则）：
  - 远程会话持有 phonefarm 的「按设备」内核锁 <device_lease_dir>/<sha256(serial)>.lock（phonefarm 新旧版本同一文件），
    所以同一台设备上 phonefarm lease 开不了，正在跑的 phonefarm lease 也让远程拿不到。
  - 另外只读检查 devlock.d / phonefarm-lease.d：记录里点名这台设备（或别名）的持有者 → 拒绝；
    只写了标签、没写设备的全局 devlock 持有者（tools/devlock run、selfsr 之类）→ 对没标记 ignore_global_lock 的设备一律拒绝。
  - 不碰 devlock.d、不排 devlock 队列：远程会话不阻塞别的设备上的实验。
  - 会话 = 心跳 TTL + 最长时长，过期自动释放；每个写动作前重新检查实验占用。
安全：只对允许名单里的序列号跑 adb/hdc（参数数组，不经宿主 shell）；超时、输出上限、限速；每个设备动作写审计日志。

CLI（在节点上）：
  python3 node_remote.py operator add <名字>        签发令牌（只显示这一次）
  python3 node_remote.py operator list | revoke <id|名字>
  python3 node_remote.py device list | enable <serial> [--ignore-global-lock] | disable <serial> | default allow|deny
  python3 node_remote.py audit [-n 50]
配置目录 HG_CONFIG_DIR（缺省 ~/.config/hgamey）：remote.operators.json、remote.devices.json、remote.audit.jsonl。
"""
import base64, fcntl, fnmatch, hashlib, hmac, json, os, re, secrets, shutil, struct, subprocess, sys, tempfile
import threading, time, uuid
from urllib.parse import parse_qs, unquote

CONFIG_DIR = os.path.expanduser(os.environ.get("HG_CONFIG_DIR") or "~/.config/hgamey")
TOKEN_RE = re.compile(r"hgop_([0-9a-f]{8})_([A-Za-z0-9_-]{32,64})\Z")
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9_.]{1,200}\Z")
ACTIVITY_RE = re.compile(r"[A-Za-z0-9_.$/]{1,300}\Z")
DEVICE_PATH_RE = re.compile(r"/[^\x00-\x1f'\\]{1,500}\Z")
DEFAULT_POLICY = {
    "default": "deny",
    "allow": ["emulator-*"],
    # 主人在用的真机：默认排除，主人用 `device enable` 打开。
    "deny": ["91253241019A", "AVXB6R5C19000481", "192.168.3.53:5555"],
    # 同一台手机的不同序列号（USB / 无线）：锁和占用检查一起算。
    "aliases": [["91253241019A", "192.168.3.53:5555"]],
    # 这些设备不受「只写标签的全局 devlock」阻挡（模拟器不在实验里用）。
    "ignore_global_lock": ["emulator-*"],
}
SESSION_TTL = 300          # 心跳：这么久没有任何动作/续期就自动释放
MAX_MINUTES_DEFAULT, MAX_MINUTES_CAP = 60, 240
SHELL_TIMEOUT_DEFAULT, SHELL_TIMEOUT_MAX = 20, 120
OUTPUT_CAP = 256 << 10
JSON_MAX = 64 << 10
APK_MAX = 300 << 20
FILE_MAX = 16 << 20
MCP_MAX = 72 << 20         # base64 APK/文件走 MCP 时的请求上限
TEXT_MAX = 1000
RATE_CAPACITY, RATE_REFILL = 60.0, 10.0
COST = {"shell": 3, "screenshot": 2, "install": 20, "push": 5, "pull": 5, "logcat": 3, "uninstall": 5}
INFLIGHT_MAX = 3
AUTH_FAILS_MAX, AUTH_FAIL_WINDOW = 20, 600
ANDROID_KEYS = {"back": 4, "home": 3, "recents": 187, "app_switch": 187, "menu": 82, "enter": 66, "del": 67,
                "backspace": 67, "power": 26, "volume_up": 24, "volume_down": 25, "tab": 61, "escape": 111,
                "wakeup": 224, "sleep": 223, "up": 19, "down": 20, "left": 21, "right": 22}
HDC_KEYS = {"back": "Back", "home": "Home", "power": "Power", "recents": "Recent"}
COOKIE = "hg_op"
WEB_SESSION_S = 12 * 3600


class RemoteError(Exception):
    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status, self.payload = status, {"error": error, **extra}


def _now():
    return time.time()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


# ═════════════ 凭据 ═════════════

class Operators:
    """操作员令牌：hgop_<8 hex id>_<secret>。文件只存 sha256(令牌)；令牌是高熵随机串，无需慢哈希。"""

    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()

    def _all(self):
        return _read_json(self.path, {"operators": []})["operators"]

    def add(self, name):
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,40}", name):
            raise ValueError("名字只能是 1-40 个字母数字 _ . @ -")
        with self._lock:
            ops = self._all()
            if any(o["name"] == name and not o.get("revoked") for o in ops):
                raise ValueError(f"已有有效的操作员 {name}；先 revoke 再签发")
            oid = secrets.token_hex(4)
            token = f"hgop_{oid}_{secrets.token_urlsafe(32)}"
            ops.append({"id": oid, "name": name, "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "created": _iso(_now()), "revoked": None})
            _atomic_json(self.path, {"operators": ops})
        return token, oid

    def revoke(self, key):
        with self._lock:
            ops, hit = self._all(), []
            for o in ops:
                if (o["id"] == key or o["name"] == key) and not o.get("revoked"):
                    o["revoked"] = _iso(_now())
                    hit.append(o["id"])
            if hit:
                _atomic_json(self.path, {"operators": ops})
        return hit

    def list(self):
        return [{k: o.get(k) for k in ("id", "name", "created", "revoked")} for o in self._all()]

    def verify(self, token):
        m = TOKEN_RE.fullmatch(token or "")
        if not m:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for o in self._all():
            if o["id"] == m.group(1) and not o.get("revoked") and hmac.compare_digest(o["sha256"], digest):
                return {"id": o["id"], "name": o["name"]}
        return None


class Policy:
    def __init__(self, path):
        self.path = path

    def load(self):
        value = _read_json(self.path, None)
        if not isinstance(value, dict):
            return json.loads(json.dumps(DEFAULT_POLICY))
        return {**json.loads(json.dumps(DEFAULT_POLICY)), **value}

    def save(self, value):
        _atomic_json(self.path, value)

    @staticmethod
    def _match(serial, patterns):
        return any(fnmatch.fnmatchcase(serial, p) for p in patterns or [])

    def aliases(self, serial, policy=None):
        policy = policy or self.load()
        for group in policy.get("aliases") or []:
            if serial in group:
                return sorted(set(group))
        return [serial]

    def enabled(self, serial, policy=None):
        policy = policy or self.load()
        names = self.aliases(serial, policy)
        if any(self._match(s, policy.get("deny")) for s in names):
            return False
        return policy.get("default") == "allow" or self._match(serial, policy.get("allow"))

    def ignores_global_lock(self, serial, policy=None):
        return self._match(serial, (policy or self.load()).get("ignore_global_lock"))

    def enable(self, serial, ignore_global=False):
        p = self.load()
        for s in self.aliases(serial, p):
            p["deny"] = [d for d in p.get("deny", []) if d != s]
        if serial not in p.get("allow", []):
            p.setdefault("allow", []).append(serial)
        if ignore_global and serial not in p.get("ignore_global_lock", []):
            p.setdefault("ignore_global_lock", []).append(serial)
        self.save(p)
        return p

    def disable(self, serial):
        p = self.load()
        p["allow"] = [a for a in p.get("allow", []) if a != serial]
        p["ignore_global_lock"] = [a for a in p.get("ignore_global_lock", []) if a != serial]
        if serial not in p.get("deny", []):
            p.setdefault("deny", []).append(serial)
        self.save(p)
        return p


class Audit:
    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()

    def write(self, **entry):
        line = json.dumps({"t": _iso(_now()), **entry}, ensure_ascii=False, default=str)
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)

    def tail(self, n):
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - (n * 2048 + 4096)))
                lines = f.read().decode(errors="replace").splitlines()
        except FileNotFoundError:
            return []
        return lines[-n:]


class RateLimit:
    def __init__(self, capacity=RATE_CAPACITY, refill=RATE_REFILL, inflight=INFLIGHT_MAX):
        self.capacity, self.refill, self.inflight_max = capacity, refill, inflight
        self._b, self._inflight, self._lock = {}, {}, threading.Lock()

    def take(self, key, cost=1):
        with self._lock:
            level, at = self._b.get(key, (self.capacity, _now()))
            level = min(self.capacity, level + (_now() - at) * self.refill)
            if level < cost:
                self._b[key] = (level, _now())
                raise RemoteError(429, f"太快了：稍等 {(cost - level) / self.refill:.1f}s 再试")
            if self._inflight.get(key, 0) >= self.inflight_max:
                raise RemoteError(429, f"同时进行的设备操作最多 {self.inflight_max} 个")
            self._b[key] = (level - cost, _now())
            self._inflight[key] = self._inflight.get(key, 0) + 1

    def done(self, key):
        with self._lock:
            self._inflight[key] = max(0, self._inflight.get(key, 1) - 1)


# ═════════════ 设备占用 ═════════════

def _since(owner_text):
    m = re.search(r" since (\S+(?: \d\d:\d\d:\d\d)?)", owner_text or "")
    return m.group(1) if m else None


class Occupancy:
    """读 phonefarm 的锁目录（只读），并持有按设备的内核锁。storage() 返回 (device_lease_dir, devlock_dir)。"""

    def __init__(self, storage):
        self._storage = storage

    def paths(self):
        paths = self._storage()
        if not paths or not all(isinstance(p, str) and os.path.isabs(p) for p in paths):
            raise RemoteError(503, "phonefarm 锁目录不可用，暂停远程写操作（phonefarm lease status --json）")
        return paths

    @staticmethod
    def _read(path, limit):
        try:
            if os.path.islink(path):
                return None
            with open(path, "rb") as f:
                return f.read(limit).decode(errors="replace")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return ""

    def holders(self):
        """所有锁记录：[{dir, label, serial, since, heartbeat_age_s, kind}]；看不懂的记录也列出来（serial=None）。"""
        _, devlock = self.paths()
        found = []
        for name in ("devlock.d", "phonefarm-lease.d"):
            d = os.path.join(devlock, name)
            if not os.path.lexists(d):
                continue
            owner = self._read(os.path.join(d, "owner"), 4096) or ""
            raw = self._read(os.path.join(d, "phonefarm-lease.json"), 64 << 10)
            try:
                marker = json.loads(raw) if raw else {}
            except ValueError:
                marker = {}
            marker = marker if isinstance(marker, dict) else {}
            label, serial = marker.get("label"), marker.get("serial") if isinstance(marker.get("serial"), str) else None
            if not label:
                m = re.match(r"phonefarm:([^:\s]+):(\S+) since", owner)
                if m:
                    label, serial = m.group(1), serial or m.group(2)
                else:
                    label = owner.split(" ", 1)[0].strip() or None
            # 约定: 只碰一台手机的 tools/devlock 持有者把序列号写进标签 「<标签>@<序列号>」 (selfsr_ab 这样做)
            if not serial and label and "@" in label:
                serial = label.rsplit("@", 1)[1] or None
            try:
                age = round(_now() - os.stat(os.path.join(d, "owner")).st_mtime, 1)
            except OSError:
                age = None
            kind = ("recovery" if marker.get("recovery_required") or marker.get("command_pending") else
                    "phonefarm-lease" if marker.get("format") == 1 else
                    "devlock" if marker.get("schema") == "phonefarm/devlock-owner@1" or not marker else "unknown")
            found.append({"dir": name, "label": label, "serial": serial, "since": _since(owner),
                          "heartbeat_age_s": age, "kind": kind, "job_id": marker.get("job_id")})
        # 外层只写标签的 devlock，若嵌套目录点名了设备，就是同一个进程树在用那台设备。
        nested = [h for h in found if h["dir"] == "phonefarm-lease.d" and h["serial"]]
        for h in found:
            if h["dir"] == "devlock.d" and not h["serial"] and nested:
                h["serial"], h["attributed"] = nested[0]["serial"], True
        return found

    def blocking(self, serial, names, ignore_global):
        out = []
        for h in self.holders():
            if h["serial"] in names:
                out.append(h)
            elif h["serial"] is None and not ignore_global:
                out.append({**h, "global": True})
        return out

    def lock_path(self, serial):
        lease_dir, _ = self.paths()
        return os.path.join(lease_dir, hashlib.sha256(serial.encode()).hexdigest() + ".lock")

    def try_lock(self, serials):
        """非阻塞拿齐所有别名的按设备锁；拿不全就全放掉，返回 None。"""
        fds = []
        for s in serials:
            path = self.lock_path(s)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                for f in fds:
                    os.close(f)
                return None
            fds.append(fd)
        return fds

    def probe(self, serials):
        fds = self.try_lock(serials)
        if fds is None:
            return True
        for fd in fds:
            os.close(fd)
        return False


def describe_holders(serial, holders):
    parts = []
    for h in holders:
        who = h.get("label") or "未知持有者"
        since = f"，自 {h['since']}" if h.get("since") else ""
        hb = f"，心跳 {h['heartbeat_age_s']:.0f}s 前" if isinstance(h.get("heartbeat_age_s"), (int, float)) else ""
        scope = "全局 devlock（没写设备，按占用所有设备算）" if h.get("global") else f"{h['dir']} 点名 {h.get('serial')}"
        parts.append(f"{who}{since}{hb}；{scope}")
    return f"设备 {serial} 正被实验占用：" + "；".join(parts)


# ═════════════ 设备后端 ═════════════

def _run(argv, timeout, cap=OUTPUT_CAP, stdin=None):
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout,
                           stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")[:cap]
        return {"exit": None, "timed_out": True, "stdout": out, "stderr": (e.stderr or b"")[:cap],
                "truncated": len(e.stdout or b"") > cap}
    except OSError as e:
        raise RemoteError(503, f"{os.path.basename(argv[0])} 不可用: {e}") from None
    return {"exit": p.returncode, "timed_out": False, "stdout": p.stdout[:cap], "stderr": p.stderr[:cap],
            "truncated": len(p.stdout) > cap or len(p.stderr) > cap}


def _q(text):
    """把一个参数安全地放进设备端 sh 的单引号里。"""
    return "'" + text.replace("'", "'\\''") + "'"


def png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":  # JPEG：找 SOF
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker, seg = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg
    return None


def shrink(data, max_side, quality=70):
    """macOS 上用 sips 转成 JPEG 并限制长边；不可用时原样返回 PNG。"""
    sips = shutil.which("sips")
    if not sips or not max_side:
        return data, "image/png"
    with tempfile.TemporaryDirectory(prefix="hg-shot-") as d:
        src, dst = os.path.join(d, "s.png"), os.path.join(d, "s.jpg")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", str(quality), "-Z", str(max_side),
                            src, "--out", dst], capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                return f.read(), "image/jpeg"
    return data, "image/png"


class AdbDevice:
    kind = "adb"

    def __init__(self, exe, serial):
        self.exe, self.serial = exe, serial

    def _cmd(self, *args, timeout=20, cap=OUTPUT_CAP, stdin=None):
        return _run([self.exe, "-s", self.serial, *args], timeout, cap, stdin)

    def shell(self, cmd, timeout):
        return self._cmd("shell", cmd, timeout=timeout)

    def screenshot(self):
        r = self._cmd("exec-out", "screencap -p", timeout=20, cap=32 << 20)
        if r["exit"] != 0 or not r["stdout"].startswith(b"\x89PNG"):
            raise RemoteError(502, "截图失败: " + (r["stderr"] or r["stdout"][:200]).decode(errors="replace"))
        return r["stdout"]

    def tap(self, x, y):
        return self.shell(f"input tap {x} {y}", 10)

    def swipe(self, x1, y1, x2, y2, ms):
        return self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}", 10 + ms // 1000)

    def text(self, text):
        # input text 不认空格：换成 %s；整体放进单引号避免设备端 shell 解释
        return self.shell("input text " + _q(text.replace("%", "\\%").replace(" ", "%s")), 20)

    def key(self, key):
        code = ANDROID_KEYS.get(key, key)
        return self.shell(f"input keyevent {int(code)}", 10)

    def install(self, path, replace=True, grant=False):
        args = ["install"] + (["-r"] if replace else []) + (["-g"] if grant else []) + [path]
        return self._cmd(*args, timeout=300)

    def uninstall(self, package):
        return self._cmd("uninstall", package, timeout=60)

    def push(self, local, remote):
        return self._cmd("push", local, remote, timeout=120)

    def pull(self, remote, local):
        return self._cmd("pull", remote, local, timeout=120)

    def logs(self, lines, spec):
        return self._cmd("logcat", "-d", "-t", str(lines), *spec, timeout=20)

    def start_app(self, package, activity):
        if activity:
            comp = activity if "/" in activity else f"{package}/{activity}"
            return self.shell("am start -n " + _q(comp), 20)
        resolve = ("cmd package resolve-activity --brief -a android.intent.action.MAIN "
                   f"-c android.intent.category.LAUNCHER {_q(package)} | tail -n 1")
        return self.shell(f'c=$({resolve}); case "$c" in */*) am start -n "$c";; '
                          f'*) echo "no launcher activity for {package}" >&2; exit 3;; esac', 20)

    def stop_app(self, package):
        return self.shell("am force-stop " + _q(package), 20)

    def size(self):
        out = self.shell("wm size", 10)["stdout"].decode(errors="replace")
        m = re.findall(r"(\d+)x(\d+)", out)
        return [int(v) for v in m[-1]] if m else None


class HdcDevice(AdbDevice):
    """OpenHarmony：hdc 等价命令（尽力而为，未在真机逐一验证）。"""
    kind = "hdc"

    def _cmd(self, *args, timeout=20, cap=OUTPUT_CAP, stdin=None):
        return _run([self.exe, "-t", self.serial, *args], timeout, cap, stdin)

    def screenshot(self):
        remote = f"/data/local/tmp/hg_remote_{uuid.uuid4().hex[:8]}.jpeg"
        r = self.shell(f"snapshot_display -f {remote}", 20)
        if r["exit"] not in (0, None) or b"fail" in r["stdout"].lower():
            raise RemoteError(502, "截图失败: " + r["stdout"][:200].decode(errors="replace"))
        with tempfile.TemporaryDirectory(prefix="hg-hdc-") as d:
            local = os.path.join(d, "s.jpeg")
            self._cmd("file", "recv", remote, local, timeout=30)
            self.shell(f"rm -f {remote}", 10)
            try:
                with open(local, "rb") as f:
                    return f.read()
            except OSError:
                raise RemoteError(502, "截图取回失败") from None

    def tap(self, x, y):
        return self.shell(f"uinput -T -c {x} {y}", 10)

    def swipe(self, x1, y1, x2, y2, ms):
        return self.shell(f"uinput -T -m {x1} {y1} {x2} {y2} {ms}", 10 + ms // 1000)

    def text(self, text):
        return self.shell("uitest uiInput text " + _q(text), 20)

    def key(self, key):
        name = HDC_KEYS.get(key)
        if name:
            return self.shell(f"uitest uiInput keyEvent {name}", 10)
        return self.shell(f"uinput -K -d {int(key)} -u {int(key)}", 10)

    def install(self, path, replace=True, grant=False):
        return self._cmd("install", *(["-r"] if replace else []), path, timeout=300)

    def push(self, local, remote):
        return self._cmd("file", "send", local, remote, timeout=120)

    def pull(self, remote, local):
        return self._cmd("file", "recv", remote, local, timeout=120)

    def logs(self, lines, spec):
        grep = (" | grep -i " + _q(spec[0])) if spec else ""
        return self.shell(f"hilog -x{grep} | tail -n {int(lines)}", 20)

    def start_app(self, package, activity):
        return self.shell(f"aa start -b {_q(package)} -a {_q(activity or 'EntryAbility')}", 20)

    def stop_app(self, package):
        return self.shell("aa force-stop " + _q(package), 20)

    def size(self):
        return None


# ═════════════ 会话 ═════════════

class Session:
    def __init__(self, serial, operator, ip, max_minutes, fds, size):
        self.id = uuid.uuid4().hex[:12]
        self.serial, self.operator, self.ip, self.fds, self.size = serial, operator, ip, fds, size
        self.since = self.beat = _now()
        self.deadline = self.since + max_minutes * 60

    def public(self):
        now = _now()
        return {"session_id": self.id, "serial": self.serial, "operator": self.operator["name"],
                "since": _iso(self.since), "expires_in_s": int(min(self.beat + SESSION_TTL, self.deadline) - now),
                "deadline": _iso(self.deadline), "heartbeat_ttl_s": SESSION_TTL, "screen_size": self.size}


class Remote:
    def __init__(self, storage, adb="adb", hdc=None, config_dir=None, reaper=True):
        cfg = config_dir or CONFIG_DIR
        self.operators = Operators(os.path.join(cfg, "remote.operators.json"))
        self.policy = Policy(os.path.join(cfg, "remote.devices.json"))
        self.audit = Audit(os.path.join(cfg, "remote.audit.jsonl"))
        self.occupancy = Occupancy(storage)
        self.adb, self.hdc = adb, hdc
        self.rate = RateLimit()
        self.sessions, self._lock = {}, threading.RLock()
        self.web, self.fails = {}, {}
        self._devcache = (0, [])
        if reaper:
            threading.Thread(target=self._reaper, daemon=True, name="remote-reaper").start()

    # ── 设备枚举（只问 adb/hdc 服务端，不在设备上跑命令） ──
    def _enumerate(self):
        if _now() - self._devcache[0] < 2:
            return self._devcache[1]
        found = []
        if self.adb:
            r = _run([self.adb, "devices", "-l"], 10)
            for line in r["stdout"].decode(errors="replace").splitlines()[1:]:
                f = line.split()
                if len(f) >= 2 and SERIAL_RE.fullmatch(f[0]):
                    info = dict(x.split(":", 1) for x in f[2:] if ":" in x)
                    found.append({"serial": f[0], "transport": "adb", "state": f[1], "model": info.get("model")})
        if self.hdc:
            r = _run([self.hdc, "list", "targets"], 10)
            for line in r["stdout"].decode(errors="replace").splitlines():
                s = line.strip()
                if s and SERIAL_RE.fullmatch(s) and "[Empty]" not in s:
                    found.append({"serial": s, "transport": "hdc", "state": "device", "model": None})
        self._devcache = (_now(), found)
        return found

    def _backend(self, serial):
        for d in self._enumerate():
            if d["serial"] == serial:
                if d["state"] != "device":
                    raise RemoteError(409, f"设备 {serial} 状态是 {d['state']}，不可操作")
                return (HdcDevice(self.hdc, serial) if d["transport"] == "hdc" else AdbDevice(self.adb, serial))
        raise RemoteError(404, f"没有在线设备 {serial}")

    def devices(self):
        policy = self.policy.load()
        try:
            holders = self.occupancy.holders()
            occ_err = None
        except RemoteError as e:
            holders, occ_err = [], e.payload["error"]
        out = []
        for d in self._enumerate():
            s = d["serial"]
            names = self.policy.aliases(s, policy)
            enabled = self.policy.enabled(s, policy)
            sess = self.sessions.get(s)
            item = {**d, "enabled": enabled, "aliases": [n for n in names if n != s],
                    "ignore_global_lock": self.policy.ignores_global_lock(s, policy), "holder": None}
            if sess:
                item["holder"] = {"kind": "remote", **sess.public()}
            else:
                hit = [h for h in holders if h["serial"] in names]
                glob_ = [h for h in holders if h["serial"] is None]
                if hit:
                    item["holder"] = {"kind": "experiment", **hit[0]}
                elif glob_ and not item["ignore_global_lock"]:
                    item["holder"] = {"kind": "experiment", "global": True, **glob_[0]}
                elif enabled and not occ_err:
                    try:
                        if self.occupancy.probe(names):
                            item["holder"] = {"kind": "phonefarm-lease", "label": None,
                                              "note": "按设备锁被 phonefarm lease 持有（没有记录详情）"}
                    except OSError:
                        pass
            out.append(item)
        return {"devices": out, "lock_storage_error": occ_err,
                "global_holders": [h for h in holders if h["serial"] is None]}

    # ── 会话 ──
    def _check_free(self, serial, names, policy):
        holders = self.occupancy.blocking(serial, names, self.policy.ignores_global_lock(serial, policy))
        if holders:
            raise RemoteError(409, describe_holders(serial, holders), holders=holders)

    def acquire(self, op, serial, ip, max_minutes=None):
        policy = self.policy.load()
        if not self.policy.enabled(serial, policy):
            raise RemoteError(403, f"设备 {serial} 没对远程开放（主人可在节点上 node_remote.py device enable {serial}）")
        backend = self._backend(serial)
        minutes = MAX_MINUTES_DEFAULT if max_minutes in (None, "") else int(max_minutes)
        if not 1 <= minutes <= MAX_MINUTES_CAP:
            raise RemoteError(400, f"max_minutes 取 1..{MAX_MINUTES_CAP}")
        names = self.policy.aliases(serial, policy)
        with self._lock:
            cur = self.sessions.get(serial)
            if cur:
                if cur.operator["id"] != op["id"]:
                    raise RemoteError(409, f"设备 {serial} 正被 {cur.operator['name']} 远程使用（自 {_iso(cur.since)}）",
                                      holder=cur.public())
                cur.beat = _now()
                return {"reused": True, **cur.public()}
            self._check_free(serial, names, policy)
            fds = self.occupancy.try_lock(names)
            if fds is None:
                raise RemoteError(409, f"设备 {serial} 的按设备锁被 phonefarm lease 持有（实验或别的工具正在用）",
                                  holders=self.occupancy.blocking(serial, names, True))
            try:
                self._check_free(serial, names, policy)  # 拿锁之后再看一次，堵住检查与上锁之间的窗口
            except RemoteError:
                for fd in fds:
                    os.close(fd)
                raise
            sess = Session(serial, op, ip, minutes, fds, None)
            self.sessions[serial] = sess
        try:
            sess.size = backend.size()
        except RemoteError:
            pass
        return {"reused": False, **sess.public()}

    def _drop(self, sess):
        with self._lock:
            if self.sessions.get(sess.serial) is sess:
                del self.sessions[sess.serial]
            for fd in sess.fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            sess.fds = []

    def session_of(self, op, serial):
        sess = self.sessions.get(serial)
        if not sess:
            raise RemoteError(409, f"先拿设备 {serial} 的控制权（acquire）", need_session=True)
        if sess.operator["id"] != op["id"]:
            raise RemoteError(409, f"设备 {serial} 正被 {sess.operator['name']} 远程使用", holder=sess.public())
        return sess

    def release(self, op, serial):
        sess = self.session_of(op, serial)
        self._drop(sess)
        return {"released": True, "serial": serial, "held_s": int(_now() - sess.since)}

    def _reaper(self):
        while True:
            time.sleep(5)
            self.reap()

    def reap(self):
        now = _now()
        for sess in list(self.sessions.values()):
            why = "deadline" if now >= sess.deadline else "heartbeat" if now - sess.beat > SESSION_TTL else None
            if why:
                self._drop(sess)
                self.audit.write(op=sess.operator["name"], op_id=sess.operator["id"], ip=sess.ip, serial=sess.serial,
                                 action="auto-release", args={"reason": why}, result={"ok": True})

    # ── 动作 ──
    def do(self, op, ip, serial, action, params, body=None):
        """所有入口（HTTP / 网页 / MCP）共用：校验 → 限速 → 执行 → 审计。返回 (dict, 二进制或 None, mime)。"""
        if not SERIAL_RE.fullmatch(serial or ""):
            raise RemoteError(400, "serial 不合法")
        params = params or {}
        audit_args = {k: (v[:300] if isinstance(v, str) else v) for k, v in params.items() if k != "data"}
        if body is not None:
            audit_args["bytes"] = len(body)
        result, ok, err = None, False, None
        key = op["id"]
        try:
            if action == "acquire":
                self.rate.take(key, 1)
                try:
                    result = self.acquire(op, serial, ip, params.get("max_minutes"))
                finally:
                    self.rate.done(key)
            elif action == "release":
                result = self.release(op, serial)
            elif action == "renew":
                sess = self.session_of(op, serial)
                sess.beat = _now()
                result = sess.public()
            else:
                result = self._device_action(op, serial, action, params, body)
            ok = True
            return result
        except RemoteError as e:
            err = e.payload["error"]
            raise
        except (ValueError, TypeError) as e:
            err = str(e)
            raise RemoteError(400, err) from None
        finally:
            if action != "renew" or not ok:
                summary = None
                if isinstance(result, tuple):
                    summary = {k: v for k, v in result[0].items() if k in ("exit", "bytes", "timed_out", "size")}
                elif isinstance(result, dict):
                    summary = {k: v for k, v in result.items()
                               if k in ("exit", "timed_out", "truncated", "session_id", "reused", "released")}
                self.audit.write(op=op["name"], op_id=op["id"], ip=ip, serial=serial, action=action,
                                 args=audit_args, result={"ok": ok, **({"error": err} if err else {}),
                                                          **(summary or {})})

    def _device_action(self, op, serial, action, p, body):
        handler = ACTIONS.get(action)
        if not handler:
            raise RemoteError(404, f"没有动作 {action}")
        policy = self.policy.load()
        if not self.policy.enabled(serial, policy):
            if self.sessions.get(serial):
                self._drop(self.sessions[serial])
            raise RemoteError(403, f"设备 {serial} 没对远程开放（或已被主人关闭；已有会话随之释放）")
        sess = self.session_of(op, serial)
        self._check_free(serial, self.policy.aliases(serial, policy), policy)
        self.rate.take(op["id"], COST.get(action, 1))
        try:
            sess.beat = _now()
            out = handler(self, self._backend(serial), p, body)
            sess.beat = _now()
            return out
        finally:
            self.rate.done(op["id"])

    # ── 认证 ──
    def _ip(self, h):
        return h.headers.get("CF-Connecting-IP", h.client_address[0])

    def _fail(self, ip):
        now = _now()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < AUTH_FAIL_WINDOW] + [now]

    def _throttled(self, ip):
        now = _now()
        return len([t for t in self.fails.get(ip, []) if now - t < AUTH_FAIL_WINDOW]) >= AUTH_FAILS_MAX

    def bearer(self, h):
        ip = self._ip(h)
        if self._throttled(ip):
            raise RemoteError(429, "认证失败太多次，10 分钟后再试")
        if h.headers.get("X-Forwarded-Proto", "https") != "https":
            raise RemoteError(400, "只接受 HTTPS（经隧道）；这个令牌已经明文发出过，请让主人 revoke 重签")
        auth = h.headers.get("Authorization", "")
        op = self.operators.verify(auth[7:].strip()) if auth.startswith("Bearer ") else None
        if not op:
            self._fail(ip)
            raise RemoteError(401, "需要操作员令牌：Authorization: Bearer hgop_...（找节点主人签发）")
        return op

    def web_op(self, h):
        for part in h.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v in self.web:
                op, exp = self.web[v]
                if exp > _now() and any(o["id"] == op["id"] and not o["revoked"] for o in self.operators.list()):
                    return op
                self.web.pop(v, None)
        return None


# ═════════════ 具体动作（backend 已按设备类型选好） ═════════════

def _num(p, k, lo=0, hi=100000):
    v = p.get(k)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{k} 需要数字")
    v = int(round(v))
    if not lo <= v <= hi:
        raise ValueError(f"{k} 超出范围")
    return v


def _out(r):
    return {"exit": r["exit"], "timed_out": r["timed_out"], "truncated": r["truncated"],
            "stdout": r["stdout"].decode(errors="replace"), "stderr": r["stderr"].decode(errors="replace")}


def _a_screenshot(remote, dev, p, body):
    png = dev.screenshot()
    size = png_size(png)
    max_side = int(p.get("max", 1280) or 0)
    data, mime = shrink(png, max_side) if max_side and dev.kind == "adb" else (png, "image/png" if png[:4] == b"\x89PNG" else "image/jpeg")
    return ({"size": list(size) if size else None, "bytes": len(data), "mime": mime}, data, mime)


def _a_tap(remote, dev, p, body):
    return _out(dev.tap(_num(p, "x"), _num(p, "y")))


def _a_swipe(remote, dev, p, body):
    ms = _num({"ms": p.get("ms", 300)}, "ms", 1, 10000)
    return _out(dev.swipe(_num(p, "x1"), _num(p, "y1"), _num(p, "x2"), _num(p, "y2"), ms))


def _a_text(remote, dev, p, body):
    text = p.get("text")
    if not isinstance(text, str) or not text or len(text) > TEXT_MAX or any(ord(c) < 32 for c in text):
        raise ValueError(f"text 需要 1..{TEXT_MAX} 个可见字符（不含换行；换行用 key enter）")
    return _out(dev.text(text))


def _a_key(remote, dev, p, body):
    key = p.get("key")
    if isinstance(key, str) and key.lower() in ANDROID_KEYS:
        key = key.lower()
    elif isinstance(key, int) and not isinstance(key, bool) and 0 <= key <= 400:
        pass
    elif isinstance(key, str) and key.isdigit() and int(key) <= 400:
        key = int(key)
    else:
        raise ValueError("key 取 back/home/recents/enter/... 或 0..400 的 keycode")
    return _out(dev.key(key))


def _a_shell(remote, dev, p, body):
    cmd = p.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 8000 or "\x00" in cmd:
        raise ValueError("cmd 需要 1..8000 字符")
    timeout = _num({"t": p.get("timeout", SHELL_TIMEOUT_DEFAULT)}, "t", 1, SHELL_TIMEOUT_MAX)
    return _out(dev.shell(cmd, timeout))


def _with_file(body, suffix, fn):
    with tempfile.TemporaryDirectory(prefix="hg-remote-") as d:
        path = os.path.join(d, "upload" + suffix)
        with open(path, "wb") as f:
            f.write(body)
        return fn(path)


def _a_install(remote, dev, p, body):
    if not body or len(body) > APK_MAX:
        raise ValueError(f"安装包为空或超过 {APK_MAX >> 20} MiB")
    if dev.kind == "adb" and body[:2] != b"PK":
        raise ValueError("不是 APK（zip）文件")
    suffix = ".hap" if dev.kind == "hdc" else ".apk"
    r = _with_file(body, suffix, lambda path: dev.install(path, p.get("replace", True) not in (False, "0"),
                                                          p.get("grant") in (True, "1")))
    return {**_out(r), "sha256": hashlib.sha256(body).hexdigest()}


def _a_uninstall(remote, dev, p, body):
    pkg = p.get("package")
    if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg):
        raise ValueError("package 不合法")
    return _out(dev.uninstall(pkg))


def _device_path(p):
    path = p.get("path")
    if not isinstance(path, str) or not DEVICE_PATH_RE.fullmatch(path):
        raise ValueError("path 需要设备上的绝对路径（不含引号、反斜杠、控制字符）")
    return path


def _a_push(remote, dev, p, body):
    path = _device_path(p)
    if body is None or len(body) > FILE_MAX:
        raise ValueError(f"文件超过 {FILE_MAX >> 20} MiB")
    return {**_out(_with_file(body, "", lambda local: dev.push(local, path))), "bytes": len(body)}


def _a_pull(remote, dev, p, body):
    path = _device_path(p)
    size = dev.shell("stat -c %s " + _q(path), 10)["stdout"].decode(errors="replace").strip()
    if not size.isdigit():
        raise RemoteError(404, f"设备上读不到 {path}")
    if int(size) > FILE_MAX:
        raise RemoteError(413, f"{path} 有 {int(size)} 字节，超过 {FILE_MAX >> 20} MiB 上限")
    with tempfile.TemporaryDirectory(prefix="hg-remote-") as d:
        local = os.path.join(d, "pulled")
        r = dev.pull(path, local)
        if r["exit"] != 0 or not os.path.isfile(local):
            raise RemoteError(502, "pull 失败: " + r["stderr"].decode(errors="replace")[:300])
        if os.path.getsize(local) > FILE_MAX:
            raise RemoteError(413, "文件超过上限")
        with open(local, "rb") as f:
            data = f.read()
    return ({"bytes": len(data), "path": path}, data, "application/octet-stream")


def _a_logcat(remote, dev, p, body):
    lines = _num({"n": p.get("lines", 200)}, "n", 1, 5000)
    spec = p.get("filter") or []
    if isinstance(spec, str):
        spec = spec.split()
    if not isinstance(spec, list) or len(spec) > 20 or any(
            not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_.:*/@-]{1,100}", s) for s in spec):
        raise ValueError("filter 是 logcat 过滤表达式，如 ['ActivityManager:I', '*:S']")
    return _out(dev.logs(lines, spec))


def _a_app_start(remote, dev, p, body):
    pkg, act = p.get("package"), p.get("activity")
    if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg):
        raise ValueError("package 不合法")
    if act is not None and (not isinstance(act, str) or not ACTIVITY_RE.fullmatch(act)):
        raise ValueError("activity 不合法")
    return _out(dev.start_app(pkg, act))


def _a_app_stop(remote, dev, p, body):
    pkg = p.get("package")
    if not isinstance(pkg, str) or not PACKAGE_RE.fullmatch(pkg):
        raise ValueError("package 不合法")
    return _out(dev.stop_app(pkg))


ACTIONS = {"screenshot": _a_screenshot, "tap": _a_tap, "swipe": _a_swipe, "text": _a_text, "key": _a_key,
           "shell": _a_shell, "install": _a_install, "uninstall": _a_uninstall, "push": _a_push, "pull": _a_pull,
           "logcat": _a_logcat, "app_start": _a_app_start, "app_stop": _a_app_stop}
BINARY_UPLOAD = {"install": APK_MAX, "push": FILE_MAX}


# ═════════════ MCP ═════════════

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_S = {"serial": {"type": "string", "description": "设备序列号（hgremote_devices 列出的）"}}
MCP_TOOLS = [
    ("hgremote_devices", "列出节点上的设备：序列号、型号、是否对远程开放、当前持有者（远程会话/实验）。", _schema({})),
    ("hgremote_acquire", "拿到设备的控制权（会话）。所有设备操作前必须先拿；实验占用时会被拒绝并说明是谁。"
     f"会话 {SESSION_TTL}s 内没有任何动作就自动释放；max_minutes 是最长时长。",
     _schema({**_S, "max_minutes": {"type": "integer", "minimum": 1, "maximum": MAX_MINUTES_CAP}}, ["serial"])),
    ("hgremote_renew", "续期会话心跳（任何设备动作也会续期）。", _schema(_S, ["serial"])),
    ("hgremote_release", "用完释放设备。", _schema(_S, ["serial"])),
    ("hgremote_screenshot", "截屏，返回图片。图片可能被缩小；tap/swipe 坐标用设备像素（文字里给出原始尺寸）。",
     _schema({**_S, "max": {"type": "integer", "description": "长边像素上限，缺省 1280，0 = 原图 PNG"}}, ["serial"])),
    ("hgremote_tap", "点击设备像素坐标。", _schema({**_S, "x": {"type": "integer"}, "y": {"type": "integer"}}, ["serial", "x", "y"])),
    ("hgremote_swipe", "滑动（设备像素），ms 为时长。",
     _schema({**_S, **{k: {"type": "integer"} for k in ("x1", "y1", "x2", "y2", "ms")}}, ["serial", "x1", "y1", "x2", "y2"])),
    ("hgremote_text", "向当前焦点输入 ASCII 文本（不含换行）。", _schema({**_S, "text": {"type": "string"}}, ["serial", "text"])),
    ("hgremote_key", "按键：back/home/recents/enter/del/power/volume_up/... 或 Android keycode 数字。",
     _schema({**_S, "key": {"type": ["string", "integer"]}}, ["serial", "key"])),
    ("hgremote_shell", f"在设备上跑 shell 命令（adb shell / hdc shell），超时缺省 {SHELL_TIMEOUT_DEFAULT}s、最多 {SHELL_TIMEOUT_MAX}s，"
     f"输出截断到 {OUTPUT_CAP >> 10} KiB。",
     _schema({**_S, "cmd": {"type": "string"}, "timeout": {"type": "integer"}}, ["serial", "cmd"])),
    ("hgremote_logcat", "logcat 尾部（鸿蒙为 hilog）。filter 例：['ActivityManager:I','*:S']。",
     _schema({**_S, "lines": {"type": "integer"}, "filter": {"type": "array", "items": {"type": "string"}}}, ["serial"])),
    ("hgremote_app_start", "启动应用：只给 package 则启动其 launcher；也可给 activity。",
     _schema({**_S, "package": {"type": "string"}, "activity": {"type": "string"}}, ["serial", "package"])),
    ("hgremote_app_stop", "强停应用。", _schema({**_S, "package": {"type": "string"}}, ["serial", "package"])),
    ("hgremote_install_apk", "安装 APK（base64，≤ 48 MiB；更大的用 HTTP PUT /remote/devices/<serial>/install）。",
     _schema({**_S, "apk_base64": {"type": "string"}, "replace": {"type": "boolean"}, "grant": {"type": "boolean"}},
             ["serial", "apk_base64"])),
    ("hgremote_uninstall", "卸载应用。", _schema({**_S, "package": {"type": "string"}}, ["serial", "package"])),
    ("hgremote_push", f"把小文件（base64，≤ {FILE_MAX >> 20} MiB）推到设备绝对路径。",
     _schema({**_S, "path": {"type": "string"}, "data_base64": {"type": "string"}}, ["serial", "path", "data_base64"])),
    ("hgremote_pull", f"从设备取小文件（≤ {FILE_MAX >> 20} MiB），返回 base64。",
     _schema({**_S, "path": {"type": "string"}}, ["serial", "path"])),
]
MCP_ACTION = {"hgremote_acquire": "acquire", "hgremote_renew": "renew", "hgremote_release": "release",
              "hgremote_screenshot": "screenshot", "hgremote_tap": "tap", "hgremote_swipe": "swipe",
              "hgremote_text": "text", "hgremote_key": "key", "hgremote_shell": "shell", "hgremote_logcat": "logcat",
              "hgremote_app_start": "app_start", "hgremote_app_stop": "app_stop", "hgremote_install_apk": "install",
              "hgremote_uninstall": "uninstall", "hgremote_push": "push", "hgremote_pull": "pull"}


def mcp_call(remote, op, ip, name, args):
    args = dict(args or {})
    if name == "hgremote_devices":
        return [{"type": "text", "text": json.dumps(remote.devices(), ensure_ascii=False)}], False
    action = MCP_ACTION.get(name)
    if not action:
        raise RemoteError(404, f"没有工具 {name}")
    serial = args.pop("serial", None)
    body = None
    for k in ("apk_base64", "data_base64"):
        if k in args:
            try:
                body = base64.b64decode(args.pop(k), validate=True)
            except (ValueError, TypeError):
                raise RemoteError(400, f"{k} 不是合法 base64") from None
    out = remote.do(op, ip, serial, action, args, body)
    if isinstance(out, tuple):
        meta, data, mime = out
        if action == "screenshot":
            note = {**meta, "hint": "tap/swipe 用设备像素：按 size（原始宽高）换算缩放后的图片坐标"}
            return [{"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime},
                    {"type": "text", "text": json.dumps(note, ensure_ascii=False)}], False
        return [{"type": "text", "text": json.dumps({**meta, "data_base64": base64.b64encode(data).decode()})}], False
    failed = isinstance(out, dict) and (out.get("exit") not in (0, None) or out.get("timed_out"))
    return [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], bool(failed)


def mcp_handle(remote, op, ip, msg):
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if "id" not in msg:
        return None  # 通知
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-03-26",
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "hgamey-remote", "version": "1"},
                  "instructions": "HGamey 远程真机：先 hgremote_devices，再 hgremote_acquire 拿控制权，"
                                  "操作完 hgremote_release。实验占用的设备会被拒绝，别重试抢占。"}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in MCP_TOOLS]}
    elif method == "tools/call":
        try:
            content, is_error = mcp_call(remote, op, ip, params.get("name"), params.get("arguments"))
        except RemoteError as e:
            content, is_error = [{"type": "text", "text": json.dumps(e.payload, ensure_ascii=False)}], True
        result = {"content": content, "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


# ═════════════ HTTP 入口（node_service 只调 handle） ═════════════

DASH_HEADERS = {"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}  # no-store/nosniff 由 node_service 加


def _send(h, code, data, ctype="application/json; charset=utf-8", extra=None):
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False).encode()
    h._send(code, data, ctype, {**DASH_HEADERS, **(extra or {})})


def _read_body(h, maximum):
    lengths = h.headers.get_all("Content-Length", []) or ["0"]
    if h.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
        h.close_connection = True
        raise RemoteError(400, "需要单个 Content-Length（不支持 chunked）")
    n = int(lengths[0])
    if n > maximum:
        h.close_connection = True
        raise RemoteError(413, f"请求体超过 {maximum >> 20 or 1} MiB 上限" if maximum >= 1 << 20 else "请求体太大")
    h.connection.settimeout(120)
    data = h.rfile.read(n) if n else b""
    if len(data) != n:
        h.close_connection = True
        raise RemoteError(400, "请求体不完整")
    return data


def handle(remote, h, method, parts, query, board=None):
    """parts: /remote/... 或 /dash/api/remote/...。返回 True 表示已处理。"""
    if parts == ["dash", "control"]:  # 控制台页面：查看密码之后再用操作员令牌登录
        if not (board and board.authed(h)):
            return h._send(303, b"", "text/plain", {**DASH_HEADERS, "Location": "/dash/login"}) or True
        return board._static(h, "control.html") or True
    dash = parts[:3] == ["dash", "api", "remote"]
    sub = parts[3:] if dash else parts[1:]
    q = {k: v[-1] for k, v in parse_qs(query or "").items()}
    ip = remote._ip(h)
    try:
        if dash:
            if not (board and board.authed(h)):
                raise RemoteError(401, "需要查看密码")
            if h.headers.get("X-HG") != "1":
                h.close_connection = True
                raise RemoteError(400, "请求格式不对（缺 X-HG 头）")
            if sub == ["login"] and method == "POST":
                if remote._throttled(ip):
                    raise RemoteError(429, "认证失败太多次，10 分钟后再试")
                data = json.loads(_read_body(h, JSON_MAX) or b"{}")
                op = remote.operators.verify(str(data.get("token", "")).strip())
                if not op:
                    remote._fail(ip)
                    time.sleep(1)
                    raise RemoteError(403, "操作员令牌不对或已吊销")
                sid = secrets.token_urlsafe(32)
                remote.web[sid] = (op, _now() + WEB_SESSION_S)
                remote.audit.write(op=op["name"], op_id=op["id"], ip=ip, serial=None, action="web-login", args={},
                                   result={"ok": True})
                ck = f"{COOKIE}={sid}; Path=/dash; Max-Age={WEB_SESSION_S}; HttpOnly; Secure; SameSite=Strict"
                return _send(h, 200, {"operator": op["name"]}, extra={"Set-Cookie": ck}) or True
            if sub == ["logout"] and method == "POST":
                _read_body(h, JSON_MAX)
                for part in h.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == COOKIE:
                        remote.web.pop(v, None)
                ck = f"{COOKIE}=; Path=/dash; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
                return _send(h, 200, {"ok": True}, extra={"Set-Cookie": ck}) or True
            op = remote.web_op(h)
            if not op:
                if sub == ["whoami"]:
                    return _send(h, 200, {"operator": None}) or True
                raise RemoteError(401, "需要操作员令牌登录", need_operator=True)
            if method in ("POST", "DELETE") and "application/json" not in h.headers.get("Content-Type", ""):
                h.close_connection = True
                raise RemoteError(400, "写接口只收 JSON")
        else:
            op = remote.bearer(h)
        if sub == ["whoami"]:
            return _send(h, 200, {"operator": op["name"], "id": op["id"]}) or True
        if sub == ["mcp"] and not dash:
            if method != "POST":
                return _send(h, 405, {"error": "MCP 只支持 POST（JSON 响应，无 SSE）"}, extra={"Allow": "POST"}) or True
            try:
                msg = json.loads(_read_body(h, MCP_MAX))
            except ValueError:
                return _send(h, 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) or True
            out = ([r for r in (mcp_handle(remote, op, ip, m) for m in msg) if r] if isinstance(msg, list)
                   else mcp_handle(remote, op, ip, msg))
            if not out:
                return _send(h, 202, b"") or True
            extra = {"Mcp-Session-Id": uuid.uuid4().hex} if isinstance(msg, dict) and msg.get("method") == "initialize" else {}
            return _send(h, 200, out, extra=extra) or True
        if sub == ["devices"] and method == "GET":
            return _send(h, 200, remote.devices()) or True
        if len(sub) == 3 and sub[0] == "devices":
            serial, action = unquote(sub[1]), sub[2]
            if action == "session":
                action = {"POST": "acquire", "DELETE": "release"}.get(method)
                if not action:
                    raise RemoteError(405, "session 用 POST 获取、DELETE 释放")
            if action in BINARY_UPLOAD:
                if method != "PUT":
                    raise RemoteError(405, f"{action} 用 PUT，请求体是文件本身（application/octet-stream）")
                if dash and h.headers.get("X-HG") != "1":
                    raise RemoteError(400, "请求格式不对")
                body = _read_body(h, BINARY_UPLOAD[action])
                params = dict(q)
            elif method == "GET" and action in ("screenshot", "pull", "logcat"):
                params, body = dict(q), None
                if "lines" in params:
                    params["lines"] = int(params["lines"]) if params["lines"].isdigit() else params["lines"]
                if "max" in params:
                    params["max"] = int(params["max"]) if params["max"].isdigit() else 1280
            elif method in ("POST", "DELETE"):
                raw = _read_body(h, JSON_MAX)
                params = json.loads(raw or b"{}")
                if not isinstance(params, dict):
                    raise RemoteError(400, "请求体要是 JSON 对象")
                body = None
            else:
                raise RemoteError(405, "方法不对")
            out = remote.do(op, ip, serial, action, params, body)
            if isinstance(out, tuple):
                meta, data, mime = out
                extra = {"X-Meta": json.dumps(meta)}
                if meta.get("size"):
                    extra.update({"X-Device-W": str(meta["size"][0]), "X-Device-H": str(meta["size"][1])})
                return _send(h, 200, data, mime, extra) or True
            return _send(h, 200, out) or True
        raise RemoteError(404, "没有这个远程接口")
    except RemoteError as e:
        return _send(h, e.status, e.payload) or True
    except ValueError as e:
        return _send(h, 400, {"error": f"请求不对: {e}"}) or True
    except Exception as e:  # noqa: BLE001
        print(time.strftime("%F %T"), f"remote {method} {'/'.join(parts)} 出错: {e!r}", file=sys.stderr, flush=True)
        return _send(h, 500, {"error": "remote 内部错误"}) or True


def storage_from_leases(leases):
    """从 node 的 LeaseState（phonefarm lease status --json）取锁目录；成功后缓存。"""
    cache = {}

    def get():
        if "paths" not in cache:
            snap = leases.snapshot(refresh=True, force=True) if leases else {}
            st = snap.get("storage") or {}
            if st.get("readiness") != "ready":
                return None
            cache["paths"] = (st.get("device_lease_dir"), st.get("devlock_dir"))
        return cache["paths"]
    return get


# ═════════════ CLI ═════════════

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="远程操作：操作员令牌、设备开放名单、审计")
    sp = ap.add_subparsers(dest="cmd", required=True)
    o = sp.add_parser("operator")
    o.add_argument("verb", choices=["add", "list", "revoke"])
    o.add_argument("name", nargs="?")
    d = sp.add_parser("device")
    d.add_argument("verb", choices=["list", "enable", "disable", "default"])
    d.add_argument("serial", nargs="?")
    d.add_argument("--ignore-global-lock", action="store_true",
                   help="这台设备不受只写标签的全局 devlock 阻挡（确认实验从不不带 serial 地碰它）")
    a = sp.add_parser("audit")
    a.add_argument("-n", type=int, default=50)
    args = ap.parse_args(argv)
    cfg = CONFIG_DIR
    if args.cmd == "operator":
        ops = Operators(os.path.join(cfg, "remote.operators.json"))
        if args.verb == "add":
            token, oid = ops.add(args.name)
            print(f"操作员 {args.name}（id {oid}）的令牌，只显示这一次：\n{token}")
        elif args.verb == "revoke":
            hit = ops.revoke(args.name or "")
            print(f"已吊销 {hit}" if hit else "没找到有效的操作员")
            return 0 if hit else 1
        else:
            for x in ops.list():
                print(f"{x['id']}  {x['name']:<20} 签发 {x['created']}" + (f"  已吊销 {x['revoked']}" if x["revoked"] else ""))
    elif args.cmd == "device":
        pol = Policy(os.path.join(cfg, "remote.devices.json"))
        if args.verb in ("enable", "disable") and not (args.serial and SERIAL_RE.fullmatch(args.serial)):
            ap.error("需要合法的 serial")
        if args.verb == "enable":
            pol.enable(args.serial, args.ignore_global_lock)
        elif args.verb == "disable":
            pol.disable(args.serial)
        elif args.verb == "default":
            if args.serial not in ("allow", "deny"):
                ap.error("default allow|deny")
            p = pol.load()
            p["default"] = args.serial
            pol.save(p)
        print(json.dumps(pol.load(), ensure_ascii=False, indent=1))
    elif args.cmd == "audit":
        for line in Audit(os.path.join(cfg, "remote.audit.jsonl")).tail(args.n):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
