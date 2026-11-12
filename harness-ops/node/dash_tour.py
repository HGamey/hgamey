"""我来当一次 Agent: a person walks the five steps an agent takes on the real platform — check in, take a task, have an
idle phone take a screenshot, leave a note, deliver — under a temporary agent identity. Each step runs the agents' own
tool (pf-node) against this center with that identity's token and keeps the command and what came back, for the page
to show beside what a person sees of it on the web.

The identity is tagged `tour` (folded away like an archived agent) and its task's ID starts with `tour-` (left out of
the home page and the task list unless asked for). A tour ends when it delivers, when the person ends it, or a day
after it started; then its identity is disabled and an undelivered task is closed."""
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time

import agent_duties
import job_selection
import node_agents

NODE = os.path.dirname(os.path.abspath(__file__))
PREFIX = agent_duties.TOUR_PREFIX
LEFT_S = 86400
STEPS = ("checkin", "claim", "shot", "note", "deliver")
PICTURE = re.compile(r".+\.(png|jpe?g|webp)\Z", re.I)
TOUR_ID = re.compile(r"tour-[0-9a-f]{6}\Z")
JOB_ID = re.compile(r"[0-9a-f]{12}\Z")
TITLE = "体验：给一台空闲手机截一张图"


def is_tour(research_id):
    return isinstance(research_id, str) and research_id.startswith(PREFIX)


class TourError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _write_envelope(operation, data, base_revision, reason):
    return {"schema": "phonefarm/research-write@1", "operation_id": operation, "base_revision": base_revision,
            "data": data, "actor_label": None, "reason": reason}


class Tours:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = os.path.join(jobs.data_root, "_tour")
        self._guard = threading.Lock()
        self._busy = set()   # tours with a step running: a second click waits for the first

    # ── stored state: one directory per tour, with its state and the identity's token ──
    def _path(self, tid, name="tour.json"):
        return os.path.join(self.root, tid, name)

    def _read(self, tid):
        if not TOUR_ID.match(tid or ""):
            return None
        try:
            with open(self._path(tid), encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError):
            return None

    def _write(self, tour):
        temporary = self._path(tour["id"], "tour.json." + secrets.token_hex(4))
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(tour, stream, ensure_ascii=False)
        os.replace(temporary, self._path(tour["id"]))

    def get(self, tid):
        tour = self._read(tid)
        if tour is None:
            raise TourError(404, "没有这次体验")
        if tour["ended"] is None and time.time() - tour["created_at"] > LEFT_S:
            tour = self.end(tid, "left")
        return tour

    def open(self):
        """Tours not ended yet, newest first; one left a day ago is ended on the way."""
        names = os.listdir(self.root) if os.path.isdir(self.root) else []
        tours = [self.get(name) for name in names if self._read(name)]
        return sorted((t for t in tours if t["ended"] is None), key=lambda t: t["created_at"], reverse=True)

    # ── starting and ending ──
    def start(self):
        tid = PREFIX + secrets.token_hex(3)
        with self._guard:
            os.makedirs(os.path.join(self.root, tid), mode=0o700)
            self.jobs.agents.create({"name": tid, "tags": [node_agents.TOUR_TAG],
                                     "note": "体验身份：有人在「我来当一次 Agent」页面上走一遍 agent 的五步"})
            token = self.jobs.agents.issue_token(tid)["token"]
            with open(os.open(self._path(tid, "token"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(token + "\n")
            # The task a person would write for an agent, written the way the page writes it.
            self.jobs.research.write("research", tid, _write_envelope(
                "create-" + tid, {"title": TITLE, "objective": "签到、认领、截图、留言、交付，走一遍 agent 的五步"},
                None, "我来当一次 Agent"), "dashboard_steer")
            tour = {"id": tid, "agent": tid, "task": tid, "created_at": time.time(), "ended": None, "end_reason": None,
                    "steps": {}, "failed": None, "job": None, "phone": None, "picture": None}
            self._write(tour)
            return tour

    def end(self, tid, reason="ended"):
        """Disable the identity (its token stops working) and close the task unless it was delivered."""
        with self._guard:
            tour = self._read(tid)
            if tour is None:
                raise TourError(404, "没有这次体验")
            if tour["ended"] is not None:
                return tour
            research = self.jobs.research
            current = research.get("research", tour["task"])
            if current["data"].get("state") not in ("delivered", "closed"):
                research.write("research", tour["task"], _write_envelope(
                    "end-" + tid, {**current["data"], "state": "closed", "state_reason": "体验结束"},
                    current["revision"], "体验结束"), "dashboard_steer")
            entry = self.jobs.agents.get(tour["agent"])
            if entry.get("enabled", True):
                self.jobs.agents.disable(tour["agent"], entry["revision"])
            tour.update(ended=time.time(), end_reason=reason)
            self._write(tour)
            return tour

    # ── the five steps ──
    def shot_args(self, tour):
        """An idle phone (this center's own first) and the command that has it take one screenshot."""
        phones = [p for p in job_selection.catalog(self.jobs) if p["idle"] and p["online"]]
        phones.sort(key=lambda p: p["node_id"] != self.jobs.node_id)
        if not phones:
            raise TourError(409, "现在没有空闲的手机，等一台空出来再做这一步")
        phone = phones[0]
        seen = phone.get("observation") or {}
        name = " ".join(x for x in (seen.get("brand"), phone.get("model")) if x) or phone["device_id"]
        return (["--research", tour["task"], "--device", phone["ref"], "--label", "体验：截一张图", "--detach",
                 "phonefarm", "screen", "capture", "--serial", "@serial", "--out", "@out/shot", "--json"],
                {"ref": phone["ref"], "name": name})

    def _args(self, tour, step):
        task, job, picture = tour["task"], tour["job"], tour["picture"]
        if step == "checkin":
            return ["checkin"]
        if step == "claim":
            return ["research", "claim", task]
        if step == "note":
            return ["research", "note", task, "截好了，这是这台手机现在的画面", "--evidence", f"job:{job}:{picture}"]
        phone = (tour["phone"] or {}).get("name") or "这台手机"
        return ["research", "deliver", task, "--summary",
                f"## 结论\n{phone} 现在的画面截下来了。\n\n![{phone} 现在的画面](job:{job}:{picture})",
                "--artifact", f"job:{job}", "--reason", "体验交付"]

    def shot_state(self, tour):
        """Where the screenshot is: None before it was asked for, else waiting, ready (with tour["picture"]) or failed."""
        if not tour["job"]:
            return None
        if tour["picture"]:
            return "ready"
        try:
            status = self.jobs.status(tour["job"])
        except (KeyError, OSError, ValueError):
            return "waiting"
        if status.get("state") != "finished":
            return "waiting"
        try:
            page = self.jobs.center.artifacts.list(tour["job"], {"path": ["shot"]})
        except (KeyError, OSError, ValueError):
            page = {"items": []}
        found = sorted(item["path"] for item in page.get("items", []) if item.get("kind") == "file" and PICTURE.match(item["name"]))
        if not found:
            return "failed"
        tour["picture"] = found[0]
        self._write(tour)
        return "ready"

    def step(self, tid, step, base_url):
        with self._guard:
            if tid in self._busy:
                raise TourError(409, "上一步还在做")
            self._busy.add(tid)
        try:
            return self._step(tid, step, base_url)
        finally:
            with self._guard:
                self._busy.discard(tid)

    def _step(self, tid, step, base_url):
        tour = self.get(tid)
        if step not in STEPS:
            raise TourError(404, "没有这一步")
        if tour["ended"] is not None:
            raise TourError(409, "这次体验已经结束")
        shot = self.shot_state(tour)
        if step in tour["steps"] and not (step == "shot" and shot == "failed"):
            return tour
        if any(s not in tour["steps"] for s in STEPS[:STEPS.index(step)]):
            raise TourError(409, "先做前面的步骤")
        if step in ("note", "deliver") and shot != "ready":
            raise TourError(409, "截图没成，重做第 3 步" if shot == "failed" else "截图还没出来，等它做完再做这一步")
        if step == "shot":
            args, tour["phone"] = self.shot_args(tour)
        else:
            args = self._args(tour, step)
        result = self.run(tour, args, base_url)
        if result["code"] != 0:
            tour["failed"] = {"step": step, **result}
            self._write(tour)
            raise TourError(502, "这一步没成：" + (result["stderr"] or result["stdout"]).strip()[-400:])
        if step == "shot":
            lines = result["stdout"].split()
            if not lines or not JOB_ID.match(lines[-1]):
                raise TourError(502, "截图作业没有返回作业号")
            tour.update(job=lines[-1], picture=None)
        tour["steps"][step], tour["failed"] = result, None
        self._write(tour)
        return self.end(tid, "delivered") if step == "deliver" else tour

    def run(self, tour, args, base_url):
        """pf-node as the identity runs it: its token in a private file, this center's address and node ID."""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("PF_") and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update(PF_NODE_URL=base_url, PF_NODE_TOKEN_FILE=self._path(tour["id"], "token"), PF_NODE_ID=self.jobs.node_id,
                   NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
        started = time.time()
        try:
            done = subprocess.run([sys.executable, os.path.join(NODE, "pf-node"), *args], env=env, capture_output=True,
                                  text=True, timeout=120, cwd=os.path.join(self.root, tour["id"]))
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired:
            code, out, err = 124, "", "pf-node 两分钟没有返回"
        return {"command": "pf-node " + shlex.join(args), "code": code, "stdout": out[-20000:], "stderr": err[-8000:],
                "at": started, "seconds": round(time.time() - started, 1)}

    # ── what a person sees of it on the web ──
    def view(self, tid):
        tour = self.get(tid)
        research, shot = self.jobs.research, self.shot_state(tour)
        entry = self.jobs.agents.get(tour["agent"])
        task = research.get("research", tour["task"])
        notes = research.list("note", {"research_id": [tour["task"]], "limit": ["20"]})["items"]
        job = None
        if tour["job"]:
            try:
                status = self.jobs.status(tour["job"])
                job = {"id": tour["job"], "label": status.get("label"), "state": status.get("state"),
                       "phase": status.get("phase"), "outcome": status.get("outcome")}
            except (KeyError, OSError, ValueError):
                job = {"id": tour["job"], "state": "unknown"}
        return {**tour, "next": next((s for s in STEPS if s not in tour["steps"]), None),
                "seen": {
                    "agent": {"name": entry["name"], "note": entry.get("note"), "enabled": entry.get("enabled", True),
                              "checkin_at": (entry.get("checkin") or {}).get("at")},
                    "task": {"id": task["id"], "title": task["data"].get("title"), "state": task["data"].get("state"),
                             "owner": task["data"].get("owner"), "state_reason": task["data"].get("state_reason"),
                             "url": "/dash/research?kind=research&id=" + task["id"],
                             "report_url": "/dash/research/report?id=" + task["id"]},
                    "job": job, "shot": shot, "phone": tour["phone"], "picture": tour["picture"],
                    "notes": [{"id": n["id"], "text": n["text"], "actor_label": n.get("actor_label"),
                               "evidence_refs": n.get("evidence_refs") or [], "updated_at": n["updated_at"]}
                              for n in notes if n.get("actor_label") == tour["agent"]]}}
