"""Explicit, immutable result metadata. Registration never starts a job or probes a device."""
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import quote

from artifact_io import SafeRoot, describe_file, stamp
from node_read import encode_cursor, decode_cursor


MAX_RECORD = 256 << 10
MAX_SOURCE = 1 << 20
MAX_IMPORT_BODY = 6 << 20
LABELS = ("game_id", "game_version", "device", "research_id", "direction_id")
OUTCOMES = {"gain", "no_gain", "regression", "failed", "not_run", "unknown"}


class ResultError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(value, maximum=64):
    if not isinstance(value, str) or value in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]{1," + str(maximum) + "}", value):
        raise ValueError("identifier requires 1 to %d letters, digits, dots, underscores or hyphens" % maximum)
    return value


def text(value, maximum=1024, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError("invalid or oversized text field")
    return value


def object_fields(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("object contains unsupported fields")
    return value


def label_values(value):
    value = object_fields({} if value is None else value, LABELS)
    return {key: text(value.get(key), 128) for key in LABELS}


def job_id(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{12}", value)):
        raise ValueError("job_id must be a local 12-character identity or null")
    return value


def result_query(query):
    allowed = set(LABELS) | {"job_id", "outcome", "limit", "cursor", "expected_node_id"}
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise ValueError("unsupported or repeated result filter")
    filters = {k: query[k][0] for k in (*LABELS, "job_id", "outcome") if query.get(k) and query[k][0]}
    for key, val in filters.items():
        text(val, 128, True)
        if key == "job_id":
            job_id(val)
        if key == "outcome" and val not in OUTCOMES:
            raise ValueError("unsupported reported outcome filter")
    limit = int(query.get("limit", [20])[0])
    if not 1 <= limit <= 100:
        raise ValueError("result limit must be 1 to 100")
    cursor = query.get("cursor", [None])[0]
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
        raise ValueError("invalid result cursor")
    return filters, limit, cursor


def pointer(document, path):
    if not isinstance(path, str) or len(path) > 2048 or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise ValueError("invalid JSON pointer")
    value = document
    for part in ([] if path == "" else path[1:].split("/")):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise ValueError("JSON pointer does not select an array index")
            try:
                value = value[int(part)]
            except (IndexError, ValueError):
                raise ValueError("JSON pointer is absent") from None
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ValueError("JSON pointer is absent")
    return value


def manifest(value, internal=False):
    value = copy.deepcopy(object_fields(value, {"schema", "result_id", "title", "job_id", "labels", "origin",
        "outcome", "source_outcome", "sources", "metrics", "claims", "limitations", "supersedes"}))
    if value.get("schema") != "phonefarm/result@1":
        raise ValueError("expected phonefarm/result@1")
    value["result_id"] = identifier(value.get("result_id"))
    value["title"] = text(value.get("title"), 240, True)
    value["job_id"] = job_id(value.get("job_id"))
    value["labels"] = label_values(value.get("labels"))
    origin = object_fields(value.get("origin") or {}, {"producer", "producer_version", "evidence_origin", "observed_at"})
    evidence = origin.get("evidence_origin", "unknown")
    observed = origin.get("observed_at")
    if evidence not in {"device", "synthetic", "unknown"} or (observed is not None and
            (type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0)):
        raise ValueError("invalid reported evidence origin or observation time")
    value["origin"] = {"producer": text(origin.get("producer"), 128), "producer_version": text(origin.get("producer_version"), 128),
                       "evidence_origin": evidence, "observed_at": observed}
    value.setdefault("outcome", "unknown")
    if value["outcome"] not in OUTCOMES:
        raise ValueError("unsupported reported outcome")
    value["source_outcome"] = text(value.get("source_outcome"), 512)
    value["supersedes"] = identifier(value["supersedes"]) if value.get("supersedes") is not None else None
    for key in ("claims", "limitations"):
        items = value.setdefault(key, [])
        if not isinstance(items, list) or len(items) > 64:
            raise ValueError(key + " must contain at most 64 strings")
        value[key] = [text(item, 4096, True) for item in items]
    sources = value.setdefault("sources", [])
    if not isinstance(sources, list) or len(sources) > 64:
        raise ValueError("at most 64 sources are supported")
    names = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        name = identifier(source.get("name"))
        if name in names:
            raise ValueError("source names must be unique")
        names.add(name)
        kind = source.get("kind")
        if kind == "job_output":
            object_fields(source, {"name", "kind", "job_id", "path"})
            if job_id(source.get("job_id")) is None:
                raise ValueError("job output requires a job_id")
            path = text(source.get("path"), 2048, True)
            if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
                raise ValueError("source path must be relative to job/out")
        elif kind == "external_reference":
            object_fields(source, {"name", "kind", "locator", "declared_size", "declared_sha256"})
            source["locator"] = text(source.get("locator"), 4096, True)
            size, digest = source.get("declared_size"), source.get("declared_sha256")
            if size is not None and (type(size) is not int or size < 0):
                raise ValueError("invalid declared source size")
            if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("invalid declared source hash")
        elif internal and kind == "imported_json":
            object_fields(source, {"name", "kind", "import_id"})
            identifier(source.get("import_id"), 48)
        else:
            raise ValueError("unsupported source kind")
    metrics = value.setdefault("metrics", [])
    if not isinstance(metrics, list) or len(metrics) > 256:
        raise ValueError("at most 256 metrics are supported")
    fields = {"name", "condition", "value", "unit", "scope", "method", "window", "denominator", "power_rail",
              "image_domain", "region", "source", "unavailable_reason"}
    for metric in metrics:
        object_fields(metric, fields)
        metric["name"] = text(metric.get("name"), 128, True)
        number = metric.setdefault("value", None)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number)):
            raise ValueError("metric value must be a finite number or null")
        for key in fields - {"name", "value", "source", "scope", "method", "region"}:
            metric[key] = text(metric.get(key), 2048)
        metric.setdefault("scope", "unknown")
        metric.setdefault("method", "unknown")
        metric.setdefault("region", None)
        if metric["scope"] not in {"module", "game_loop", "display", "gpu_submit_proxy", "power", "image", "unknown"}:
            raise ValueError("unsupported metric scope")
        if metric["method"] not in {"measured", "derived", "estimated", "reported", "unknown"}:
            raise ValueError("unsupported metric method")
        if metric["region"] not in {None, "full_frame", "roi", "unknown"}:
            raise ValueError("unsupported image region")
        reference = metric.setdefault("source", None)
        if reference is not None:
            object_fields(reference, {"name", "pointer"})
            if reference.get("name") not in names:
                raise ValueError("metric source must name a declared source")
            path = reference.get("pointer")
            if not isinstance(path, str) or len(path) > 2048 or (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
                raise ValueError("invalid metric source pointer")
    if len(encoded(value).encode()) > MAX_RECORD:
        raise ResultError(413, "result_too_large", "result metadata exceeds 256 KiB")
    return value


class NodeResults:
    def __init__(self, jobs, node_id):
        self.jobs, self.node_id = jobs, node_id
        root = SafeRoot(jobs.data_root)
        try:
            root.mkdir("_results")
        finally:
            root.close()
        self.path = os.path.join(jobs.data_root, "_results", "index.sqlite3")
        with self._db(True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS results (
                sequence INTEGER PRIMARY KEY, result_id TEXT NOT NULL UNIQUE, registered_at REAL NOT NULL,
                title TEXT NOT NULL, job_id TEXT, game_id TEXT, game_version TEXT, device TEXT, research_id TEXT,
                direction_id TEXT, outcome TEXT NOT NULL, manifest TEXT NOT NULL, provenance TEXT NOT NULL,
                observations TEXT NOT NULL, metric_evidence TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS imports (
                import_id TEXT PRIMARY KEY, request TEXT NOT NULL, content BLOB NOT NULL,
                receipt TEXT NOT NULL)""")

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _find(db, result_id):
        return db.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()

    def _job_exists(self, jid):
        if jid is None or self.jobs.dispatch.queue.get(jid):
            return
        try:
            fd = self.jobs._read_root.open_fd(jid, directory=True)
            os.close(fd)
        except (OSError, ValueError):
            raise ResultError(404, "result_job_not_found", "local result job does not exist; use an external reference for historical IDs") from None

    def _observe(self, value, imported=None):
        observed, documents, budget = [], {}, 0
        needed = {m["source"]["name"] for m in value["metrics"] if m["source"]}
        for source in value["sources"]:
            name, kind = source["name"], source["kind"]
            row = {"name": name, "kind": kind, "status": "unavailable", "size": None, "sha256": None, "reason": None}
            if kind == "external_reference":
                row.update(status="external", reason="historical locator is not bound to a downloadable local source")
            elif kind == "imported_json":
                if not imported or source["import_id"] != imported["import_id"]:
                    raise ValueError("adapter referenced a different import")
                row.update(status="available", size=imported["size"], sha256=imported["sha256"])
                documents[name] = imported["document"]
            else:
                try:
                    with self.jobs._read_root.open_file(source["job_id"] + "/out/" + source["path"]) as stream:
                        before = os.fstat(stream.fileno())
                        row.update(status="available", size=before.st_size, stamp=list(stamp(before)))
                        if name in needed:
                            if before.st_size > MAX_SOURCE or budget + before.st_size > 4 * MAX_SOURCE:
                                row["reason"] = "source exceeds bounded JSON inspection; declared metrics remain unverified"
                            else:
                                raw = stream.read(MAX_SOURCE + 1)
                                if list(stamp(os.fstat(stream.fileno()))) != row["stamp"] or len(raw) != before.st_size:
                                    raise ResultError(409, "result_source_changed", "source changed during explicit JSON inspection")
                                budget += len(raw)
                                row["sha256"] = hashlib.sha256(raw).hexdigest()
                                try:
                                    documents[name] = json.loads(raw)
                                except (ValueError, UnicodeError):
                                    row["reason"] = "source is not a readable JSON document; metrics remain unverified"
                                with self.jobs._out_guard:
                                    if len(self.jobs._sha_cache) >= 2048:
                                        self.jobs._sha_cache.clear()
                                    self.jobs._sha_cache[tuple(row["stamp"])] = row["sha256"]
                except ResultError:
                    raise
                except (OSError, ValueError):
                    row.update(status="unavailable", reason="source is missing, unreadable or outside the supported artifact boundary")
            observed.append(row)
        evidence = []
        for index, metric in enumerate(value["metrics"]):
            ref = metric["source"]
            item = {"index": index, "status": "unverified", "source_name": ref["name"] if ref else None,
                    "pointer": ref["pointer"] if ref else None, "reason": "no readable bounded source binding"}
            if ref and ref["name"] in documents:
                try:
                    actual = pointer(documents[ref["name"]], ref["pointer"])
                except ValueError as error:
                    raise ResultError(400, "result_pointer_invalid", str(error)) from None
                if (actual is not None and type(actual) not in (int, float)) or actual != metric["value"]:
                    raise ResultError(409, "result_value_mismatch", "declared metric differs from its explicit source pointer")
                item.update(status="matched_source", reason="declared value matches source bytes; experimental validity is not inferred")
            evidence.append(item)
        return observed, evidence

    def _insert(self, db, value, provenance, observations, evidence, now):
        old = self._find(db, value["result_id"])
        if old:
            if old["manifest"] != encoded(value) or old["provenance"] != encoded(provenance):
                raise ResultError(409, "result_conflict", "result ID already has different immutable content")
            return
        if value["supersedes"] and not self._find(db, value["supersedes"]):
            raise ResultError(404, "result_not_found", "superseded result is absent on this node")
        db.execute("""INSERT INTO results (result_id,registered_at,title,job_id,game_id,game_version,device,research_id,
            direction_id,outcome,manifest,provenance,observations,metric_evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (value["result_id"], now, value["title"], value["job_id"], *(value["labels"][k] for k in LABELS), value["outcome"],
             encoded(value), encoded(provenance), encoded(observations), encoded(evidence)))

    def register(self, payload):
        value = manifest(payload)
        provenance = {"kind": "registration", "import_id": None}
        with self._db() as db:
            old = self._find(db, value["result_id"])
        if old:
            if old["manifest"] != encoded(value) or old["provenance"] != encoded(provenance):
                raise ResultError(409, "result_conflict", "result ID already has different immutable content")
            return self.get(value["result_id"])
        self._job_exists(value["job_id"])
        observations, evidence = self._observe(value)
        with self._db(True) as db:
            self._insert(db, value, provenance, observations, evidence, time.time())
        return self.get(value["result_id"])

    def _row(self, result_id):
        with self._db() as db:
            row = self._find(db, identifier(result_id))
        if not row:
            raise ResultError(404, "result_not_found", "no registered result with this ID")
        return row

    def list(self, query):
        filters, limit, page_cursor = result_query(query)
        after = None
        with self._db() as db:
            upper = db.execute("SELECT COALESCE(MAX(sequence),0) FROM results").fetchone()[0]
            if page_cursor is not None:
                cursor = decode_cursor(page_cursor)
                if (not isinstance(cursor, list) or len(cursor) != 4 or cursor[:2] != [self.node_id, filters]
                        or any(type(n) is not int or not 0 <= n <= (1 << 63) - 1 for n in cursor[2:])):
                    raise ValueError("result cursor belongs to another node or filter")
                upper, after = cursor[2:]
            terms, params = ["sequence<=?"], [upper]
            if after is not None:
                terms.append("sequence<?")
                params.append(after)
            for key, val in filters.items():
                terms.append(key + "=?")
                params.append(val)
            rows = db.execute("SELECT * FROM results WHERE " + " AND ".join(terms) + " ORDER BY sequence DESC LIMIT ?",
                              (*params, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            value = json.loads(row["manifest"])
            items.append({"result_id": row["result_id"], "title": row["title"], "job_id": row["job_id"],
                          "labels": value["labels"], "outcome": row["outcome"], "registered_at": row["registered_at"],
                          "provenance": json.loads(row["provenance"]), "metric_count": len(value["metrics"]),
                          "source_count": len(value["sources"])})
        cursor = encode_cursor([self.node_id, filters, upper, rows[limit - 1]["sequence"]]) if len(rows) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": items, "next_cursor": cursor}

    def _current_source(self, rid, source, observation):
        item = {"name": source["name"], "status": observation["status"], "reason": observation["reason"], "url": None}
        if source["kind"] == "imported_json":
            item.update(status="available", reason=None)
        elif source["kind"] == "job_output":
            try:
                with self.jobs._read_root.open_file(source["job_id"] + "/out/" + source["path"]) as stream:
                    same = list(stamp(os.fstat(stream.fileno()))) == observation.get("stamp")
                item.update(status="available" if same else "changed", reason=None if same else "source no longer matches its registration observation")
            except (OSError, ValueError):
                item.update(status="unavailable", reason="source is no longer readable through the registered path")
        if item["status"] == "available":
            item["url"] = "/results/" + quote(rid, safe="") + "/sources/" + quote(source["name"], safe="")
        return item

    def get(self, rid):
        row = self._row(rid)
        value, observations = json.loads(row["manifest"]), json.loads(row["observations"])
        current_job = None
        if value["job_id"]:
            current_job = {"job_id": value["job_id"], "status": "unavailable", "record": None, "reason": None}
            try:
                current_job.update(status="available", record=self.jobs.status(value["job_id"]))
            except (OSError, ValueError, KeyError, TypeError):
                current_job["reason"] = "job metadata is currently unavailable; the immutable result remains registered"
        sources = [self._current_source(rid, source, observation) for source, observation in zip(value["sources"], observations)]
        return {"schema_version": 1, "node_id": self.node_id, "result_id": rid, "sequence": row["sequence"],
                "registered_at": row["registered_at"], "manifest": value, "provenance": json.loads(row["provenance"]),
                "source_observations": [{k: v for k, v in item.items() if k != "stamp"} for item in observations],
                "metric_evidence": json.loads(row["metric_evidence"]), "current": {"job": current_job, "sources": sources}}

    def import_report(self, payload):
        from result_adapters import convert

        object_fields(payload, {"schema", "import_id", "adapter", "filename", "content", "labels"})
        if payload.get("schema") != "phonefarm/result-import@1":
            raise ValueError("expected phonefarm/result-import@1")
        iid, adapter = identifier(payload.get("import_id"), 48), text(payload.get("adapter"), 64, True)
        filename = text(payload.get("filename"), 240, True)
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be the original UTF-8 JSON text")
        raw = content.encode("utf-8")
        if len(raw) > MAX_SOURCE:
            raise ResultError(413, "result_import_too_large", "import source exceeds 1 MiB")
        labels = label_values(payload.get("labels"))
        request = encoded({"adapter": adapter, "filename": filename, "labels": labels})
        with self._db() as db:
            old = db.execute("SELECT * FROM imports WHERE import_id=?", (iid,)).fetchone()
        if old:
            if old["request"] != request or old["content"] != raw:
                raise ResultError(409, "result_import_conflict", "import ID already has different immutable content")
            return json.loads(old["receipt"])
        document = json.loads(raw)
        values = [manifest(v, internal=True) for v in convert(adapter, iid, document, labels)]
        if not values or len(values) > 257 or len({v["result_id"] for v in values}) != len(values):
            raise ValueError("adapter returned an empty, oversized or duplicate result collection")
        digest, now = hashlib.sha256(raw).hexdigest(), time.time()
        imported = {"import_id": iid, "document": document, "size": len(raw), "sha256": digest}
        entries = [(value, *self._observe(value, imported)) for value in values]
        receipt = {"schema_version": 1, "node_id": self.node_id, "import_id": iid, "adapter": adapter, "filename": filename,
                   "size": len(raw), "sha256": digest, "registered_at": now, "result_ids": [v["result_id"] for v in values],
                   "source_url": "/result-imports/" + quote(iid, safe="") + "/source"}
        with self._db(True) as db:
            old = db.execute("SELECT * FROM imports WHERE import_id=?", (iid,)).fetchone()
            if old:
                if old["request"] != request or old["content"] != raw:
                    raise ResultError(409, "result_import_conflict", "import ID already has different immutable content")
                return json.loads(old["receipt"])
            provenance = {"kind": "import", "import_id": iid, "adapter": adapter}
            for value, observations, evidence in entries:
                self._insert(db, value, provenance, observations, evidence, now)
            db.execute("INSERT INTO imports VALUES (?,?,?,?)", (iid, request, raw, encoded(receipt)))
        return receipt

    def imported(self, iid, source=False):
        with self._db() as db:
            row = db.execute("SELECT * FROM imports WHERE import_id=?", (identifier(iid, 48),)).fetchone()
        if not row:
            raise ResultError(404, "result_import_not_found", "no completed import with this ID")
        receipt = json.loads(row["receipt"])
        return (row["content"], {"size": receipt["size"], "sha256": receipt["sha256"], "kind": "json"}) if source else receipt

    def source(self, rid, name):
        row = self._row(rid)
        sources, observations = json.loads(row["manifest"])["sources"], json.loads(row["observations"])
        pair = next(((s, o) for s, o in zip(sources, observations) if s["name"] == name), None)
        if pair is None:
            raise ResultError(404, "result_source_not_found", "no named source on this result")
        source, observation = pair
        if source["kind"] == "imported_json":
            return self.imported(source["import_id"], source=True)
        if source["kind"] != "job_output" or not observation.get("stamp"):
            raise ResultError(409, "result_source_unbound", "source was not bound to readable local bytes at registration")
        try:
            stream = self.jobs._read_root.open_file(source["job_id"] + "/out/" + source["path"])
        except (OSError, ValueError):
            raise ResultError(404, "result_source_unavailable", "registered source is missing or unreadable") from None
        try:
            if list(stamp(os.fstat(stream.fileno()))) != observation["stamp"]:
                raise ResultError(409, "result_source_changed", "source changed after registration; old result was not rewritten")
            info = describe_file(stream, self.jobs._sha_cache, self.jobs._out_guard)
            if list(stream.artifact_stamp) != observation["stamp"]:
                raise ResultError(409, "result_source_changed", "source changed before the download snapshot was prepared")
            if observation.get("sha256") and info["sha256"] != observation["sha256"]:
                raise ResultError(409, "result_source_changed", "source bytes no longer match the registered JSON")
            return stream, {"kind": "file", **info}
        except BaseException:
            stream.close()
            raise
