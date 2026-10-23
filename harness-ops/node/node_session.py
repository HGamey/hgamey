"""Host-only gateway to phonefarm's single session publisher and status reader."""
import json
import os
import re
import subprocess

from artifact_io import relative_path
from job_queue import RequestConflict

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


def recover_session(jobs, jid, request_id):
    """Queue one recovery of the recorded context; never run device IO in HTTP."""
    key = jobs.dispatch.request_key(request_id)
    raw = jobs._read_meta(jid)
    if raw.get("bin") != "phonefarm":
        raise SessionError(404, "not_found", "this job has no phonefarm control session")
    bound = descriptor(jobs._read_root.read_json(jid + "/out/session-control.json", maximum=MAX_REQUEST), jid)
    spec = {"bin": "phonefarm", "argv": ["gray-layer", "session-recover", "--session",
            jobs.path(jid, "out", bound["session_path"]), "--serial", bound["serial"], "--json"],
            "label": "Recover session for " + jid, "execution_policy": "process_group",
            "device_access": "recovery", "recovery_for": jid}
    # Reuse the frozen request even when its recovery has since closed the source.
    # start() still verifies the full normalized request fingerprint.
    if jobs.dispatch.queue.by_request(key):
        return jobs.start(spec, b"", request_id=key)
    status = jobs.status(jid)
    row = jobs.dispatch.queue.get(jid)
    if status.get("phase") in {"finalizing", "storage_blocked"}:
        raise RequestConflict("commit the source result first; use retry-finalize if storage is blocked")
    if not row or row["state"] != "blocked" or status.get("state") != "finished":
        raise RequestConflict("session recovery requires a finished job with a blocked reservation")
    execution = status.get("execution") or {}
    if (raw.get("phase") != "finished" or raw.get("lost")
            or execution.get("kind") != "supervised-v1" or execution.get("worker_alive") is not False):
        raise RequestConflict("source worker completion is unconfirmed; reconcile its execution domain first")
    contained = (execution.get("process_management") or {}).get("policy") == "contained"
    scope = "owned_cgroup" if contained else "owned_process_group"
    if (execution.get(scope) or {}).get("status") != "absent":
        raise RequestConflict("source " + scope + " must be confirmed absent before session recovery")
    if contained:
        fd = jobs._read_root.open_fd(jid + "/out", directory=True)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid() or st.st_mode & 0o7777 != 0o700:
                raise RequestConflict("source output handback to the node's private identity is unconfirmed")
        finally:
            os.close(fd)
    current = session_call(jobs, jid)
    if current.get("phase") != "recovery_required" or current.get("live_owner") is not False:
        raise RequestConflict("session must require recovery and have no live owner")
    return jobs.start(spec, b"", request_id=key)


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
    if meta.get("state") == "finished":
        # The worker has sealed and returned final output to the node's private
        # identity before publishing this state. Keep historical queries usable.
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
