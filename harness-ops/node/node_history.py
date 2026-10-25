"""Read explicitly configured old job roots without starting their queue or owners."""
import json
import math
import os
import re
import stat
import threading

from artifact_io import SafeRoot, ArtifactChanged, describe_file, relative_path
from node_read import artifact_page, page_size, encode_cursor, decode_cursor


SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
JOB_ID = re.compile(r"[0-9a-f]{12}\Z")
MAX_REVISION = 2**53 - 1


class HistoryError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.payload = {"code": code, "error": message}


def load_history_config(path):
    if path is None:
        return []
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ValueError("history-roots must name an absolute configuration file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("history configuration must be a regular file")
        raw = source.read((64 << 10) + 1)
    if len(raw) > 64 << 10:
        raise ValueError("history configuration exceeds 64 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid history configuration JSON") from None
    if (not isinstance(value, dict) or set(value) != {"schema", "sources"}
            or value["schema"] != "hgamey/job-history@1"
            or not isinstance(value["sources"], list) or len(value["sources"]) > 16):
        raise ValueError("history configuration requires hgamey/job-history@1 and at most 16 sources")
    seen = set()
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise ValueError("history configuration must contain finite JSON and valid UTF-8") from None
    for entry in value["sources"]:
        if (not isinstance(entry, dict) or set(entry) != {"source_id", "revision", "label", "jobs_root"}
                or not isinstance(entry["source_id"], str) or not SOURCE_ID.fullmatch(entry["source_id"])
                or entry["source_id"] in seen
                or type(entry["revision"]) is not int or not 1 <= entry["revision"] <= MAX_REVISION
                or not isinstance(entry["label"], str) or not 1 <= len(entry["label"]) <= 160
                or not isinstance(entry["jobs_root"], str) or not os.path.isabs(entry["jobs_root"])
                or "\0" in entry["jobs_root"]):
            raise ValueError("invalid or duplicate history source")
        seen.add(entry["source_id"])
    return value["sources"]


def one(query, key, default=None):
    values = query.get(key, [default])
    if not isinstance(values, list) or len(values) != 1:
        raise HistoryError(400, "history_invalid_request", key + " must occur once")
    return values[0]


def query_fields(query, allowed):
    if set(query) - {*allowed, "source_revision", "expected_node_id"}:
        raise HistoryError(400, "history_invalid_request", "unsupported historical read parameter")
    for key in query:
        if not isinstance(one(query, key), str):
            raise HistoryError(400, "history_invalid_request", "query values must be strings")


def job_id(value):
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise HistoryError(400, "history_invalid_request", "job ID must be 12 lowercase hex characters")
    return value


class HistoricalRoot:
    def __init__(self, entry, current_jobs_root):
        self.entry = dict(entry)
        self.root = None
        self.reason = None
        if os.path.realpath(entry["jobs_root"]) == os.path.realpath(current_jobs_root):
            self.reason = "current_jobs_root: the active queue cannot be a historical source"
        else:
            try:
                self.root = SafeRoot(entry["jobs_root"])
            except (OSError, ValueError):
                self.reason = "historical root could not be opened; preserve the configured source"
        self.sha_cache, self.out_guard = {}, threading.Lock()

    def close(self):
        if self.root is not None:
            self.root.close()

    def public(self):
        return {**self.entry, "read_only": True,
                "availability": "available" if self.root is not None else "unavailable",
                "reason": self.reason}

    def record(self, jid):
        job_id(jid)
        value = self.root.read_json(jid + "/meta.json")
        if not isinstance(value, dict) or value.get("id") != jid:
            raise ValueError("invalid historical job metadata")
        # Original non-JSON numbers must not make the browser's entire list unreadable.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value

    def projection(self, jid, record):
        started = record.get("started")
        if (type(started) not in (int, float) or abs(started) > MAX_REVISION
                or not math.isfinite(started)):
            started = None
        code = record.get("exit")
        exit_source = "meta" if "exit" in record else None
        if "exit" not in record:
            try:
                code = int(self.root.read_bytes(jid + "/exitcode", 128).strip())
                exit_source = "exitcode"
            except (OSError, ValueError):
                code = None
        if type(code) is not int or not -(2**31) <= code < 2**31:
            code, exit_source = None, None
        def text(key, maximum=256):
            value = record.get(key)
            return value[:maximum] if isinstance(value, str) else None
        return {"id": jid, "label": text("label"), "bin": text("bin"), "started": started,
                "recorded_state": text("state", 64), "recorded_phase": text("phase", 64),
                "recorded_exit": code, "recorded_exit_source": exit_source,
                "observation": "historical_readonly"}


class JobHistory:
    def __init__(self, sources, current_jobs_root, node_id):
        self.node_id = node_id
        self.roots = {entry["source_id"]: HistoricalRoot(entry, current_jobs_root) for entry in sources}

    def close(self):
        for root in self.roots.values():
            root.close()

    def sources(self):
        return {"schema": "hgamey/job-sources@1", "node_id": self.node_id,
                "items": [root.public() for root in self.roots.values()]}

    def _source(self, source_id, query):
        source = self.roots.get(source_id)
        if source is None:
            raise HistoryError(404, "history_source_not_found", "historical source is not configured")
        revision = one(query, "source_revision")
        if (not isinstance(revision, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", revision)
                or int(revision) > MAX_REVISION):
            raise HistoryError(400, "history_invalid_request", "source_revision is required as a positive integer")
        if int(revision) != source.entry["revision"]:
            raise HistoryError(409, "history_source_changed", "historical source revision differs; keep the original reference and reselect explicitly")
        if source.root is None:
            raise HistoryError(503, "history_source_unavailable", source.reason)
        return source

    def _identity(self, source):
        return {"node_id": self.node_id, "source_id": source.entry["source_id"],
                "source_revision": source.entry["revision"]}

    @staticmethod
    def _record(source, jid):
        try:
            return source.record(jid)
        except HistoryError:
            raise
        except FileNotFoundError:
            raise HistoryError(404, "history_job_not_found", "historical job metadata is absent") from None
        except (OSError, ValueError, RecursionError):
            raise HistoryError(400, "history_invalid_record", "historical job metadata is unreadable or invalid") from None

    def list_jobs(self, source_id, query):
        query_fields(query, {"query", "limit", "cursor"})
        source = self._source(source_id, query)
        try:
            limit = page_size(query)
            search = one(query, "query", "").casefold()
            if len(search) > 1024:
                raise ValueError("query exceeds 1024 characters")
            prefix = [source_id, source.entry["revision"], search]
            after = None
            if one(query, "cursor") is not None:
                cursor = decode_cursor(one(query, "cursor"))
                if (not isinstance(cursor, list) or len(cursor) != 5 or cursor[:3] != prefix
                        or type(cursor[3]) not in (int, float) or not math.isfinite(cursor[3])
                        or not isinstance(cursor[4], str) or not JOB_ID.fullmatch(cursor[4])):
                    raise ValueError("cursor must belong to this source, revision and query")
                after = tuple(cursor[3:])
        except (ValueError, TypeError, RecursionError, OverflowError) as error:
            raise HistoryError(400, "history_invalid_request", str(error)) from None
        items, warnings, warning_count = [], [], 0
        try:
            names = os.listdir(source.root.fd)
        except OSError:
            raise HistoryError(503, "history_source_unavailable", "historical root could not be listed") from None
        for jid in names:
            if not JOB_ID.fullmatch(jid):
                continue
            try:
                record = source.record(jid)
                value = source.projection(jid, record)
                key = (value["started"] if value["started"] is not None else 0, jid)
                if after is not None and key >= after:
                    continue
                if search and search not in json.dumps({k: record.get(k) for k in ("id", "bin", "label", "argv")}, ensure_ascii=False).casefold():
                    continue
                items.append((key, value))
            except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                warning_count += 1
                if len(warnings) < 100:
                    warnings.append({"job": jid, "error": "metadata_unavailable"})
        items.sort(key=lambda pair: pair[0], reverse=True)
        page = items[:limit]
        cursor = encode_cursor([*prefix, *page[-1][0]]) if len(items) > limit else None
        return {"schema": "hgamey/historical-jobs@1", **self._identity(source),
                "items": [item for _, item in page], "next_cursor": cursor,
                "warnings": warnings, "warning_count": warning_count}

    def detail(self, source_id, jid, query):
        query_fields(query, set())
        source = self._source(source_id, query)
        record = self._record(source, jid)
        return {"schema": "hgamey/historical-job@1", **self._identity(source),
                **source.projection(jid, record), "record": record}

    def artifacts(self, source_id, jid, query):
        query_fields(query, {"path", "limit", "cursor"})
        source = self._source(source_id, query)
        self._record(source, jid)
        try:
            value = artifact_page(source.root, jid, query, cursor_scope=(source_id, source.entry["revision"]))
        except (OSError, ValueError, TypeError, RecursionError) as error:
            raise HistoryError(400, "history_artifact_unavailable", str(error)) from None
        return {**value, "schema": "hgamey/historical-artifacts@1", **self._identity(source)}

    def log_from(self, source_id, jid, query):
        query_fields(query, {"offset", "limit"})
        source = self._source(source_id, query)
        self._record(source, jid)
        try:
            offset, limit = int(one(query, "offset", "0")), int(one(query, "limit", str(4 << 20)))
            if not 0 <= offset <= (1 << 63) - 1 or not 1 <= limit <= 4 << 20:
                raise ValueError("invalid log offset or limit")
            with source.root.open_file(jid + "/log") as stream:
                stream.seek(offset)
                return stream.read(limit)
        except (OSError, ValueError) as error:
            raise HistoryError(400, "history_artifact_unavailable", str(error)) from None

    def out_blob(self, source_id, jid, query):
        query_fields(query, {"path", "meta", "offset", "length", "etag"})
        source = self._source(source_id, query)
        self._record(source, jid)
        fd = None
        try:
            rel = relative_path(one(query, "path", ""))
            if not rel:
                raise HistoryError(409, "history_directory_export_unsupported", "historical downloads require one explicit file path")
            fd = source.root.open_fd(jid + "/out/" + rel)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HistoryError(409, "history_directory_export_unsupported", "historical directories are not archived in their original root")
            stream, fd = os.fdopen(fd, "rb"), None
            try:
                info = describe_file(stream, source.sha_cache, source.out_guard)
                return stream, {"kind": "file", "path": os.path.normpath(rel), **info, **self._identity(source)}
            except BaseException:
                stream.close()
                raise
        except (HistoryError, ArtifactChanged):
            raise
        except (OSError, ValueError) as error:
            raise HistoryError(400, "history_artifact_unavailable", str(error)) from None
        finally:
            if fd is not None:
                os.close(fd)
