"""What an agent owes right now. `pf-node checkin` prints this at the start of every loop,
so the loop depends on the node's record instead of the agent's memory."""
import json
import time

SCHEMA = "hgamey/agent-checkin@1"
STATUS_STALE_SECONDS = 30 * 60
TERMINAL_DIRECTIONS = {"verified", "no_gain", "rejected", "deferred"}
OPEN_RESEARCH = {"active", "idle", "paused"}
LOOP = [
    "最短完整路径：checkin → research claim → plan-template/create direction → --research 提交任务 → jobs --brief/status → research note 回应留言（附件用 job:<任务>:<路径>）→ research deliver；需要人定的事用 --ask 提问后继续其他方向。",
    "设备锁序列化实际操作；任务预留只在执行中占调度资源，终态自动释放。宿主完成未知或设备仍待恢复时由负责人或管理员 reset，不代表恢复。",
    "recovery required 的已结束任务先还原设备，再用自己的 token 执行 pf-node reset <id> --note；别人任务由负责人或管理员处理。",
    "设备写任务只占一台：研究只有一台 device_requirements 时默认同机；已登记的手机默认走自己的 lane，否则用 --resource device:<串号>，显式参数优先，global 会占全部设备。",
    "typed 设备任务里的 devlock 只占本机 lane，嵌套会话沿用同一实例；全局范围任务仍用全农场 devlock。",
    "每轮开始先运行 pf-node checkin，按 duties 的顺序处理；duties 为空时再推进实验。",
    "只处理自己负责的研究；别人研究里的留言是写给那个研究的负责人的。",
    "人工在 research 里的留言要回应：pf-node research note <研究> \"<回复>\" --reply-to <留言> --decision adopt|defer|reject。没回应之前，这个研究不能交付，方向也不能下结论。",
    "其他 agent 和维护者的留言也会进待办，同样要回应，但不挡交付。",
    "图在自己机器上画，用 research note --attach 附上；实验 Python 只保证有标准库。",
    "devlock busy 默认等 DEVLOCK_WAIT_SECONDS=900 秒，超时清自己的队列标签并记录阻塞者；立即失败设 0。单机用 phonefarm lease --serial <串号>（已注册设备走 lane），多机才用全局 devlock。",
    "需要人判断时发 request，然后继续做其他方向，不要停下来等人。",
    "为研究提交任务时加 --research <研究ID>，网页的手机占用和任务页才看得到这个任务；属于某方向时再加 --direction <方向ID>，运行会挂到该方向下。",
    "用自己的 token 提交任务、写 research、发言，记录都会落在你的名下。",
    "每轮结束用 pf-node agents status <你的名字> \"<在做什么>\" 更新状态。",
    "做完后用 pf-node research deliver 交付：总结、产物（任务或结果）、适用的游戏版本和设备。",
    "研究交付不会自动给方向下结论：pf-node research conclude <方向ID> verified|no_gain|rejected|deferred --reason \"<依据>\" [--evidence job:<任务>:<路径>]。",
]


def _latest(db, kind):
    return db.execute("SELECT r.* FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id AND r.revision=o.revision "
                      "WHERE o.kind=? ORDER BY r.sequence", (kind,)).fetchall()


def duties(jobs, name, now=None):
    """Compute duties for agent `name`; reads only."""
    now = time.time() if now is None else now
    entry = jobs.agents.get(name)
    checkin = entry.get("checkin") or {}
    seen, last = checkin.get("seen_event_seq", 0), checkin.get("at")
    with jobs.research._db() as db:
        head = jobs.research._upper(db)
        studies = [(row, json.loads(row["data"])) for row in _latest(db, "research")]
        owned = {row["id"]: data for row, data in studies if data.get("owner") == name}
        mine = {rid: data for rid, data in owned.items() if data["state"] in OPEN_RESEARCH}
        notes = [(row, json.loads(row["data"]), json.loads(row["audit"])) for row in _latest(db, "note")
                 if row["research_id"] in owned]
        requests = [(row, json.loads(row["data"]), json.loads(row["audit"])) for row in _latest(db, "request")
                    if row["research_id"] in mine]
        feedback = {}
        for row in db.execute("SELECT request_id, sequence FROM revisions WHERE kind='feedback'").fetchall():
            feedback.setdefault(row["request_id"], []).append(row["sequence"])
        directions = {}
        for row in _latest(db, "direction"):
            if row["research_id"] in owned:
                directions.setdefault(row["research_id"], []).append((row["id"], row["state"]))

    owed, waiting = [], []
    answered = {data["reply_to"] for _, data, audit in notes if audit.get("actor_label") == name and data["reply_to"]}
    own_questions = {row["id"]: (row, data) for row, data, audit in notes
                     if data.get("ask") is True and audit.get("actor_label") == name}
    answers_by_question, question_answers = set(), []
    for row, data, audit in notes:
        if data.get("reply_to") not in own_questions or audit.get("auth_channel") not in ("dashboard_steer", "admin_token"):
            continue
        answers_by_question.add(data["reply_to"])
        question_answers.append({"research_id": row["research_id"], "id": row["id"], "reply_to": data["reply_to"],
                                 "text": data["text"], "at": row["at"], "unread": row["sequence"] > seen})
    for question_id, (row, data) in own_questions.items():
        if question_id not in answers_by_question:
            waiting.append({"kind": "note_question", "research_id": row["research_id"], "request_id": question_id,
                            "title": data["text"][:200], "overdue": False})
    for row, data, audit in notes:
        if audit.get("actor_label") == name or row["id"] in answered:
            continue
        speaker = "human" if audit.get("auth_channel") == "dashboard_steer" else audit.get("actor_label") or "admin token"
        owed.append({"kind": "answer_note", "research_id": row["research_id"], "target": row["id"],
                     "unread": row["sequence"] > seen,
                     "text": f"{speaker} 在 {row['research_id']} 留言：{data['text'][:200]}",
                     "command": f"pf-node research note {row['research_id']} \"<回复>\" --reply-to {row['id']} "
                                "--decision adopt|defer|reject"})
    for row, data, audit in requests:
        if data["state"] != "open":
            continue
        answers = feedback.get(row["id"], [])
        if answers:
            owed.append({"kind": "process_feedback", "research_id": row["research_id"], "target": row["id"],
                         "unread": max(answers) > seen,
                         "text": f"请求 {row['id']}（{data['title']}）已有 {len(answers)} 条反馈；处理后关闭请求",
                         "command": f"pf-node research show request {row['id']}"})
        else:
            waiting.append({"research_id": row["research_id"], "request_id": row["id"], "title": data["title"],
                            "overdue": data["due_at"] is not None and data["due_at"] < now})

    active, blocked, finished = [], [], []
    for row in jobs.dispatch.queue.rows():
        research_id = row["spec"].get("research_id")
        if row["spec"].get("actor_id") != name and research_id not in owned:
            continue
        item = {"job_id": row["job_id"], "label": row["spec"].get("label"), "bin": row["spec"].get("bin"),
                "research_id": research_id}
        if row["state"] == "blocked":
            blocked.append(item)
            try:
                meta = jobs.status(row["job_id"])
            except (OSError, ValueError, KeyError):
                continue
            if meta.get("state") == "finished" and (meta.get("recovery") or {}).get("status") == "required":
                owed.insert(0, {"kind": "reset_job", "target": row["job_id"],
                                "text": f"任务 {row['job_id']} 只待设备恢复；先还原设备，再重置",
                                "command": f"pf-node reset {row['job_id']} --note \"<已还原内容与核查>\""})
        elif row["state"] == "done":
            if last is not None and (row.get("status_committed_at") or 0) > last:
                finished.append(item)
        else:
            active.append({**item, "state": row["state"]})

    status = entry.get("status")
    if (mine or active) and (status is None or now - status["at"] > STATUS_STALE_SECONDS):
        owed.append({"kind": "post_status", "text": "状态" + ("还没报过" if status is None else "超过 30 分钟没更新"),
                     "command": f"pf-node agents status {name} \"<在做什么>\""})
    for rid, data in mine.items():
        states = [state for _, state in directions.get(rid, [])]
        if not states:
            owed.append({"kind": "plan_directions", "research_id": rid,
                         "text": f"{rid}（{data['title']}）还没有方向；先写下要试的方向",
                         "command": f"pf-node research create direction <方向ID> <文件>   # research_id={rid}"})
        elif data["state"] == "active" and all(state in TERMINAL_DIRECTIONS for state in states):
            owed.append({"kind": "deliver", "research_id": rid,
                         "text": f"{rid} 的方向都已有结论；交付，或者再开一个方向",
                         "command": f"pf-node research deliver {rid} --summary <文件> --artifact job:<任务ID>"})
    for rid, data in owned.items():
        states = [state for _, state in directions.get(rid, [])]
        if data["state"] == "delivered" or (data["state"] == "active" and states and all(state in TERMINAL_DIRECTIONS for state in states)):
            for did, state in directions.get(rid, []):
                if state not in TERMINAL_DIRECTIONS:
                    owed.append({"kind": "conclude_direction", "research_id": rid, "target": did,
                                 "text": f"{rid} 已交付或准备交付，方向 {did} 仍为 {state}；给方向下结论",
                                 "command": f"pf-node research conclude {did} verified|no_gain|rejected|deferred "
                                            "--reason \"<依据>\" [--evidence job:<任务>:<路径>]"})
    unclaimed = [{"research_id": row["id"], "title": data["title"]} for row, data in studies
                 if data.get("owner") is None and data["state"] == "active"]
    return {"schema": SCHEMA, "node_id": jobs.agents.local_node_id, "agent": name, "at": now,
            "previous_checkin_at": last, "event_head": head, "duties": owed, "waiting_on_human": waiting,
            "question_answers": question_answers,
            "jobs": {"active": active, "blocked": blocked, "finished_since_last_checkin": finished},
            "research": {"mine": [{"research_id": rid, "title": data["title"], "state": data["state"]} for rid, data in mine.items()],
                         "unclaimed": unclaimed},
            "loop": LOOP}


def checkin(jobs, name):
    """Duties as of now, then remember the check-in so the next one only marks newer items unread."""
    value = duties(jobs, name)
    jobs.agents.record_checkin(name, value["event_head"])
    return value
