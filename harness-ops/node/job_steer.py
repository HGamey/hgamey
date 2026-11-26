"""Mid-run steering: a person leaves a short message for a running phonefarm job.

The center writes `<job>/steer/<id>.json`; phonefarm (src/steer.rs) reads it before its next
model decision, puts it in the model's context as a person's instruction and writes
`<id>.ack.json` with the step it was read at. Messages change what the model plans next;
they never bypass approvals.
"""

import json
import os
import re
import secrets
import time

ID = re.compile(r"st-[A-Za-z0-9_-]{1,100}")
JOB_ID = re.compile(r"[0-9a-f]{12}")
SCHEMA = "phonefarm/steer@1"
TEXT_MAX = 500


def _dir(jobs, jid):
    return jobs.path(jid, "steer")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _meta(jobs, jid):
    return _load(jobs.path(jid, "meta.json")) or {}


def steerable(meta):
    """Only model-driven runs read messages: `phonefarm run …`, or a skill replay that hands over to
    the model when it breaks (`skill run … --fallback`). A plain zero-model replay has nobody to tell."""
    argv = [str(a) for a in (meta.get("argv") or [])]
    if meta.get("bin") != "phonefarm" or not argv:
        return False
    return argv[0] == "run" or argv[:2] == ["skill", "run"] and "--fallback" in argv


def list_messages(jobs, jid):
    root = _dir(jobs, jid)
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json") or name.endswith(".ack.json") or not ID.fullmatch(name[:-5]):
            continue
        msg = _load(os.path.join(root, name))
        if not msg or msg.get("schema") != SCHEMA:
            continue
        ack = _load(os.path.join(root, name[:-5] + ".ack.json"))
        msg["read_at_step"] = ack.get("step") if ack else None
        out.append(msg)
    out.sort(key=lambda m: float(m.get("at") or 0))
    return out


def post(jobs, jid, body, who):
    """Leave a message. Returns (http_code, payload)."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return 400, {"code": "invalid_request", "error": "body must be JSON"}
    text = str((data or {}).get("text", "")).strip()
    if not text:
        return 400, {"code": "invalid_request", "error": "text is empty"}
    if len(text) > TEXT_MAX:
        return 400, {"code": "invalid_request", "error": f"text is longer than {TEXT_MAX} characters"}
    meta = _meta(jobs, jid)
    if not meta:
        return 404, {"code": "job_not_found", "error": "no such job"}
    if meta.get("state") != "running":
        return 409, {"code": "not_running", "error": "the job is not running", "state": meta.get("state")}
    if not steerable(meta):
        return 409, {"code": "not_steerable", "error": "only `phonefarm run` jobs read messages"}
    root = _dir(jobs, jid)
    os.makedirs(root, exist_ok=True)
    now = time.time()
    mid = f"st-{int(now * 1000)}-{secrets.token_hex(4)}"
    record = {"schema": SCHEMA, "id": mid, "text": text, "by": who, "at": now}
    # 先写临时文件再改名: phonefarm 不会读到写了一半的留言
    tmp = os.path.join(root, f".{mid}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(root, mid + ".json"))
    return 200, record


def running(jobs, limit=20):
    """Running jobs that accept messages, newest first, with their messages (dashboard)."""
    items = []
    try:
        names = os.listdir(jobs.dir)
    except FileNotFoundError:
        return []
    for jid in names:
        if not JOB_ID.fullmatch(jid) or not os.path.isfile(os.path.join(jobs.dir, jid, "meta.json")):
            continue
        try:
            meta = _meta(jobs, jid)
        except (KeyError, ValueError):  # 符号链接之类的怪目录: 跳过, 不让整张表 500
            continue
        if meta.get("state") != "running" or not steerable(meta):
            continue
        argv = meta.get("argv") or []
        items.append({"job_id": jid, "label": meta.get("label") or "", "started": meta.get("started"),
                      "argv": " ".join(str(a) for a in argv)[:300], "messages": list_messages(jobs, jid)[-5:]})
    items.sort(key=lambda r: -float(r.get("started") or 0))
    return items[:limit]
