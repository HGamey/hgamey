#!/usr/bin/env python3
"""Authenticated remote gateway; PF owns device locks, commands and recovery.

HTTP/MCP use the center identity. No handler performs raw
ADB/HDC device I/O or grants ownership based on labels. Saved PF references and
stable request IDs survive node restart; unknown actions are queried, not replayed.
"""
import base64, json, os, re, subprocess, sys, tempfile
import threading, time, uuid
from urllib.parse import parse_qs, unquote
from remote_owner import OwnerError
from device_scope import binding, scope, same_binding
from device_registry import RegistryError

CONFIG_DIR = os.path.expanduser(os.environ.get("HG_CONFIG_DIR") or "~/.config/hgamey")
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9_.]{1,200}\Z")
ACTIVITY_RE = re.compile(r"[A-Za-z0-9_.$/]{1,300}\Z")
DEVICE_PATH_RE = re.compile(r"/[^\x00-\x1f'\\]{1,500}\Z")
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
COST = {"shell": 3, "screenshot": 2, "install": 20, "push": 5, "pull": 5, "logcat": 3, "diagnostics": 3, "uninstall": 5}
INFLIGHT_MAX = 3
ANDROID_KEYS = {"back": 4, "home": 3, "recents": 187, "app_switch": 187, "menu": 82, "enter": 66, "del": 67,
                "backspace": 67, "power": 26, "volume_up": 24, "volume_down": 25, "tab": 61, "escape": 111,
                "wakeup": 224, "sleep": 223, "up": 19, "down": 20, "left": 21, "right": 22}
HDC_KEYS = {"back": "Back", "home": "Home", "power": "Power", "recents": "Recent"}


class RemoteError(Exception):
    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status, self.payload = status, {"error": error, **extra}


def _now():
    return time.time()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


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

class Occupancy:
    """PF status describes current fd holders; this reader never grants ownership."""

    def __init__(self, storage):
        self._storage = storage

    def holders(self):
        snapshot = self._storage()
        if snapshot.get("status") not in {"clear", "held"}:
            raise RemoteError(503, snapshot.get("error") or "phonefarm 当前锁状态不可用")
        devices = {d["device_id"]: d.get("aliases", [])
                   for d in (snapshot.get("device_scopes") or {}).get("devices", [])}
        return [{"dir": entry.get("scope"), "label": entry.get("label"),
                 "serial": entry.get("serial"), "since": entry.get("since"),
                 "heartbeat_age_s": None, "kind": "device-lock", "job_id": entry.get("job_id"),
                 "global": entry.get("scope") == "global",
                 "aliases": devices.get(entry.get("device_id"), [entry.get("serial")])}
                for entry in snapshot.get("entries", [])]


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


ACTIONS = {'screenshot', 'tap', 'swipe', 'text', 'key', 'app_start', 'app_stop', 'logcat', 'diagnostics'}
INPUT_CONTRACT = 'frame-bound-v1'
UNSUPPORTED = {'shell', 'install', 'uninstall', 'push', 'pull'}
BINARY_UPLOAD = {}  # Legacy uploads are refused before reading their body.


def valid_request_id(value, field='request_id'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value):
        raise RemoteError(400, f'{field} requires 1..64 ASCII letters, digits, dot, underscore or hyphen')
    return value


def validate_action(action, params):
    fields = {'tap': {'frame_id', 'x', 'y'}, 'swipe': {'frame_id', 'x1', 'y1', 'x2', 'y2', 'ms'},
              'text': {'text'}, 'key': {'key'}, 'screenshot': {'max', 'max_side'}, 'logcat': {'lines', 'pid'},
              'app_start': {'package', 'activity'}, 'app_stop': {'package'}, 'diagnostics': {'package'},
              'renew': set(), 'release': set()}
    if set(params) - fields[action]:
        raise RemoteError(400, 'unsupported action parameters')
    p = dict(params)
    def integer(key, low, high):
        if type(p.get(key)) is not int or not low <= p[key] <= high:
            raise RemoteError(400, f'{key} requires integer {low}..{high}')
    if action in ('tap', 'swipe'):
        valid_request_id(p.get('frame_id'), 'frame_id')
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
    elif action == 'logcat':
        p.setdefault('lines', 200)
        integer('lines', 1, 2000)
        if 'pid' in p:
            integer('pid', 1, 2147483647)
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
    elif action in ('app_start', 'app_stop') or action == 'diagnostics' and 'package' in p:
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

    def scope(self):
        return scope({'device_access': 'write', 'argv': ['--serial', self.serial],
                      **({'device_binding': self.ref['device_binding']} if 'device_binding' in self.ref else {})})


class Remote:
    def __init__(self, storage, adb='adb', hdc=None, config_dir=None, backend=None):
        cfg = config_dir or CONFIG_DIR
        self.audit = Audit(os.path.join(cfg, 'remote.audit.jsonl'))
        self.occupancy = Occupancy(storage)
        self.adb, self.hdc, self.backend = adb, hdc, backend
        self.device_scopes, self.leases = None, None
        self.rate = RateLimit()
        self.sessions, self._lock = {}, threading.RLock()
        self._devcache = (0, [])
        if backend:
            for ref in backend.load():
                self.sessions[ref['id']] = Session(ref, backend.status(ref))

    def capabilities(self):
        aliases = {'start_app': 'app_start', 'stop_app': 'app_stop'}
        return {'available': bool(self.backend and self.backend.available),
                'reason': self.backend.reason if self.backend else 'PF remote owner is not configured',
                'actions': ['acquire'] + [aliases.get(x, x) for x in self.backend.actions]
                           if self.backend and self.backend.available else [],
                'ownership': 'strong-lease-v1',
                'input_contract': getattr(self.backend, 'input_contract', None),
                'device_binding_v1': bool(getattr(self.backend, 'device_binding_available', False)
                                          and self.device_scopes is not None),
                'unsupported': sorted(UNSUPPORTED)}

    def resolve(self, serial):
        if self.device_scopes is None:
            raise RemoteError(503, 'configured device registry is unavailable')
        try:
            return self.device_scopes.resolve(serial)
        except RegistryError as error:
            raise RemoteError(error.status, str(error), code=error.payload['code']) from error

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
        caps = self.capabilities()
        try:
            holders, occ_err = self.occupancy.holders(), None
        except RemoteError as error:
            holders, occ_err = [], error.payload['error']
        with self._lock:
            sessions = list(self.sessions.values())
        active = [{**self._refresh(s), 'device_scope': s.scope()} for s in sessions]
        active = [s for s in active if s.get('live_owner') and s.get('phase') in ('ready', 'closing')]
        out = []
        for d in self._enumerate():
            serial = d['serial']
            names = {serial}
            for holder in holders:
                if serial in holder['aliases']:
                    names.update(holder['aliases'])
            item = {**d, 'enabled': True,
                    'aliases': sorted(names - {serial}),
                    'capabilities': caps['actions'] if caps['available'] and d['transport'] == 'adb' else [],
                    'holder': None}
            matches = [s for s in active if s['device_scope']['scope'] == 'global' or s['serial'] in names]
            if matches:
                item['holder'] = {'kind': 'remote', **matches[-1]}
            else:
                matching = next((h for h in holders if h['global'] or any(alias in names for alias in h['aliases'])), None)
                if matching:
                    item['holder'] = {'kind': 'experiment', **matching}
            out.append(item)
        # A live owner remains visible even if host enumeration lost its device.
        # Starting and historical sessions are still queryable by their saved ID.
        present = {d['serial'] for d in out}
        for sess in active:
            if sess['serial'] not in present:
                out.append({'serial': sess['serial'], 'transport': 'adb', 'state': 'offline',
                    'enabled': True, 'aliases': [],
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
        value = binding(params['device_binding']) if 'device_binding' in params else None
        if value is not None and value['serial'] != serial:
            raise RemoteError(400, 'acquisition serial must match device_binding')
        self._require_backend()
        with self._lock:
            for sess in self.sessions.values():
                if sess.operator['id'] == op['id'] and sess.ref['acquisition_id'] == acquisition:
                    if (sess.serial != serial or sess.ref['max_seconds'] != minutes * 60
                            or not same_binding(sess.ref.get('device_binding'), value)):
                        raise RemoteError(409, 'acquisition request ID already has different content')
                    return {'reused': True, **self._refresh(sess)}
            if value is not None and not self.capabilities()['device_binding_v1']:
                raise RemoteError(503, 'configured PF remote owner does not support device binding; no global fallback')
            device = next((d for d in self._enumerate() if d['serial'] == serial), None)
            if not device or device['state'] != 'device':
                raise RemoteError(409, '设备当前不在线')
            if device['transport'] != 'adb':
                raise RemoteError(501, '此 PF remote owner 尚未支持 HDC；未发出设备动作')
            ref = self.backend.start(serial, op, ip, minutes * 60, acquisition,
                                     **({'device_binding': value} if value else {}))
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

    def close(self):
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
                sess = self.session_of(op, serial, params.get('session_id'), params.get('acquisition_id'))
                if action in ('tap', 'swipe'):
                    # A new helper's capabilities cannot upgrade a saved owner.
                    # Receipt GET remains available, including for old owners.
                    self._refresh(sess)
                    if sess.descriptor.get('input_contract') != INPUT_CONTRACT:
                        raise RemoteError(409, 'session does not support frame-bound input; query the original request ID to retrieve an existing receipt',
                            code='input_contract_unavailable', request_id=rid,
                            session_id=params.get('session_id'), required_input_contract=INPUT_CONTRACT)
                if action in ('logcat', 'diagnostics'):
                    self._refresh(sess)
                    if action not in sess.descriptor.get('capabilities', []):
                        raise RemoteError(409, f'session does not support {action}; query an existing receipt by request ID',
                                          code='action_unavailable', request_id=rid, session_id=params.get('session_id'))
                cleaned = {k: v for k, v in params.items() if k not in ('request_id', 'session_id', 'acquisition_id')}
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

# ═════════════ MCP ═════════════

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_S = {'serial': {'type': 'string'}, 'session_id': {'type': 'string'}, 'request_id': {'type': 'string'}}
MCP_TOOLS = [
    ('hgremote_devices', 'List device policy, PF ownership and capabilities.', _schema({})),
    ('hgremote_acquire', 'Acquire a PF owner. Keep request_id; after a timeout query acquisition_id, never create a new attempt.',
     _schema({'serial': _S['serial'], 'request_id': _S['request_id'], 'max_minutes': {'type': 'integer'},
              'device_binding': {'type': 'object'}}, ['serial', 'request_id'])),
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
    'logcat': {'lines': {'type': 'integer', 'minimum': 1, 'maximum': 2000, 'default': 200},
               'pid': {'type': 'integer', 'minimum': 1, 'maximum': 2147483647}},
    'diagnostics': {'package': {'type': 'string', 'pattern': r'^[A-Za-z0-9_.]{1,200}$'}},
}.items():
    _required = ['serial', 'session_id', 'request_id']
    _input_hint = ''
    if _action in ('tap', 'swipe'):
        _props['frame_id'] = {'type': 'string', 'pattern': r'^[A-Za-z0-9_.-]{1,64}$'}
        _required.append('frame_id')
        _input_hint = (' Requires the source screenshot frame_id and original image pixel coordinates; '
                       'the session must declare frame-bound-v1. Never capture or replay automatically.')
    MCP_TOOLS.append(('hgremote_' + _action,
        'Submit one typed PF action. accepted/started are pending; unknown requires querying the same request_id. '
        'release cancels a starting acquisition, or closes admission and waits for the current action; '
        'only session phase closed proves handback.' + _input_hint,
        _schema({**_S, **_props}, _required)))
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
            note = {**meta, "hint": "tap/swipe 必须携本次截图 result.frame_id，按 result.original_size 换算原图像素；资格由当前会话 input_frame_id 决定"}
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
                                  "手机被占用时返回 starting 并等待；按原 ID 查询，ready 后再操作。"
                                  "等待中可 hgremote_release 取消，操作完也用它交回；以会话 closed 确认结束。"}
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
    ip = h.headers.get("CF-Connecting-IP", h.client_address[0])
    try:
        if dash:
            if not (board and board.authed(h)):
                raise RemoteError(401, "需要查看密码")
            steered = board.steered(h)
            if sub == ['whoami']:
                return _send(h, 200, {'operator': 'human' if steered else None, 'id': 'human'}) or True
            if method != 'GET' and h.headers.get('X-HG') != '1':
                h.close_connection = True
                raise RemoteError(400, '请求格式不对（缺 X-HG 头）')
            # Screenshots are new device actions even when requested by GET.
            readonly = sub == ['devices'] or sub == ['node'] or sub[:2] == ['devices', 'resolve'] or (
                len(sub) >= 3 and (sub[2] == 'requests' or sub[2] == 'session' and method == 'GET'))
            if not readonly and h.headers.get('X-HG') != '1':
                raise RemoteError(400, '请求格式不对（缺 X-HG 头）')
            if not readonly and not steered:
                raise RemoteError(403, '需要掌舵权限', need_steer=True)
            op = {'id': 'human', 'name': 'human'}
            if method in ('POST', 'DELETE') and 'application/json' not in h.headers.get('Content-Type', ''):
                h.close_connection = True
                raise RemoteError(400, '写接口只收 JSON')
        else:
            if not h._authed():
                return True
            op = {'id': 'agent:' + h.agent, 'name': h.agent} if h.agent else {'id': 'admin', 'name': 'admin'}
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
        if sub == ["devices", "resolve"] and method == "GET":
            if node is None or not (h.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                raise RemoteError(400, "device resolution requires an explicit node identity", code="target_required")
            if set(q) - {"serial", "expected_node_id"} or not q.get("serial"):
                raise RemoteError(400, "device resolution requires one serial")
            return _send(h, 200, {"node_id": node.node_id, "binding": remote.resolve(q["serial"])}) or True
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
            elif method == "GET" and action in ("screenshot", "pull", "logcat", "diagnostics"):
                params, body = dict(q), None
                for key in ("lines", "pid"):
                    if key in params and params[key].isdigit():
                        params[key] = int(params[key])
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
    """Read the current selected PF snapshot on every occupancy check."""
    return lambda: leases.snapshot(refresh=True)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='远控审计；身份使用中心 agent token')
    ap.add_argument('command', choices=['audit'])
    ap.add_argument('-n', type=int, default=50)
    args = ap.parse_args(argv)
    for line in Audit(os.path.join(CONFIG_DIR, 'remote.audit.jsonl')).tail(args.n):
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
