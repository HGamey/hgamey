#!/usr/bin/env python3
"""Authenticated remote gateway; PF owns device locks, commands and recovery.

Operator credentials, policy and HTTP/MCP remain here. No handler performs raw
ADB/HDC device I/O or grants ownership based on labels. Saved PF references and
stable request IDs survive node restart; unknown actions are queried, not replayed.
"""
import base64, fnmatch, hashlib, hmac, json, os, re, secrets, subprocess, sys, tempfile
import threading, time, uuid
from urllib.parse import parse_qs, unquote
from remote_owner import OwnerError

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
MCP_MAX = JSON_MAX          # Only typed short requests; files use the game distribution API.
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
            try:
                age = round(_now() - os.stat(os.path.join(d, "owner")).st_mtime, 1)
            except OSError:
                age = None
            kind = ("recovery" if marker.get("recovery_required") or marker.get("command_pending") else
                    "phonefarm-lease" if marker.get("format") == 1 else
                    "devlock" if marker.get("schema") == "phonefarm/devlock-owner@1" or not marker else "unknown")
            found.append({"dir": name, "label": label, "serial": serial, "since": _since(owner),
                          "heartbeat_age_s": age, "kind": kind, "job_id": marker.get("job_id")})
        return found


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

def _run(argv, timeout, cap=OUTPUT_CAP, env=None):
    # Host transport enumeration only; bounded files avoid unbounded PIPE buffers.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env)
        except OSError as exc:
            raise RemoteError(503, str(exc)) from exc
        end, limited = time.monotonic() + timeout, False
        while proc.poll() is None:
            if time.monotonic() >= end or max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > cap:
                limited = True
                proc.kill()
                proc.wait()
                break
            time.sleep(.02)
        out.seek(0); err.seek(0)
        return {'exit': None if limited else proc.returncode, 'stdout': out.read(cap), 'stderr': err.read(cap)}


ACTIONS = {'screenshot', 'tap', 'swipe', 'text', 'key', 'app_start', 'app_stop'}
UNSUPPORTED = {'shell', 'install', 'uninstall', 'push', 'pull', 'logcat'}
BINARY_UPLOAD = {}  # Legacy uploads are refused before reading their body.


def valid_request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
        raise RemoteError(400, 'request_id requires 1..64 ASCII letters, digits, dot, underscore or hyphen')
    return value


def validate_action(action, params):
    fields = {'tap': {'x', 'y'}, 'swipe': {'x1', 'y1', 'x2', 'y2', 'ms'},
              'text': {'text'}, 'key': {'key'}, 'screenshot': {'max', 'max_side'},
              'app_start': {'package', 'activity'}, 'app_stop': {'package'}, 'renew': set(), 'release': set()}
    if set(params) - fields[action]:
        raise RemoteError(400, 'unsupported action parameters')
    p = dict(params)
    def integer(key, low, high):
        if type(p.get(key)) is not int or not low <= p[key] <= high:
            raise RemoteError(400, f'{key} requires integer {low}..{high}')
    if action in ('tap', 'swipe'):
        for key in ('x', 'y') if action == 'tap' else ('x1', 'y1', 'x2', 'y2'):
            integer(key, 0, 100000)
        if action == 'swipe':
            p.setdefault('ms', 300)
            integer('ms', 1, 10000)
    elif action == 'screenshot':
        if 'max' in p and 'max_side' in p:
            raise RemoteError(400, 'use max_side only')
        p['max_side'] = p.pop('max', p.get('max_side', 1280))
        integer('max_side', 0, 8192)
    elif action == 'text':
        text = p.get('text')
        if not isinstance(text, str) or not 1 <= len(text) <= TEXT_MAX or any(ord(c) < 32 for c in text):
            raise RemoteError(400, 'text requires 1..1000 visible characters')
    elif action == 'key':
        key = p.get('key')
        if isinstance(key, str):
            key = ANDROID_KEYS.get(key.lower(), int(key) if key.isdigit() else key)
        if type(key) is not int or not 0 <= key <= 400:
            raise RemoteError(400, 'unknown key or keycode outside 0..400')
        p['key'] = key
    elif action in ('app_start', 'app_stop'):
        if not isinstance(p.get('package'), str) or not PACKAGE_RE.fullmatch(p['package']):
            raise RemoteError(400, 'invalid package')
        if 'activity' in p and (not isinstance(p['activity'], str) or not ACTIVITY_RE.fullmatch(p['activity'])):
            raise RemoteError(400, 'invalid activity')
    return p


# ═════════════ 会话 ═════════════

class Session:
    def __init__(self, ref, descriptor):
        self.ref, self.descriptor = ref, descriptor
        self.serial, self.operator = ref['serial'], ref['operator']

    def public(self):
        return {**self.descriptor, 'operator': self.operator['name'],
                'acquisition_id': self.ref['acquisition_id'], 'since': _iso(self.ref['created_at']),
                'heartbeat_ttl_s': SESSION_TTL,
                'screen_size': (self.descriptor.get('latest_image') or {}).get('original_size')}


class Remote:
    def __init__(self, storage, adb='adb', hdc=None, config_dir=None, reaper=True, backend=None):
        cfg = config_dir or CONFIG_DIR
        self.operators = Operators(os.path.join(cfg, 'remote.operators.json'))
        self.policy = Policy(os.path.join(cfg, 'remote.devices.json'))
        self.audit = Audit(os.path.join(cfg, 'remote.audit.jsonl'))
        self.occupancy = Occupancy(storage)
        self.adb, self.hdc, self.backend = adb, hdc, backend
        self.rate = RateLimit()
        self.sessions, self._lock = {}, threading.RLock()
        self.web, self.fails = {}, {}
        self._devcache = (0, [])
        self._closed = threading.Event()
        if backend:
            for ref in backend.load():
                self.sessions[ref['id']] = Session(ref, backend.status(ref))
        if reaper:
            threading.Thread(target=self._reaper, daemon=True, name='remote-reaper').start()

    def capabilities(self):
        aliases = {'start_app': 'app_start', 'stop_app': 'app_stop'}
        return {'available': bool(self.backend and self.backend.available),
                'reason': self.backend.reason if self.backend else 'PF remote owner is not configured',
                'actions': ['acquire'] + [aliases.get(x, x) for x in self.backend.actions]
                           if self.backend and self.backend.available else [],
                'ownership': 'strong-lease-v1', 'unsupported': sorted(UNSUPPORTED)}

    def _enumerate(self):
        # These commands enumerate the host transport server; they do not probe
        # size, properties, power or aliases on any device.
        if _now() - self._devcache[0] < 2:
            return self._devcache[1]
        found = []
        if self.adb:
            r = _run([self.adb, 'devices', '-l'], 10, env=getattr(self.backend, 'env', None))
            if r['exit'] != 0:
                raise RemoteError(503, 'ADB device directory unavailable')
            for line in r['stdout'].decode(errors='replace').splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2 and SERIAL_RE.fullmatch(fields[0]):
                    info = dict(x.split(':', 1) for x in fields[2:] if ':' in x)
                    found.append({'serial': fields[0], 'transport': 'adb', 'state': fields[1], 'model': info.get('model')})
        if self.hdc:
            r = _run([self.hdc, 'list', 'targets'], 10, env=getattr(self.backend, 'env', None))
            for line in r['stdout'].decode(errors='replace').splitlines():
                s = line.strip()
                if s and SERIAL_RE.fullmatch(s) and '[Empty]' not in s:
                    found.append({'serial': s, 'transport': 'hdc', 'state': 'device', 'model': None})
        self._devcache = (_now(), found)
        return found

    def _refresh(self, sess):
        sess.descriptor = self.backend.status(sess.ref)
        return sess.public()

    def devices(self):
        policy, caps = self.policy.load(), self.capabilities()
        try:
            holders, occ_err = self.occupancy.holders(), None
        except RemoteError as error:
            holders, occ_err = [], error.payload['error']
        with self._lock:
            sessions = list(self.sessions.values())
        active = [self._refresh(s) for s in sessions]
        active = [s for s in active if s.get('phase') != 'closed']
        out = []
        for d in self._enumerate():
            serial = d['serial']
            names = self.policy.aliases(serial, policy)
            item = {**d, 'enabled': self.policy.enabled(serial, policy),
                    'aliases': [n for n in names if n != serial], 'ignore_global_lock': False,
                    'capabilities': caps['actions'] if caps['available'] and d['transport'] == 'adb' else [],
                    'holder': None}
            matches = [s for s in active if s['serial'] in names]
            if matches:
                item['holder'] = {'kind': 'remote', **matches[-1]}
            elif holders:
                item['holder'] = {'kind': 'experiment', **holders[0], 'global': True}
            out.append(item)
        # Old/offline references remain visible even if host enumeration lost
        # the device. Missing enumeration cannot erase a recovery obligation.
        present = {d['serial'] for d in out}
        for sess in active:
            if sess['serial'] not in present:
                out.append({'serial': sess['serial'], 'transport': 'adb', 'state': 'offline',
                    'enabled': self.policy.enabled(sess['serial'], policy), 'aliases': [],
                    'capabilities': [], 'holder': {'kind': 'remote', **sess}})
        return {'devices': out, 'capabilities': caps, 'lock_storage_error': occ_err,
                'global_holders': holders}

    def _require_backend(self):
        if not self.backend or not self.backend.available:
            raise RemoteError(503, self.capabilities()['reason'], capability='remote-owner-v1')

    def acquire(self, op, serial, ip, params):
        acquisition = valid_request_id(params.get('request_id'))
        minutes = params.get('max_minutes', MAX_MINUTES_DEFAULT)
        if type(minutes) is not int or not 1 <= minutes <= MAX_MINUTES_CAP:
            raise RemoteError(400, f'max_minutes requires 1..{MAX_MINUTES_CAP}')
        self._require_backend()
        with self._lock:
            for sess in self.sessions.values():
                if sess.operator['id'] == op['id'] and sess.ref['acquisition_id'] == acquisition:
                    if sess.serial != serial or sess.ref['max_seconds'] != minutes * 60:
                        raise RemoteError(409, 'acquisition request ID already has different content')
                    return {'reused': True, **self._refresh(sess)}
            if not self.policy.enabled(serial):
                raise RemoteError(403, f'设备 {serial} 未开放远程控制')
            for sess in self.sessions.values():
                state = self._refresh(sess)
                if state.get('phase') != 'closed':
                    raise RemoteError(409, f"设备域由 {sess.operator['name']} 的会话占用或等待恢复", holder=state)
            device = next((d for d in self._enumerate() if d['serial'] == serial), None)
            if not device or device['state'] != 'device':
                raise RemoteError(409, '设备当前不在线')
            if device['transport'] != 'adb':
                raise RemoteError(501, '此 PF remote owner 尚未支持 HDC；未发出设备动作')
            # Advisory records explain refusal. Only PF's Lease, acquired inside
            # start(), can actually grant ownership; labels never bypass it.
            holders = self.occupancy.holders()
            if holders:
                raise RemoteError(409, describe_holders(serial, holders), holders=holders)
            ref = self.backend.start(serial, op, ip, minutes * 60, acquisition)
            sess = Session(ref, self.backend.status(ref))
            self.sessions[ref['id']] = sess
            return {'reused': False, **sess.public()}

    def session_of(self, op, serial, session_id=None, acquisition_id=None):
        if not session_id and not acquisition_id:
            raise RemoteError(400, '需要 session_id 或 acquisition_id；先获取控制权', need_session=True)
        with self._lock:
            sessions = list(self.sessions.values())
        for sess in sessions:
            if sess.serial != serial:
                continue
            # Acquisition references are scoped to an operator; session IDs are
            # still authenticated and never authorize access by themselves.
            if acquisition_id and sess.ref['acquisition_id'] == acquisition_id and sess.operator['id'] == op['id']:
                self._refresh(sess)
                return sess
            if session_id and (sess.descriptor.get('session_id') == session_id or
                               self._refresh(sess).get('session_id') == session_id):
                if sess.operator['id'] != op['id']:
                    raise RemoteError(403, f"会话属于 {sess.operator['name']}")
                return sess
        raise RemoteError(404, '未找到原会话；不会创建或重放设备动作')

    def query(self, op, serial, *, session_id=None, acquisition_id=None, request_id=None, image=False):
        sess = self.session_of(op, serial, session_id, acquisition_id)
        if not request_id:
            return self._refresh(sess)
        receipt = self.backend.receipt(sess.ref, valid_request_id(request_id))
        if receipt.get('schema') != 'phonefarm/remote-control-action-result@1':
            raise RemoteError(404 if receipt.get('code') == 'not_found' else 409,
                              receipt.get('error') or 'PF receipt unavailable', request_id=request_id)
        return self._image(sess, receipt) if image else receipt

    def _image(self, sess, receipt):
        data, mime = self.backend.image(sess.ref, receipt)
        result = receipt.get('result') or {}
        size = result.get('original_size') or [result.get('width'), result.get('height')]
        return ({**receipt, 'size': size, 'bytes': len(data), 'mime': mime}, data, mime)

    def _submit(self, sess, action, params, rid):
        sid = sess.descriptor.get('session_id')
        request = {'schema': 'phonefarm/remote-control-action@1', 'session_id': sid,
                   'request_id': rid, 'action': action, 'params': params}
        try:
            result = self.backend.submit(sess.ref, request)
        except OwnerError as error:
            return {'schema': 'phonefarm/remote-control-action-result@1', 'session_id': sid,
                    'request_id': rid, 'status': 'unknown', 'result': None,
                    'error': str(error), 'query_required': True}
        if result.get('schema') != 'phonefarm/remote-control-action-result@1':
            raise RemoteError(409, result.get('error') or 'PF rejected request', request_id=rid, session_id=sid)
        return result

    def _reaper(self):
        while not self._closed.wait(5):
            try:
                self.reap()
            except Exception as error:
                print('remote owner policy refresh failed:', type(error).__name__, file=sys.stderr)

    def reap(self):
        valid_ops = {o['id'] for o in self.operators.list() if not o['revoked']}
        with self._lock:
            sessions = list(self.sessions.values())
        for sess in sessions:
            state = self._refresh(sess)
            if state.get('phase') == 'ready' and (sess.operator['id'] not in valid_ops or not self.policy.enabled(sess.serial)):
                self._submit(sess, 'release', {}, 'node-policy-close')
        # TTL and maximum lifetime are enforced by PF, including when no web
        # request arrives. Nothing in this process closes a device lease FD.

    def close(self):
        self._closed.set()
        if self.backend:
            self.backend.close()

    def do(self, op, ip, serial, action, params, body=None):
        if not SERIAL_RE.fullmatch(serial or '') or not isinstance(params, dict):
            raise RemoteError(400, 'invalid serial or action object')
        if action in UNSUPPORTED:
            raise RemoteError(501, f'{action} 尚无持久 owner/恢复合同；未发出设备动作', capability='unsupported')
        if action not in ACTIONS | {'acquire', 'renew', 'release'}:
            raise RemoteError(404, f'没有动作 {action}')
        if body is not None:
            raise RemoteError(400, 'typed remote actions only accept JSON')
        self.rate.take(op['id'], COST.get(action, 1))
        outcome = None
        try:
            if action == 'acquire':
                outcome = self.acquire(op, serial, ip, params)
            else:
                rid = valid_request_id(params.get('request_id'))
                sess = self.session_of(op, serial, params.get('session_id'))
                if action != 'release' and not self.policy.enabled(serial):
                    self._submit(sess, 'release', {}, 'node-policy-close')
                    raise RemoteError(403, '设备已关闭远程开放；正在有序交回')
                cleaned = {k: v for k, v in params.items() if k not in ('request_id', 'session_id')}
                normalized = validate_action(action, cleaned)
                mapped = {'app_start': 'start_app', 'app_stop': 'stop_app'}.get(action, action)
                outcome = self._submit(sess, mapped, normalized, rid)
                if action == 'screenshot' and outcome.get('status') == 'completed':
                    return self._image(sess, outcome)
            return outcome
        except OwnerError as error:
            raise RemoteError(503, str(error)) from error
        finally:
            self.rate.done(op['id'])
            self.audit.write(op=op['name'], op_id=op['id'], ip=ip, serial=serial, action=action,
                args={k: v for k, v in params.items() if k in ('request_id', 'session_id', 'max_minutes')},
                result={k: v for k, v in (outcome or {}).items() if k in ('status', 'phase', 'request_id', 'session_id')})

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



# ═════════════ MCP ═════════════

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_S = {'serial': {'type': 'string'}, 'session_id': {'type': 'string'}, 'request_id': {'type': 'string'}}
MCP_TOOLS = [
    ('hgremote_devices', 'List device policy, PF ownership and capabilities.', _schema({})),
    ('hgremote_acquire', 'Acquire a PF owner. Keep request_id; after a timeout query acquisition_id, never create a new attempt.',
     _schema({'serial': _S['serial'], 'request_id': _S['request_id'], 'max_minutes': {'type': 'integer'}}, ['serial', 'request_id'])),
    ('hgremote_status', 'Query a session or the original action receipt without device I/O.',
     _schema({**_S, 'acquisition_id': {'type': 'string'}}, ['serial'])),
]
for _action, _props in {
    'renew': {}, 'release': {}, 'screenshot': {'max_side': {'type': 'integer'}},
    'tap': {k: {'type': 'integer'} for k in ('x', 'y')},
    'swipe': {k: {'type': 'integer'} for k in ('x1', 'y1', 'x2', 'y2', 'ms')},
    'text': {'text': {'type': 'string'}}, 'key': {'key': {'type': ['string', 'integer']}},
    'app_start': {'package': {'type': 'string'}, 'activity': {'type': 'string'}},
    'app_stop': {'package': {'type': 'string'}},
}.items():
    MCP_TOOLS.append(('hgremote_' + _action,
        'Submit one typed PF action. accepted/started are pending; unknown requires querying the same request_id. '
        'release closes admission and waits for current action; only session phase closed proves handback.',
        _schema({**_S, **_props}, ['serial', 'session_id', 'request_id'])))
MCP_ACTION = {name: name.removeprefix('hgremote_') for name, _, _ in MCP_TOOLS if name not in ('hgremote_devices', 'hgremote_status')}


def mcp_call(remote, op, ip, name, args):
    args = dict(args or {})
    if name == "hgremote_devices":
        return [{"type": "text", "text": json.dumps(remote.devices(), ensure_ascii=False)}], False
    if name == 'hgremote_status':
        serial = args.pop('serial', None)
        value = remote.query(op, serial, **args)
        return [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], value.get('status') in ('unknown', 'failed')
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
    failed = isinstance(out, dict) and (out.get("status") in ("failed", "unknown", "not_executed"))
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
                  "serverInfo": {"name": "hgamey-remote", "version": "2"},
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


def _send_result(h, out):
    if isinstance(out, tuple):
        meta, data, mime = out
        extra = {"X-Meta": json.dumps(meta)}
        if meta.get("size"):
            extra.update({"X-Device-W": str(meta["size"][0]), "X-Device-H": str(meta["size"][1])})
        return _send(h, 200, data, mime, extra)
    pending = out.get("status") in ("accepted", "started") or out.get("phase") in ("starting", "closing")
    return _send(h, 202 if pending else 200, out)


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


def handle(remote, h, method, parts, query, board=None, node=None):
    """parts: /remote/... 或 /dash/api/remote/...。返回 True 表示已处理。"""
    if parts == ["dash", "control"]:  # 控制台页面：查看密码之后再用操作员令牌登录
        if not (board and board.authed(h)):
            if board:
                return board._login_redirect(h) or True
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
        if sub == ["node"] and method == "GET":
            if node is None:
                raise RemoteError(503, "node identity provider unavailable")
            return _send(h, 200, node.identity()) or True
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
        if method == "GET" and len(sub) == 4 and sub[0] == "devices" and sub[2] == "requests":
            out = remote.query(op, unquote(sub[1]), session_id=q.get("session_id"),
                               request_id=unquote(sub[3]), image=q.get("image") == "1")
            return _send_result(h, out) or True
        if len(sub) == 3 and sub[0] == "devices":
            serial, action = unquote(sub[1]), sub[2]
            if action == "session":
                if method == "GET":
                    out = remote.query(op, serial, session_id=q.get("session_id"), acquisition_id=q.get("acquisition_id"))
                    return _send_result(h, out) or True
                action = {"POST": "acquire", "DELETE": "release"}.get(method)
                if not action:
                    raise RemoteError(405, "session 用 POST 获取、DELETE 释放")
            if action in UNSUPPORTED:
                h.close_connection = True
                raise RemoteError(501, f"{action} 尚无持久 owner/恢复合同；未发出设备动作")
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
            return _send_result(h, out) or True
        raise RemoteError(404, "没有这个远程接口")
    except RemoteError as e:
        return _send(h, e.status, e.payload) or True
    except OwnerError as e:
        return _send(h, 503, {"error": str(e), "query_required": True}) or True
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
                   help="已退役；所有remote动作必须共用PF全局bridge")
    a = sp.add_parser("audit")
    a.add_argument("-n", type=int, default=50)
    args = ap.parse_args(argv)
    cfg = CONFIG_DIR
    if getattr(args, "ignore_global_lock", False):
        ap.error("--ignore-global-lock 已退役；不能绕过PF实际所有权")
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
