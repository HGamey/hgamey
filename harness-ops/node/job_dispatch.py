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

from job_queue import JobQueue, RequestConflict, recovery_reservations, reservation_conflicts, global_waiter
from device_scope import scope, lease_problem
from job_policy import require_policy, request_facts
from node_lease import device_access, prepare_receipt_directory
from node_session import recovery_source_out, SessionError
import job_runtime
import job_budget


def request_fingerprint(spec, body):
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


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
                **request_facts(spec),
                "request_id": row["request_id"], "started": row["submitted_at"],
                "device_access": device_access(spec), "recovery_for": spec.get("recovery_for"),
                **({"device_binding": spec["device_binding"]} if "device_binding" in spec else {}),
                **({"time_budget": dict(spec["time_budget"])} if "time_budget" in spec else {}),
                "state": "running", "phase": "preparing", "execution": {"kind": "supervised-v1",
                    **({"time_budget": job_budget.initial(spec["time_budget"])} if "time_budget" in spec else {})},
                "lock_label": None if name == "devlock" else f"job-{jid}",
                "lock_mode": "management" if name == "devlock" else "devlock_run" if name in {"adb", "fetch_apk"} else "internal_or_helper",
                "recovery": {"status": "not_needed", "reason": "command has not started"}}

    def submit(self, spec, body, request_id, *, admit=None):
        key = self.request_key(uuid.uuid4().hex if request_id is None else request_id)
        fingerprint = request_fingerprint(spec, body)
        with file_lock(self.intake_path):
            if admit is not None and not self.queue.by_request(key):
                admit()
            if spec.get("recovery_for") and not self.queue.by_request(key):
                parent = self.queue.get(spec["recovery_for"])
                if not parent or parent["state"] != "blocked":
                    raise RequestConflict("recovery_for must name a blocked reservation")
                if (parent.get("state_reason") or {}).get("kind") == "durability":
                    raise RequestConflict("retry-finalize the target before submitting a recovery job")
            row, created = self.queue.reserve(key, fingerprint, spec, spec["resources"])
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

    @staticmethod
    def unstarted_meta(row, fact):
        meta = JobDispatch.base_meta(row)
        meta.update(state="finished", phase="finished", outcome=fact["outcome"], exit=None,
                    ended=fact["at"], note=fact["note"],
                    recovery={"status": "not_needed", "reason": "command was not started"})
        meta["execution"].update(worker_alive=False, child_pid=None, returncode=None,
                                 owned_process_group={"scope": "original_process_group", "status": "not_started",
                                                      "pgid": None, "checked_at": None, "error": None})
        meta["controls"] = {"cancel_supported": False, "force_supported": False, "reason": "job is terminal"}
        if fact["outcome"] == "cancelled":
            meta["cancel"] = {"mode": "graceful", "requested_at": fact["at"], "status": "not_applied", "signals": []}
        return meta

    def finish_unstarted(self, row, outcome, reason):
        row = self.queue.record_unstarted(row["job_id"], outcome, reason)
        if row["state"] == "done":
            # Cancellation is already authoritative in SQLite; output is optional.
            try:
                self.jobs._read_root.mkdir(row["job_id"])
                self.jobs._write_meta(row["job_id"], self.unstarted_meta(row, {
                    "outcome": "cancelled", "note": "cancelled before execution",
                    "at": (row.get("state_reason") or {}).get("at", time.time())}))
            except (OSError, ValueError):
                pass
            return
        self.finalize(row)

    def block_storage(self, row, stage, error):
        current = self.queue.get(row["job_id"])
        if current["state"] == "done":
            return
        reason = {"kind": "durability", "stage": stage,
                  "message": f"{type(error).__name__}: {error}", "at": time.time()}
        unstarted = (current.get("state_reason") or {}).get("unstarted")
        if unstarted:
            reason["unstarted"] = unstarted
        # If SQLite itself fails, this raises and retains the previous reservation.
        self.queue.set_state(row["job_id"], "blocked", reason)

    def finalize(self, row):
        """Caller holds intake; commit a stable candidate, never launch/replay it."""
        jid, store = row["job_id"], self.jobs._read_root
        stage = "candidate"
        try:
            fact = (row.get("state_reason") or {}).get("unstarted")
            if fact:
                stage = "unstarted"
                store.mkdir(jid)
            with job_runtime.runner_lock(store, jid) as acquired:
                if not acquired:
                    return False
                if fact:
                    self.jobs._write_meta(jid, self.unstarted_meta(row, fact))
                candidate = self.jobs._execution_status(jid, worker_alive=False)
                if candidate["state"] != "finished":
                    return False
                if candidate.get("lost"):
                    self.jobs._write_meta(jid, candidate)
                stage = "records"
                store.sync(jid + "/meta.json", directory=False)
                if candidate.get("exit") is not None and not candidate.get("lost"):
                    store.sync(jid + "/exitcode", directory=False)
                for name in ("prepared.json", "launch.json", "cancel.json", "cancel.force.json", "queue-release.json"):
                    try:
                        store.sync(jid + "/" + name, directory=False)
                    except FileNotFoundError:
                        pass
                stage = "directories"
                store.sync(jid, directory=True)
                store.sync("", directory=True)
                recovery = candidate.get("recovery", {}).get("status")
                access = device_access(row["spec"])
                # Resource recovery settles a limited producer scope. Keep its
                # reservation for explicit lease review, just like initial writes.
                argv = row["spec"].get("argv", [])
                scoped_recovery = (access == "recovery" and row["spec"].get("bin") == "phonefarm"
                                   and (argv[:1] == ["game-deploy"] or argv[:2] == ["gray-layer", "session-recover"]))
                blocked = (candidate.get("lost") or recovery == "required" or
                           (access == "write" or scoped_recovery)
                           and recovery == "not_reported")
                self.queue.set_state(jid, "blocked" if blocked else "done")
                return True
        except (OSError, ValueError) as error:
            self.block_storage(row, stage, error)
            return False

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
            with file_lock(self.intake_path):
                try:
                    with self.jobs._read_root.open_file(jid + "/launch.json"):
                        launched = True
                except FileNotFoundError:
                    launched = False
                except (OSError, ValueError) as error:
                    self.block_storage(row, "launch-record", error)
                    continue
                if not launched:
                    self._launch(row)
                current = self.queue.get(jid)
                if current["state"] == "claimed":
                    self.finalize(current)
        lease = self.jobs.leases.snapshot(refresh=True) if any(
            device_access(r["spec"]) == "write" or "device_binding" in r["spec"]
            for r in self.queue.rows(("queued",))) else {}
        while True:
            row = self.queue.claim_next(self.max_running, admit=lambda r:
                lease_problem(r["spec"], lease) is None)
            if row is None:
                break
            if self.launch(row) is False:
                break

    def launch(self, row):
        with file_lock(self.intake_path):
            return self._launch(row)

    def _launch(self, row):
        jid = row["job_id"]
        try:
            policy = require_policy(row["spec"])
        except ValueError as error:
            self.finish_unstarted(row, "failed", f"execution policy unavailable: {error}")
            return
        spec = row["spec"]
        observation = (self.jobs.leases.snapshot(refresh=True) if
                       device_access(spec) == "write" or "device_binding" in spec else {})
        if lease_problem(spec, observation) is not None:
            self.queue.set_state(jid, "queued")
            return False
        try:
            prepared = self.jobs._read_root.read_json(jid + "/prepared.json")
            if not isinstance(prepared.get("command"), list) or not prepared["command"] or not isinstance(prepared.get("cwd"), str):
                raise ValueError("invalid prepared command")
            self.jobs.validate_prepared_scope(row, prepared)
            if policy == "process_group":
                shared_gid = (int(self.jobs.pf_env["PHONEFARM_SHARED_GID"])
                              if self.jobs.pf_env is not None else None)
                prepare_receipt_directory(self.jobs._read_root, jid, shared_gid=shared_gid)
            recovery_out = recovery_source_out(self.jobs, row)
        except (OSError, ValueError, AttributeError, KeyError, SessionError) as error:
            self.finish_unstarted(row, "failed", f"prepared input is unavailable: {error}")
            return
        try:
            meta = self.jobs._read_meta(jid)
            meta.update(phase="starting", state="running", recovery={"status": "not_reported", "reason": "execution has not reported recovery"})
            meta["execution"].update(launch_deadline=time.time() + 30)
            self.jobs._write_meta(jid, meta)
        except (OSError, ValueError) as error:
            self.finish_unstarted(row, "failed", f"launch preparation failed: {error}")
            return
        env = dict(self.jobs.pf_env if self.jobs.pf_env is not None else os.environ,
                   DEVLOCK_LABEL=f"job-{jid}", PF_JOB_OUT=self.jobs.path(jid, "out"),
                   PF_JOB_IN=self.jobs.path(jid, "in"), PF_JOB_EXITF=self.jobs.path(jid, "exitcode"),
                   PF_JOB_ID=jid, PF_JOB_LEASE_DIR=self.jobs.path(jid, "out", "lease-receipts"))
        env.pop("PF_RECOVERY_OUT", None)
        if "device_binding" in spec:
            env.pop("DEVLOCK_INSTANCE_ID", None)
        if recovery_out is not None:
            env["PF_RECOVERY_OUT"] = recovery_out
        if "phonefarm" in self.jobs.bins:
            env["PHONEFARM_BIN"] = self.jobs.bins["phonefarm"][0]
        try:
            proc = job_runtime.launch(self.jobs._read_root, jid, self.jobs.path(jid), prepared["command"], prepared["cwd"],
                                      env=env, execution_policy=policy,
                                      **({"time_budget": spec["time_budget"]} if "time_budget" in spec else {}),
                                      service_at_launch=getattr(self.jobs, "service_at_launch", None))
        except OSError as error:
            self.finish_unstarted(row, "failed", f"worker could not start: {error}")
            return
        threading.Thread(target=proc.wait, daemon=True).start()

    def waiting(self, row, rows=None):
        rows = rows if rows is not None else self.queue.rows(("preparing", "queued", "claimed", "blocked"))
        state = row["state"]
        if state in ("preparing", "blocked"):
            return {"kind": "preparing" if state == "preparing" else "durability" if
                    (row.get("state_reason") or {}).get("kind") == "durability" else "recovery", "blocking_jobs": []}
        if state != "queued":
            return {"kind": None, "blocking_jobs": []}
        borrowed = recovery_reservations(row, rows)
        owners = [r for r in rows if r["state"] in ("claimed", "blocked") and r["job_id"] not in borrowed]
        declared = scope(row["spec"])
        resources = set(row["resources"])
        collisions = [r["job_id"] for r in owners if resources.intersection(r["resources"])]
        if collisions:
            return {"kind": "resource", "blocking_jobs": collisions, "scope": declared,
                    "reason": "declared resources overlap an active reservation"}
        scope_collisions = [r["job_id"] for r in owners if reservation_conflicts(row, r)]
        if scope_collisions:
            # A global/device scope barrier is not an overlap in user-named
            # resources. Keep that distinction for legacy queue consumers.
            return {"kind": "device_lease", "blocking_jobs": scope_collisions, "scope": declared,
                    "lease_status": self.jobs.leases.snapshot()["status"],
                    "reason": "device scope overlaps an active reservation"}
        if device_access(row["spec"]) == "write" and declared["scope"] == "device":
            earlier = []
            for prior in rows:
                if prior["job_id"] == row["job_id"]:
                    break
                if prior["state"] == "queued" and global_waiter(prior):
                    earlier.append(prior["job_id"])
            if earlier:
                return {"kind": "device_lease", "blocking_jobs": earlier, "scope": declared,
                        "reason": "an earlier global device job is waiting"}
        lease = self.jobs.leases.snapshot()
        problem = lease_problem(row["spec"], lease)
        if problem:
            return {"kind": "device_lease", "blocking_jobs": [], "lease_status": lease["status"],
                    "scope": declared, "reason": problem}
        running = [r for r in owners if r["state"] == "claimed"]
        if len(running) >= self.max_running:
            return {"kind": "capacity", "blocking_jobs": [r["job_id"] for r in running]}
        return {"kind": None, "blocking_jobs": []}

    def attach(self, meta, row):
        if row:
            meta["request_id"] = row["request_id"]
            meta["device_access"] = device_access(row["spec"])
            meta["recovery_for"] = row["spec"].get("recovery_for")
            if "device_binding" in row["spec"]:
                meta["device_binding"] = row["spec"]["device_binding"]
            meta["queue"] = {"state": row["state"], "submitted_at": row["submitted_at"],
                             "resources": row["resources"], "waiting": self.waiting(row),
                             "device_scope": scope(row["spec"]),
                             "release_eligibility": self.jobs.queue_release_eligibility(row, meta),
                             "state_reason": row.get("state_reason"),
                             "recovery_for": row["spec"].get("recovery_for")}
            if row["state"] == "done":
                try:
                    meta["queue"]["release"] = self.jobs._read_root.read_json(row["job_id"] + "/queue-release.json")
                except (OSError, ValueError):
                    pass
        return meta

    def snapshot(self):
        lease = self.jobs.leases.snapshot(refresh=True)
        rows = self.queue.rows(("preparing", "queued", "claimed", "blocked"))
        items = []
        for row in rows:
            state = row["state"]
            phase = self.jobs.status(row["job_id"])["phase"] if state == "claimed" else state
            items.append({"job_id": row["job_id"], "request_id": row["request_id"], "phase": phase,
                          "recovery_for": row["spec"].get("recovery_for"),
                          "device_scope": scope(row["spec"]),
                          "submitted_at": row["submitted_at"], "resources": row["resources"],
                          "waiting": self.waiting(row, rows), "state_reason": row.get("state_reason")})
        return {"schema_version": 1, "max_running": self.max_running, "lease": lease,
                "active_count": sum(r["state"] == "claimed" for r in rows),
                "blocked_count": sum(r["state"] == "blocked" for r in rows),
                "queued_count": sum(r["state"] in ("preparing", "queued") for r in rows),
                "dispatcher_error": getattr(self, "last_error", None), "items": items}
