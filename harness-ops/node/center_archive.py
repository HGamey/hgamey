"""The center export format, hgamey/center-export@1: one zip64 holding whole tasks of a center.

    manifest.json              the source center, every task (and the unassigned group) with what it holds, and
                               every other member with its size and SHA-256
    research/<table>.jsonl     objects, revisions, events, operations and dependencies, one row per line
    results/<table>.jsonl      registered results and the result imports they came from
    queue/<table>.jsonl        jobs, job_events and job_targets rows of the exported jobs
    jobs/<job id>/<path>       each job directory as it is on the center: meta, log, out, collected receipts
    attachments/<id>[.json]    research attachments and their metadata

Rows are written as they are in the database, column by column; bytes are {"$base64": ...}."""
import base64
import hashlib
import json
import re

SCHEMA = "hgamey/center-export@1"
MANIFEST = "manifest.json"
TABLES = {"research": ("objects", "revisions", "events", "operations", "dependencies"),
          "results": ("results", "imports"),
          "queue": ("jobs", "job_events", "job_targets")}
EXCLUDED = ["游戏资源缓存（可以在新中心重新准备）", "节点目录（从节点的加入记录）", "agent 账号和 token", "设备登记",
            "掌舵密码和查看密码", "平台配置", "历史作业目录（--history-roots 指向的旧作业）", "作业目录的导出缓存"]
JOB = re.compile(r"[0-9a-f]{12}\Z")
ATTACHMENT = re.compile(r"[0-9a-f]{32}\Z")
CHUNK = 1 << 20


def table_member(group, table):
    return f"{group}/{table}.jsonl"


def job_member(jid, path):
    return f"jobs/{jid}/{path}"


def attachment_member(aid, metadata=False):
    return f"attachments/{aid}" + (".json" if metadata else "")


def row_line(row):
    return json.dumps({key: {"$base64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value
                       for key, value in row.items()}, ensure_ascii=False, separators=(",", ":")) + "\n"


def line_row(line):
    row = json.loads(line)
    return {key: base64.b64decode(value["$base64"]) if isinstance(value, dict) and set(value) == {"$base64"} else value
            for key, value in row.items()}


def member(name):
    """A member name the importer accepts: a known table, or a plain relative path under jobs/<id>/ or
    attachments/. Anything else (absolute, '..', backslashes, empty parts) is refused."""
    if not isinstance(name, str) or len(name) > 4096 or "\\" in name or "\0" in name:
        raise ValueError(f"不认识的包内路径：{name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"不认识的包内路径：{name!r}")
    if name == MANIFEST or name in {table_member(g, t) for g, tables in TABLES.items() for t in tables}:
        return name
    if parts[0] == "jobs" and len(parts) >= 3 and JOB.match(parts[1]):
        return name
    if parts[0] == "attachments" and len(parts) == 2 and ATTACHMENT.match(parts[1].removesuffix(".json")):
        return name
    raise ValueError(f"不认识的包内路径：{name!r}")


def read_manifest(archive):
    try:
        value = json.loads(archive.read(MANIFEST))
    except KeyError:
        raise ValueError("包里没有 manifest.json，不是中心导出文件") from None
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"manifest 的 schema 不是 {SCHEMA}")
    for key in ("source", "tasks", "files"):
        if key not in value:
            raise ValueError(f"manifest 缺少 {key}")
    return value


def verify(archive, manifest, progress=None):
    """Every member is listed with its size and SHA-256 and nothing else is in the zip. Returns the problems."""
    listed, problems = {}, []
    for entry in manifest["files"]:
        try:
            listed[member(entry["path"])] = entry
        except (ValueError, KeyError, TypeError) as error:
            problems.append(str(error))
    names = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        names.add(info.filename)
        try:
            member(info.filename)
        except ValueError as error:
            problems.append(str(error))
            continue
        if info.filename != MANIFEST and info.filename not in listed:
            problems.append(f"{info.filename} 不在 manifest 的文件清单里")
    for name, entry in sorted(listed.items()):
        if name not in names:
            problems.append(f"{name} 在 manifest 里，包里却没有")
            continue
        digest, size = hashlib.sha256(), 0
        try:
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(CHUNK), b""):
                    digest.update(block)
                    size += len(block)
        except Exception as error:  # noqa: BLE001 - a damaged zip raises zlib, zipfile or OS errors alike
            problems.append(f"{name} 读不出来：{error}")
            continue
        if size != entry.get("size") or digest.hexdigest() != entry.get("sha256"):
            problems.append(f"{name} 的大小或 SHA-256 和 manifest 不一致")
        if progress:
            progress(size)
    return problems
