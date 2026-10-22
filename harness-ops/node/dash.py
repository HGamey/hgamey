"""实时看板 /dash：节点自带的只读看板（node_service 只负责挂载，逻辑都在这里）。

只读纪律：
  - 本模块不起任何任务、不碰设备锁、不往手机写东西。唯一碰手机的是 `phonefarm fleet`（只读快照），
    它自己按设备锁分档：锁被别人持有时只 cat sysfs，不跑 dumpsys、不截图。
  - 截图只在「锁空闲且已空闲 ≥ FREE_GRACE_S 秒」时交给 fleet 截，最小间隔 SHOT_EVERY_S；
    锁刚释放的那一小段（别人可能马上又拿锁开测）连 fleet 都不跑。
  - 没人看页面时（VIEW_IDLE_S 内无请求）采样线程完全停下，对手机零开销。

首页（/dash）只放产品视角：手机槽位、目标运行（goal mode，见 dash_goals.py）、时间线、掌舵；
开发 agent 的任务、设备锁、日志、旧的实验证据在维护页（/dash/maint）。

访问控制：独立查看密码 ~/.config/hgamey/dash.pass（与管理 token 无关），只能看；
  掌舵密码 ~/.config/hgamey/steer.pass：改 human.md、停止、起新目标（每次记时间与来源 IP，见 steer.audit.jsonl）。
  写接口只收 JSON + X-HG 头（跨站表单发不出），cookie 均为 HttpOnly/Secure/SameSite=Strict。
  浏览器：/dash/login 表单 → HttpOnly cookie（值是密码的 HMAC，不是密码本身）。
  命令行：HTTP Basic，用户名随意，密码 = 查看密码。

接口（都要密码，都只读）：
  GET /dash                 页面
  GET /dash/static/<文件>    页面资源
  GET /dash/api/state       手机 + 设备锁 + 任务 + 温度/电量历史（页面每 3 秒拉一次）
  GET /dash/api/log?job=ID  任务日志尾部
  GET /dash/api/exps?ver=V  实验证据（optimize / autoloop / eval）汇总；ver 没变时只回 {"same": true}
  GET /dash/api/shot[?serial=S]  最近一张手机截图（jpeg）
  GET /dash/maint           维护页
  GET /dash/api/home        首页：手机槽位（至少 4 格）+ 目标运行列表
  GET /dash/api/run?id=R    一个目标运行：state、goal.md、human.md 与历史、events、report
  POST /dash/api/steer/unlock|lock         掌舵解锁 / 上锁
  POST /dash/api/run/human|stop            改 human.md / 停止（掌舵密码）
  POST /dash/api/goal/draft|confirm        新目标起草 / 确认并开跑（掌舵密码）
"""
import collections, glob, hashlib, hmac, json, os, re, threading, time
from urllib.parse import parse_qs

import dash_goals
from job_queue import RequestConflict
from node_fleet import (FleetState, read_lock, VIEW_IDLE_S, FLEET_EVERY_FREE_S,
                        FLEET_EVERY_HELD_S, FREE_GRACE_S, SHOT_EVERY_S)

PASS_FILE = os.path.expanduser("~/.config/hgamey/dash.pass")     # 查看密码：只能看
STEER_FILE = os.path.expanduser("~/.config/hgamey/steer.pass")   # 掌舵密码：改 human.md、起新目标、停止
STEER_COOKIE = "hg_steer"
STEER_MAX_AGE = 12 * 3600
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dash_static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
COOKIE = "hg_dash"
SLOTS_MIN = 4
SCAN_EVERY_S = 30         # 实验证据多久扫一次（只在有人看时）
JOBS_SHOWN = 30
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\r")
REDACT = re.compile(r"(Bearer\s+|sk-[A-Za-z0-9]{4})[A-Za-z0-9._\-]{8,}|((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+", re.I)
HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                                      "script-src 'self'; frame-ancestors 'none'"}


def _now():
    return time.time()


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def log_err(s):
    print(time.strftime("%F %T"), s, file=__import__("sys").stderr, flush=True)


def _redact(s):
    return REDACT.sub(lambda m: (m.group(1) or m.group(2)) + "***", s)


# ═════════════ 实验证据解析 ═════════════

VERDICT = {"PASS": "win", "KEEP": "win", "REJECT": "lose", "SLOW": "lose", "POOR_QUALITY": "lose",
           "CRASH": "lose", "ABORT": "void", "REVERTED": "other"}
METRIC_KEYS = {"fps_mean": "fps", "frame_p95": "frame_p95", "fps_p95_ms": "frame_p95", "frame_p95_ms": "frame_p95",
               "power_w_mean": "power", "power_watt": "power"}
ORIGIN = {"llm": "大模型", "local_mutation": "本地变异器", "external": "外部出方案进程"}


def _change_text(c):
    if not isinstance(c, dict):
        return str(c)
    if "params" in c and isinstance(c["params"], dict):
        return ", ".join(f"{k}={v}" for k, v in c["params"].items())
    if "rewrites" in c:
        rs = c["rewrites"] or []
        return " + ".join(r.get("knob", "?") + (f"[{r['scope']}]" if r.get("scope") else "") if isinstance(r, dict) else str(r)
                          for r in rs)
    if "payload" in c:
        return _change_text(c["payload"])
    if "track" in c:
        return f"{c.get('track')}: {c.get('shader_path')}"
    return json.dumps(c, ensure_ascii=False)[:200]


def _metrics_from_per_metric(pm):
    out = {}
    for k, v in (pm or {}).items():
        key = METRIC_KEYS.get(k)
        if not key or not isinstance(v, dict):
            continue
        out[key] = {"a": v.get("a_mean"), "b": v.get("b_mean"), "pct": v.get("diff_pct"),
                    "p": v.get("p", v.get("perm_p_two_sided")), "status": v.get("status"),
                    "sig": v.get("significant_at_0.05")}
    return out


def _cand(**kw):
    c = {"id": None, "gen": None, "why": "", "change": "", "status": None, "outcome": "pending", "reason": None,
         "p": None, "metrics": {}, "temp_max_c": None, "abs": {}}
    c.update(kw)
    if c["status"]:
        c["outcome"] = VERDICT.get(str(c["status"]).upper(), "other")
    return c


def parse_optimize(d):
    rep = _load(os.path.join(d, "report.json"))
    if rep is not None and "archive" not in rep:
        rep = None
    cands, order, gens_meta = {}, [], {}
    if rep:
        for e in rep.get("archive") or []:
            v = e.get("verdict") or {}
            m = e.get("metrics") or {}
            cands[e["id"]] = _cand(id=e["id"], gen=e.get("generation"), layer=e.get("layer"), why=e.get("why", ""),
                                   change=_change_text(e.get("change")), status=e.get("status"),
                                   reason=v.get("reason"), p=v.get("p_value"),
                                   metrics=_metrics_from_per_metric(v.get("per_metric")),
                                   temp_max_c=m.get("soc_temp_max_c"), kept=e.get("kept"),
                                   abs={"fps": m.get("fps_mean"), "frame_p95": m.get("frame_p95_ms") or m.get("fps_p95_ms"),
                                        "power": m.get("power_watt")})
            order.append(e["id"])
    pdir = os.path.join(d, "proposer")
    for fin in sorted(glob.glob(os.path.join(pdir, "*_g*.input.json")), key=_mtime):
        stem = os.path.basename(fin)[:-len(".input.json")]
        kind, _, g = stem.rpartition("_g")
        inp = _load(fin) or {}
        for h in inp.get("history") or []:
            cid = h.get("candidate_id")
            if not cid or cid in cands:
                continue
            cands[cid] = _cand(id=cid, gen=h.get("generation"), layer=kind, why=(h.get("candidate") or {}).get("why", ""),
                               change=_change_text(h.get("candidate")), status=h.get("status"), reason=h.get("reason"),
                               p=h.get("p_value"), metrics=_metrics_from_per_metric(h.get("per_metric")), kept=h.get("kept"))
            order.append(cid)
        out = _load(os.path.join(pdir, stem + ".output.json")) or {}
        prop = out.get("proposed") or {}
        gm = gens_meta.setdefault(int(g) if g.isdigit() else g, {})
        gm.update(origin=ORIGIN.get(prop.get("origin"), prop.get("origin")), error=prop.get("error"),
                  rejected=(out.get("rejected") or [])[:12], proposed=len(prop.get("candidates") or []))
        prefix = {"sysparam": "sys", "gray": "gray"}.get(kind, kind)
        for i, c in enumerate(out.get("accepted") or []):
            cid = f"{prefix}_g{g}c{i + 1}"
            if cid not in cands:
                cands[cid] = _cand(id=cid, gen=int(g) if g.isdigit() else g, layer=kind, why=c.get("why", ""),
                                   change=_change_text(c))
                order.append(cid)
    if not cands and not rep and not os.path.isdir(pdir) and not os.path.isfile(os.path.join(d, "caps.json")):
        return None
    title = "optimize"
    meta = {}
    if rep:
        title = f"optimize · {rep.get('game') or ''}".strip(" ·")
        meta = {"game": rep.get("game"), "device": rep.get("device_model"), "headline": rep.get("headline"),
                "layers": [{"name": l.get("layer_display") or l.get("layer"), "used": l.get("used"), "reason": l.get("reason"),
                            "proposer": l.get("proposer")} for l in rep.get("layers") or []],
                "proposer": (rep.get("proposer") or {}).get("describe"), "done": not rep.get("plan_only")}
    return {"kind": "optimize", "title": title, "meta": meta, "cands": [cands[i] for i in order],
            "gens": {str(k): v for k, v in gens_meta.items()}}


def parse_autoloop(d):
    rep = _load(os.path.join(d, "report.json"))
    if rep is not None and "results" not in rep:
        rep = None
    gdirs = sorted((g for g in glob.glob(os.path.join(d, "gen*")) if os.path.isdir(g)),
                   key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    if not rep and not gdirs and not os.path.isfile(os.path.join(d, "rule.json")):
        return None
    cands, gens_meta = [], {}
    for gd in gdirs:
        g = int(re.sub(r"\D", "", os.path.basename(gd)) or 0)
        cj = _load(os.path.join(gd, "candidates.json")) or {}
        gens_meta[str(g)] = {"origin": cj.get("source"), "proposed": len(cj.get("candidates") or [])}
        for i, c in enumerate(cj.get("candidates") or []):
            r = _load(os.path.join(gd, f"cand{i + 1}", "result.json"))
            base = dict(id=f"g{g}c{i + 1}", gen=g, why=c.get("why", ""), change=_change_text(c))
            if r:
                cands.append(_cand(**base, status=r.get("verdict"), reason=r.get("reason"),
                                   metrics=_metrics_from_per_metric(r.get("comparisons")), temp_max_c=r.get("temp_max_c")))
            else:
                cands.append(_cand(**base))
    if not gdirs and rep:  # 只有总报告（证据目录被裁剪过）
        for i, r in enumerate(rep.get("results") or []):
            cands.append(_cand(id=f"c{i + 1}", gen=r.get("generation"), why=r.get("why", ""), change=_change_text(r),
                               status=r.get("verdict"), reason=r.get("reason"),
                               metrics=_metrics_from_per_metric(r.get("comparisons")), temp_max_c=r.get("temp_max_c")))
    meta = {}
    rule = (rep or {}).get("rule") or _load(os.path.join(d, "rule.json")) or {}
    if rep:
        n_keep = len(rep.get("kept") or [])
        meta = {"goal": rep.get("goal"), "device": (rep.get("test_conditions") or {}).get("device"),
                "headline": f"保留 {n_keep} 组 / 共 {len(rep.get('results') or [])} 组", "done": True}
    meta["rule"] = rule.get("keep_condition")
    return {"kind": "autoloop", "title": f"autoloop · {(rep or {}).get('goal') or os.path.basename(d)}", "meta": meta,
            "cands": cands, "gens": gens_meta}


def parse_eval_log(path):
    """phonefarm eval --json 把判定报告整行打到 stdout（进了任务日志）。从日志尾部捞最后一份。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - (512 << 10)))
            data = f.read().decode(errors="replace")
    except OSError:
        return None
    rep, dec = None, json.JSONDecoder()
    for mt in re.finditer(r"(?m)^\{", data):  # 报告可能是一行，也可能是缩进排版的多行
        try:
            obj, _ = dec.raw_decode(data, mt.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and "candidate_id" in obj and "verdict" in obj:
            rep = obj
    if not rep:
        return None
    v, m = rep.get("verdict") or {}, rep.get("metrics") or {}
    c = _cand(id=rep.get("candidate_id"), gen=1, layer=rep.get("kind"), status=rep.get("status"), reason=v.get("reason"),
              p=v.get("p_value"), metrics=_metrics_from_per_metric(v.get("per_metric")), temp_max_c=m.get("soc_temp_max_c"),
              abs={"fps": m.get("fps_mean"), "frame_p95": m.get("frame_p95_ms") or m.get("fps_p95_ms"), "power": m.get("power_watt")})
    return {"kind": "eval", "title": f"eval · {rep.get('kind') or ''} · {rep.get('candidate_id')}", "meta": {"done": True},
            "cands": [c], "gens": {}}


def detect(d):
    if os.path.isdir(os.path.join(d, "proposer")) or os.path.isfile(os.path.join(d, "caps.json")):
        return parse_optimize(d)
    if os.path.isfile(os.path.join(d, "rule.json")) or os.path.isfile(os.path.join(d, "whitelist.json")) \
            or glob.glob(os.path.join(d, "gen*", "candidates.json")):
        return parse_autoloop(d)
    rep = _load(os.path.join(d, "report.json"))
    if isinstance(rep, dict):
        if "archive" in rep:
            return parse_optimize(d)
        if "results" in rep:
            return parse_autoloop(d)
    return None


def signature(d):
    """(最新 mtime, 文件数) —— 没变就不重新解析；最新 mtime 也当「最近更新」。"""
    newest, n = 0.0, 0
    for pat in ("report.json", "caps.json", "rule.json", "proposer/*.json", "gen*/candidates.json", "gen*/cand*/result.json"):
        for p in glob.glob(os.path.join(d, pat)):
            newest, n = max(newest, _mtime(p)), n + 1
    return (newest, n)


# ═════════════ 主体 ═════════════

class Dash:
    def __init__(self, phonefarm_root, gol_root, jobs_dir, jobs=None, fleet=None):
        self.pf_root, self.gol_root, self.jobs_dir = phonefarm_root, gol_root, jobs_dir
        self.job_service = jobs
        node_dir = os.path.dirname(os.path.dirname(jobs_dir.rstrip("/")))  # ~/node
        self.goals = dash_goals.Goals(os.environ.get("HG_GOALS_DIR", os.path.join(node_dir, "goals")),
                                      os.path.join(os.path.dirname(jobs_dir.rstrip("/")), "_dash", "demo-goals"),
                                      jobs=jobs, gol_exe=os.path.join(gol_root, "game_opt_loop"))
        self.shot_dir = os.path.join(os.path.dirname(jobs_dir.rstrip("/")), "_dash", "screens")
        self.last_view = 0.0
        self.monitor = fleet or FleetState(self.pf_root, None, self.shot_dir,
                                          lease_state=jobs.leases if jobs else None)
        self.meta_cache = {}
        self.walk_cache = {}
        self.exp_cache = {}
        self.exps, self.exps_ver = [], "0"
        self.fails = collections.defaultdict(list)
        self.wake = threading.Event()
        threading.Thread(target=self._scanner, daemon=True, name="dash-scan").start()

    # ── 认证 ──
    def _pass(self):
        try:
            p = open(PASS_FILE).read().strip()
            return p if len(p) >= 12 else None
        except OSError:
            return None

    def _cookie_val(self, pw, salt=b"hgamey-dash-v1"):
        return hmac.new(pw.encode(), salt, hashlib.sha256).hexdigest()

    def _steer_pass(self):
        try:
            p = open(STEER_FILE).read().strip()
            return p if len(p) >= 12 else None
        except OSError:
            return None

    def _cookie(self, h, name):
        for part in h.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def steered(self, h):
        pw = self._steer_pass()
        v = self._cookie(h, STEER_COOKIE)
        return bool(pw and v and hmac.compare_digest(v, self._cookie_val(pw, b"hgamey-steer-v1")))

    def _throttled(self, ip):
        now = _now()
        self.fails[ip] = [t for t in self.fails[ip] if now - t < 600]
        return len(self.fails[ip]) >= 10

    def authed(self, h):
        pw = self._pass()
        if not pw:
            return False
        for part in h.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, self._cookie_val(pw)):
                return True
        auth = h.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            import base64
            try:
                _, _, given = base64.b64decode(auth[6:]).decode().partition(":")
                return hmac.compare_digest(given.encode(), pw.encode())
            except ValueError:
                return False
        return False

    def _ip(self, h):
        return h.headers.get("CF-Connecting-IP", h.client_address[0])

    # ── HTTP 入口：node_service 只调这两个 ──
    def handle_get(self, h, parts, query):
        sub = parts[1:]
        if sub == ["login"]:
            return self._login_page(h)
        if not self.authed(h):
            if sub and sub[0] == "api":
                return self._send(h, 401, {"error": "需要查看密码"}, extra={"WWW-Authenticate": 'Basic realm="HGamey dash"'})
            return self._redirect(h, "/dash/login")
        if sub in (["jobs"], ["fleet"]):
            return self._static(h, sub[0] + ".html")
        if len(sub) == 2 and sub[0] == "static" and sub[1] in ("jobs.js", "jobs.css", "fleet.js", "fleet.css", "app.css"):
            return self._static(h, sub[1])
        self.last_view = _now()
        self.wake.set()
        # Only existing observation endpoints enable screenshots; assets and run/log reads do not.
        if sub in (["api", "home"], ["api", "state"], ["api", "shot"]):
            self.monitor.request(observe=True)
        q = parse_qs(query)
        if not sub:
            return self._static(h, "home.html")
        if sub == ["maint"]:
            return self._static(h, "maint.html")
        if sub == ["api", "home"]:
            return self._send(h, 200, self.home(h))
        if sub == ["api", "run"]:
            try:
                return self._send(h, 200, self.goals.detail((q.get("id") or [""])[0]))
            except KeyError:
                return self._send(h, 404, {"error": "没有这个目标运行"})
        if sub == ["api", "run", "human_rev"]:
            try:
                t = self.goals.history_text((q.get("id") or [""])[0], (q.get("name") or [""])[0])
                return self._send(h, 200, {"text": t})
            except KeyError:
                return self._send(h, 404, {"error": "没有这个版本"})
        if sub[0] == "static" and len(sub) == 2:
            return self._static(h, sub[1])
        if sub == ["api", "state"]:
            return self._send(h, 200, self.state())
        if sub == ["api", "log"]:
            jid = (q.get("job") or [""])[0]
            return self._send(h, 200, self.log_tail(jid, int((q.get("bytes") or ["16384"])[0])))
        if sub == ["api", "exps"]:
            if (q.get("ver") or [""])[0] == self.exps_ver:
                return self._send(h, 200, {"same": True, "ver": self.exps_ver})
            return self._send(h, 200, {"ver": self.exps_ver, "exps": self.exps})
        if sub == ["api", "shot"]:
            return self._shot(h, (q.get("serial") or [""])[0])
        return self._send(h, 404, {"error": "not found"})

    def handle_post(self, h, parts, body):
        if parts[1:] == ["login"]:
            ip, now = self._ip(h), _now()
            self.fails[ip] = [t for t in self.fails[ip] if now - t < 600]
            if len(self.fails[ip]) >= 10:
                return self._login_page(h, "失败次数太多，10 分钟后再试", 429)
            pw = (parse_qs(body.decode(errors="replace")).get("pw") or [""])[0].strip()
            real = self._pass()
            if real and hmac.compare_digest(pw.encode(), real.encode()):
                ck = f"{COOKIE}={self._cookie_val(real)}; Path=/dash; Max-Age=2592000; HttpOnly; Secure; SameSite=Strict"
                return self._redirect(h, "/dash", {"Set-Cookie": ck})
            self.fails[ip].append(now)
            time.sleep(1)
            return self._login_page(h, "密码不对", 401)
        if parts[1:] == ["logout"]:
            return self._redirect(h, "/dash/login", {"Set-Cookie": [
                f"{COOKIE}=; Path=/dash; Max-Age=0; HttpOnly; Secure; SameSite=Strict",
                f"{STEER_COOKIE}=; Path=/dash; Max-Age=0; HttpOnly; Secure; SameSite=Strict"]})
        sub = parts[1:]
        if not (sub and sub[0] == "api"):
            return self._send(h, 404, {"error": "not found"})
        if not self.authed(h):
            return self._send(h, 401, {"error": "需要查看密码"})
        # 写接口一律要 JSON + 自定义头（跨站表单发不出来），再加掌舵密码
        if h.headers.get("X-HG") != "1" or "application/json" not in h.headers.get("Content-Type", ""):
            return self._send(h, 400, {"error": "请求格式不对"})
        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(h, 400, {"error": "请求不是 JSON"})
        ip = self._ip(h)
        if sub == ["api", "steer", "unlock"]:
            if self._throttled(ip):
                return self._send(h, 429, {"error": "失败次数太多，10 分钟后再试"})
            real = self._steer_pass()
            if not real:
                return self._send(h, 503, {"error": "节点上还没设置掌舵密码"})
            if hmac.compare_digest(str(data.get("pw", "")).strip().encode(), real.encode()):
                ck = (f"{STEER_COOKIE}={self._cookie_val(real, b'hgamey-steer-v1')}; Path=/dash; Max-Age={STEER_MAX_AGE}; "
                      "HttpOnly; Secure; SameSite=Strict")
                log_err(f"dash 掌舵解锁 来自 {ip}")
                return self._send(h, 200, {"ok": True}, extra={"Set-Cookie": ck})
            self.fails[ip].append(_now())
            time.sleep(1)
            return self._send(h, 403, {"error": "掌舵密码不对"})
        if sub == ["api", "steer", "lock"]:
            return self._send(h, 200, {"ok": True}, extra={"Set-Cookie": f"{STEER_COOKIE}=; Path=/dash; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
        if not self.steered(h):
            return self._send(h, 403, {"error": "需要掌舵密码", "need_steer": True})
        try:
            if sub == ["api", "jobs"]:
                return self._send(h, 200, self.job_service.start(data, b"", request_id=h.headers.get("X-Request-ID")))
            if len(sub) == 4 and sub[:2] == ["api", "jobs"] and sub[3] == "queue-release":
                return self._send(h, 200, self.job_service.release_queue(sub[2], data.get("note")))
            if len(sub) == 4 and sub[:2] == ["api", "jobs"] and sub[3] == "cancel":
                if not self.job_service:
                    return self._send(h, 503, {"error": "作业服务不可用"})
                r = self.job_service.cancel(sub[2], force=data.get("force", False))
                code = 409 if not r["accepted"] and r["job"]["state"] == "running" else 200
                log_err(f"dash job cancel {sub[2]} accepted={r['accepted']} 来自 {ip}")
                return self._send(h, code, r)
            elif sub == ["api", "run", "human"]:
                r = self.goals.save_human(str(data.get("id", "")), data.get("text"), ip, base_mtime=data.get("base_mtime"))
            elif sub == ["api", "run", "stop"]:
                r = self.goals.stop(str(data.get("id", "")), ip)
            elif sub == ["api", "goal", "draft"]:
                r = self.goals.draft(data.get("intent"), data.get("game"), data.get("device"), ip)
            elif sub == ["api", "goal", "confirm"]:
                r = self.goals.confirm(str(data.get("id", "")), data.get("goal_md"), ip)
            else:
                return self._send(h, 404, {"error": "not found"})
        except (KeyError, FileNotFoundError):
            return self._send(h, 404, {"error": "没有这个任务或目标运行"})
        except RequestConflict as e:
            return self._send(h, 409, {"error": str(e)})
        except ValueError as e:
            return self._send(h, 400 if sub[:2] == ["api", "jobs"] else 409, {"error": str(e)})
        log_err(f"dash 掌舵 {'/'.join(sub[1:])} 来自 {ip}: {json.dumps(r, ensure_ascii=False)[:200]}")
        return self._send(h, 200, r)

    # ── 响应小工具 ──
    def _send(self, h, code, data, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        h._send(code, data, ctype, {**HEADERS, **(extra or {})})

    def _redirect(self, h, to, extra=None):
        h._send(303, b"", "text/plain", {**HEADERS, "Location": to, **(extra or {})})

    def _static(self, h, name):
        ext = os.path.splitext(name)[1]
        if not re.fullmatch(r"[a-z0-9_.-]+", name) or ext not in STATIC_TYPES:
            return self._send(h, 404, {"error": "not found"})
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                data = f.read()
        except OSError:
            return self._send(h, 404, {"error": "not found"})
        self._send(h, 200, data, STATIC_TYPES[ext])

    def _login_page(self, h, msg="", code=200):
        html = LOGIN_HTML.replace("{{msg}}", f'<p class="err">{msg}</p>' if msg else "")
        if not self._pass():
            html = html.replace("{{hint}}", "节点上还没设置查看密码（~/.config/hgamey/dash.pass）")
        html = html.replace("{{hint}}", "")
        self._send(h, code, html.encode(), "text/html; charset=utf-8")

    def _shot(self, h, serial=""):
        pat = (re.sub(r"[^0-9A-Za-z_-]", "_", serial) if serial else "*") + ".jpg"
        shots = sorted(glob.glob(os.path.join(self.shot_dir, pat)), key=_mtime)
        if not shots:
            return self._send(h, 404, {"error": "还没有截图"})
        self._send(h, 200, open(shots[-1], "rb").read(), "image/jpeg")

    # ── 任务 ──
    def _job_meta(self, jid):
        p = os.path.join(self.jobs_dir, jid, "meta.json")
        mt = _mtime(p)
        c = self.meta_cache.get(jid)
        if c and c[0] == mt:
            return c[1]
        m = _load(p)
        if m:
            self.meta_cache[jid] = (mt, m)
        return m

    def _alive(self, pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _pretty_argv(self, jid, argv):
        jin = os.path.join(self.jobs_dir, jid, "in") + "/"
        jout = os.path.join(self.jobs_dir, jid, "out")
        out = []
        for a in argv or []:
            a = a.replace(jout, "@out")
            a = re.sub(re.escape(jin) + r"\d+/", "", a)
            out.append(_redact(a))
        return out

    def jobs(self, limit=JOBS_SHOWN):
        try:
            ids = [e.name for e in os.scandir(self.jobs_dir) if e.is_dir() and re.fullmatch(r"[0-9a-f]{12}", e.name)]
        except OSError:
            return []
        metas = []
        for jid in ids:
            m = self._job_meta(jid)
            if m:
                metas.append(m)
        metas.sort(key=lambda m: m.get("started", 0), reverse=True)
        out = []
        for m in metas[:limit * 3]:
            if self.job_service:
                try:
                    m = self.job_service.status(m["id"])
                except (KeyError, OSError, ValueError):
                    continue
                state = "lost" if m.get("lost") else m["state"]
            elif "exit" in m:
                state = "finished"
            elif self._alive(m.get("pid")):
                state = "running"
            else:
                state = "lost"
            if state != "running" and len(out) >= limit:
                continue
            logp = os.path.join(self.jobs_dir, m["id"], "log")
            argv = m.get("argv") or []
            label = m.get("label") or next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--lock-label"), None)
            out.append({"id": m["id"], "label": label, "bin": m.get("bin"),
                        "argv": self._pretty_argv(m["id"], m.get("argv")), "started": m.get("started"),
                        "ended": m.get("ended"), "exit": m.get("exit"), "state": state,
                        "log_bytes": os.path.getsize(logp) if os.path.exists(logp) else 0, "log_mtime": _mtime(logp)})
        return out

    def log_tail(self, jid, nbytes):
        if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
            return {"error": "bad job id"}
        p = os.path.join(self.jobs_dir, jid, "log")
        nbytes = max(1024, min(nbytes, 256 << 10))
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - nbytes))
                data = f.read()
        except OSError:
            return {"error": "no such job"}
        if size > nbytes:
            data = data.split(b"\n", 1)[-1]
        text = _redact(ANSI.sub(b"", data).decode(errors="replace"))
        return {"job": jid, "size": size, "text": text}

    # ── 实验证据扫描线程 ──
    def _candidate_dirs(self, jobs):
        """(目录, 任务) 对：任务的 @out 目录树 + 任务参数里 --out 指向的节点本地目录 + 常见 runs 目录。"""
        seen, res = set(), []

        def add(d, job):
            d = os.path.realpath(d)
            if d not in seen and os.path.isdir(d):
                seen.add(d)
                res.append((d, job))

        for j in jobs:
            meta = self._job_meta(j["id"]) or {}
            found = self.walk_cache.get(j["id"])
            if found is None or j["state"] == "running":
                found, visited = [], 0
                jout = os.path.join(self.jobs_dir, j["id"], "out")
                for root, dirs, files in os.walk(jout):
                    visited += 1
                    depth = root[len(jout):].count(os.sep)
                    if detect_markers(root, dirs, files):
                        found.append(root)
                        dirs[:] = []
                    elif depth >= 3 or visited > 200:
                        dirs[:] = []
                self.walk_cache[j["id"]] = found  # 结束了的任务输出不再变，只走一次
            for root in found:
                add(root, j)
            argv = meta.get("argv") or []
            cwd = self.gol_root if meta.get("bin") == "game_opt_loop" else self.pf_root
            for i, a in enumerate(argv):
                val = None
                if a in ("--out", "--out-dir", "-o") and i + 1 < len(argv):
                    val = argv[i + 1]
                elif a.startswith("--out="):
                    val = a.split("=", 1)[1]
                if val and "@out" not in val and "/_jobs/" not in val:
                    add(val if os.path.isabs(val) else os.path.join(cwd, val), j)
        cutoff = _now() - 3 * 86400
        for pat in (os.path.join(self.gol_root, "runs", "*"), os.path.join(self.pf_root, "loop_v1", "runs*"),
                    os.path.join(self.pf_root, "loop_v1", "runs*", "*")):
            for d in glob.glob(pat):
                if os.path.isdir(d) and _mtime(d) > cutoff and detect_markers(d, os.listdir(d), os.listdir(d)):
                    add(d, None)
        return res

    def _scan_once(self):
        jobs = self.jobs(limit=200)
        exps = []
        for d, job in self._candidate_dirs(jobs):
            sig = signature(d)
            c = self.exp_cache.get(d)
            if not c or c[0] != sig:
                try:
                    c = (sig, detect(d))
                except Exception as e:  # 证据格式千奇百怪，一个目录坏了不能拖垮看板
                    c = (sig, {"kind": "?", "title": os.path.basename(d), "meta": {"error": str(e)[:200]}, "cands": [], "gens": {}})
                self.exp_cache[d] = c
            if c[1]:
                exps.append(self._wrap(c[1], d, job, sig))
        for j in jobs:
            meta = self._job_meta(j["id"]) or {}
            if j["bin"] == "phonefarm" and (meta.get("argv") or [""])[0] == "eval":
                logp = os.path.join(self.jobs_dir, j["id"], "log")
                key = "log:" + j["id"]
                sig = _mtime(logp)
                c = self.exp_cache.get(key)
                if not c or c[0] != sig:
                    c = (sig, parse_eval_log(logp))
                    self.exp_cache[key] = c
                e = c[1] or {"kind": "eval", "title": "eval（测量中）" if j["state"] == "running" else "eval（没有判定输出）",
                             "meta": {}, "cands": [], "gens": {}}
                exps.append(self._wrap(e, key, j, sig))
        exps.sort(key=lambda e: e["updated"], reverse=True)
        exps = exps[:15]
        ver = hashlib.sha1(json.dumps([(e["id"], e["updated"], e["running"], len(e["cands"])) for e in exps]).encode()).hexdigest()[:12]
        self.exps, self.exps_ver = exps, ver

    def _wrap(self, e, d, job, sig):
        running = bool(job and job["state"] == "running")
        cands = [dict(c) for c in e["cands"]]
        if running:  # 正在跑的那一个 = 第一个还没有判定的
            nxt = next((c for c in cands if c["outcome"] == "pending"), None)
            if nxt:
                nxt["outcome"] = "measuring"
        rel = d
        for base, tag in ((self.jobs_dir, "_jobs"), (self.gol_root, "game_opt_loop"), (self.pf_root, "phonefarm")):
            if d.startswith(base):
                rel = tag + d[len(base):]
                break
        cnt = collections.Counter(c["outcome"] for c in cands)
        return {**e, "cands": cands, "id": hashlib.sha1(d.encode()).hexdigest()[:10], "path": rel,
                "job": job and {"id": job["id"], "label": job["label"], "state": job["state"]},
                "running": running, "updated": max(sig[0] if isinstance(sig, tuple) else sig, job["started"] if job else 0),
                "counts": dict(cnt)}

    def _scanner(self):
        last = 0.0
        while True:
            time.sleep(1)
            if _now() - self.last_view > VIEW_IDLE_S or _now() - last < SCAN_EVERY_S:
                continue  # 没人看就不扫；有人看时每 SCAN_EVERY_S 扫一次（第一次打开页面 1 秒内就扫）
            last = _now()
            try:
                self._scan_once()
            except Exception as e:  # noqa: BLE001
                log_err(f"dash 扫实验证据出错: {e!r}")

    # ── 汇总 ──
    def home(self, h):
        """首页：手机槽位 + 目标运行列表。不含开发 agent 的任务、锁队列（那些在维护页）。"""
        runs = self.goals.list()
        running = [r for r in runs if r["status"] in ("running", "paused") and not r["demo"]]  # 示例不占真手机
        snapshot = self.monitor.snapshot()
        lock = snapshot["lock"]
        fl = snapshot
        slots = []
        for d in fl.get("devices") or []:
            serial = d.get("serial")
            goal = next((r for r in running if (r["state"].get("device") in (serial,)) or
                         (lock.get("held_by") == r.get("lock_label") and r["state"].get("device") in (None, "any"))), None)
            # 目标在跑但手机此刻被别人占着（目标在排队）：也标出来，免得看着像「没有目标」
            waiting = None
            if not goal:
                waiting = next((r for r in running if r["state"].get("device") in (serial, None, "any")), None)
            shot = os.path.join(self.shot_dir, re.sub(r"[^0-9A-Za-z_-]", "_", serial or "") + ".jpg")
            b, t = d.get("battery") or {}, d.get("thermal") or {}
            slots.append({"serial": serial, "online": d.get("online"), "state": d.get("state"),
                          "model": " ".join(x for x in (d.get("brand"), d.get("model")) if x) or None, "os": d.get("os"),
                          "battery_pct": b.get("level_pct"), "battery_status": b.get("status"), "on_battery": b.get("on_battery"),
                          "soc_c": t.get("soc_max_c"), "gpu_c": t.get("gpu_c"), "battery_c": b.get("temp_c") or t.get("battery_c"),
                          "busy": lock["status"] != "free", "light": d.get("probe_depth") == "light",
                          "shot_at": int(_mtime(shot)) if os.path.exists(shot) else None,
                          "goal": goal and {"run_id": goal["run_id"], "summary": goal["state"].get("goal_summary") or goal["intent"],
                                            "demo": goal["demo"]},
                          "waiting_goal": waiting and {"run_id": waiting["run_id"],
                                                       "summary": waiting["state"].get("goal_summary") or waiting["intent"],
                                                       "holder": lock.get("held_by")}})
        while len(slots) < SLOTS_MIN:
            slots.append(None)
        return {"now": _now(), "slots": slots, "fleet_age_s": snapshot["age_s"],
                "fleet_err": snapshot["error"], "runs": runs, "steer": self.steered(h), "steer_configured": bool(self._steer_pass()),
                "harness_ready": self.goals.harness_support(),
                "busy": lock["status"] != "free",
                # 首页「现在」一行要用：谁在用手机、排队顺序（只给名字，不给日志）
                "lock": {"held_by": lock.get("held_by"), "since": lock.get("since"), "stale": lock.get("stale"),
                         "queue": lock.get("queue") or []}}

    def state(self):
        snapshot = self.monitor.snapshot()
        lock = snapshot["lock"]
        fl = snapshot
        shots = sorted(glob.glob(os.path.join(self.shot_dir, "*.jpg")), key=_mtime)
        shot = {"age_s": int(_now() - _mtime(shots[-1])), "at": int(_mtime(shots[-1]))} if shots else None
        return {"now": _now(), "lock": lock, "devices": fl.get("devices") or [], "notes": fl.get("notes") or [],
                "fleet_age_s": snapshot["age_s"], "fleet_err": snapshot["error"],
                "fleet_skip": snapshot["deferred"], "shot": shot, "jobs": self.jobs(), "hist": self.monitor.history(),
                "policy": {"shot_every_s": SHOT_EVERY_S, "free_grace_s": FREE_GRACE_S,
                           "fleet_every_free_s": FLEET_EVERY_FREE_S, "fleet_every_held_s": FLEET_EVERY_HELD_S}}


def detect_markers(root, dirs, files):
    dirs, files = set(dirs), set(files)
    if "proposer" in dirs or "caps.json" in files or "rule.json" in files:
        return True
    if any(re.fullmatch(r"gen\d+", x) for x in dirs):
        return True
    if "report.json" in files:
        rep = _load(os.path.join(root, "report.json"))
        return isinstance(rep, dict) and ("archive" in rep or "results" in rep)
    return False


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>HGamey 实时看板</title><link rel="icon" href="data:,">
<style>
:root{--bg:#f4f3ef;--card:#fcfcfb;--ink:#0b0b0b;--sub:#52514e;--line:rgba(11,11,11,.12);--acc:#2a78d6;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--sub:#c3c2b7;--line:rgba(255,255,255,.12);--acc:#3987e5}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);font:16px system-ui,-apple-system,"PingFang SC",sans-serif}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;width:min(340px,calc(100vw - 32px));box-sizing:border-box}
h1{margin:0;font-size:22px}.sub{color:var(--sub);margin:4px 0 18px}.err{color:var(--bad)}.hint{color:var(--sub);font-size:13px}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:11px 12px;border-radius:9px;border:1px solid var(--line);background:transparent;color:var(--ink)}
button{margin-top:10px;background:var(--acc);border:0;color:#fff;font-weight:600;cursor:pointer}
</style></head><body>
<form method="post" action="/dash/login" class="card">
<h1>HGamey 实时看板</h1><p class="sub">真手机实时状态 · 用查看密码只能看</p>{{msg}}
<input type="password" name="pw" placeholder="查看密码" autofocus autocomplete="current-password" required>
<button type="submit">进入</button><p class="hint">{{hint}}</p></form></body></html>"""
