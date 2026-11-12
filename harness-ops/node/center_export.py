"""Export a center's data by task, for moving it to another center (center_import.py reads it back).

A task is a research object with everything that belongs to it: its objects with all their revisions, events and
operations, its directions' dependencies, the jobs whose research_id is the task, its registered results and the
attachments its revisions or results refer to. Jobs, results and attachments of no task form one more group, 未归属任务
的数据. A selection is whole tasks and that group, nothing finer. Jobs run on other computers are collected first;
what cannot be collected is listed as 未带原件. The zip is written into the export cache next to the center's data,
kept for a week and can be deleted sooner."""
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from urllib.parse import quote

import center_archive as fmt
from artifact_io import matches_persisted_stamp, stamp
from job_export_cache import CACHE_FILE
from runtime_identity import read_build_info

RETAIN_S = 7 * 86400
MARGIN = 256 << 20          # free space kept beyond the estimate
SIZE_TTL = 600              # job directory sizes are read again after this
UNFINISHED = {"preparing", "queued", "claimed", "blocked"}
EXPORT_ID = re.compile(r"ex-[0-9a-f]{12}\Z")


class CenterExportError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra

    def payload(self):
        return {"code": self.code, "error": str(self), **self.extra}


def readonly(path):
    db = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def rows(path, sql, args=()):
    if not os.path.exists(path):
        return []
    db = readonly(path)
    try:
        db.execute("BEGIN")
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.close()


def attachment_ids(value):
    """Attachment IDs anywhere in a stored JSON value (evidence references, bindings, result sources)."""
    if isinstance(value, dict):
        aid = value.get("attachment_id")
        if isinstance(aid, str) and fmt.ATTACHMENT.match(aid):
            yield aid
        for item in value.values():
            yield from attachment_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from attachment_ids(item)


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class Inventory:
    """Which jobs, results and attachments belong to which task, read from the center's databases and job
    directories as they are now. Group None is 未归属任务的数据."""

    def __init__(self, jobs, sizes):
        self.jobs = jobs
        research = jobs.research.path
        self.tasks = {}
        for row in rows(research, """SELECT o.id, o.created_at, o.updated_at, r.title, r.state, r.data FROM objects o
                JOIN revisions r ON r.kind=o.kind AND r.id=o.id AND r.revision=o.revision WHERE o.kind='research'"""):
            data = json.loads(row["data"])
            self.tasks[row["id"]] = {"id": row["id"], "title": row["title"], "state": row["state"], "owner": data.get("owner"),
                                     "follows": data.get("follows"), "created_at": row["created_at"],
                                     "updated_at": row["updated_at"], "objects": 0}
        for row in rows(research, "SELECT research_id, COUNT(*) AS n FROM objects GROUP BY research_id"):
            if row["research_id"] in self.tasks:
                self.tasks[row["research_id"]]["objects"] = row["n"]
        group = lambda rid: rid if rid in self.tasks else None   # noqa: E731
        self.attachments = {}
        for row in rows(research, "SELECT research_id, data, bindings FROM revisions"):
            for column in ("data", "bindings"):
                for aid in attachment_ids(json.loads(row[column] or "null")):
                    self.attachments.setdefault(aid, set()).add(group(row["research_id"]))
        self.results = {}
        for row in rows(jobs.results.path, "SELECT result_id, research_id, manifest FROM results"):
            self.results[row["result_id"]] = group(row["research_id"])
            for aid in attachment_ids(json.loads(row["manifest"])):
                self.attachments.setdefault(aid, set()).add(group(row["research_id"]))
        self.job_group, self.unfinished, self.remote = {}, set(), {}
        queue = jobs.dispatch.queue.path
        for row in rows(queue, "SELECT job_id, state, json_extract(spec,'$.research_id') AS rid FROM jobs"):
            self.job_group[row["job_id"]] = group(row["rid"])
            if row["state"] in UNFINISHED:
                self.unfinished.add(row["job_id"])
        has_targets = rows(queue, "SELECT name FROM sqlite_master WHERE type='table' AND name='job_targets'")
        for row in rows(queue, "SELECT job_id, value FROM job_targets") if has_targets else []:
            value = json.loads(row["value"])
            self.job_group.setdefault(row["job_id"], group(((value.get("request") or {}).get("spec") or {}).get("research_id")))
            if value.get("execution_node_id") != jobs.node_id:
                self.remote[row["job_id"]] = value
                if ((value.get("observation") or {}).get("state")) != "finished":
                    self.unfinished.add(row["job_id"])
        self.directories = set()
        for name in sorted(os.listdir(jobs.dir)):
            if fmt.JOB.match(name) and os.path.isdir(os.path.join(jobs.dir, name)) and not os.path.islink(os.path.join(jobs.dir, name)):
                self.directories.add(name)
                if name not in self.job_group:
                    try:
                        with open(os.path.join(jobs.dir, name, "meta.json"), "rb") as stream:
                            rid = json.loads(stream.read(1 << 20)).get("research_id")
                    except (OSError, ValueError, AttributeError):
                        rid = None
                    self.job_group[name] = group(rid)
        directory = os.path.join(jobs.data_root, "_research", "attachments")
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else ():
            if fmt.ATTACHMENT.match(name):
                self.attachments.setdefault(name, set())
        for aid, owners in self.attachments.items():
            if not owners:
                owners.add(None)
        self.sizes = sizes

    def members(self, key):
        jobs = sorted(j for j, g in self.job_group.items() if g == key)
        results = sorted(r for r, g in self.results.items() if g == key)
        attachments = sorted(a for a, owners in self.attachments.items() if key in owners)
        return jobs, results, attachments

    def job_bytes(self, jid):
        return sum(size for _, size in self.sizes(jid)) if jid in self.directories else 0

    def attachment_bytes(self, aid):
        try:
            return os.stat(os.path.join(self.jobs.data_root, "_research", "attachments", aid)).st_size
        except OSError:
            return 0

    def group(self, key):
        jobs, results, attachments = self.members(key)
        return {"jobs": len(jobs), "results": len(results), "attachments": len(attachments),
                "bytes": sum(map(self.job_bytes, jobs)) + sum(map(self.attachment_bytes, attachments)),
                "unfinished_jobs": [j for j in jobs if j in self.unfinished],
                "originals_elsewhere": [j for j in jobs if j in self.remote
                                        and (self.remote[j].get("artifacts_sync") or {}).get("status") != "complete"]}

    def listing(self):
        tasks = [{**task, **self.group(tid)} for tid, task in self.tasks.items()]
        chains = {}
        for task in tasks:
            root, seen = task["id"], set()
            while self.tasks.get(root, {}).get("follows") in self.tasks and root not in seen:
                seen.add(root)
                root = self.tasks[root]["follows"]
            task["chain"] = root
            chains.setdefault(root, []).append(task["id"])
        tasks.sort(key=lambda t: -(t["updated_at"] or 0))
        return {"tasks": tasks, "unassigned": self.group(None),
                "chains": [{"root": root, "tasks": sorted(ids, key=lambda i: self.tasks[i]["created_at"] or 0)}
                           for root, ids in chains.items() if len(ids) > 1]}


class CenterExports:
    def __init__(self, jobs):
        self.jobs = jobs
        configured = jobs.export_cache.configured_root
        self.root = os.path.join(configured, "center-export") if configured else os.path.join(jobs.data_root, "_center_export")
        self._guard = threading.Lock()
        self._running = None
        self._sizes = {}
        for name in self._names():
            if name.endswith(".json") and EXPORT_ID.match(name[:-5]):
                status = self._read(name[:-5])
                if status and status["state"] == "running":   # the service stopped while it was writing
                    self._write({**status, "state": "failed", "error": "中心服务重启，导出中断了；请重新导出",
                                 "finished_at": time.time(), "expires_at": time.time() + RETAIN_S})
                    self._remove(status["id"] + ".zip.partial")

    def _names(self):
        return os.listdir(self.root) if os.path.isdir(self.root) else []

    def _free(self):
        path = self.root
        while not os.path.isdir(path):
            path = os.path.dirname(path)
        return shutil.disk_usage(path).free

    # ── what can be exported ──
    def _files(self, jid):
        """Regular files of a job directory as (relative path, size), symbolic links and derived caches left out."""
        cached = self._sizes.get(jid)
        if cached and time.time() - cached[0] < SIZE_TTL:
            return cached[1]
        files, base = [], os.path.join(self.jobs.dir, jid)
        for parent, directories, names in os.walk(base):
            directories[:] = sorted(d for d in directories if not os.path.islink(os.path.join(parent, d)))
            for name in sorted(names):
                path = os.path.join(parent, name)
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode) and not CACHE_FILE.match(name):
                    files.append((os.path.relpath(path, base).replace(os.sep, "/"), info.st_size))
        self._sizes[jid] = (time.time(), files)
        return files

    def inventory(self):
        return Inventory(self.jobs, self._files)

    def tasks(self):
        return {**self.inventory().listing(), "excluded": fmt.EXCLUDED,
                "cache": {"root": self.root, "free_bytes": self._free(), "retain_s": RETAIN_S}}

    # ── exports in the cache ──
    def _path(self, name):
        return os.path.join(self.root, name)

    def _read(self, eid):
        try:
            with open(self._path(eid + ".json"), encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, ValueError):
            return None

    def _write(self, status):
        temporary = self._path(status["id"] + ".json." + uuid.uuid4().hex)
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            json.dump(status, stream, ensure_ascii=False)
        os.replace(temporary, self._path(status["id"] + ".json"))

    def _remove(self, name):
        try:
            os.unlink(self._path(name))
        except FileNotFoundError:
            pass

    def _purge(self):
        now = time.time()
        for name in self._names():
            eid = name[:-5] if name.endswith(".json") else None
            if eid and EXPORT_ID.match(eid):
                status = self._read(eid)
                if status and status["state"] != "running" and (status.get("expires_at") or now) < now:
                    self.delete(eid)

    def list(self):
        with self._guard:
            self._purge()
            found = [self._read(name[:-5]) for name in self._names()
                     if name.endswith(".json") and EXPORT_ID.match(name[:-5])]
        return {"exports": sorted((s for s in found if s), key=lambda s: -s["created_at"]), "retain_s": RETAIN_S}

    def get(self, eid):
        if not EXPORT_ID.match(eid or ""):
            raise CenterExportError(404, "export_not_found", "没有这个导出")
        status = self._read(eid)
        if status is None:
            raise CenterExportError(404, "export_not_found", "没有这个导出")
        return status

    def open(self, eid):
        status = self.get(eid)
        if status["state"] != "done":
            raise CenterExportError(409, "export_not_ready", "导出还没完成")
        return open(self._path(eid + ".zip"), "rb"), status

    def delete(self, eid):
        status = self.get(eid)
        if status["state"] == "running":
            raise CenterExportError(409, "export_running", "导出还在进行，完成或失败后才能删除")
        for name in (eid + ".zip", eid + ".zip.partial", eid + ".json"):
            self._remove(name)
        return {"id": eid, "deleted": True}

    # ── starting one ──
    def start(self, body):
        """Body: {"tasks": [task IDs], "unassigned": bool} and nothing else; a task is always taken whole."""
        if not isinstance(body, dict) or set(body) - {"tasks", "unassigned"}:
            raise CenterExportError(400, "whole_tasks_only", "只能按任务整体选择：请求里只能有 tasks 和 unassigned")
        tasks, unassigned = body.get("tasks", []), body.get("unassigned", False)
        if (not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks) or len(set(tasks)) != len(tasks)
                or type(unassigned) is not bool):
            raise CenterExportError(400, "whole_tasks_only", "tasks 要是不重复的任务 ID 列表，unassigned 要是 true 或 false")
        if not tasks and not unassigned:
            raise CenterExportError(400, "nothing_selected", "没有选任何任务")
        with self._guard:
            if self._running and self._running.is_alive():
                raise CenterExportError(409, "export_running", "已经有一个导出在进行，等它完成再开始下一个")
            self._purge()
            inventory = self.inventory()
            missing = [t for t in tasks if t not in inventory.tasks]
            if missing:
                raise CenterExportError(400, "task_not_found", "没有这些任务：" + "、".join(missing[:20]), tasks=missing)
            keys = [*tasks, *([None] if unassigned else [])]
            jobs = sorted({j for key in keys for j in inventory.members(key)[0]})
            attachments = sorted({a for key in keys for a in inventory.members(key)[2]})
            estimate = sum(map(inventory.job_bytes, jobs)) + 2 * sum(map(inventory.attachment_bytes, attachments)) + (1 << 20)
            free = self._free()
            if free < estimate + MARGIN:
                raise CenterExportError(507, "export_space", f"导出缓存所在磁盘（{self.root}）只剩 {human(free)}，这次导出约需 "
                                  f"{human(estimate)}，另要留 {human(MARGIN)} 余量；先腾出至少 {human(estimate + MARGIN - free)}",
                                  needed_bytes=estimate + MARGIN, free_bytes=free)
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            now = time.time()
            status = {"id": "ex-" + uuid.uuid4().hex[:12], "state": "running", "phase": "collect", "created_at": now,
                      "finished_at": None, "expires_at": None, "tasks": tasks, "unassigned": unassigned,
                      "estimate_bytes": estimate, "bytes_done": 0, "bytes_total": None, "files_done": 0, "files_total": None,
                      "size": None, "sha256": None, "error": None,
                      "unfinished_jobs": [j for j in jobs if j in inventory.unfinished], "missing_originals": []}
            self._write(status)
            self._running = threading.Thread(target=self._run, args=(status, inventory, keys), daemon=True, name="center-export")
            self._running.start()
            return status

    def _run(self, status, inventory, keys):
        partial = self._path(status["id"] + ".zip.partial")
        try:
            Writer(self, status, inventory, keys, partial).write()
            os.replace(partial, self._path(status["id"] + ".zip"))
            status.update(state="done", phase="done", finished_at=time.time(), expires_at=time.time() + RETAIN_S)
        except Exception as error:  # noqa: BLE001 - the status file is the only place the outcome is seen
            self._remove(status["id"] + ".zip.partial")
            space = isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT)
            status.update(state="failed", finished_at=time.time(), expires_at=time.time() + RETAIN_S,
                          error=("导出缓存所在磁盘写满了" if space else "导出失败") + f"：{error}")
        self._write(status)


class Writer:
    def __init__(self, exports, status, inventory, keys, path):
        self.exports, self.jobs, self.status, self.inventory, self.keys, self.path = (
            exports, exports.jobs, status, inventory, keys, path)
        self.files, self.observed = [], {}
        self.last_saved = 0

    def progress(self, force=False):
        if force or time.time() - self.last_saved > 1:
            self.exports._write(self.status)
            self.last_saved = time.time()

    def collect(self, jobs):
        """Bring home what jobs run on other computers wrote; what cannot be brought is listed, not dropped."""
        missing = []
        for jid in jobs:
            target = self.inventory.remote.get(jid)
            if target is None:
                if jid not in self.inventory.directories:
                    missing.append({"job_id": jid, "reason": "中心的作业目录里没有它（可能在历史作业目录里，或已删除）"})
                continue
            failure = None
            if (target.get("artifacts_sync") or {}).get("status") != "complete":
                if (target.get("observation") or {}).get("state") == "finished":
                    try:
                        self.jobs.center.artifacts.collect(jid)
                    except Exception as error:  # noqa: BLE001 - one computer out of reach must not stop the export
                        failure = str(error)
                target = self.jobs.center.targets.get(jid) or target
            sync = target.get("artifacts_sync") or {}
            if sync.get("status") != "complete":
                reason = ("作业还没结束，原件还在从节点上" if (target.get("observation") or {}).get("state") != "finished"
                          else f"从节点上的原件没收回来：{failure or sync.get('error') or sync.get('status') or '还没开始收'}")
                missing.append({"job_id": jid, "execution_node_id": target.get("execution_node_id"), "reason": reason})
            if jid not in self.inventory.directories and os.path.isdir(os.path.join(self.jobs.dir, jid)):
                self.inventory.directories.add(jid)
        return missing

    def snapshot(self, tasks, jobs, results):
        """The rows of the selected tasks, jobs and results, read in one transaction per database."""
        tables, chosen = {}, set(tasks)
        db = readonly(self.jobs.research.path)
        try:
            db.execute("BEGIN")
            pick = lambda sql: [dict(r) for r in db.execute(sql) if r["research_id"] in chosen]   # noqa: E731
            tables["research/objects"] = pick("SELECT * FROM objects ORDER BY kind, id")
            tables["research/revisions"] = pick("SELECT * FROM revisions ORDER BY sequence")
            tables["research/events"] = pick("SELECT * FROM events ORDER BY sequence")
            operations = {e["operation_id"] for e in tables["research/events"]}
            tables["research/operations"] = [dict(r) for r in db.execute("SELECT * FROM operations ORDER BY id") if r["id"] in operations]
            directions = {o["id"] for o in tables["research/objects"] if o["kind"] == "direction"}
            tables["research/dependencies"] = [dict(r) for r in db.execute("SELECT * FROM dependencies ORDER BY direction_id, id")
                                               if r["direction_id"] in directions]
        finally:
            db.close()
        db = readonly(self.jobs.results.path)
        try:
            db.execute("BEGIN")
            tables["results/results"] = [dict(r) for r in db.execute("SELECT * FROM results ORDER BY sequence") if r["result_id"] in results]
            imports = {json.loads(r["provenance"]).get("import_id") for r in tables["results/results"]}
            tables["results/imports"] = [dict(r) for r in db.execute("SELECT * FROM imports ORDER BY import_id") if r["import_id"] in imports]
        finally:
            db.close()
        chosen_jobs = set(jobs)
        queue = self.jobs.dispatch.queue.path
        db = readonly(queue)
        try:
            db.execute("BEGIN")
            tables["queue/jobs"] = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY sequence") if r["job_id"] in chosen_jobs]
            tables["queue/job_events"] = [dict(r) for r in db.execute("SELECT * FROM job_events ORDER BY sequence") if r["job_id"] in chosen_jobs]
            has_targets = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_targets'").fetchone()
            tables["queue/job_targets"] = [dict(r) for r in db.execute("SELECT * FROM job_targets ORDER BY job_id")
                                           if r["job_id"] in chosen_jobs] if has_targets else []
        finally:
            db.close()
        return tables

    def add(self, archive, name, chunks, compress):
        info = zipfile.ZipInfo(name, date_time=time.localtime(time.time())[:6])
        info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        info.external_attr = 0o600 << 16
        digest, size = hashlib.sha256(), 0
        with archive.open(info, "w", force_zip64=True) as stream:
            for block in chunks:
                stream.write(block)
                digest.update(block)
                size += len(block)
                if not compress:   # bytes_total counts the files; the table rows are small
                    self.status["bytes_done"] += len(block)
                    self.progress()
        entry = {"path": name, "size": size, "sha256": digest.hexdigest()}
        self.files.append(entry)
        self.status["files_done"] += 1
        return entry

    def add_file(self, archive, name, path):
        """A file as it is now. Its identity before reading and whether it changed while read are kept, to tell
        later whether a registered result source was still the registered file."""
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            before = stamp(os.fstat(stream.fileno()))
            entry = self.add(archive, name, iter(lambda: stream.read(fmt.CHUNK), b""), False)
            after = stamp(os.fstat(stream.fileno()))
        entry.update(mode=stat.S_IMODE(before[2]), mtime_ns=before[4])
        self.observed[name] = (before, before == after and before[3] == entry["size"], entry["sha256"])

    def write(self):
        inventory, keys = self.inventory, self.keys
        groups = {key: inventory.members(key) for key in keys}
        jobs = sorted({j for members in groups.values() for j in members[0]})
        results = {r for members in groups.values() for r in members[1]}
        attachments = sorted({a for members in groups.values() for a in members[2]})
        missing = self.collect(jobs)
        self.status.update(phase="write", missing_originals=missing)
        tables = self.snapshot([k for k in keys if k is not None], jobs, results)
        sources = [(fmt.job_member(jid, rel), os.path.join(self.jobs.dir, jid, *rel.split("/")), size)
                   for jid in jobs if jid in inventory.directories for rel, size in self.exports._files(jid)]
        attachment_root = os.path.join(self.jobs.data_root, "_research", "attachments")
        for aid in attachments:
            for metadata in (False, True):
                path = os.path.join(attachment_root, aid + (".json" if metadata else ""))
                if os.path.isfile(path) and not os.path.islink(path):
                    sources.append((fmt.attachment_member(aid, metadata), path, os.path.getsize(path)))
        self.status.update(bytes_total=sum(size for _, _, size in sources), files_total=len(sources) + len(tables))
        self.progress(True)
        with open(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as raw:
            with zipfile.ZipFile(raw, "w", allowZip64=True) as archive:
                counts = {}
                for name, items in tables.items():
                    counts[name] = len(items)
                    self.add(archive, name + ".jsonl", (fmt.row_line(row).encode() for row in items), True)
                for name, path, _ in sources:
                    self.add_file(archive, name, path)
                manifest = self.manifest(groups, tables, counts, missing)
                info = zipfile.ZipInfo(fmt.MANIFEST, date_time=time.localtime(time.time())[:6])
                info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o600 << 16
                archive.writestr(info, json.dumps(manifest, ensure_ascii=False, indent=1))
            raw.flush()
            os.fsync(raw.fileno())
            size = raw.tell()
        digest = hashlib.sha256()
        with open(self.path, "rb") as stream:
            for block in iter(lambda: stream.read(fmt.CHUNK), b""):
                digest.update(block)
        self.status.update(size=size, sha256=digest.hexdigest())

    def bindings(self, tables):
        """Registered result sources whose file is in this export and is still the registered one (same file
        identity, or same SHA-256 as registered): the importer binds them to the imported file. Others are listed."""
        verified, unverified = [], []
        for row in tables["results/results"]:
            declared = {s["name"]: s for s in json.loads(row["manifest"])["sources"]}
            for observation in json.loads(row["observations"]):
                source = declared.get(observation["name"], {})
                if observation.get("kind") not in ("job_output", "attachment") or observation.get("status") != "available":
                    continue
                name = (fmt.job_member(source["job_id"], "out/" + source["path"]) if observation["kind"] == "job_output"
                        else fmt.attachment_member(source["attachment_id"]))
                item = {"result_id": row["result_id"], "source": observation["name"], "member": name}
                seen = self.observed.get(name)
                if seen is None:
                    unverified.append({**item, "reason": "文件不在这次导出里（它的作业属于没选的任务，或没带原件）"})
                    continue
                before, steady, sha = seen
                if (steady and matches_persisted_stamp(before, observation.get("stamp"))) or observation.get("sha256") == sha:
                    verified.append({**item, "sha256": sha})
                else:
                    unverified.append({**item, "reason": "登记以后文件变过：身份和 SHA-256 都对不上登记时的"})
        return verified, unverified

    def manifest(self, groups, tables, counts, missing):
        inventory = self.inventory
        verified, unverified = self.bindings(tables)
        lost = {item["job_id"]: item for item in missing}

        def group(key):
            jobs, results, attachments = groups[key]
            return {"jobs": jobs, "results": results, "attachments": attachments,
                    "unfinished_jobs": [j for j in jobs if j in inventory.unfinished],
                    "missing_originals": [lost[j] for j in jobs if j in lost]}
        return {"schema": fmt.SCHEMA,
                "source": {"node_id": self.jobs.node_id, "hostname": socket.gethostname(), "exported_at": time.time(),
                           "platform": read_build_info(os.path.dirname(os.path.abspath(__file__)))},
                "excluded": fmt.EXCLUDED,
                "tasks": [{"id": key, **{k: inventory.tasks[key][k] for k in ("title", "state", "owner", "follows")},
                           "objects": sum(1 for o in tables["research/objects"] if o["research_id"] == key), **group(key)}
                          for key in groups if key is not None],
                "unassigned": group(None) if None in groups else None,
                "rows": counts, "result_sources": {"verified": verified, "unverified": unverified},
                "files": self.files}
