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
# approval_sign.Signer when the node signs decisions (set at service start); None = legacy unsigned files
SIGNER = None
DECISIONS = {"allow", "deny"}


def _dir(jobs, jid):
    # 作业目录下、out/ 之外: 产物下载接口不会把审批截图带出去
    return jobs.path(jid, "approvals")


def decision_dir(jobs, jid):
    """Where decisions go. Signed: a center-owned directory beside the jobs root, not inside the job's
    own directory (same OS user, so this is convention; the signature is what jobs cannot forge)."""
    return os.path.join(os.path.dirname(os.path.normpath(jobs.dir)), "_approval_decisions", jid)


def _content(jobs, jid, approval_id):
    """content_hash of the request and screenshot exactly as they are on disk now (what the person sees)."""
    import approval_sign
    root = _dir(jobs, jid)
    with open(os.path.join(root, approval_id + ".json"), "rb") as f:
        req = f.read()
    try:
        with open(os.path.join(root, approval_id + ".jpg"), "rb") as f:
            shot = f.read()
    except FileNotFoundError:
        shot = b""
    return approval_sign.content_hash(req, shot)


def _decision(jobs, jid, approval_id):
    d = _load(os.path.join(decision_dir(jobs, jid), approval_id + ".decision.json"))
    if d and SIGNER is not None:
        # 签名对不上(没签名、伪造, 或请求在决定后被改过)的「决定」不算; phonefarm 那边同样不认
        try:
            # 按此刻磁盘上的请求重算, 不信决定文件里记的哈希
            ok = SIGNER.valid(jid, approval_id, d.get("decision"), _content(jobs, jid, approval_id), d.get("sig"))
        except (OSError, TypeError):
            ok = False
        if not ok:
            return None
    return d


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
        decision = _decision(jobs, jid, name[:-5])
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
    ddir = decision_dir(jobs, jid)
    # 决定不是秘密(有签名): 独立用户跑的作业(contained)要读得到
    os.makedirs(ddir, mode=0o755, exist_ok=True)
    target = os.path.join(ddir, approval_id + ".decision.json")
    note = str((data or {}).get("note", ""))[:500]
    record = {"decision": decision, "by": who, "note": note, "at": time.time()}
    if SIGNER is not None:
        # 签的是此刻磁盘上的请求和截图(人看到的就是它们); 被改过的话 phonefarm 那边对不上, 按拒绝
        content = _content(jobs, jid, approval_id)
        record["content"] = content
        record["sig"] = SIGNER.sign(jid, approval_id, decision, content)
    # 先写临时文件再 link 过去: 读方永远看不到写了一半的决定; 目标已存在时 link 失败 = 先写者胜
    tmp = f"{target}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "w", encoding="utf-8") as f:
        json.dump(record, f)
    already = {"code": "already_decided", "error": "this approval already has a decision"}
    try:
        try:
            os.link(tmp, target)
        except FileExistsError:
            return 409, {**already, "decision": _load(target)}
        except OSError:
            # 不支持硬链接的文件系统: 退回独占创建(内容可能短暂不完整, 读方会重读)
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return 409, {**already, "decision": _load(target)}
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f)
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
        if not JOB_ID.fullmatch(jid) or not os.path.isdir(os.path.join(jobs.dir, jid, "approvals")):
            continue
        for item in list_requests(jobs, jid, pending_only=True):
            item["job_id"] = jid
            items.append(item)
    items.sort(key=lambda r: -float(r.get("created_at") or 0))
    return items[:limit]
