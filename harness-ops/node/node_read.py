"""Host-only node identity and generic job/output discovery. No device probing."""
import base64
import json
import math
import os
import platform
import re
import socket
import stat
import subprocess
import time
import uuid
from artifact_io import relative_path


def page_size(query, default=50):
    n = int(query.get("limit", [str(default)])[0])
    if not 1 <= n <= 200:
        raise ValueError("limit must be between 1 and 200")
    return n


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_cursor(value):
    try:
        if len(value) > 2048:
            raise ValueError
        return json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        raise ValueError("invalid cursor") from None



class NodeRead:
    def __init__(self, jobs, data_root, source_dir):
        self.jobs = jobs
        self.started = time.time()
        self.data_root = os.path.abspath(data_root)
        identity_dir = os.path.join(self.data_root, "_node")
        os.makedirs(identity_dir, exist_ok=True)
        identity_path = os.path.join(identity_dir, "id")
        temporary = identity_path + "." + uuid.uuid4().hex
        try:
            with open(temporary, "x", encoding="utf-8") as f:
                f.write(uuid.uuid4().hex + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, identity_path)  # Publish complete bytes, never a partially written identity.
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        with open(identity_path, encoding="utf-8") as f:
            self.node_id = f.read().strip()
        if not re.fullmatch(r"[0-9a-f]{32}", self.node_id):
            raise ValueError("invalid persistent node identity; preserve and repair _node/id")
        self.service = {"revision": None, "dirty": None}
        try:
            self.service["revision"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source_dir, stderr=subprocess.DEVNULL, timeout=3).decode().strip()
            self.service["dirty"] = bool(subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_dir,
                stderr=subprocess.DEVNULL, timeout=3).strip())
        except (OSError, subprocess.SubprocessError):
            pass

    def identity(self):
        return {"schema_version": 1, "node_id": self.node_id,
                "host": {"hostname": socket.gethostname(), "platform": platform.system()},
                "service": self.service, "started_at": self.started,
                "storage": {"jobs_root": os.path.abspath(self.jobs.dir), "data_root": self.data_root},
                "capabilities": ["fleet.read", "jobs.list", "jobs.read", "jobs.artifacts", "jobs.download.range",
                                 "jobs.cancel", "jobs.supervised-v1"],
                "devices": {"status": "not_probed", "reason": "node identity does not probe phones"}}

    def list_jobs(self, query):
        limit = page_size(query)
        search = query.get("query", [""])[0].casefold()
        state = query.get("state", [""])[0]
        if state not in ("", "running", "finished", "lost"):
            raise ValueError("state must be running, finished or lost")
        after = None
        if query.get("cursor"):
            cursor = decode_cursor(query["cursor"][0])
            if not (isinstance(cursor, list) and len(cursor) == 4 and
                    isinstance(cursor[0], (int, float)) and math.isfinite(cursor[0]) and
                    isinstance(cursor[1], str) and re.fullmatch(r"[0-9a-f]{12}", cursor[1]) and
                    cursor[2:] == [search, state]):
                raise ValueError("cursor must belong to the same query and state")
            after = tuple(cursor[:2])
        entries, warnings = [], []
        for jid in os.listdir(self.jobs.dir):
            if not re.fullmatch(r"[0-9a-f]{12}", jid):
                continue
            try:
                meta = self.jobs.status(jid)
                started = meta.get("started", 0)
                if not isinstance(started, (int, float)) or not math.isfinite(started):
                    raise ValueError("invalid start time")
                key = (started, jid)
                if after is not None and key >= after:
                    continue
                if state == "lost" and not meta.get("lost"):
                    continue
                if state in ("running", "finished") and meta["state"] != state:
                    continue
                haystack = json.dumps({k: meta.get(k) for k in ("id", "bin", "label", "argv")}, ensure_ascii=False).casefold()
                if search and search not in haystack:
                    continue
                entries.append((key, meta))
            except (OSError, ValueError, KeyError, TypeError):
                warnings.append({"job": jid, "error": "metadata_unavailable"})
        entries.sort(key=lambda pair: pair[0], reverse=True)
        page = entries[:limit]
        cursor = encode_cursor([*page[-1][0], search, state]) if len(entries) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": [m for _, m in page],
                "next_cursor": cursor, "warnings": warnings[:100], "warning_count": len(warnings)}

    def artifacts(self, jid, query):
        self.jobs.status(jid)
        rel = relative_path(query.get("path", [""])[0])
        limit = page_size(query, 100)
        after = ""
        if query.get("cursor"):
            cursor = decode_cursor(query["cursor"][0])
            if not (isinstance(cursor, list) and len(cursor) == 3 and cursor[:2] == [jid, rel]
                    and isinstance(cursor[2], str)):
                raise ValueError("cursor must belong to this artifact directory")
            after = cursor[2]
        target = jid + "/out" + ("/" + rel if rel else "")
        directory = self.jobs._read_root.open_fd(target, directory=True)
        entries = []
        try:
            names = sorted(name for name in os.listdir(directory) if name > after)
            for name in names[:limit]:
                item = {"name": name, "path": os.path.join(rel, name)}
                try:
                    st = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                        item.update(kind="unavailable", reason="links and special files are not downloadable")
                    else:
                        item.update(kind="directory" if stat.S_ISDIR(st.st_mode) else "file",
                                    size=st.st_size, modified=st.st_mtime)
                except OSError:
                    item.update(kind="unavailable", reason="artifact changed or is unreadable")
                entries.append(item)
        finally:
            os.close(directory)
        cursor = encode_cursor([jid, rel, entries[-1]["name"]]) if len(names) > limit else None
        return {"schema_version": 1, "job": jid, "path": rel, "items": entries, "next_cursor": cursor}
