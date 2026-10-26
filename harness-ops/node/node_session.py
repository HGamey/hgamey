"""Host-only gateway to phonefarm's single session publisher and status reader."""
import json
import os
import re
import subprocess

from artifact_io import relative_path
from device_scope import binding, lease_problem
from runtime_identity import is_gray_session

MAX_REQUEST = 64 << 10
HELPER_TIMEOUT = 5
CAPABILITY = "jobs.session-control-v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class SessionError(Exception):
    def __init__(self, status, code, error, **extra):
        super().__init__(error)
        self.status = status
        self.payload = {"schema": "phonefarm/gray-control-error@1", "code": code, "error": error, **extra}


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid session or action request ID")
    return value


def output_path(value):
    relative_path(value)
    if not value or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("session paths must be nonempty relative output paths")
    return value


def descriptor(value, jid):
    if (not isinstance(value, dict) or value.get("schema") != "phonefarm/gray-control@1"
            or value.get("producer") != "phonefarm" or value.get("job_id") != jid):
        raise ValueError("session descriptor producer/job identity mismatch")
    identifier(value.get("session_id"))
    serial = value.get("serial")
    if not isinstance(serial, str) or not serial or len(serial) > 256 or any(ord(c) < 32 for c in serial):
        raise ValueError("invalid session serial")
    path = output_path(value.get("session_path"))
    recovery = value.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("session_path") != path:
        raise ValueError("session recovery identity mismatch")
    reports = recovery.get("reports", [])
    if not isinstance(reports, list):
        raise ValueError("invalid session recovery references")
    for report in reports:
        output_path(report)
    image = value.get("latest_image")
    if image is not None:
        if not isinstance(image, dict):
            raise ValueError("invalid session image")
        output_path(image.get("path"))
    return value


def _recovery_spec(jobs, source_id, bound, target_id):
    # Keep the first-attempt spec byte-for-byte compatible after normalization.
    source = jobs.dispatch.queue.get(source_id)
    original = _source_binding(source, bound)
    return {"bin": "phonefarm", "argv": ["gray-layer", "session-recover", "--session",
            jobs.path(source_id, "out", bound["session_path"]), "--serial", bound["serial"], "--json"],
            "label": "Recover session for " + target_id, "execution_policy": "process_group",
            "device_access": "recovery", "recovery_for": target_id,
            **({"device_binding": original} if original is not None else {})}


def _source_binding(source, bound):
    original = source["spec"].get("device_binding")
    if "device_binding" not in source["spec"]:
        if "device_binding" in bound or bound.get("lease_scope") == "device":
            raise SessionError(409, "session_recovery_binding", "source command did not record this device scope")
        return None
    original = binding(original)
    if (bound.get("device_binding") != original or bound.get("lease_scope") != "device"
            or bound.get("serial") != original["serial"]):
        raise SessionError(409, "session_recovery_binding", "source session scope differs from its frozen command")
    return original


def validate_recovery_spec(jobs, spec):
    """A public spec cannot borrow a different device with a recovery flag."""
    source, bound = _bound_recovery(jobs, spec.get("recovery_for"))
    expected = {**_recovery_spec(jobs, source["job_id"], bound, spec["recovery_for"]),
                "resources": source["resources"]}
    if spec != expected:
        raise SessionError(409, "session_recovery_binding", "bound recovery must inherit the exact original session and resources")


def _check_attempt(jobs, source, bound, child):
    expected = {**_recovery_spec(jobs, source["job_id"], bound, child["spec"]["recovery_for"]),
                "resources": source["resources"]}
    if child["spec"] != expected or child["resources"] != source["resources"]:
        raise SessionError(409, "session_recovery_binding", "recovery command does not match its original session binding")


def _bound_recovery(jobs, jid, rows=None):
    """Resolve immutable command ancestry, independent of its current eligibility."""
    jobs.path(jid)
    rows = jobs.dispatch.queue.rows() if rows is None else rows
    indexed = {row["job_id"]: row for row in rows}
    chain, seen, current = [], set(), jid
    while True:
        if current in seen:
            raise SessionError(409, "session_recovery_binding", "recovery ancestry contains a cycle")
        seen.add(current)
        row = indexed.get(current)
        if row is None:
            raise SessionError(404, "not_found", "session recovery requires a recorded queue job")
        chain.append(row)
        parent = row["spec"].get("recovery_for")
        if not parent:
            break
        current = parent
    source = chain[-1]
    spec = source["spec"]
    raw = jobs._read_meta(current)
    if (not is_gray_session(spec)
            or raw.get("bin") != "phonefarm" or raw.get("argv") != spec["argv"]):
        raise SessionError(404, "not_found", "source is not a recorded phonefarm gray control session")
    bound = descriptor(jobs._read_root.read_json(current + "/out/session-control.json", maximum=MAX_REQUEST), current)
    _source_binding(source, bound)
    for child in reversed(chain[:-1]):
        _check_attempt(jobs, source, bound, child)
    return source, bound


def recovery_source_out(jobs, row):
    """Bind only the gray typed path; other producers retain their existing source."""
    spec = row["spec"]
    parent = spec.get("recovery_for")
    if not parent:
        return None
    if spec.get("bin") == "phonefarm" and spec.get("argv", [])[:2] == ["gray-layer", "session-recover"]:
        source, bound = _bound_recovery(jobs, row["job_id"])
        # The opened directory verifies components without changing ownership.
        os.close(jobs._read_root.open_fd(source["job_id"] + "/out/" + bound["session_path"], directory=True))
        return jobs.path(source["job_id"], "out")
    return jobs.path(parent, "out")


def _recovery_qualification(jobs, row):
    jid = row["job_id"]
    status = jobs.status(jid)
    if status.get("phase") in {"finalizing", "storage_blocked"}:
        return "commit the source result first; use retry-finalize if storage is blocked"
    if row["state"] != "blocked" or status.get("state") != "finished":
        return "session recovery requires a finished job with a blocked reservation"
    return jobs.host_execution_problem(jid, status)


def recovery_plan(jobs, jid):
    """Read the unique recovery leaf; no job creation, device IO or state repair."""
    rows = jobs.dispatch.queue.rows()
    source, bound = _bound_recovery(jobs, jid, rows)
    source_id = source["job_id"]
    plan = {"schema_version": 1, "node_id": jobs.node_id, "job_id": jid,
            "supported": "phonefarm" in jobs.bins, "eligible": False, "reason": None,
            "source_job_id": source_id, "session_id": bound["session_id"], "serial": bound["serial"],
            "session_path": bound["session_path"], "target_job_id": source_id, "attempts": []}
    if "device_binding" in source["spec"]:
        plan.update(device_binding=source["spec"]["device_binding"], lease_scope="device")
    current, visited = source, {source_id}
    while True:
        children = [row for row in rows if row["spec"].get("recovery_for") == current["job_id"]
                    and (row.get("state_reason") or {}).get("kind") != "cancelled_before_launch"]
        for child in children:
            plan["attempts"].append({"job_id": child["job_id"], "recovery_for": current["job_id"],
                                     "request_id": child["request_id"], "queue_state": child["state"]})
        if not children:
            break
        if len(children) != 1 or children[0]["job_id"] in visited:
            plan["reason"] = "recovery ancestry is ambiguous; inspect existing attempts without choosing by timestamp"
            return plan
        current = children[0]
        visited.add(current["job_id"])
        plan["target_job_id"] = current["job_id"]
        try:
            _check_attempt(jobs, source, bound, current)
        except (OSError, ValueError, KeyError, SessionError):
            plan["reason"] = "an existing recovery attempt does not match the original session binding"
            return plan
        if current["state"] != "blocked":
            plan["reason"] = ("the recovery attempt is settled; inspect its receipt and explicitly release source reservations"
                              if current["state"] == "done" else "an existing recovery attempt is active; inspect its original job")
            return plan
    if not plan["supported"]:
        plan["reason"] = "phonefarm helper is not configured"
        return plan
    # Original contained ownership and the leaf's own host scope both matter.
    for row in (source, current) if current != source else (source,):
        reason = _recovery_qualification(jobs, row)
        if reason:
            plan["reason"] = reason
            return plan
    try:
        fresh = session_call(jobs, source_id)
        if any(fresh.get(k) != bound.get(k) for k in ("session_id", "serial", "session_path")):
            plan["reason"] = "original session identity changed; inspect its evidence"
        elif fresh.get("phase") != "recovery_required" or fresh.get("live_owner") is not False:
            plan["reason"] = ("original session is closed; review recovery receipts and explicitly release attempts from leaf to source"
                              if fresh.get("phase") == "closed" else "session must require recovery and have no live owner")
        else:
            problem = (lease_problem({**source["spec"], "device_access": "recovery"},
                                     jobs.leases.snapshot(refresh=True), "recovery")
                       if "device_binding" in source["spec"] else None)
            plan["reason"] = problem
            plan["eligible"] = problem is None
    except (OSError, ValueError, KeyError, SessionError) as error:
        plan["reason"] = "original session observation is unavailable: " + str(error)
    return plan


def recover_session(jobs, jid, request_id):
    """Queue one attempt with a frozen source and an atomically qualified leaf."""
    key = jobs.dispatch.request_key(request_id)
    source, bound = _bound_recovery(jobs, jid)
    spec = _recovery_spec(jobs, source["job_id"], bound, jid)

    def admit():
        plan = recovery_plan(jobs, jid)
        if plan["target_job_id"] != jid:
            raise SessionError(409, "session_recovery_leaf", "recover the current bound leaf, not its ancestor",
                               target_job_id=plan["target_job_id"], source_job_id=source["job_id"])
        if not plan["eligible"]:
            raise SessionError(409, "session_recovery_blocked", plan["reason"], target_job_id=jid,
                               source_job_id=source["job_id"])
        if (plan["source_job_id"], plan["session_id"], plan["serial"], plan["session_path"]) != (
                source["job_id"], bound["session_id"], bound["serial"], bound["session_path"]):
            raise SessionError(409, "session_recovery_binding", "original session binding changed during admission")

    # submit() checks the original ID first, so closed sessions still replay the
    # identical old spec; new IDs recheck the current chain under the intake lock.
    return jobs.start(spec, b"", request_id=key, admit=admit)


def session_call(jobs, jid, *, request_id=None, action=None):
    """Never publish mail or access ADB here. Only the configured PF helper does host IO."""
    meta = jobs._read_meta(jid)
    if meta.get("bin") != "phonefarm":
        raise SessionError(404, "not_found", "this job has no phonefarm control session")
    if "phonefarm" not in jobs.bins:
        raise SessionError(503, "io_error", "phonefarm helper is not configured")
    if request_id is not None:
        identifier(request_id)
    raw = jobs._read_root.read_json(jid + "/out/session-control.json", maximum=MAX_REQUEST)
    bound = descriptor(raw, jid)
    # Pin/check all producer-controlled directory components through SafeRoot.
    for rel in ("", "gray-control", bound["session_path"]):
        os.close(jobs._read_root.open_fd(jid + "/out" + ("/" + rel if rel else ""), directory=True))
    payload = None
    if action is not None:
        if (not isinstance(action, dict) or action.get("schema") != "phonefarm/gray-control-action@1"
                or action.get("session_id") != bound["session_id"]):
            raise ValueError("action must identify this control session")
        request_id = identifier(action.get("request_id"))
        payload = json.dumps(action, ensure_ascii=False).encode()
        if len(payload) > MAX_REQUEST:
            raise SessionError(413, "invalid_request", "session action exceeds 64 KiB")
    exe, cwd = jobs.bins["phonefarm"]
    command = [exe, "gray-layer", "session-action" if action is not None else "session-status",
               "--out", jobs.path(jid, "out"), "--json"]
    if action is not None:
        command += ["--request", "-"]
    elif request_id is not None:
        command += ["--request-id", request_id]
    fixed_env = getattr(jobs, "pf_env", None)
    env = dict(fixed_env) if fixed_env is not None else {
        k: v for k, v in os.environ.items() if not k.startswith("PF_JOB_") and k != "DEVLOCK_LABEL"}
    if action is None or meta.get("state") == "finished":
        # Status only reads existing records/owner locks. Output can become
        # node-private before terminal metadata is published; ordinary Unix
        # access works across that handback without applying a stale write policy.
        env.pop("PHONEFARM_SHARED_GID", None)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, input=payload, capture_output=True,
                                timeout=HELPER_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise SessionError(504, "io_error", "session helper timed out; query the same request ID before taking another action",
                           request_id=request_id) from None
    except OSError as error:
        raise SessionError(503, "io_error", f"session helper unavailable: {error}") from None
    if len(result.stdout) > (1 << 20):
        raise SessionError(502, "io_error", "session helper response exceeds its limit")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise SessionError(502, "io_error", "session helper returned invalid JSON") from None
    if not isinstance(value, dict):
        raise SessionError(502, "io_error", "session helper returned an invalid object")
    if value.get("schema") == "phonefarm/gray-control-error@1":
        code = value.get("code", "io_error")
        status = {"invalid_request": 400, "conflict": 409, "not_found": 404,
                  "owner_unavailable": 409, "session_closed": 409, "io_error": 500}.get(code, 502)
        raise SessionError(status, code, str(value.get("error", "session helper failed")), request_id=request_id)
    if result.returncode != 0:
        raise SessionError(502, "io_error", "session helper exited without a valid result")
    if request_id is None:
        descriptor(value, jid)
        if any(value.get(k) != bound.get(k) for k in ("session_id", "serial", "session_path")):
            raise SessionError(409, "conflict", "session identity changed; refresh before continuing")
        if not isinstance(value.get("live_owner"), bool):
            raise SessionError(502, "io_error", "session helper did not report live owner status")
    elif (value.get("schema") != "phonefarm/gray-control-action-result@1"
          or value.get("session_id") != bound["session_id"] or value.get("request_id") != request_id
          or value.get("status") not in ("accepted", "started", "completed", "failed", "unknown", "not_executed")):
        raise SessionError(502, "io_error", "session action receipt identity or state is invalid")
    return value
