"""Read-only model for the command view (/dash/command): this node's tasks, directions and notes, its jobs with
their queue history, phones and agents, in one briefly cached response. Nothing here writes or samples devices."""
import os
import re
import socket
import threading
import time

# What a run did, read from the script it ran (argv[0] for experiment jobs); plain names for the view.
KINDS = (("refresh_ab", "刷新率 A/B"), ("policy_snapshot", "策略快照"), ("soak30", "30 分钟长测"), ("hold120", "120Hz 保持"),
         ("repro", "复现"), ("measure_default", "默认设置测帧"), ("prepare_genshin", "准备原神"), ("inventory", "设备盘点"),
         ("platform_figures", "出图"), ("runtime-info", "平台自检"), ("fleet", "设备巡检"), ("devices", "设备巡检"),
         ("timestats", "帧时间探针"), ("refresh_probe", "刷新率探针"), ("host_artifacts", "出图"), ("build_layer", "构建渲染层"),
         ("optimize", "优化方案"), ("charging", "充电控制"), ("dumpsys battery", "电池读数"), ("refresh", "刷新率读数"),
         ("DisplaySettings", "显示设置"), ("VkLayer", "渲染层检查"), ("dumpsys package", "游戏信息"), ("--help", "平台自检"),
         ("status", "设备锁查询"))
JOB_ID = re.compile(r"[0-9a-f]{12}")


def job_kind(meta):
    argv = [str(x) for x in (meta.get("argv") or [])]
    head = argv[0] if meta.get("bin") == "experiment" and argv else " ".join(argv)
    return next((name for key, name in KINDS if key in head), "实验")


class CommandModel:
    def __init__(self, jobs, fleet, ttl=5.0):
        self.jobs, self.fleet, self.ttl = jobs, fleet, ttl
        self._lock = threading.Lock()
        self._cached = None
        self._created = {}   # (kind, id) -> created_at; never changes
        self._done = {}      # job id -> compact job once it is finished and out of the queue
        self._queue = {}     # job id -> {"queued", "claimed", "waits": [{at, kind, blocking_jobs}]}
        self._stream, self._after = None, 0

    def get(self):
        with self._lock:
            if self._cached is None or time.time() - self._cached["at"] >= self.ttl:
                self._cached = self._build()
            return self._cached

    def _every(self, kind):
        research, items, query = self.jobs.research, [], {"limit": ["100"]}
        while True:
            page = research.list(kind, query)
            items += page["items"]
            if not page.get("next_cursor"):
                return items
            query = {"limit": ["100"], "cursor": [page["next_cursor"]]}

    def _created_at(self, kind, oid):
        key = (kind, oid)
        if key not in self._created:
            self._created[key] = self.jobs.research.get(kind, oid).get("created_at")
        return self._created[key]

    def _fold_events(self):
        """Fold new job events into first queued/claimed times and each change of what held a queued job."""
        events = self.jobs.events
        while True:
            page = events.events({"after": [str(self._after)], "limit": ["200"]})
            if page["stream_id"] != self._stream:
                if self._stream is not None and self._after:
                    self._queue, self._after, self._stream = {}, 0, page["stream_id"]
                    continue
                self._stream = page["stream_id"]
            for item in page["items"]:
                status = item.get("committed_status") or {}
                facts = self._queue.setdefault(item["job_id"], {"waits": []})
                state = status.get("queue_state")
                if state == "queued":
                    facts.setdefault("queued", item["at"])
                    held = status.get("waiting")
                    last = facts["waits"][-1] if facts["waits"] else None
                    if held and (last is None or (last["kind"], last["blocking_jobs"]) != (held.get("kind"), held.get("blocking_jobs"))):
                        facts["waits"].append({"at": item["at"], "kind": held.get("kind"), "blocking_jobs": list(held.get("blocking_jobs") or [])})
                elif state == "claimed":
                    facts.setdefault("claimed", item["at"])
                self._after = item["sequence"]
            if not page.get("has_more"):
                return

    def _job(self, jid):
        if jid in self._done:
            return self._done[jid]
        meta = self.jobs.status(jid)
        job = {"id": jid, "kind": job_kind(meta), "agent": meta.get("actor_id"), "research_id": meta.get("research_id"),
               "device": (meta.get("device_binding") or {}).get("device_id"), "started": meta.get("started"),
               "ended": meta.get("ended"), "outcome": meta.get("outcome"), "label": meta.get("label"),
               "phone": meta.get("device_access") == "write", "direction_id": meta.get("direction_id"),
               "state": meta.get("state"), "phase": meta.get("phase")}
        if meta.get("state") == "finished" and (meta.get("queue") or {}).get("state") in (None, "done"):
            self._done[jid] = job
        return job

    def _build(self):
        now = time.time()
        tasks, dirs, notes = self._every("research"), self._every("direction"), self._every("note")
        self._fold_events()
        ids = {jid for jid in os.listdir(self.jobs.dir) if JOB_ID.fullmatch(jid)}
        ids |= {row["job_id"] for row in self.jobs.dispatch.queue.rows()}
        jobs = []
        for jid in sorted(ids):
            try:
                job = dict(self._job(jid))
            except (OSError, ValueError, KeyError):
                continue
            facts = self._queue.get(jid, {})
            job.update(queued=facts.get("queued"), claimed=facts.get("claimed"), waits=facts.get("waits", []))
            jobs.append(job)
        snapshot = self.fleet.snapshot() if self.fleet is not None and hasattr(self.fleet, "snapshot") else {}
        phones = [{"serial": d.get("serial"), "brand": d.get("brand"), "model": d.get("model"),
                   "temp_c": (d.get("battery") or {}).get("temp_c"), "soc_c": (d.get("thermal") or {}).get("soc_max_c"),
                   "state": d.get("state")} for d in (snapshot.get("devices") or [])]
        return {
            "at": now, "live": True, "source": socket.gethostname(),
            "agents": [a["name"] for a in self.jobs.agents.list()],
            "phones": phones, "phones_sampled_at": snapshot.get("sampled_at"),
            "research": [{"id": t["id"], "title": t["title"], "state": t["state"], "owner": t.get("owner"), "follows": t.get("follows"),
                          "updated_at": t["updated_at"], "created_at": self._created_at("research", t["id"])} for t in tasks],
            "directions": [{"id": d["id"], "research_id": d["research_id"], "title": d["title"], "state": d["state"],
                            "updated_at": d["updated_at"], "created_at": self._created_at("direction", d["id"])} for d in dirs],
            "notes": [{"id": n["id"], "research_id": n["research_id"],
                       "who": "human" if n.get("auth_channel") == "dashboard_steer" else (n.get("actor_label") or "admin"),
                       "decision": n.get("decision"), "reply_to": n.get("reply_to"), "text": (n.get("text") or "")[:140],
                       "at": n["updated_at"], "ask": bool(n.get("ask")), "open": bool(n.get("question_unanswered"))} for n in notes],
            "jobs": jobs,
        }
