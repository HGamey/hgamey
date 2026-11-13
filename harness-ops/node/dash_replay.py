"""我来当一次 Agent, second part: a task played back step by step from what the platform kept of it — every write to
the task, its directions, notes, requests and answers, and each job it ran — with what was sent, what the platform
answered and what a person saw of it on the web at that moment. Read-only: nothing here writes."""

CLIP, ITEMS = 300, 6   # longest text and list kept in a request or an answer; the pages each step links to hold the rest
TASK_STATE = {"active": "推进中", "idle": "暂空闲", "paused": "已暂停", "closed": "已关闭", "delivered": "已交付"}
DIRECTION_STATE = {"proposed": "待探索", "preparing": "准备中", "ready": "可开展", "running": "进行中", "waiting_device": "等待设备",
                   "waiting_human": "等待协助", "waiting_material": "等待素材", "awaiting_evaluation": "等待评价",
                   "verified": "已验证", "no_gain": "无收益", "rejected": "已排除", "deferred": "已暂缓"}
CONCLUDED = {"verified", "no_gain", "rejected", "deferred"}
DECISION = {"adopt": "采纳", "defer": "暂缓", "reject": "拒绝"}
OUTCOME = {"succeeded": "成功", "failed": "失败", "cancelled": "已取消", "interrupted": "已中断", "unknown": "结果未知"}
FIELD = {"title": "标题", "objective": "目标", "game": "游戏", "constraints": "约束", "budgets": "预算", "device_requirements": "设备要求",
         "state": "状态", "state_reason": "状态说明", "coverage": "覆盖范围", "criteria": "判据", "owner": "负责人",
         "deliverable": "交付", "follows": "接续", "hypothesis": "假设", "priority": "优先级", "priority_reason": "优先级理由",
         "prerequisites": "前提", "cost_expectation": "成本预估", "expected_benefit": "预期收益", "next_experiment": "下一步实验",
         "dependencies": "依赖", "feedback_decisions": "对人评的决定", "based_on": "基于", "evidence_refs": "证据",
         "need": "需要什么", "why": "为什么", "resume_hint": "继续提示", "context": "上下文", "close_reason": "关闭理由",
         "due_at": "期限", "supersedes": "取代"}
# Steps a person follows the story by; the rest (progress notes without pictures, field edits, a job ending) fold away.
KEY = {"task_create", "claim", "assign", "release", "deliver", "reopen", "close", "direction_new", "direction_end",
       "human_note", "ask", "reply", "note_media", "eval_new", "request_new", "feedback", "job_submit"}


def clip(value, n=CLIP):
    """`value` with every text longer than `n` and every list longer than ITEMS cut, however deep."""
    if isinstance(value, str):
        return value if len(value) <= n else value[:n] + f"…（共 {len(value)} 字）"
    if isinstance(value, dict):
        return {key: clip(item, n) for key, item in value.items()}
    if isinstance(value, list):
        return [clip(item, n) for item in value[:ITEMS]] + ([f"…（共 {len(value)} 项）"] if len(value) > ITEMS else [])
    return value


def line(text, n=60):
    """One line of `text` for a summary."""
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def who(audit):
    channel = audit.get("auth_channel")
    if channel == "dashboard_steer":
        return {"kind": "human", "name": "人"}
    if channel == "agent_token":
        return {"kind": "agent", "name": audit.get("actor_label")}
    return {"kind": "admin", "name": "管理员"}


def task_url(rid):
    return "/dash/research?kind=research&id=" + rid


def evidence(refs):
    keep = ("kind", "job_id", "path", "attachment_id", "filename", "content_type", "result_id", "source_name")
    return [{key: ref[key] for key in keep if key in ref} for ref in refs or [] if isinstance(ref, dict)]


class Story:
    """The steps of one task, built in order. `before` holds each object's previous version for the diff."""

    def __init__(self, rid):
        self.rid, self.steps, self.before = rid, [], {}
        self.notes, self.titles, self.versions = {}, {}, 0

    def add(self, at, actor, kind, summary, sent, answer, seen):
        self.steps.append({"at": at, "who": actor, "kind": kind, "key": kind in KEY, "summary": summary,
                           "sent": sent, "answer": answer, "seen": seen})
        return self.steps[-1]

    # ── writes to the research store ──
    def write(self, row, reopened=None):
        """One write as a step. `reopened`: the state the platform reopened the task from in the same write."""
        kind, oid, data, audit = row["kind"], row["id"], row["data"], row["audit"]
        old = self.before.get((kind, oid))
        self.before[(kind, oid)] = data
        changed = [key for key in data if old is not None and data.get(key) != old.get(key)]
        actor = who(audit)
        step_kind, summary, seen = getattr(self, "_" + kind)(row, old, changed, actor)
        base = "/dash/api" if actor["kind"] == "human" else ""
        body = {"schema": "phonefarm/research-write@1", "operation_id": row["operation_id"],
                "base_revision": row["revision"] - 1 if old is not None else None,
                "data": data if old is None else {key: data.get(key) for key in changed},
                **({"actor_label": audit.get("actor_label")} if actor["kind"] == "admin" and audit.get("actor_label") else {}),
                "reason": audit.get("reason")}
        words = "已记下" + (f"，第 {row['revision']} 版" if old is not None else "")
        if changed:
            words += "，改了" + "、".join(FIELD.get(key, key) for key in changed)
        if reopened:
            words += f"；任务从「{TASK_STATE.get(reopened, reopened)}」回到「推进中」"
        receipt = row.get("receipt") or {}
        self.add(row["updated_at"], actor, step_kind, summary,
                 {"method": "POST", "path": f"{base}/research-state/{kind}/{oid}", "body": clip(body)},
                 {"status": 201, "text": words, "raw": receipt, **({"reopened": True} if reopened else {})}, seen)

    def _research(self, row, old, changed, actor):
        data = row["data"]
        seen = {"type": "task", "title": data.get("title"), "state": data.get("state"), "state_word": TASK_STATE.get(data.get("state")),
                "owner": data.get("owner"), "state_reason": line(data.get("state_reason"), 200) or None, "url": task_url(row["id"])}
        if old is None:
            return "task_create", "新建任务：" + line(data.get("title")), seen
        if "deliverable" in changed and data.get("deliverable") or "state" in changed and data.get("state") == "delivered":
            if "deliverable" in changed and data.get("deliverable"):
                self.versions += 1
            deliverable = data.get("deliverable") or {}
            seen = {"type": "report", "title": data.get("title"), "version": self.versions, "revision": row["revision"],
                    "summary": clip(deliverable.get("summary") or "", 800), "artifacts": len(deliverable.get("artifacts") or []),
                    "url": "/dash/research/report?id=" + row["id"]}
            if "deliverable" not in changed:
                return "deliver", f"再次交付（第 {self.versions} 版报告）", seen
            again = old.get("deliverable") is not None
            return "deliver", ("更新交付报告" if again else "交付报告") + f"（第 {self.versions} 版）", seen
        if "owner" in changed:
            owner = data.get("owner")
            if owner is None:
                return "release", "放回待认领", seen
            if actor["kind"] == "agent" and owner == actor["name"]:
                return "claim", "认领这个任务", seen
            return "assign", "把任务指派给 " + owner, seen
        if "state" in changed:
            reason = line(row["audit"].get("reason") or data.get("state_reason"), 40)
            if data.get("state") == "active":
                return "reopen", "重新打开任务" + ("：" + reason if reason else ""), seen
            if data.get("state") == "closed":
                return "close", "关闭任务" + ("：" + reason if reason else ""), seen
            return "task_update", "把任务改为" + TASK_STATE.get(data.get("state"), data.get("state")), seen
        return "task_update", "修改任务：" + "、".join(FIELD.get(key, key) for key in changed), seen

    def _direction(self, row, old, changed, actor):
        data = row["data"]
        title = data.get("title") or row["id"]
        self.titles[row["id"]] = title
        seen = {"type": "direction", "id": row["id"], "title": title, "state": data.get("state"),
                "state_word": DIRECTION_STATE.get(data.get("state")), "state_reason": line(data.get("state_reason"), 200) or None,
                "hypothesis": line(data.get("hypothesis"), 200) or None, "evidence": evidence(data.get("evidence_refs")),
                "url": "/dash/research?kind=direction&id=" + row["id"]}
        if old is None:
            return "direction_new", "提出研究方向：" + line(title), seen
        if "state" in changed and data.get("state") in CONCLUDED:
            return "direction_end", f"给方向「{line(title, 30)}」下结论：{DIRECTION_STATE[data['state']]}", seen
        if "state" in changed:
            return "direction_update", f"把方向「{line(title, 30)}」改为{DIRECTION_STATE.get(data.get('state'), data.get('state'))}", seen
        return "direction_update", f"更新方向「{line(title, 30)}」：" + "、".join(FIELD.get(key, key) for key in changed), seen

    def _note(self, row, old, changed, actor):
        data, audit = row["data"], row["audit"]
        self.notes[row["id"]] = {"who": actor["name"], "text": data.get("text") or ""}
        replied = self.notes.get(data.get("reply_to")) if data.get("reply_to") else None
        refs = evidence(data.get("evidence_refs"))
        seen = {"type": "note", "id": row["id"], "who": actor, "text": clip(data.get("text") or "", 800),
                "decision": data.get("decision"), "ask": bool(data.get("ask")), "to": data.get("to"),
                "reply_to": ({"id": data["reply_to"], "who": replied["who"], "text": line(replied["text"], 120)}
                             if replied else {"id": data["reply_to"]} if data.get("reply_to") else None),
                "evidence": refs, "url": task_url(self.rid) + "#note-" + row["id"]}
        text = line(data.get("text"))
        if actor["kind"] == "human":
            if data.get("reply_to"):
                return "human_note", f"回答 {(replied or {}).get('who') or 'agent'} 的提问：" + text, seen
            return "human_note", (f"在任务页上给 {data['to']} 留言：" if data.get("to") else "在任务页上留言：") + text, seen
        if data.get("ask"):
            return "ask", "向人提问：" + text, seen
        if data.get("reply_to"):
            decision = DECISION.get(data.get("decision"))
            return "reply", ("答复留言（" + decision + "）：" if decision else "回复留言：") + text, seen
        if data.get("to"):
            return "note", f"给 {data['to']} 留言：" + text, seen
        return ("note_media" if refs else "note"), ("留言并附 %d 份材料：" % len(refs) if refs else "留言：") + text, seen

    def _request(self, row, old, changed, actor):
        data = row["data"]
        title = data.get("title") or row["id"]
        self.titles[row["id"]] = title
        spec = (row.get("derived") or {}).get("evaluation")
        if spec:   # each slot a person compared, by letter, with what kind of media it is and where it is read
            kinds = [item.get("media_kind") if isinstance(item, dict) else None for item in spec["items"]] if spec.get("items") else None
            slots = [{"slot": slot, "kind": kinds[i] if kinds else spec.get("media_kind"), "url": url}
                     for i, (slot, url) in enumerate(sorted((spec.get("media") or {}).items()))]
        seen = {"type": "request", "id": row["id"], "title": title, "need": line(data.get("need"), 200) or None,
                "state": data.get("state"), "close_reason": line(data.get("close_reason"), 120) or None,
                "evaluation": {"slots": slots, "pick": spec.get("pick")} if spec else None,
                "url": "/dash/research?kind=request&id=" + row["id"]}
        if old is None:
            if data.get("type") == "evaluation":
                return "eval_new", "发出盲评：" + line(title), seen
            return "request_new", ("请人拿主意：" if data.get("type") == "guidance" else "请人协助：") + line(title), seen
        if "state" in changed and data.get("state") == "closed":
            return "request_close", f"关闭请求「{line(title, 30)}」" + (
                "：" + line(data.get("close_reason"), 40) if data.get("close_reason") else ""), seen
        return "request_update", f"更新请求「{line(title, 30)}」：" + "、".join(FIELD.get(key, key) for key in changed), seen

    def _feedback(self, row, old, changed, actor):
        data = row["data"]
        title = self.titles.get(data.get("request_id")) or data.get("request_id")
        seen = {"type": "feedback", "request": {"id": data.get("request_id"), "title": title}, "text": clip(data.get("text") or "", 1200),
                "url": "/dash/research?kind=request&id=" + str(data.get("request_id"))}
        return "feedback", f"答复「{line(title, 30)}」：" + line(data.get("text"), 40), seen

    # ── jobs the task ran ──
    def job(self, job, meta):
        jid, actor = job["id"], ({"kind": "agent", "name": job["agent"]} if job.get("agent") else {"kind": "admin", "name": "管理员"})
        name = job.get("label") or job.get("kind") or "实验"
        phone = meta.get("device_access") == "write"
        spec = {key: meta[key] for key in ("bin", "argv", "label", "research_id", "direction_id", "experiment", "device_access",
                                           "device_binding", "time_budget", "priority") if meta.get(key) is not None}
        seen = {"type": "job", "id": jid, "label": job.get("label"), "kind": job.get("kind"), "phone": phone,
                "direction": ({"id": job["direction_id"], "title": self.titles.get(job["direction_id"])} if job.get("direction_id") else None),
                "state": "queued", "url": "/dash/jobs#job=" + jid}
        self.add(job["started"], actor, "job_submit", ("在手机上跑：" if phone else "跑一个作业：") + line(name, 40),
                 {"method": "POST", "path": "/jobs", "headers": {"X-Job": clip(spec)}},
                 {"status": 200, "text": f"收下了，作业号 {jid}，排队等{'手机' if phone else '空位'}",
                  "raw": {"id": jid, "state": "queued", "started": job["started"], **{key: spec[key] for key in ("research_id", "direction_id") if key in spec}}},
                 seen)
        if not job.get("ended"):
            return
        ran = job["ended"] - (job.get("claimed") or job["started"])
        outcome = job.get("outcome") or meta.get("outcome")
        words = [OUTCOME.get(outcome, outcome or "结束")]
        if meta.get("exit") is not None:
            words.append(f"退出码 {meta['exit']}")
        if job.get("claimed") and job["claimed"] - job["started"] >= 60:
            words.append(f"排队 {round((job['claimed'] - job['started']) / 60)} 分钟")
        words.append(f"跑了 {max(1, round(ran / 60))} 分钟" if ran >= 30 else f"跑了 {max(0, round(ran))} 秒")
        self.add(job["ended"], {"kind": "platform", "name": "平台", "for": job.get("agent")}, "job_end", f"作业「{line(name, 30)}」结束：" + words[0],
                 None, {"text": "，".join(words), "raw": {"id": jid, "outcome": outcome, "exit": meta.get("exit"),
                                                         "claimed": job.get("claimed"), "ended": job["ended"]}},
                 {**seen, "state": "finished", "outcome": outcome, "outputs": True})


def build(jobs, rid, model_jobs):
    """The task `rid` played back: {task, agents, steps}. `model_jobs` are the command view's jobs (dash_command)."""
    story = Story(rid)
    reopened = None
    for row in jobs.research.replay(rid):
        if (row.get("operation_id") or "").startswith("auto-reopen-"):
            # The platform reopened the delivered or closed task in the same write as the note or direction that follows.
            reopened = (story.before.get((row["kind"], row["id"])) or {}).get("state")
            story.before[(row["kind"], row["id"])] = row["data"]
            continue
        story.write(row, reopened)
        reopened = None
    for job in (j for j in model_jobs if j.get("research_id") == rid and j.get("started")):
        try:
            meta = jobs.status(job["id"])
        except (KeyError, OSError, ValueError):
            meta = {}
        story.job(job, meta)
    steps = sorted(story.steps, key=lambda step: step["at"])
    for n, step in enumerate(steps):
        step["n"] = n
    task = jobs.research.get("research", rid, blind=True)
    data = task["data"]
    agents = list(dict.fromkeys(step["who"]["name"] for step in steps if step["who"]["kind"] == "agent"))
    return {"task": {"id": rid, "title": data.get("title"), "state": data.get("state"), "owner": data.get("owner"),
                     "created_at": task["created_at"], "revision": task["revision"], "url": task_url(rid),
                     "report_url": "/dash/research/report?id=" + rid if data.get("deliverable") else None},
            "agents": agents, "steps": steps}

