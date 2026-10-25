"""Optional execution deadline, owned by the existing independent worker."""
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
