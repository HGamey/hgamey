#!/usr/bin/env python3
"""Add a center export (made on the 数据导出 page, center_export.py) to this center's data.

    python3 harness-ops/node/center_import.py <zip> --data-root <中心数据根> [--apply]

The data root is the directory the center's node_service.py runs on: its --phonefarm directory, or its --data-dir.
The center must have run once there (it has _node/id and its databases) and must be stopped for --apply.

Without --apply nothing is written: every file in the zip is checked against its SHA-256, and the plan is printed —
which tasks, jobs, results and attachments are new, which are already here, which conflict, which references stay
dangling. With --apply the same checks run first; any damaged file or any conflict refuses the whole import. The
three databases are backed up, new files are staged next to the data and moved in, and on any failure everything
is put back as it was. Importing the same zip again changes nothing.

What changes on the way in:
- Sequence numbers (research events and revisions, results, queue jobs and events) continue after this center's
  own when they would collide; their order is kept, times are kept. Imported events then come after the events
  agents here have already read, so a running agent sees them as new: when importing part of a center into one in
  use, pause its agents first.
- References to the source center's node ID become this center's node ID, when the two differ.
- Result sequence numbers frozen in research rows (criteria) follow the results, also across imports: a task whose
  result is already here, and a task imported earlier that builds on a result coming in now.
- Registered result sources whose file was verified at export are bound to the imported file, so results, report
  pictures and blind comparisons open here as they did there. Others are listed and stay unavailable.
- A job that had not finished at export does not run here: one not yet started is recorded as cancelled before
  launch, one that had started as lost (its execution there is unknown), with its committed state and an event as
  the queue records them. One that ran on another computer is lost too, and this center does not ask that computer
  about it; finished ones on other computers open from what the center recorded and the originals it collected.
- The center must be stopped. When the data root of a running center cannot be told (a relative path or ~ whose
  working or home directory cannot be read), the import is refused too."""
import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import center_archive as fmt   # noqa: E402
import job_events   # noqa: E402
from artifact_io import stamp   # noqa: E402
from job_queue import JobQueue   # noqa: E402
from node_results import encoded   # noqa: E402

UNFINISHED = {"preparing", "queued", "claimed", "blocked"}
NAMES = {"queue/jobs": "作业队列记录", "queue/job_events": "作业事件", "queue/job_targets": "作业路由记录",
         "results/results": "结果", "results/imports": "结果导入记录", "research/objects": "研究对象",
         "research/objects (新版本)": "研究对象（追加新版本）", "research/revisions": "研究对象版本",
         "research/events": "研究事件", "research/operations": "研究操作", "research/dependencies": "方向依赖",
         "research/revisions (结果序号)": "这里已有的研究版本（改写里面冻结的结果序号）"}
LOST = "imported from another center before it finished; its execution there is unknown"
REPLACED = "\0replaced"   # rides along with a rewritten row until it is planned, then counted
PAUSE_NOTE = "往在用的中心部分导入时，agent 会把导入的事件当新事件看到；建议先暂停 agent。"


class Refused(Exception):
    def __init__(self, code, message, details=()):
        super().__init__(message)
        self.code, self.details = code, list(details)


# ── the target center ──
def service_root(argv, cwd, home=None):
    """The data root of a node_service.py command line: its --data-dir, else its --phonefarm (default
    ~/node/phonefarm), symbolic links resolved. `cwd` and `home` are the process's own working directory and home
    directory. None when it is not node_service.py, or when the root cannot be told: a relative path whose working
    directory is unknown, or ~ whose user's home is unknown."""
    if not any(os.path.basename(a) == "node_service.py" for a in argv):
        return None
    options = {}
    for i, arg in enumerate(argv):
        for name in ("--data-dir", "--phonefarm"):
            if arg == name and i + 1 < len(argv):
                options[name] = argv[i + 1]
            elif arg.startswith(name + "="):
                options[name] = arg.split("=", 1)[1]
    value = options.get("--data-dir") or options.get("--phonefarm") or "~/node/phonefarm"
    if value == "~" or value.startswith("~/"):
        if not home:
            return None
        value = home + value[1:]
    value = os.path.expanduser(value)   # ~user/... names its user
    if not os.path.isabs(value):
        if not cwd:
            return None
        value = os.path.join(cwd, value)
    return os.path.realpath(value)


def _home(uid):
    try:
        return pwd.getpwuid(uid).pw_dir
    except (KeyError, TypeError):
        return None


def node_service_roots():
    """(pid, data root or None when it cannot be told) of every node_service.py running on this computer."""
    listed = []
    if os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as stream:
                        argv = [a.decode(errors="replace") for a in stream.read().split(b"\0") if a]
                    uid = os.stat(f"/proc/{pid}").st_uid
                except OSError:
                    continue
                try:
                    cwd = os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    cwd = None
                listed.append((int(pid), argv, cwd, _home(uid)))
    else:
        try:
            output = subprocess.run(["ps", "-axww", "-o", "pid=,uid=,command="], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        for line in output.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                listed.append((int(parts[0]), parts[2].split(), None, _home(int(parts[1]))))
    found = []
    for pid, argv, cwd, home in listed:
        if pid != os.getpid() and any(os.path.basename(a) == "node_service.py" for a in argv):
            if cwd is None and not os.path.isdir("/proc"):
                try:   # macOS: the working directory from lsof
                    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], capture_output=True, text=True, timeout=10).stdout
                    cwd = next((line[1:] for line in out.splitlines() if line.startswith("n")), None)
                except (OSError, subprocess.SubprocessError):
                    cwd = None
            found.append((pid, service_root(argv, cwd, home)))
    return found


STOP_HELP = ("launchd 用 launchctl bootout gui/$(id -u)/<服务名>，systemd 用 systemctl --user stop <服务名>，手动起的直接结束 "
             "run_center.sh；导入完照原来的方式启动（launchctl bootstrap / systemctl --user start / run_center.sh）")


def target(data_root, jobs_dirname=None):
    root = os.path.realpath(data_root)
    if not os.path.isdir(root):
        raise Refused(1, f"数据根 {root} 不存在")
    try:
        with open(os.path.join(root, "_node", "id"), encoding="utf-8") as stream:
            node_id = stream.read().strip()
    except OSError:
        node_id = None
    if not node_id or not re.fullmatch(r"[0-9a-f]{32}", node_id):
        raise Refused(1, f"{root} 里没有中心身份（_node/id）。先在这里启动一次中心服务再停掉，或者把原中心的 _node/id 先拷过来")
    if jobs_dirname is None:
        present = [n for n in ("_jobs", "jobs") if os.path.isfile(os.path.join(root, n, ".queue.sqlite3"))]
        if len(present) > 1:
            raise Refused(1, f"{root} 里 _jobs 和 jobs 都有作业库，用 --jobs-dir 指定是哪一个")
        jobs_dirname = present[0] if present else "_jobs"
    paths = {"research": os.path.join(root, "_research", "state.sqlite3"),
             "results": os.path.join(root, "_results", "index.sqlite3"),
             "queue": os.path.join(root, jobs_dirname, ".queue.sqlite3")}
    absent = [p for p in paths.values() if not os.path.isfile(p)]
    if absent:
        raise Refused(1, "中心的库还没建好：" + "、".join(absent) + "。先在这里启动一次中心服务再停掉")
    return {"data_root": root, "node_id": node_id, "jobs_dir": os.path.join(root, jobs_dirname), "db": paths}


def connect(path):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def columns(db, table):
    return [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]


def file_sha(path):
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(fmt.CHUNK), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


# ── rewriting rows on the way in ──
class Remap:
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.results, self.events = {}, {}

    def node(self, row):
        """Only a whole JSON string equal to the source node ID is replaced, never part of a longer string."""
        if self.source == self.target:
            return dict(row)
        old, new = f'"{self.source}"', f'"{self.target}"'
        return {**{k: v.replace(old, new) if isinstance(v, str) else v for k, v in row.items()},
                REPLACED: sum(v.count(old) for v in row.values() if isinstance(v, str))}

    def result_sequences(self, text, only=None):
        """Result sequence numbers frozen in research bindings follow the results to their numbers here: those in
        the zip, and those already here (a task imported after the one whose result it builds on)."""
        if not text or "result_sequence" not in text:
            return text
        value, changed = json.loads(text), False

        def walk(item):
            nonlocal changed
            if isinstance(item, dict):
                rid = item.get("result_id")
                if rid is None and isinstance(item.get("reference"), dict):
                    rid = item["reference"].get("result_id")
                if (type(item.get("result_sequence")) is int and rid in self.results and self.results[rid] != item["result_sequence"]
                        and (only is None or rid in only)):
                    item["result_sequence"] = self.results[rid]
                    changed = True
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
        walk(value)
        return encoded(value) if changed else text

    def receipt(self, text):
        value = json.loads(text)
        if value.get("event_seq") in self.events and self.events[value["event_seq"]] != value["event_seq"]:
            value["event_seq"] = self.events[value["event_seq"]]
            return encoded(value)
        return text


def offset(new_old_sequences, target_max):
    """Shift new rows past this center's own only when they would collide; an empty center keeps the numbers."""
    if not new_old_sequences:
        return 0
    low = min(new_old_sequences)
    return target_max - low + 1 if target_max >= low else 0


# ── the plan ──
class Plan:
    def __init__(self, zip_path, data_root, jobs_dirname=None, progress=None):
        self.zip_path = os.path.abspath(zip_path)
        try:
            self.archive = zipfile.ZipFile(self.zip_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise Refused(2, f"打不开 {zip_path}：{error}") from None
        try:
            self.manifest = fmt.read_manifest(self.archive)
            self.directories = fmt.directories(self.manifest)
        except ValueError as error:
            raise Refused(2, str(error)) from None
        problems = fmt.verify(self.archive, self.manifest, progress)
        if problems:
            raise Refused(2, "导出文件校验没通过，整体拒绝，目标中心没有改动", problems)
        self.target = target(data_root, jobs_dirname)
        self.files = {entry["path"]: entry for entry in self.manifest["files"]}
        self.tables = {}
        for group, names in fmt.TABLES.items():
            for table in names:
                name = fmt.table_member(group, table)
                raw = self.archive.read(name).decode() if name in self.files else ""
                self.tables[f"{group}/{table}"] = [fmt.line_row(line) for line in raw.splitlines() if line]
        self.remap = Remap(self.manifest["source"]["node_id"], self.target["node_id"])
        self.conflicts, self.dangling, self.notes = [], [], []
        self.replaced = {"research": 0, "results": 0, "queue": 0}
        self.new, self.same = {}, {}
        self.inserts, self.updates = {}, {}
        self.settled, self.renumbered = [], []
        self.now = time.time()
        self._queue()
        self._results()
        self._research()
        self._files()
        self._references()

    def count(self, kind, name, n=1):
        bucket = self.new if kind == "new" else self.same
        bucket[name] = bucket.get(name, 0) + n

    def add(self, table, row):
        row = dict(row)
        self.replaced[table.split("/")[0]] += row.pop(REPLACED, 0)
        self.inserts.setdefault(table, []).append(row)
        self.count("new", table)

    def settle(self, row, lost):
        """A job that had not finished is done here: its committed state says so, with an event, as the queue itself
        records a cancellation or a released unknown execution."""
        extra = {k: row.pop(k) for k in (REPLACED,) if k in row}
        parsed = JobQueue._row({**row})
        observation = {"phase": "interrupted", "outcome": "unknown", "lost": True, "exit": None} if lost else None
        projection = json.dumps(job_events.projection(parsed, observation))
        revision = (row.get("status_revision") or 0) + 1
        self.settled.append({"event_id": secrets.token_hex(16), "job_id": row["job_id"], "revision": revision,
                             "kind": "baseline" if revision == 1 else "queue_transition", "at": self.now,
                             "committed_status": projection})
        return {**row, "status_revision": revision, "status_projection": projection, "status_committed_at": self.now, **extra}

    def lost_elsewhere(self, jid, value):
        """A job that had not finished on another computer is lost here: the center keeps what it last saw of it and
        no longer asks that computer (which this center may not even know)."""
        seen = value.get("observation")
        base = dict(seen) if seen else {**((value.get("request") or {}).get("spec") or {}), "id": jid,
                                         "started": value.get("submitted_at"), "exit": None}
        self.notes.append(f"作业 {jid} 在从节点上，导出时还没结束，导入后记为丢失（执行情况未知），不会再去那台电脑读它")
        return {**value, "observed_at": self.now,
                "observation": {**base, "id": jid, "state": "unknown", "phase": "interrupted", "outcome": "unknown", "lost": True,
                                "queue": {"resources": [], **(base.get("queue") or {}), "state": "done"},
                                "recovery": {"status": "required", "reason": LOST}}}

    # Queue: jobs, their events and their routing rows.
    def _queue(self):
        db = connect(self.target["db"]["queue"])
        try:
            db.execute("BEGIN")
            remap, meta = self.remap, {}
            for name in self.files:
                parts = name.split("/")
                if parts[0] == "jobs" and parts[2:] == ["meta.json"]:
                    meta[parts[1]] = name
            remote = {}
            for row in self.tables["queue/job_targets"]:
                value = json.loads(row["value"])
                remote[row["job_id"]] = value.get("execution_node_id") not in (remap.source, remap.target)
            new_jobs = []
            for row in self.tables["queue/jobs"]:
                row = remap.node(row)
                here = db.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
                other = db.execute("SELECT job_id FROM jobs WHERE request_id=?", (row["request_id"],)).fetchone()
                if here:
                    if (here["spec"], here["fingerprint"], here["request_id"]) != (row["spec"], row["fingerprint"], row["request_id"]):
                        self.conflicts.append(f"作业 {row['job_id']}：这里已有同 ID 的作业，内容不同")
                    else:
                        self.count("same", "queue/jobs")
                    continue
                if other:
                    self.conflicts.append(f"作业 {row['job_id']}：它的请求 ID 在这里已经被作业 {other['job_id']} 用了")
                    continue
                if row["state"] in UNFINISHED and not remote.get(row["job_id"]):
                    started = row["state"] == "claimed"
                    if not started and row["job_id"] in meta:
                        try:
                            started = json.loads(self.archive.read(meta[row["job_id"]])).get("phase") not in (None, "preparing", "queued")
                        except ValueError:
                            pass
                    if started and row["job_id"] in meta:
                        reason = {"kind": "manual_unknown", "message": LOST}
                        release = {"at": self.now, "basis": "center_import"}
                    else:
                        reason, release = {"kind": "cancelled_before_launch", "at": self.now}, {"at": self.now, "basis": "not_started"}
                    self.notes.append(f"作业 {row['job_id']} 导出时还没结束（{row['state']}），导入后记为"
                                      + ("丢失（在原中心的执行情况未知）" if reason["kind"] == "manual_unknown" else "开始前已取消") + "，不会在这里运行")
                    row = self.settle({**row, "state": "done", "state_reason": json.dumps(reason), "host_capacity_release": json.dumps(release)},
                                      lost=reason["kind"] == "manual_unknown")
                new_jobs.append(row)
            top = db.execute("SELECT COALESCE(MAX(sequence),0) FROM jobs").fetchone()[0]
            shift = offset([r["sequence"] for r in new_jobs], top)
            for row in new_jobs:
                self.add("queue/jobs", {**row, "sequence": row["sequence"] + shift})
            new_events = []
            for row in self.tables["queue/job_events"]:
                row = remap.node(row)
                here = db.execute("SELECT job_id, revision, kind FROM job_events WHERE event_id=? OR (job_id=? AND revision=?)",
                                  (row["event_id"], row["job_id"], row["revision"])).fetchone()
                if here:
                    if (here["job_id"], here["revision"], here["kind"]) != (row["job_id"], row["revision"], row["kind"]):
                        self.conflicts.append(f"作业事件 {row['event_id']}：这里已有，内容不同")
                    else:
                        self.count("same", "queue/job_events")
                    continue
                new_events.append(row)
            top = max(db.execute("SELECT COALESCE(MAX(sequence),0) FROM job_events").fetchone()[0],
                      (db.execute("SELECT seq FROM sqlite_sequence WHERE name='job_events'").fetchone() or [0])[0])
            shift = offset([r["sequence"] for r in new_events], top)
            for row in new_events:
                self.add("queue/job_events", {**row, "sequence": row["sequence"] + shift})
            for event in self.settled:   # numbered after the imported ones
                self.add("queue/job_events", event)
            for row in self.tables["queue/job_targets"]:
                row = remap.node(row)
                value = json.loads(row["value"])
                if remote.get(row["job_id"]) and (value.get("observation") or {}).get("state") != "finished":
                    row = {**row, "value": json.dumps(self.lost_elsewhere(row["job_id"], value))}
                here = db.execute("SELECT * FROM job_targets WHERE job_id=? OR request_id=?", (row["job_id"], row["request_id"])).fetchone()
                if here:
                    same = (here["job_id"], here["request_id"]) == (row["job_id"], row["request_id"]) and \
                        json.loads(here["value"]).get("request") == json.loads(row["value"]).get("request")
                    if same:
                        self.count("same", "queue/job_targets")
                    else:
                        self.conflicts.append(f"作业 {row['job_id']} 的路由记录：这里已有，内容不同")
                    continue
                self.add("queue/job_targets", row)
        finally:
            db.close()

    # Results and the imports they came from.
    def _results(self):
        db = connect(self.target["db"]["results"])
        try:
            db.execute("BEGIN")
            new = []
            for row in self.tables["results/results"]:
                row = self.remap.node(row)
                here = db.execute("SELECT sequence, manifest, provenance FROM results WHERE result_id=?", (row["result_id"],)).fetchone()
                if here:
                    if (here["manifest"], here["provenance"]) != (row["manifest"], row["provenance"]):
                        self.conflicts.append(f"结果 {row['result_id']}：这里已有同 ID 的结果，内容不同")
                    else:
                        self.remap.results[row["result_id"]] = here["sequence"]
                        self.count("same", "results/results")
                    continue
                new.append(row)
            shift = offset([r["sequence"] for r in new], db.execute("SELECT COALESCE(MAX(sequence),0) FROM results").fetchone()[0])
            for row in new:
                self.remap.results[row["result_id"]] = row["sequence"] + shift
                self.add("results/results", {**row, "sequence": row["sequence"] + shift})
            self.fresh_results = {row["result_id"] for row in new}
            # A task may build on a result that is not in the zip but already here (imported earlier).
            for rid, sequence in db.execute("SELECT result_id, sequence FROM results"):
                self.remap.results.setdefault(rid, sequence)
            for row in self.tables["results/imports"]:
                row = self.remap.node(row)
                here = db.execute("SELECT request, content FROM imports WHERE import_id=?", (row["import_id"],)).fetchone()
                if here:
                    if (here["request"], bytes(here["content"])) != (row["request"], row["content"]):
                        self.conflicts.append(f"结果导入记录 {row['import_id']}：这里已有，内容不同")
                    else:
                        self.count("same", "results/imports")
                    continue
                self.add("results/imports", row)
        finally:
            db.close()

    # Research: objects with all their revisions, events, operations and dependencies.
    def _research(self):
        db = connect(self.target["db"]["research"])
        remap = self.remap
        try:
            db.execute("BEGIN")
            compared = ("kind", "id", "revision", "research_id", "title", "state", "request_id", "data", "audit", "bindings", "observations", "at")
            new = []
            for row in self.tables["research/revisions"]:
                row = remap.node(row)
                row.update(bindings=remap.result_sequences(row["bindings"]), observations=remap.result_sequences(row["observations"]))
                here = db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND revision=?", (row["kind"], row["id"], row["revision"])).fetchone()
                if here:
                    if any(here[c] != row[c] for c in compared):
                        self.conflicts.append(f"研究对象 {row['kind']}/{row['id']} 第 {row['revision']} 版：这里已有，内容不同")
                    else:
                        self.count("same", "research/revisions")
                    continue
                new.append(row)
            fresh = {(r["kind"], r["id"], r["revision"]) for r in new}
            events = []
            for row in self.tables["research/events"]:
                row = remap.node(row)
                key = (row["kind"], row["object_id"], row["revision"])
                if key in fresh:
                    events.append(row)
                    continue
                here = db.execute("SELECT sequence FROM events WHERE kind=? AND object_id=? AND revision=?", key).fetchone()
                if here:
                    remap.events[row["sequence"]] = here["sequence"]
                    self.count("same", "research/events")
                else:
                    events.append(row)
            top = max(db.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0],
                      db.execute("SELECT COALESCE(MAX(sequence),0) FROM revisions").fetchone()[0],
                      (db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone() or [0])[0])
            shift = offset([r["sequence"] for r in events] + [r["sequence"] for r in new], top)
            for row in events:
                remap.events[row["sequence"]] = row["sequence"] + shift
                self.add("research/events", {**row, "sequence": row["sequence"] + shift})
            for row in new:
                self.add("research/revisions", {**row, "sequence": row["sequence"] + shift})
            for row in self.tables["research/operations"]:
                row = remap.node(row)
                here = db.execute("SELECT request FROM operations WHERE id=?", (row["id"],)).fetchone()
                if here:
                    if here["request"] != row["request"]:
                        self.conflicts.append(f"研究操作 {row['id']}：这里已有同 ID 的操作，内容不同")
                    else:
                        self.count("same", "research/operations")
                    continue
                self.add("research/operations", {**row, "receipt": remap.receipt(row["receipt"])})
            changed = set()
            for row in self.tables["research/objects"]:
                here = db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (row["kind"], row["id"])).fetchone()
                if here is None:
                    self.add("research/objects", row)
                    changed.add((row["kind"], row["id"]))
                elif here["research_id"] != row["research_id"]:
                    self.conflicts.append(f"研究对象 {row['kind']}/{row['id']}：这里已有，属于另一个任务 {here['research_id']}")
                elif here["revision"] < row["revision"]:
                    self.updates.setdefault("research/objects", []).append(row)
                    self.count("new", "research/objects (新版本)")
                    changed.add((row["kind"], row["id"]))
                else:
                    self.count("same", "research/objects")
            for row in self.tables["research/dependencies"]:
                if ("direction", row["direction_id"]) in changed:
                    self.add("research/dependencies", row)
            self.replace_dependencies = sorted(oid for kind, oid in changed if kind == "direction")
            # A task imported before the one whose result it builds on froze that result's number there.
            if self.fresh_results:
                for row in db.execute("SELECT sequence, bindings, observations FROM revisions WHERE "
                                      "bindings LIKE '%result_sequence%' OR observations LIKE '%result_sequence%'"):
                    bindings = remap.result_sequences(row["bindings"], self.fresh_results)
                    observations = remap.result_sequences(row["observations"], self.fresh_results)
                    if (bindings, observations) != (row["bindings"], row["observations"]):
                        self.renumbered.append((row["sequence"], bindings, observations))
                if self.renumbered:
                    self.count("new", "research/revisions (结果序号)", len(self.renumbered))
            self.known = {"objects": {(r["kind"], r["id"]) for r in db.execute("SELECT kind, id FROM objects")}}
        finally:
            db.close()

    # Job directories and attachments: new ones are added, existing ones must hold the same bytes.
    def _files(self):
        jobs, attachments = {}, {}
        for name, entry in self.files.items():
            parts = name.split("/")
            if parts[0] == "jobs":
                jobs.setdefault(parts[1], []).append(entry)
            elif parts[0] == "attachments":
                attachments[parts[1]] = entry
        for jid in self.directories:
            jobs.setdefault(jid, [])
        self.new_jobs, self.new_attachments = [], []
        for jid, entries in sorted(jobs.items()):
            base = os.path.join(self.target["jobs_dir"], jid)
            if not os.path.lexists(base):
                self.new_jobs.append(jid)
                self.count("new", "作业目录")
                continue
            differing = []
            for entry in entries:
                path = os.path.join(base, *entry["path"].split("/")[2:])
                try:
                    if os.path.islink(path) or file_sha(path) != (entry["size"], entry["sha256"]):
                        differing.append(entry["path"])
                except OSError:
                    differing.append(entry["path"])
            if differing:
                self.conflicts.append(f"作业目录 {jid}：这里已有，{len(differing)} 个文件不同或缺少（如 {differing[0]}）")
            else:
                self.count("same", "作业目录")
        root = os.path.join(self.target["data_root"], "_research", "attachments")
        for name, entry in sorted(attachments.items()):
            path = os.path.join(root, name)
            if not os.path.lexists(path):
                self.new_attachments.append(name)
                self.count("new", "附件文件")
            elif os.path.islink(path) or file_sha(path) != (entry["size"], entry["sha256"]):
                self.conflicts.append(f"附件 {name}：这里已有，内容不同")
            else:
                self.count("same", "附件文件")

    # References that will point at nothing here; listed, the import goes ahead.
    def _references(self):
        source, here = self.remap.source, self.remap.target
        objects = self.known["objects"] | {(r["kind"], r["id"]) for r in self.tables["research/objects"]}
        jobs = {name.split("/")[1] for name in self.files if name.startswith("jobs/")} | {r["job_id"] for r in self.tables["queue/jobs"]}
        if os.path.isdir(self.target["jobs_dir"]):
            jobs |= {n for n in os.listdir(self.target["jobs_dir"]) if fmt.JOB.match(n)}
        db = connect(self.target["db"]["results"])
        try:
            results = {r[0] for r in db.execute("SELECT result_id FROM results")} | set(self.remap.results)
        finally:
            db.close()
        root = os.path.join(self.target["data_root"], "_research", "attachments")
        attachments = ({n for n in os.listdir(root) if fmt.ATTACHMENT.match(n)} if os.path.isdir(root) else set()) | \
            {name.split("/")[1] for name in self.files if name.startswith("attachments/") and not name.endswith(".json")}
        seen = set()

        def note(text):
            if text not in seen:
                seen.add(text)
                self.dangling.append(text)
        latest = {}
        for row in self.tables["research/revisions"]:
            if row["revision"] >= latest.get((row["kind"], row["id"]), {"revision": 0})["revision"]:
                latest[(row["kind"], row["id"])] = row
        for (kind, oid), row in sorted(latest.items()):
            data = json.loads(row["data"])
            if kind == "research" and data.get("follows") and ("research", data["follows"]) not in objects:
                note(f"任务 {oid} 接续的任务 {data['follows']} 不在这里，也不在这次导入里")
            based = data.get("based_on") or {}
            if kind == "direction" and based.get("direction_id") and ("direction", based["direction_id"]) not in objects:
                note(f"方向 {oid} 基于的方向 {based['direction_id']} 不在这里，也不在这次导入里")
            for dep in data.get("dependencies") or []:
                if (dep.get("kind"), dep.get("target_id")) not in objects:
                    note(f"方向 {oid} 依赖的 {dep.get('kind')}/{dep.get('target_id')} 不在这里，也不在这次导入里")
            for ref in data.get("evidence_refs") or []:
                if ref.get("node_id") not in (source, here):
                    continue
                if ref.get("kind") == "job" and ref.get("job_id") not in jobs:
                    note(f"{kind}/{oid} 引用的作业 {ref.get('job_id')} 不在这里，也不在这次导入里")
                elif ref.get("kind") in ("result", "result_source") and ref.get("result_id") not in results:
                    note(f"{kind}/{oid} 引用的结果 {ref.get('result_id')} 不在这里，也不在这次导入里")
                elif ref.get("kind") == "attachment" and ref.get("attachment_id") not in attachments:
                    note(f"{kind}/{oid} 引用的附件 {ref.get('attachment_id')} 不在这里，也不在这次导入里")
        for row in self.tables["results/results"]:
            value = json.loads(row["manifest"])
            for job in {value.get("job_id"), *(s.get("job_id") for s in value.get("sources", []) if s.get("kind") == "job_output")} - {None}:
                if job not in jobs:
                    note(f"结果 {row['result_id']} 引用的作业 {job} 不在这里，也不在这次导入里")

    def report(self, applied=False, backup=None):
        sources = self.manifest.get("result_sources") or {}
        unfinished = [j for t in self.manifest["tasks"] + [self.manifest.get("unassigned") or {}] for j in t.get("unfinished_jobs", [])]
        missing = [m for t in self.manifest["tasks"] + [self.manifest.get("unassigned") or {}] for m in t.get("missing_originals", [])]
        found = node_service_roots()
        services = [pid for pid, root in found if root == self.target["data_root"] or root is None]
        return {"zip": self.zip_path, "applied": applied, "backup": backup,
                "source": self.manifest["source"], "target": {k: self.target[k] for k in ("data_root", "node_id", "jobs_dir")},
                "node_id_rewritten": None if self.remap.source == self.remap.target else {
                    "from": self.remap.source, "to": self.remap.target, "replaced": self.replaced},
                "tasks": [{k: t.get(k) for k in ("id", "title", "state", "owner", "follows")} | {
                    "jobs": len(t["jobs"]), "results": len(t["results"]), "attachments": len(t["attachments"])}
                    for t in self.manifest["tasks"]],
                "unassigned": None if not self.manifest.get("unassigned") else {k: len(v) for k, v in self.manifest["unassigned"].items() if k in ("jobs", "results", "attachments")},
                "new": self.new, "same": self.same, "conflicts": self.conflicts, "dangling": self.dangling,
                "unfinished_jobs": unfinished, "missing_originals": missing, "notes": self.notes,
                "rebound_sources": len(sources.get("verified", [])), "unverified_sources": sources.get("unverified", []),
                "excluded": self.manifest.get("excluded", []), "running_services": services,
                "pause_agents": PAUSE_NOTE if any(k.startswith("research/") for k in self.new) else None}


# ── applying it ──
def extract(archive, entry, path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    digest = hashlib.sha256()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, entry.get("mode", 0o600) & 0o777)
    with os.fdopen(fd, "wb") as out, archive.open(entry["path"]) as stream:
        for block in iter(lambda: stream.read(fmt.CHUNK), b""):
            out.write(block)
            digest.update(block)
        out.flush()
        os.fsync(out.fileno())
    if digest.hexdigest() != entry["sha256"]:
        raise OSError(f"{entry['path']} 解出来的内容和 manifest 不一致")
    if entry.get("mtime_ns") is not None:
        os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]))


def insert(db, table, row):
    names = columns(db, table)
    extra = [k for k, v in row.items() if k not in names and v is not None]
    if extra:
        raise Refused(1, f"这个中心的 {table} 表比导出的旧，缺少列 {', '.join(extra)}；先把这里的平台升级到导出时的版本")
    keys = [k for k in row if k in names]
    db.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [row[k] for k in keys])


def rebind(plan, row):
    """The verified sources of a new result now stand for the imported files: their identity is read here."""
    verified = {item["source"]: item for item in (plan.manifest.get("result_sources") or {}).get("verified", [])
                if item["result_id"] == row["result_id"]}
    if not verified:
        return row
    observations = json.loads(row["observations"])
    for observation in observations:
        item = verified.get(observation["name"])
        if not item:
            continue
        parts = item["member"].split("/")
        path = (os.path.join(plan.target["jobs_dir"], parts[1], *parts[2:]) if parts[0] == "jobs"
                else os.path.join(plan.target["data_root"], "_research", "attachments", parts[1]))
        if file_sha(path) != (plan.files[item["member"]]["size"], item["sha256"]):
            raise OSError(f"结果 {row['result_id']} 的源 {observation['name']} 导入后内容不对")
        observation["stamp"] = list(stamp(os.stat(path, follow_symlinks=False)))
        observation["sha256"] = observation.get("sha256") or item["sha256"]
    return {**row, "observations": encoded(observations)}


def apply(plan):
    if plan.conflicts:
        raise Refused(3, "有冲突，整体中止，目标中心没有改动", plan.conflicts)
    services = node_service_roots()
    running = [pid for pid, root in services if root == plan.target["data_root"]]
    if running:
        raise Refused(4, f"中心服务正在这个数据根上运行（PID {', '.join(map(str, running))}）。先停掉再导入：{STOP_HELP}")
    unknown = [pid for pid, root in services if root is None]
    if unknown:
        pids = ", ".join(map(str, unknown))
        raise Refused(4, f"这台电脑上有中心服务（PID {pids}），判断不了它用的是不是这个数据根（它的 --phonefarm / --data-dir 是相对路径"
                         "或 ~ 开头，又读不到它的工作目录或它所属用户的家目录），所以没有导入。确认办法：ps -o user=,command= -p <PID> "
                         "看它的参数和用户，Linux 上 ls -l /proc/<PID>/cwd、macOS 上 lsof -a -d cwd -p <PID> 看它的工作目录。"
                         f"不管是不是，导入前都先停掉它：{STOP_HELP}")
    work = os.path.join(plan.target["data_root"], "_center_import", time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    os.makedirs(os.path.join(work, "backup"), mode=0o700)
    moved = []
    try:
        for jid in plan.new_jobs:
            os.makedirs(os.path.join(work, "jobs", jid), mode=0o700)
            for rel in plan.directories.get(jid, []):
                os.makedirs(os.path.join(work, "jobs", jid, *rel.split("/")), mode=0o700, exist_ok=True)
            for name, entry in plan.files.items():
                if name.startswith(f"jobs/{jid}/"):
                    extract(plan.archive, entry, os.path.join(work, "jobs", jid, *name.split("/")[2:]))
        for name in plan.new_attachments:
            extract(plan.archive, plan.files["attachments/" + name], os.path.join(work, "attachments", name))
        for kind, path in plan.target["db"].items():
            source, copy = connect(path), sqlite3.connect(os.path.join(work, "backup", kind + ".sqlite3"))
            try:
                source.backup(copy)
            finally:
                source.close()
                copy.close()
    except BaseException as error:
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(error, Exception):
            raise Refused(1, f"导入失败，目标中心没有改动：{error}") from error
        raise
    committed = []
    try:
        for jid in plan.new_jobs:
            os.rename(os.path.join(work, "jobs", jid), os.path.join(plan.target["jobs_dir"], jid))
            moved.append(os.path.join(plan.target["jobs_dir"], jid))
        if plan.new_attachments:
            root = os.path.join(plan.target["data_root"], "_research", "attachments")
            os.makedirs(root, mode=0o700, exist_ok=True)
            for name in plan.new_attachments:
                os.rename(os.path.join(work, "attachments", name), os.path.join(root, name))
                moved.append(os.path.join(root, name))
        for kind, tables in (("queue", ("jobs", "job_events", "job_targets")), ("results", ("results", "imports")),
                             ("research", ("events", "revisions", "operations", "objects", "dependencies"))):
            db = connect(plan.target["db"][kind])
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                if kind == "research":
                    for row in plan.updates.get("research/objects", []):
                        db.execute("UPDATE objects SET revision=?, updated_at=? WHERE kind=? AND id=?",
                                   (row["revision"], row["updated_at"], row["kind"], row["id"]))
                    for oid in plan.replace_dependencies:
                        db.execute("DELETE FROM dependencies WHERE direction_id=?", (oid,))
                    for sequence, bindings, observations in plan.renumbered:
                        db.execute("UPDATE revisions SET bindings=?, observations=? WHERE sequence=?", (bindings, observations, sequence))
                for table in tables:
                    for row in plan.inserts.get(f"{kind}/{table}", []):
                        insert(db, table, rebind(plan, row) if table == "results" else row)
                db.execute("COMMIT")
                committed.append(kind)
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()
    except BaseException as error:
        for kind in committed:
            copy, live = connect(os.path.join(work, "backup", kind + ".sqlite3")), connect(plan.target["db"][kind])
            try:
                copy.backup(live)
            finally:
                copy.close()
                live.close()
        for path in reversed(moved):
            shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(error, Refused):
            raise
        raise Refused(1, f"导入失败，已全部还原：{error}") from error
    backup = os.path.join(os.path.dirname(work), "backup-" + os.path.basename(work))
    os.rename(os.path.join(work, "backup"), backup)
    shutil.rmtree(work, ignore_errors=True)
    return plan.report(applied=True, backup=backup)


# ── the command ──
def text(report):
    lines = [f"导出文件：{report['zip']}",
             f"来源中心：{report['source'].get('hostname')}（{report['source'].get('node_id')}），导出于 "
             + time.strftime("%Y-%m-%d %H:%M", time.localtime(report["source"].get("exported_at") or 0)),
             f"目标中心：{report['target']['data_root']}（{report['target']['node_id']}），作业目录 {report['target']['jobs_dir']}"]
    if report["node_id_rewritten"]:
        swap = report["node_id_rewritten"]
        lines.append(f"node_id 对照：来源 {swap['from']} → 这里 {swap['to']}。新写入的行里，值正好是来源 node_id 的字符串换成这里的："
                     + "，".join(f"{name} {swap['replaced'][key]} 处" for key, name in (("research", "研究库"), ("results", "结果库"), ("queue", "作业库")))
                     + "（manifest 和作业目录里的文件不改）")
    lines.append(f"任务 {len(report['tasks'])} 个：")
    lines += [f"  {t['id']}  {t['title'] or ''}  作业 {t['jobs']} · 结果 {t['results']} · 附件 {t['attachments']}" for t in report["tasks"]]
    if report["unassigned"]:
        u = report["unassigned"]
        lines.append(f"未归属任务的数据：作业 {u['jobs']} · 结果 {u['results']} · 附件 {u['attachments']}")
    lines.append("新增：" + ("，".join(f"{NAMES.get(k, k)} {v}" for k, v in report["new"].items()) or "没有"))
    lines.append("已存在、跳过：" + ("，".join(f"{NAMES.get(k, k)} {v}" for k, v in report["same"].items()) or "没有"))
    for title, items in (("冲突（有冲突就整体中止）", report["conflicts"]), ("悬空的引用（照常导入）", report["dangling"]),
                         ("导出时还没结束的作业", report["notes"]),
                         ("未带原件的作业", [f"{m['job_id']}：{m['reason']}" for m in report["missing_originals"]]),
                         ("不会重新绑定的结果源（导入后不可用）", [f"{u['result_id']} / {u['source']}：{u['reason']}" for u in report["unverified_sources"]])):
        if items:
            lines.append(f"{title} {len(items)} 项：")
            lines += ["  " + item for item in items]
    lines.append(f"重新绑定的结果源：{report['rebound_sources']} 个")
    lines.append("这次导入不包含：" + "、".join(report["excluded"]))
    if report["pause_agents"]:
        lines.append(report["pause_agents"])
    if report["running_services"] and not report["applied"]:
        lines.append(f"注意：中心服务正在这个数据根上运行（PID {', '.join(map(str, report['running_services']))}），--apply 前要先停掉")
    if report["applied"]:
        lines.append(f"已导入。导入前的三个库备份在 {report['backup']}，确认无误后可以删掉")
    else:
        lines.append("只做了检查，没有写入。确认后加 --apply 导入" if not report["conflicts"] else "有冲突，--apply 也会被拒绝")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把数据导出页面生成的 zip 加进这台中心的数据。不加 --apply 只检查并打印计划，什么都不写；加了 --apply 先校验\n"
                    "全部文件的 SHA-256，有损坏或冲突就整体拒绝，写入失败整体还原。--apply 前要先停掉这台中心的服务。",
        epilog="导入的研究事件、结果和作业的序号接在本机已有的之后，时间保留原值。\n" + PAUSE_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip", help="数据导出页面生成的 zip")
    parser.add_argument("--data-root", required=True, help="目标中心的数据根：它的 --phonefarm 目录，或 --data-dir")
    parser.add_argument("--jobs-dir", choices=("_jobs", "jobs"), help="作业目录名；默认按数据根里已有的作业库判断（--phonefarm 是 _jobs，--data-dir 是 jobs）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加只检查并打印计划")
    parser.add_argument("--json", action="store_true", help="用 JSON 打印报告")
    args = parser.parse_args(argv)
    try:
        plan = Plan(args.zip, args.data_root, args.jobs_dir)
        report = apply(plan) if args.apply else plan.report()
    except Refused as refused:
        print(json.dumps({"refused": str(refused), "code": refused.code, "details": refused.details}, ensure_ascii=False, indent=1)
              if args.json else "\n".join([str(refused), *("  " + d for d in refused.details)]), file=sys.stderr)
        return refused.code
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else text(report))
    return 3 if report["conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
