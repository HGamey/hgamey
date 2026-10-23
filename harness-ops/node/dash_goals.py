"""实时看板的「目标模式」一侧：读目标运行目录、掌舵（改 human.md / 停止）、新目标（起草 → 确认 → 开跑）。

目录与文件格式见 lead 的 GOAL_SPEC（~/node/goals/<run_id>/ 下 intent.txt、goal.md、human.md、
human.history/、state.json、events.jsonl、report.md、optimize/）。

分工：state.json 和 events.jsonl 归 harness（game_opt_loop goal …）写；本模块只写
  - intent.txt（新目标时）、goal.md（人确认时）、human.md 与 human.history/（人掌舵时）；
  - steer.audit.jsonl（每次写操作的时间、来源 IP、动作），以及 harness 还没接管前的 state.json 草稿态。
起任务一律经节点服务的 Jobs（和 pf-node 起的任务同一条通路，会出现在「维护」页，能被 kill）。

harness 的 `goal draft / goal run` 还没上线时：
  - 起草退回本地模板（goal.md 按 GOAL_SPEC 的默认值填好，等人改），事件里写明「模板起草」；
  - 「确认并开始」只保存 goal.md，不起任务，明确回「harness 的 goal run 还没上线」。
没有任何真实目标运行时，首页展示 dash_demo/ 下的示例运行（复制到 ~/node/_dash/demo-goals/，标「示例」）。
"""
import json, os, re, shutil, subprocess, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_SRC = os.path.join(HERE, "dash_demo")
RUN_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,63}")
MAX_DOC = 64 << 10           # goal.md / human.md 上限
STATUS_ZH = {"drafting": "起草中", "awaiting_confirm": "等你确认", "running": "进行中", "paused": "已暂停",
             "done": "已完成", "failed": "失败"}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _read(path, limit=MAX_DOC * 4):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_atomic(path, text):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


def read_events(path, limit=600):
    """events.jsonl 尾部最多 limit 行；坏行跳过（harness 写到一半时最后一行可能不完整）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - (1 << 20)))
            lines = f.read().decode(errors="replace").splitlines()
        if size > (1 << 20):
            lines = lines[1:]
    except OSError:
        return []
    out = []
    for l in lines[-limit:]:
        try:
            e = json.loads(l)
            if isinstance(e, dict):
                out.append(e)
        except ValueError:
            pass
    return out


def template_goal_md(intent, device, game="com.miHoYo.Yuanshen"):
    """harness 起草上线前的本地模板：GOAL_SPEC 的默认值 + 人的原话。"""
    return f"""---
game: {game}
device: {device or 'any'}
objective:
  metric: power_watt
  direction: lower
  target_pct: 5
constraints:
  - fps_mean: no_worse
  - frame_p95: no_worse
  - image: psnr_min_db 30
layers: [sysparam, gray]
per_generation: 2
forbid: []
budget:
  wall_minutes: 180
  max_generations: 6
  min_generations: 3
stop_when:
  - target_reached
  - budget_exhausted
  - no_gain: 2
proposer: llm
gate: shadow
---
{intent.strip()}

（这份草稿是模板填的：harness 的自动起草还没上线。请按你的意思改上面的字段，比如主指标、预算、允许动的层。）
"""


class Goals:
    def __init__(self, goals_dir, demo_dir, jobs=None, gol_exe=None):
        self.dir, self.demo_dir, self.jobs, self.gol_exe = goals_dir, demo_dir, jobs, gol_exe
        self.lock = threading.Lock()
        self._support = (0.0, None)
        os.makedirs(self.dir, exist_ok=True)

    # ── harness 支不支持 goal 子命令（缓存 10 分钟）──
    def harness_support(self):
        t, v = self._support
        if time.time() - t < 600 and v is not None:
            return v
        ok = False
        if self.gol_exe and os.path.exists(self.gol_exe):
            try:
                r = subprocess.run([self.gol_exe, "goal", "--help"], capture_output=True, timeout=10,
                                   stdin=subprocess.DEVNULL)
                txt = (r.stdout + r.stderr).decode(errors="replace")
                ok = r.returncode == 0 and "draft" in txt and "run" in txt
            except (OSError, subprocess.TimeoutExpired):
                ok = False
        self._support = (time.time(), ok)
        return ok

    # ── 列表 ──
    def _ensure_demo(self):
        src = os.path.join(DEMO_SRC, "demo-run")
        dst = os.path.join(self.demo_dir, "demo-run")
        if os.path.isdir(src) and not os.path.isdir(dst):
            os.makedirs(self.demo_dir, exist_ok=True)
            shutil.copytree(src, dst)
        return dst if os.path.isdir(dst) else None

    def run_dirs(self):
        """[(run_id, 目录, 是否示例)]；有真实运行就不出示例。"""
        real = []
        try:
            for e in os.scandir(self.dir):
                if e.is_dir() and RUN_ID.fullmatch(e.name) and not e.name.startswith("_") and (
                        os.path.exists(os.path.join(e.path, "state.json")) or os.path.exists(os.path.join(e.path, "intent.txt"))):
                    real.append((e.name, e.path, False))
        except OSError:
            pass
        if real:
            return real
        d = self._ensure_demo()
        return [("demo-run", d, True)] if d else []

    def find(self, run_id):
        for rid, d, demo in self.run_dirs():
            if rid == run_id:
                return d, demo
        raise KeyError(run_id)

    def summary(self, rid, d, demo):
        st = _load(os.path.join(d, "state.json")) or {}
        dm = self._dash_meta(d)
        owner = dm.get("lock_label") or (f"job-{dm['run_job']}" if dm.get("run_job") else f"goal-{rid}")
        intent = (_read(os.path.join(d, "intent.txt"), 2000) or "").strip()
        upd = max(_mtime(os.path.join(d, "state.json")), _mtime(os.path.join(d, "events.jsonl")), _mtime(d))
        return {"run_id": rid, "demo": demo, "state": st, "intent": intent, "updated": upd, "lock_label": owner,
                "job_id": dm.get("run_job"),
                "status": st.get("status") or "drafting", "status_zh": STATUS_ZH.get(st.get("status"), st.get("status") or "起草中")}

    def list(self):
        out = [self.summary(*x) for x in self.run_dirs()]
        out.sort(key=lambda s: (s["status"] in ("running", "paused", "awaiting_confirm", "drafting"), s["updated"]), reverse=True)
        return out

    def detail(self, run_id):
        d, demo = self.find(run_id)
        s = self.summary(run_id, d, demo)
        hist_dir = os.path.join(d, "human.history")
        hist = []
        if os.path.isdir(hist_dir):
            for n in sorted(os.listdir(hist_dir), reverse=True)[:50]:
                if n.endswith(".md"):
                    p = os.path.join(hist_dir, n)
                    hist.append({"name": n, "t": _mtime(p), "size": os.path.getsize(p)})
        audit = []
        try:
            with open(os.path.join(d, "steer.audit.jsonl"), encoding="utf-8") as f:
                for l in f.readlines()[-30:]:
                    try:
                        audit.append(json.loads(l))
                    except ValueError:
                        pass
        except OSError:
            pass
        s.update(goal_md=_read(os.path.join(d, "goal.md")), human_md=_read(os.path.join(d, "human.md")) or "",
                 human_mtime=_mtime(os.path.join(d, "human.md")), human_history=hist, audit=audit[::-1],
                 events=read_events(os.path.join(d, "events.jsonl")), report_md=_read(os.path.join(d, "report.md")),
                 harness_ready=self.harness_support(), dash=self._dash_meta(d),
                 frozen=os.path.exists(os.path.join(d, "goal.frozen.md")), run_alive=self._run_active(d))
        return s

    def history_text(self, run_id, name):
        d, _ = self.find(run_id)
        if not re.fullmatch(r"[0-9A-Za-z_.-]+\.md", name or ""):
            raise KeyError(name)
        t = _read(os.path.join(d, "human.history", name))
        if t is None:
            raise KeyError(name)
        return t

    # ── 写操作（调用方已核过掌舵密码）──
    def _audit(self, d, ip, action, **kw):
        rec = {"t": _now_iso(), "ip": ip, "action": action, **kw}
        with open(os.path.join(d, "steer.audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        gdir = os.path.dirname(self.demo_dir.rstrip("/"))
        os.makedirs(gdir, exist_ok=True)
        with open(os.path.join(gdir, "steer_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({**rec, "run_id": os.path.basename(d)}, ensure_ascii=False) + "\n")

    def save_human(self, run_id, text, ip, action="edit_human", base_mtime=None):
        if not isinstance(text, str) or len(text.encode()) > MAX_DOC:
            raise ValueError("human.md 太长（上限 64 KB）")
        d, _ = self.find(run_id)
        with self.lock:
            p = os.path.join(d, "human.md")
            if base_mtime is not None and abs(_mtime(p) - float(base_mtime)) > 0.001 and os.path.exists(p):
                raise ValueError("human.md 在你打开之后被别人改过了，刷新后再改")
            if (_read(p) or "") == text:
                return {"saved": False, "note": "内容没变"}
            _write_atomic(p, text)
            hd = os.path.join(d, "human.history")
            os.makedirs(hd, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + ".md"
            i = 1
            while os.path.exists(os.path.join(hd, name)):  # 同一秒存两次
                i += 1
                name = time.strftime("%Y%m%d-%H%M%S") + f"-{i}.md"
            _write_atomic(os.path.join(hd, name), text)
            self._audit(d, ip, action, bytes=len(text.encode()), history=name)
        return {"saved": True, "history": name, "mtime": _mtime(p)}

    def _run_active(self, d):
        """Use the job receipt, including starting, without treating a PID as ownership."""
        m = self._dash_meta(d)
        jid = m.get("run_job")
        if not jid or not self.jobs:
            return False
        try:
            jm = self.jobs.status(jid)
        except (KeyError, OSError, ValueError):
            return False
        return jm.get("state") == "running"

    def stop(self, run_id, ip):
        d, _ = self.find(run_id)
        jid = self._dash_meta(d).get("run_job")
        reason = "没有可确认所有权的运行任务"
        if jid and self.jobs:
            receipt = self.jobs.cancel(jid, force=False)
            if receipt["accepted"]:
                self._audit(d, ip, "stop", via="job_cancel", job=jid)
                return {"saved": False, **receipt,
                        "note": "停止请求已记录，由作业 worker 发 SIGINT；请继续查看终态和还原证据"}
            reason = receipt.get("reason") or reason
        cur = _read(os.path.join(d, "human.md")) or ""
        if re.search(r"(?im)^\s*(停止|stop)\s*$", cur):
            return {"saved": False, "note": "human.md 里已经写了「停止」，harness 下一代开始前读到就停"}
        text = cur.rstrip("\n") + ("\n\n" if cur.strip() else "") + "停止\n"
        r = self.save_human(run_id, text, ip, action="stop")
        r["note"] = f"{reason}；已在 human.md 末尾写上「停止」，由 harness 下一次读取时处理，尚未确认停止"
        return r

    def _dash_meta(self, d, **upd):
        p = os.path.join(d, "dash.json")
        m = _load(p) or {}
        if upd:
            m.update(upd)
            _write_atomic(p, json.dumps(m, ensure_ascii=False, indent=1))
        return m

    def draft(self, intent, game, device, ip):
        intent = (intent or "").strip()
        game = (game or "").strip()
        if not intent or len(intent) > 2000:
            raise ValueError("目标要写一句话（2000 字以内）")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", game):
            raise ValueError("游戏要填包名，比如 com.miHoYo.Yuanshen")
        if device and device != "any" and not re.fullmatch(r"[0-9A-Za-z._:-]{1,64}", device):
            raise ValueError("手机序列号不对")
        rid = time.strftime("%Y%m%d-%H%M%S")
        while os.path.exists(os.path.join(self.dir, rid)):
            rid += "b"
        d = os.path.join(self.dir, rid)
        os.makedirs(d)
        _write_atomic(os.path.join(d, "intent.txt"), intent + "\n")
        self._dash_meta(d, device=device or "any", game=game)
        self._audit(d, ip, "new_goal", intent=intent[:200], game=game, device=device or "any")
        if self.harness_support() and self.jobs:
            meta = self.jobs.start({"bin": "game_opt_loop", "label": f"goal-draft-{rid}",
                                    "argv": ["goal", "draft", "--intent", intent, "--game", game, "--out", d]}, b"")
            self._dash_meta(d, draft_job=meta["id"])
            return {"run_id": rid, "job": meta["id"], "mode": "harness"}
        # harness 还没上线：本地模板起草，直接等人确认
        _write_atomic(os.path.join(d, "human.md"), "")
        _write_atomic(os.path.join(d, "goal.md"), template_goal_md(intent, device, game))
        st = {"run_id": rid, "status": "awaiting_confirm", "goal_summary": intent[:80], "device": device or None,
              "generation": 0, "updated_at": _now_iso()}
        _write_atomic(os.path.join(d, "state.json"), json.dumps(st, ensure_ascii=False, indent=1))
        with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": _now_iso(), "type": "note", "gen": 0, "stage": "draft", "by": "template",
                                "message": "草稿用网页的本地模板生成（节点上的 harness 不认 goal 命令），请检查后确认"}, ensure_ascii=False) + "\n")
        return {"run_id": rid, "mode": "template"}

    def confirm(self, run_id, goal_md, ip):
        if not isinstance(goal_md, str) or not goal_md.lstrip().startswith("---") or len(goal_md.encode()) > MAX_DOC:
            raise ValueError("goal.md 要以 --- 开头的 YAML 头起始（64 KB 以内）")
        d, demo = self.find(run_id)
        if demo:
            raise ValueError("示例运行不能开跑")
        st = _load(os.path.join(d, "state.json")) or {}
        if st.get("status") not in (None, "drafting", "awaiting_confirm"):
            raise ValueError(f"这个目标已经是「{STATUS_ZH.get(st.get('status'), st.get('status'))}」，goal.md 已冻结")
        with self.lock:
            _write_atomic(os.path.join(d, "goal.md"), goal_md)
            self._audit(d, ip, "confirm", bytes=len(goal_md.encode()))
        if not (self.harness_support() and self.jobs):
            return {"started": False, "note": "goal.md 已保存。harness 的 goal run 还没上线（core 在做），上线后再点「确认并开始」"}
        meta = self.jobs.start({"bin": "game_opt_loop", "label": f"goal-{run_id}", "argv": ["goal", "run", d]}, b"")
        self._dash_meta(d, run_job=meta["id"], run_pid=meta.get("pid"), lock_label=meta.get("lock_label"))
        failed = meta.get("outcome") in {"failed", "interrupted"}
        return {"started": not failed, "job": meta["id"], "lock_label": meta.get("lock_label"),
                "phase": meta.get("phase"), "outcome": meta.get("outcome"),
                "note": ("任务已报告执行失败，请查看任务日志：" + (meta.get("error") or "执行失败")) if failed else
                        "任务已提交；请在任务详情核对启动、排队和执行状态，提交不代表已取得手机"}
