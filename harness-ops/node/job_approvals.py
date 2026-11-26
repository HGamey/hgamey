"""Sensitive-action approvals raised by running phonefarm jobs.

phonefarm (src/approval.rs) writes `<job>/out/approvals/<id>.json` before it pays, deletes,
touches a wallet, sends a message or types a password, then waits for `<id>.decision.json`.
This module lists those requests and records a person's decision. It never decides on its own:
an unanswered request times out inside phonefarm and is denied there.
"""

import json
import os
import re
import time

ID = re.compile(r"ap-[A-Za-z0-9_-]{1,120}")
DECISIONS = {"allow", "deny"}


def _dir(jobs, jid):
    return jobs.path(jid, "out", "approvals")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def list_requests(jobs, jid, pending_only=False):
    root = _dir(jobs, jid)
    items = []
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    for name in names:
        if not name.endswith(".json") or name.endswith(".decision.json") or not ID.fullmatch(name[:-5]):
            continue
        request = _load(os.path.join(root, name))
        if request is None or request.get("schema") != "phonefarm/approval@1":
            continue
        decision = _load(os.path.join(root, name[:-5] + ".decision.json"))
        if decision and request.get("state") == "pending":
            request["state"] = "allowed" if decision.get("decision") == "allow" else "denied"
            request["decided_by"] = decision.get("by")
        if request.get("state") == "pending" and time.time() > float(request.get("expires_at") or 0) + 5:
            request["state"] = "expired"
        shot = request.pop("screenshot", None)
        request["has_screenshot"] = bool(shot) and os.path.isfile(os.path.join(root, request["id"] + ".jpg"))
        if not pending_only or request["state"] == "pending":
            items.append(request)
    return items


def screenshot_path(jobs, jid, approval_id):
    if not ID.fullmatch(approval_id or ""):
        raise KeyError(approval_id)
    path = os.path.join(_dir(jobs, jid), approval_id + ".jpg")
    if not os.path.isfile(path):
        raise KeyError(approval_id)
    return path


def decide(jobs, jid, approval_id, body, who):
    """Record allow/deny. Returns (http_code, payload). First decision wins."""
    if not ID.fullmatch(approval_id or ""):
        return 404, {"code": "approval_not_found", "error": "no such approval"}
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return 400, {"code": "invalid_request", "error": "body must be JSON"}
    decision = str((data or {}).get("decision", "")).strip().lower()
    if decision not in DECISIONS:
        return 400, {"code": "invalid_request", "error": "decision must be allow or deny"}
    root = _dir(jobs, jid)
    request = _load(os.path.join(root, approval_id + ".json"))
    if request is None:
        return 404, {"code": "approval_not_found", "error": "no such approval"}
    # 只接受仍在等待、还没过期的请求: 进程已经超时走人的请求不能再报「已允许」
    if request.get("state") != "pending" or time.time() > float(request.get("expires_at") or 0):
        return 409, {"code": "not_pending", "error": "this approval is no longer waiting for a decision",
                     "state": request.get("state")}
    target = os.path.join(root, approval_id + ".decision.json")
    note = str((data or {}).get("note", ""))[:500]
    record = {"decision": decision, "by": who, "note": note, "at": time.time()}
    # 先写临时文件再 link 过去: 读方永远看不到写了一半的决定; 目标已存在时 link 失败 = 先写者胜
    tmp = f"{target}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    try:
        os.link(tmp, target)
    except FileExistsError:
        return 409, {"code": "already_decided", "error": "this approval already has a decision",
                     "decision": _load(target)}
    finally:
        os.unlink(tmp)
    return 200, {"id": approval_id, **record}


JOB_ID = re.compile(r"[0-9a-f]{12}")


def pending_all(jobs, limit=50):
    """Pending approvals across this node's jobs, newest first (dashboard / Telegram bridge)."""
    items = []
    try:
        names = os.listdir(jobs.dir)
    except FileNotFoundError:
        return []
    for jid in names:
        if not JOB_ID.fullmatch(jid) or not os.path.isdir(os.path.join(jobs.dir, jid, "out", "approvals")):
            continue
        for item in list_requests(jobs, jid, pending_only=True):
            item["job_id"] = jid
            items.append(item)
    items.sort(key=lambda r: -float(r.get("created_at") or 0))
    return items[:limit]
