"""Node-local durable admission and dispatch around independently owned workers."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import threading
import tarfile
import time
import uuid

from job_queue import JobQueue
import job_runtime


@contextmanager
def file_lock(path, blocking=True):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


class JobDispatch:
    def __init__(self, jobs, max_running):
        self.jobs = jobs
        self.max_running = max_running
        self.queue = JobQueue(os.path.join(jobs.dir, ".queue.sqlite3"))
        self.intake_path = os.path.join(jobs.dir, ".intake.lock")
        self.dispatch_path = os.path.join(jobs.dir, ".dispatcher.lock")
        self.guard = threading.Lock()
        self.thread = None

    @staticmethod
    def request_key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("request ID must be 1-128 letters, digits, dots, underscores, colons or hyphens")
        return value

    @staticmethod
    def base_meta(row):
        spec, jid = row["spec"], row["job_id"]
        name = spec["bin"]
        return {"id": jid, "bin": name, "argv": spec.get("argv", []), "label": spec.get("label"),
                "request_id": row["request_id"], "started": row["submitted_at"],
                "state": "running", "phase": "preparing", "execution": {"kind": "supervised-v1"},
                "lock_label": None if name == "devlock" else f"job-{jid}",
                "lock_mode": "management" if name == "devlock" else "devlock_run" if name in {"adb", "fetch_apk"} else "internal_or_helper",
                "recovery": {"status": "not_needed", "reason": "command has not started"}}

    def submit(self, spec, body, request_id):
        key = self.request_key(uuid.uuid4().hex if request_id is None else request_id)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        digest.update(b"\0")
        digest.update(body)
        with file_lock(self.intake_path):
            row, created = self.queue.reserve(key, digest.hexdigest(), spec, spec["resources"])
            if created:
                jid = row["job_id"]
                try:
                    self.jobs._prepare(row, body)
                    ready = self.queue.ready(jid)
                    if ready["state"] == "done":
                        self.finish_unstarted(row, "cancelled", "cancelled before execution")
                except (OSError, ValueError, tarfile.TarError) as error:
                    self.finish_unstarted(row, "failed", f"input preparation failed: {error}")
            jid = row["job_id"]
        self.start()
        return self.jobs.status(jid)

    def finish_unstarted(self, row, outcome, reason):
        jid = row["job_id"]
        os.makedirs(self.jobs.path(jid), exist_ok=True)
        meta = self.base_meta(row)
        meta.update(state="finished", phase="finished", outcome=outcome, exit=None,
                    ended=time.time(), note=reason,
                    recovery={"status": "not_needed", "reason": "command was not started"})
        self.jobs._write_meta(jid, meta)
        self.queue.set_state(jid, "done")

    def start(self):
        with self.guard:
            if self.thread and self.thread.is_alive():
                return
            if not self.queue.rows(("preparing", "queued", "claimed")):
                return
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        try:
            while True:
                with file_lock(self.dispatch_path, blocking=False) as acquired:
                    if acquired:
                        while self.queue.rows(("preparing", "queued", "claimed")):
                            self.tick()
                            self.last_error = None
                            time.sleep(0.2)
                # Release the flock before publishing an idle thread. A submit
                # racing this check either keeps this loop alive or starts a new one.
                with self.guard:
                    if not self.queue.rows(("preparing", "queued", "claimed")):
                        self.thread = None
                        return
                time.sleep(0.2)
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"

    def tick(self):
        with file_lock(self.intake_path, blocking=False) as acquired:
            if acquired:
                for row in self.queue.rows(("preparing",)):
                    self.finish_unstarted(row, "failed", "input preparation was interrupted; submit a new request")
        for row in self.queue.rows(("claimed",)):
            jid = row["job_id"]
            try:
                with self.jobs._read_root.open_file(jid + "/launch.json"):
                    launched = True
            except FileNotFoundError:
                launched = False
            if not launched:
                self.launch(row)
            status = self.jobs.status(jid)
            if status["state"] == "finished":
                blocked = status.get("lost") or status.get("recovery", {}).get("status") == "required"
                self.queue.set_state(jid, "blocked" if blocked else "done")
        while True:
            row = self.queue.claim_next(self.max_running)
            if row is None:
                break
            self.launch(row)

    def launch(self, row):
        jid = row["job_id"]
        try:
            prepared = self.jobs._read_root.read_json(jid + "/prepared.json")
            if not isinstance(prepared.get("command"), list) or not prepared["command"] or not isinstance(prepared.get("cwd"), str):
                raise ValueError("invalid prepared command")
        except (OSError, ValueError, AttributeError) as error:
            self.finish_unstarted(row, "failed", f"prepared input is unavailable: {error}")
            return
        meta = self.jobs._read_meta(jid)
        meta.update(phase="starting", state="running", recovery={"status": "not_reported", "reason": "execution has not reported recovery"})
        meta["execution"].update(launch_deadline=time.time() + 30)
        self.jobs._write_meta(jid, meta)
        env = dict(os.environ, DEVLOCK_LABEL=f"job-{jid}", PF_JOB_OUT=self.jobs.path(jid, "out"),
                   PF_JOB_IN=self.jobs.path(jid, "in"), PF_JOB_EXITF=self.jobs.path(jid, "exitcode"))
        try:
            proc = job_runtime.launch(self.jobs._read_root, jid, self.jobs.path(jid), prepared["command"], prepared["cwd"], env=env)
        except OSError as error:
            self.finish_unstarted(row, "failed", f"worker could not start: {error}")
            return
        threading.Thread(target=proc.wait, daemon=True).start()

    def waiting(self, row, rows=None):
        rows = rows if rows is not None else self.queue.rows(("preparing", "queued", "claimed", "blocked"))
        state = row["state"]
        if state in ("preparing", "blocked"):
            return {"kind": "preparing" if state == "preparing" else "recovery", "blocking_jobs": []}
        if state != "queued":
            return {"kind": None, "blocking_jobs": []}
        owners = [r for r in rows if r["state"] in ("claimed", "blocked")]
        collisions = [r["job_id"] for r in owners if set(row["resources"]) & set(r["resources"])]
        if collisions:
            return {"kind": "resource", "blocking_jobs": collisions}
        if len(owners) >= self.max_running:
            return {"kind": "capacity", "blocking_jobs": [r["job_id"] for r in owners]}
        return {"kind": None, "blocking_jobs": []}

    def attach(self, meta, row):
        if row:
            meta["request_id"] = row["request_id"]
            meta["queue"] = {"state": row["state"], "submitted_at": row["submitted_at"],
                             "resources": row["resources"], "waiting": self.waiting(row)}
            try:
                meta["queue"]["release"] = self.jobs._read_root.read_json(row["job_id"] + "/queue-release.json")
            except FileNotFoundError:
                pass
        return meta

    def snapshot(self):
        rows = self.queue.rows(("preparing", "queued", "claimed", "blocked"))
        items = []
        for row in rows:
            state = row["state"]
            phase = self.jobs.status(row["job_id"])["phase"] if state == "claimed" else state
            items.append({"job_id": row["job_id"], "request_id": row["request_id"], "phase": phase,
                          "submitted_at": row["submitted_at"], "resources": row["resources"],
                          "waiting": self.waiting(row, rows)})
        return {"schema_version": 1, "max_running": self.max_running,
                "active_count": sum(r["state"] == "claimed" for r in rows),
                "blocked_count": sum(r["state"] == "blocked" for r in rows),
                "queued_count": sum(r["state"] in ("preparing", "queued") for r in rows),
                "dispatcher_error": getattr(self, "last_error", None), "items": items}
