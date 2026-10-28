"""What an agent owes right now. `pf-node checkin` prints this at the start of every loop,
so the loop depends on the node's record instead of the agent's memory."""
import json
import time

SCHEMA = "hgamey/agent-checkin@1"
STATUS_STALE_SECONDS = 30 * 60
TERMINAL_DIRECTIONS = {"verified", "no_gain", "rejected", "deferred"}
OPEN_RESEARCH = {"active", "idle", "paused"}
LOOP = [
    "每轮开始先运行 pf-node checkin，按 duties 的顺序处理；duties 为空时再推进实验。",
    "人工在 research 里的留言要回应：pf-node research note <研究> \"<回复>\" --reply-to <留言> --decision adopt|defer|reject。",
    "需要人判断时发 request，然后继续做其他方向，不要停下来等人。",
    "用自己的 token 提交任务、写 research、发言，记录都会落在你的名下。",
    "每轮结束用 pf-node agents status <你的名字> \"<在做什么>\" 更新状态。",
    "做完后用 pf-node research deliver 交付：总结、产物（任务或结果）、适用的游戏版本和设备。",
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
        mine = {row["id"]: data for row, data in studies if data.get("owner") == name and data["state"] in OPEN_RESEARCH}
        notes = [(row, json.loads(row["data"]), json.loads(row["audit"])) for row in _latest(db, "note")
                 if row["research_id"] in mine]
        requests = [(row, json.loads(row["data"]), json.loads(row["audit"])) for row in _latest(db, "request")
                    if row["research_id"] in mine]
        feedback = {}
        for row in db.execute("SELECT request_id, sequence FROM revisions WHERE kind='feedback'").fetchall():
            feedback.setdefault(row["request_id"], []).append(row["sequence"])
        directions = {}
        for row in _latest(db, "direction"):
            if row["research_id"] in mine:
                directions.setdefault(row["research_id"], []).append(row["state"])

    owed, waiting = [], []
    answered = {data["reply_to"] for _, data, audit in notes if audit.get("actor_label") == name and data["reply_to"]}
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
        if row["spec"].get("actor_id") != name:
            continue
        item = {"job_id": row["job_id"], "label": row["spec"].get("label"), "bin": row["spec"].get("bin")}
        if row["state"] == "blocked":
            blocked.append(item)
            owed.append({"kind": "unblock_job", "target": row["job_id"],
                         "text": f"任务 {row['job_id']} 卡在 blocked，占着资源；核查后释放或请人处理",
                         "command": f"pf-node status {row['job_id']}"})
        elif row["state"] == "done":
            if last is not None and (row.get("status_committed_at") or 0) > last:
                finished.append(item)
        else:
            active.append({**item, "state": row["state"]})

    status = entry.get("status")
    if status is None or now - status["at"] > STATUS_STALE_SECONDS:
        owed.append({"kind": "post_status", "text": "状态" + ("还没报过" if status is None else "超过 30 分钟没更新"),
                     "command": f"pf-node agents status {name} \"<在做什么>\""})
    for rid, data in mine.items():
        states = directions.get(rid, [])
        if not states:
            owed.append({"kind": "plan_directions", "research_id": rid,
                         "text": f"{rid}（{data['title']}）还没有方向；先写下要试的方向",
                         "command": f"pf-node research create direction <方向ID> <文件>   # research_id={rid}"})
        elif data["state"] == "active" and all(state in TERMINAL_DIRECTIONS for state in states):
            owed.append({"kind": "deliver", "research_id": rid,
                         "text": f"{rid} 的方向都已有结论；交付，或者再开一个方向",
                         "command": f"pf-node research deliver {rid} --summary <文件> --artifact job:<任务ID>"})
    unclaimed = [{"research_id": row["id"], "title": data["title"]} for row, data in studies
                 if data.get("owner") is None and data["state"] == "active"]
    return {"schema": SCHEMA, "node_id": jobs.agents.local_node_id, "agent": name, "at": now,
            "previous_checkin_at": last, "event_head": head, "duties": owed, "waiting_on_human": waiting,
            "jobs": {"active": active, "blocked": blocked, "finished_since_last_checkin": finished},
            "research": {"mine": [{"research_id": rid, "title": data["title"], "state": data["state"]} for rid, data in mine.items()],
                         "unclaimed": unclaimed},
            "loop": LOOP}


def checkin(jobs, name):
    """Duties as of now, then remember the check-in so the next one only marks newer items unread."""
    value = duties(jobs, name)
    jobs.agents.record_checkin(name, value["event_head"])
    return value
