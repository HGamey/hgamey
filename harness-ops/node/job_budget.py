"""Optional execution deadline, owned by the existing independent worker."""
import os
import time


def normalize(value):
    if not isinstance(value, dict) or set(value) != {"wall_seconds", "timeout_action"}:
        raise ValueError("time_budget requires only wall_seconds and timeout_action")
    seconds = value["wall_seconds"]
    if type(seconds) is not int or not 1 <= seconds <= 604800:
        raise ValueError("time_budget.wall_seconds must be an integer from 1 to 604800")
    if value["timeout_action"] not in ("graceful", "escalate"):
        raise ValueError("time_budget.timeout_action must be graceful or escalate")
    return dict(value)


def initial(value):
    return {"schema": "phonefarm/job-time-budget@1", **normalize(value), "status": "not_started",
            "started_at": None, "deadline_at": None, "observed_at": None,
            "elapsed_seconds": None, "exceeded_at": None, "finished_at": None}


class RunBudget:
    def __init__(self, store, jid, meta, spec):
        self.store, self.jid, self.meta = store, jid, meta
        self.record = initial(spec["time_budget"]) if "time_budget" in spec else None
        self.started = None
        if self.record is not None:
            meta["execution"]["time_budget"] = self.record

    def start(self):
        if self.record is None:
            return
        self.started = time.monotonic()
        now = time.time()
        self.record.update(status="running", started_at=now, observed_at=now,
                           deadline_at=now + self.record["wall_seconds"], elapsed_seconds=0.0)

    def check(self, request_cancel):
        if self.started is None or self.record["exceeded_at"] is not None:
            return
        elapsed = time.monotonic() - self.started
        if elapsed < self.record["wall_seconds"]:
            return
        now = time.time()
        self.record.update(status="exceeded", exceeded_at=now, observed_at=now, elapsed_seconds=elapsed)
        # The current owner latches this cause before attempting persistence.
        # Failure to save must not discard its intent or release supervision.
        request_cancel(self.store, self.jid, force=self.record["timeout_action"] == "escalate",
                       source="execution_deadline")

    def finish(self):
        if self.started is None:
            return
        now = time.time()
        self.record.update(status="exceeded" if self.record["exceeded_at"] is not None else "finished",
                           finished_at=now, observed_at=now, elapsed_seconds=time.monotonic() - self.started)


def normalize_disk_guard(value):
    if not isinstance(value, dict) or set(value) != {"max_log_bytes", "min_free_bytes", "stop_action"}:
        raise ValueError("disk_guard requires only max_log_bytes, min_free_bytes and stop_action")
    for key in ("max_log_bytes", "min_free_bytes"):
        if type(value[key]) is not int or not 1 <= value[key] <= 9007199254740991:
            raise ValueError("disk_guard." + key + " must be an integer from 1 to 9007199254740991")
    if value["stop_action"] not in ("graceful", "escalate"):
        raise ValueError("disk_guard.stop_action must be graceful or escalate")
    return dict(value)


def initial_disk_guard(value):
    return {"schema": "phonefarm/job-disk-guard@1", **normalize_disk_guard(value),
            "enforcement": "observed_stop_request", "status": "not_started",
            "started_at": None, "observed_at": None, "finished_at": None,
            "triggered_at": None, "trigger_reason": None, "observation": None}


class RunDiskGuard:
    """Small observations through pinned descriptors; never a filesystem quota."""
    def __init__(self, store, jid, meta, spec):
        self.store, self.jid = store, jid
        self.record = initial_disk_guard(spec["disk_guard"]) if "disk_guard" in spec else None
        self.fds, self.filesystems = {}, []
        self.next_sample = 0.0
        if self.record is not None:
            meta["execution"]["disk_guard"] = self.record

    def _unknown(self, error, observation=None):
        value = observation or {"log_bytes": None, "filesystems": []}
        value.update(status="unknown", error={"code": "disk_guard_observation_failed",
                     "message": (type(error).__name__ + ": " + str(error))[:512]})
        self.record.update(observed_at=time.time(), observation=value)
        return "observation_failed"

    def _observe(self):
        observation = {"status": "known", "log_bytes": None, "filesystems": [], "error": None}
        try:
            observation["log_bytes"] = os.fstat(self.fds["log"]).st_size
            for device, roles, fd in self.filesystems:
                item = {"device": device, "roles": roles, "available_bytes": None}
                observation["filesystems"].append(item)
                space = os.fstatvfs(fd)
                item["available_bytes"] = space.f_bavail * space.f_frsize
        except OSError as error:
            return self._unknown(error, observation)
        self.record.update(observed_at=time.time(), observation=observation)
        if observation["log_bytes"] >= self.record["max_log_bytes"]:
            return "log_limit"
        if any(f["available_bytes"] < self.record["min_free_bytes"] for f in observation["filesystems"]):
            return "disk_space_low"
        return None

    def prepare(self, log_fd):
        if self.record is None:
            return
        try:
            self.fds["log"] = os.dup(log_fd)  # Non-inheritable; producer uses the original FD.
            self.fds["job"] = self.store.open_fd(self.jid, directory=True)
            self.fds["out"] = self.store.open_fd(self.jid + "/out", directory=True)
            by_device = {}
            for role, fd in self.fds.items():
                device = os.fstat(fd).st_dev
                by_device.setdefault(device, ([], fd))[0].append(role)
            self.filesystems = [(device, roles, fd) for device, (roles, fd) in by_device.items()]
            reason = self._observe()
        except (OSError, ValueError) as error:
            reason = self._unknown(error)
        self.next_sample = time.monotonic() + 1.0
        if reason is not None:
            self.record.update(status="rejected", trigger_reason=reason, finished_at=time.time())
            raise ValueError("disk guard rejected command before start: " + reason)

    def start(self):
        if self.record is not None:
            self.record.update(status="running", started_at=time.time())

    def check(self, request_cancel):
        if (self.record is None or self.record["status"] != "running"
                or time.monotonic() < self.next_sample):
            return
        self.next_sample = time.monotonic() + 1.0
        reason = self._observe()
        if reason is not None:
            self.record.update(status="triggered", triggered_at=time.time(), trigger_reason=reason)
            source = {"log_limit": "disk_log_limit", "disk_space_low": "disk_space_low",
                      "observation_failed": "disk_observation_failed"}[reason]
            request_cancel(self.store, self.jid, force=self.record["stop_action"] == "escalate", source=source)

    def finish(self):
        if self.record is None or self.record["started_at"] is None:
            return
        self._observe()  # End observation never requests a late stop.
        self.record.update(finished_at=time.time())
        if self.record["status"] == "running":
            self.record["status"] = "finished"

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
