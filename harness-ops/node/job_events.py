"""Committed job-state projection and readonly, node-scoped event pages."""
import copy
import json
import re

from job_capacity import job_facts


OBSERVATION_FIELDS = (
    "phase", "outcome", "exit", "lost", "recovery_status", "cancel_mode",
    "cancel_status", "cancel_signals", "time_budget_status", "time_budget_exceeded_at",
    "owned_scope", "owned_status", "reconciliation_state",
)
DISK_FIELDS = ("disk_guard_status", "disk_guard_trigger_reason", "disk_guard_triggered_at")


def _object(value):
    return value if isinstance(value, dict) else {}


def projection(row, observation=None):
    """Keep SQL facts current without inventing a new worker observation."""
    prior = _object(row.get("status_projection"))
    # Optional extension: observing an old job must not manufacture new null
    # keys and publish an event merely because the service was upgraded.
    disk = {key: copy.deepcopy(prior[key]) for key in DISK_FIELDS if key in prior}
    worker = {key: copy.deepcopy(prior.get(key)) for key in OBSERVATION_FIELDS}
    if worker["cancel_signals"] is None:
        worker["cancel_signals"] = []
    if observation is not None:
        execution = _object(observation.get("execution"))
        cancel = _object(observation.get("cancel"))
        budget = _object(execution.get("time_budget"))
        if "disk_guard" in execution:
            guard = _object(execution["disk_guard"])
            disk = {"disk_guard_status": guard.get("status"),
                    "disk_guard_trigger_reason": guard.get("trigger_reason"),
                    "disk_guard_triggered_at": guard.get("triggered_at")}
        group = _object(execution.get("owned_cgroup")) or _object(execution.get("owned_process_group"))
        worker = {
            "phase": observation.get("phase"), "outcome": observation.get("outcome"),
            "exit": observation.get("exit"), "lost": observation.get("lost"),
            "recovery_status": _object(observation.get("recovery")).get("status"),
            "cancel_mode": cancel.get("mode"), "cancel_status": cancel.get("status"),
            "cancel_signals": [s["name"] for s in (cancel.get("signals") or [])
                               if isinstance(s, dict) and isinstance(s.get("name"), str)],
            "time_budget_status": budget.get("status"),
            "time_budget_exceeded_at": budget.get("exceeded_at"),
            "owned_scope": group.get("scope"), "owned_status": group.get("status"),
            "reconciliation_state": _object(execution.get("reconciliation")).get("state"),
        }
    reason = _object(row.get("state_reason"))
    cancelled = reason.get("kind") == "cancelled_before_launch"
    unstarted = _object(reason.get("unstarted"))
    if cancelled or unstarted:
        worker = dict.fromkeys(OBSERVATION_FIELDS)
        worker.update(phase="finished", outcome="cancelled" if cancelled else unstarted.get("outcome"),
                      lost=False, recovery_status="not_needed", cancel_signals=[],
                      owned_scope="original_process_group", owned_status="not_started")
        if cancelled:
            worker.update(cancel_mode="graceful", cancel_status="not_applied")
    storage = reason if reason.get("kind") == "durability" else {}
    return {"queue_state": row["state"], "storage_kind": storage.get("kind"),
            "storage_stage": storage.get("stage"),
            "host_capacity_state": job_facts(row)["state"], **worker, **disk}


def public_fields(row):
    return {"status_revision": row.get("status_revision", 0) if row is not None else None,
            "status_revision_scope": "committed_job_state",
            "status_committed_at": row.get("status_committed_at") if row is not None else None,
            "committed_status": copy.deepcopy(row.get("status_projection")) if row is not None else None}


class JobEventError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.payload = {"code": code, "error": message}


class NodeJobEvents:
    def __init__(self, queue, node_id):
        self.queue, self.node_id = queue, node_id

    def events(self, query):
        # Queue publication imports projection; do not import NodeRead at module load.
        from node_read import decode_cursor, encode_cursor

        if (set(query) - {"job_id", "cursor", "after", "limit", "expected_node_id"}
                or any(len(values) != 1 for values in query.values())):
            raise ValueError("unsupported or repeated job event query")
        after, limit = int(query.get("after", [0])[0]), int(query.get("limit", [100])[0])
        if not 0 <= after <= (1 << 63) - 1 or not 1 <= limit <= 200:
            raise ValueError("invalid job event sequence or page limit")
        jid = query.get("job_id", [None])[0]
        cursor = None
        if "cursor" in query:
            if "after" in query:
                raise ValueError("cursor and after are mutually exclusive")
            cursor = decode_cursor(query["cursor"][0])
            if (not isinstance(cursor, dict)
                    or set(cursor) != {"v", "node_id", "stream_id", "job_id", "after", "event_id"}
                    or type(cursor["v"]) is not int or cursor["v"] != 1
                    or type(cursor["after"]) is not int or not 0 <= cursor["after"] <= (1 << 63) - 1
                    or not isinstance(cursor["node_id"], str) or not isinstance(cursor["stream_id"], str)
                    or cursor["event_id"] is not None and not isinstance(cursor["event_id"], str)):
                raise ValueError("invalid job event cursor")
            if "job_id" in query and jid != cursor["job_id"]:
                raise JobEventError(409, "job_cursor_scope_changed", "cursor job scope differs; preserve the original cursor")
            jid, after = cursor["job_id"], cursor["after"]
        if jid is not None and (not isinstance(jid, str) or not re.fullmatch(r"[0-9a-f]{12}", jid)):
            raise ValueError("job_id must be a 12-character job identity")

        with self.queue._transaction() as db:
            stream = db.execute("SELECT stream_id FROM job_event_stream WHERE id=1").fetchone()[0]
            if cursor is not None and (cursor["node_id"] != self.node_id or cursor["stream_id"] != stream):
                raise JobEventError(409, "job_cursor_stream_changed", "cursor belongs to another node or stream; preserve the original cursor")
            if jid is not None and db.execute("SELECT 1 FROM jobs WHERE job_id=?", (jid,)).fetchone() is None:
                raise JobEventError(404, "job_not_found", "job has no registered event history")
            head = db.execute("SELECT COALESCE(MAX(sequence),0) FROM job_events").fetchone()[0]
            if after > head:
                raise JobEventError(409, "job_cursor_ahead", "event position is beyond this stream; preserve the original cursor")
            anchor = db.execute("SELECT event_id,job_id FROM job_events WHERE sequence=?", (after,)).fetchone() if after else None
            if (after and (anchor is None or jid is not None and anchor["job_id"] != jid)
                    or cursor is not None and cursor["event_id"] != (anchor["event_id"] if anchor else None)):
                raise JobEventError(409, "job_cursor_history_changed", "original event position no longer matches; preserve the original cursor")
            clause, params = "sequence>?", [after]
            if jid is not None:
                clause += " AND job_id=?"
                params.append(jid)
            rows = db.execute("SELECT * FROM job_events WHERE " + clause + " ORDER BY sequence LIMIT ?",
                              (*params, limit + 1)).fetchall()
            items = [{**{key: row[key] for key in ("sequence", "event_id", "job_id", "revision", "kind", "at")},
                      "committed_status": json.loads(row["committed_status"])} for row in rows[:limit]]
            position = items[-1]["sequence"] if items else after
            event_id = items[-1]["event_id"] if items else anchor["event_id"] if anchor else None
        next_cursor = encode_cursor({"v": 1, "node_id": self.node_id, "stream_id": stream,
                                     "job_id": jid, "after": position, "event_id": event_id})
        return {"schema_version": 1, "node_id": self.node_id, "stream_id": stream,
                "job_id": jid, "items": items, "head": head, "next_after": position,
                "next_cursor": next_cursor, "has_more": len(rows) > limit}
