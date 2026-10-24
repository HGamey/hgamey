"""Versioned research metadata and asynchronous feedback, independent of execution."""
from contextlib import contextmanager
import copy
import json
import math
import os
import re
import sqlite3
import time
from urllib.parse import quote, urlencode

from artifact_io import SafeRoot
from node_read import encode_cursor, decode_cursor
from node_results import encoded, identifier, object_fields, text
from node_media import LIMITS, MediaError

MAX_DATA = 64 << 10
MAX_WRITE = 128 << 10
STATES = {
    "research": {"active", "idle", "paused", "closed"},
    "direction": {"proposed", "preparing", "ready", "running", "waiting_device", "waiting_human",
                  "waiting_material", "awaiting_evaluation", "verified", "no_gain", "rejected", "deferred"},
    "request": {"open", "closed"}, "feedback": set(),
}


class ResearchError(ValueError):
    def __init__(self, status, code, message, **facts):
        super().__init__(message)
        self.status = status
        self.payload = {"code": code, "error": message, **facts}


def positive(value):
    if type(value) is not int or not 1 <= value <= (1 << 63) - 1:
        raise ValueError("revision must be a positive integer")
    return value


def array(value):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("metadata arrays must contain at most 64 items")
    return value


def strings(value):
    return [text(item, 16384, True) for item in array(value)]


def refs(value):
    for ref in array(value):
        if not isinstance(ref, dict):
            raise ValueError("evidence reference must be an object")
        kind = ref.get("kind")
        fields = {"job": {"job_id"}, "result": {"result_id"},
                  "result_source": {"result_id", "source_name"}, "external": {"locator"}}
        if kind not in fields:
            raise ValueError("unsupported evidence reference kind")
        object_fields(ref, fields[kind] | {"kind", "node_id"})
        nid = ref.get("node_id")
        if not (kind == "external" and nid is None) and (not isinstance(nid, str) or not re.fullmatch(r"[0-9a-f]{32}", nid)):
            raise ValueError("evidence reference requires an explicit node identity")
        ref["node_id"] = nid
        if kind == "external":
            ref["locator"] = text(ref.get("locator"), 4096, True)
        elif kind == "job":
            if not isinstance(ref.get("job_id"), str) or not re.fullmatch(r"[0-9a-f]{12}", ref["job_id"]):
                raise ValueError("evidence job identity must have 12 hexadecimal characters")
        else:
            identifier(ref.get("result_id"))
            if kind == "result_source":
                identifier(ref.get("source_name"))
    return value


def unique(items, key):
    if len({item[key] for item in items}) != len(items):
        raise ValueError("duplicate metadata item identity")


def evaluation(kind, value, evidence_refs):
    if kind == "request":
        object_fields(value, {"schema", "media_kind", "slots"})
        if value.get("schema") != "phonefarm/ab-evaluation@1" or value.get("media_kind") not in LIMITS:
            raise ValueError("evaluation requires phonefarm/ab-evaluation@1 and image/video")
        slots = object_fields(value.get("slots"), {"A", "B"})
        if set(slots) != {"A", "B"} or any(type(i) is not int or not 0 <= i < len(evidence_refs) for i in slots.values()):
            raise ValueError("evaluation slots must select two evidence reference indices")
        if slots["A"] == slots["B"] or any(evidence_refs[i]["kind"] != "result_source" for i in slots.values()):
            raise ValueError("evaluation slots must be distinct registered source reference indices")
    else:
        fields = {"schema", "preference", "difference", "reviewed", "client_display", "identity_revealed"}
        object_fields(value, fields)
        if set(value) != fields or value.get("schema") != "phonefarm/ab-feedback@1":
            raise ValueError("evaluation feedback requires all phonefarm/ab-feedback@1 fields")
        if value["preference"] not in {"A", "B", "no_preference", "unable_to_judge", None}:
            raise ValueError("unsupported subjective preference")
        if value["difference"] not in {"none", "small", "moderate", "large", "unknown", None}:
            raise ValueError("unsupported subjective difference")
        reviewed = object_fields(value["reviewed"], {"A", "B"})
        display = object_fields(value["client_display"], {"A", "B"})
        if set(reviewed) != {"A", "B"} or any(v is not None and type(v) is not bool for v in reviewed.values()):
            raise ValueError("reviewed requires A/B boolean or null declarations")
        if set(display) != {"A", "B"} or any(v not in {"decoded", "failed", "not_loaded", "unknown"} for v in display.values()):
            raise ValueError("client_display requires A/B display observations")
        if value["identity_revealed"] is not None and type(value["identity_revealed"]) is not bool:
            raise ValueError("identity_revealed must be a boolean or null declaration")
        if value["preference"] in {"A", "B", "no_preference"}:
            if not all(v is True for v in reviewed.values()):
                raise ValueError("a subjective preference requires both reviewed declarations")
        elif value["difference"] not in {"unknown", None}:
            raise ValueError("unjudged media must retain unknown/null difference")
    return value


def normalize(kind, data):
    common = {"evidence_refs"} | ({"evaluation"} if kind in ("request", "feedback") else set())
    fields = {
        "research": {"title", "objective", "game", "constraints", "budgets", "device_requirements", "state", "state_reason", "coverage"},
        "direction": {"research_id", "title", "hypothesis", "priority", "priority_reason", "prerequisites", "cost_expectation",
                      "expected_benefit", "next_experiment", "state", "state_reason", "dependencies", "feedback_decisions"},
        "request": {"research_id", "type", "title", "need", "why", "resume_hint", "context", "supersedes", "state", "close_reason", "due_at"},
        "feedback": {"request_id", "request_revision", "text", "supersedes"},
    }
    if kind not in fields:
        raise ValueError("unsupported research object kind")
    value = copy.deepcopy(object_fields(data, fields[kind] | common))
    value["evidence_refs"] = refs(value.get("evidence_refs", []))
    if kind != "feedback":
        value["title"] = text(value.get("title"), 240, True)
        value.setdefault("state", {"research": "active", "direction": "proposed", "request": "open"}[kind])
        if value["state"] not in STATES[kind]:
            raise ValueError("unsupported declared object state")
    if kind in ("direction", "request"):
        identifier(value.get("research_id"))
    if kind in ("research", "direction"):
        value["state_reason"] = text(value.get("state_reason"), 16384)
        needs_reason = (kind == "research" and value["state"] != "active" or kind == "direction"
                        and value["state"] not in {"proposed", "preparing", "ready", "running"})
        if needs_reason and value["state_reason"] is None:
            raise ValueError("this declared state requires a reason")
    if kind == "research":
        value["objective"] = text(value.get("objective"), 16384, True)
        value.setdefault("game", None)
        if value["game"] is not None:
            game = object_fields(value["game"], {"game_id", "game_version"})
            value["game"] = {key: text(game.get(key), 128) for key in ("game_id", "game_version")}
        for key in ("constraints", "device_requirements"):
            value[key] = strings(value.get(key, []))
        value["budgets"] = array(value.get("budgets", []))
        for budget in value["budgets"]:
            object_fields(budget, {"name", "limit", "unit"})
            budget["name"] = text(budget.get("name"), 128, True)
            budget["unit"] = text(budget.get("unit"), 128, True)
            number = budget.get("limit")
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ValueError("budget limit must be a finite nonnegative number")
        value["coverage"] = array(value.get("coverage", []))
        for item in value["coverage"]:
            object_fields(item, {"key", "disposition", "reason"})
            identifier(item.get("key"))
            if item.get("disposition") not in {"planned", "in_progress", "not_applicable", "blocked", "rejected", "evidence_available"}:
                raise ValueError("unsupported coverage disposition")
            item["reason"] = text(item.get("reason"), 16384)
        unique(value["coverage"], "key")
    elif kind == "direction":
        value["hypothesis"] = text(value.get("hypothesis"), 16384, True)
        for key in ("priority_reason", "cost_expectation", "expected_benefit"):
            value[key] = text(value.get(key), 16384)
        value.setdefault("priority", None)
        if value["priority"] is not None and (type(value["priority"]) is not int or abs(value["priority"]) > (1 << 31) - 1):
            raise ValueError("priority must be a bounded declared integer or null")
        value["prerequisites"] = strings(value.get("prerequisites", []))
        value.setdefault("next_experiment", None)
        if value["next_experiment"] is not None:
            exp = object_fields(value["next_experiment"], {"title", "plan", "research_revision"})
            exp["title"], exp["plan"] = text(exp.get("title"), 240, True), text(exp.get("plan"), 16384, True)
            positive(exp.get("research_revision"))
        value["dependencies"] = array(value.get("dependencies", []))
        for dep in value["dependencies"]:
            object_fields(dep, {"id", "kind", "target_id", "target_revision", "blocking", "reason", "resolution"})
            identifier(dep.get("id")); identifier(dep.get("target_id")); positive(dep.get("target_revision"))
            if dep.get("kind") not in {"direction", "request"} or type(dep.get("blocking")) is not bool:
                raise ValueError("dependency requires a supported kind and explicit blocking boolean")
            dep["reason"] = text(dep.get("reason"), 16384, True)
            dep.setdefault("resolution", None)
            if dep["resolution"] is not None:
                resolution = object_fields(dep["resolution"], {"decision", "reason", "feedback_ids"})
                if resolution.get("decision") not in {"satisfied", "waived"}:
                    raise ValueError("dependency resolution must be satisfied or waived")
                resolution["reason"] = text(resolution.get("reason"), 16384, True)
                resolution["feedback_ids"] = [identifier(fid) for fid in array(resolution.get("feedback_ids", []))]
                if dep["kind"] == "direction" and resolution["feedback_ids"]:
                    raise ValueError("direction dependencies do not take request feedback IDs")
        unique(value["dependencies"], "id")
        value["feedback_decisions"] = array(value.get("feedback_decisions", []))
        for decision in value["feedback_decisions"]:
            object_fields(decision, {"feedback_id", "decision", "reason"})
            identifier(decision.get("feedback_id"))
            if decision.get("decision") not in {"adopt", "defer", "reject"}:
                raise ValueError("unsupported feedback decision")
            decision["reason"] = text(decision.get("reason"), 16384, True)
        unique(value["feedback_decisions"], "feedback_id")
    elif kind == "request":
        if value.get("type") not in {"guidance", "assistance", "evaluation"}:
            raise ValueError("unsupported human request type")
        for key in ("need", "why"):
            value[key] = text(value.get(key), 16384, True)
        value["resume_hint"] = text(value.get("resume_hint"), 16384)
        context = object_fields(value.get("context"), {"research_revision", "directions"})
        positive(context.get("research_revision"))
        context["directions"] = array(context.get("directions", []))
        for direction in context["directions"]:
            object_fields(direction, {"id", "revision"})
            identifier(direction.get("id")); positive(direction.get("revision"))
        unique(context["directions"], "id")
        value["close_reason"] = text(value.get("close_reason"), 16384, value["state"] == "closed")
        if value["state"] == "open" and value["close_reason"] is not None:
            raise ValueError("an open request must have null close_reason")
        due = value.setdefault("due_at", None)
        if due is not None and (type(due) not in (int, float) or not math.isfinite(due) or due < 0):
            raise ValueError("due_at must be a finite timestamp or null")
        if value["type"] == "evaluation" and not any(ref["kind"] == "result_source" for ref in value["evidence_refs"]):
            raise ValueError("evaluation requires an explicit registered source reference")
    else:
        identifier(value.get("request_id")); positive(value.get("request_revision"))
        value["text"] = text(value.get("text"), 16384, True)
    if kind in ("request", "feedback"):
        value["supersedes"] = identifier(value["supersedes"]) if value.get("supersedes") is not None else None
        if "evaluation" in value:
            if kind == "request" and value["type"] != "evaluation":
                raise ValueError("A/B media requires an evaluation request")
            evaluation(kind, value["evaluation"], value["evidence_refs"])
    if len(encoded(value).encode()) > MAX_DATA:
        raise ResearchError(413, "research_too_large", "research object exceeds 64 KiB")
    return value


class NodeResearch:
    def __init__(self, results, node_id):
        self.results, self.node_id = results, node_id
        root = SafeRoot(results.jobs.data_root)
        try:
            root.mkdir("_research")
        finally:
            root.close()
        self.path = os.path.join(results.jobs.data_root, "_research", "state.sqlite3")
        with self._db(True) as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS objects (kind TEXT,id TEXT,research_id TEXT,revision INTEGER,created_at REAL,updated_at REAL,PRIMARY KEY(kind,id))",
                "CREATE TABLE IF NOT EXISTS revisions (sequence INTEGER PRIMARY KEY,kind TEXT,id TEXT,revision INTEGER,research_id TEXT,title TEXT,state TEXT,request_id TEXT,data TEXT,audit TEXT,bindings TEXT,observations TEXT,at REAL,UNIQUE(kind,id,revision))",
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY,request TEXT,receipt TEXT)",
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,research_id TEXT,kind TEXT,object_id TEXT,revision INTEGER,operation_id TEXT,at REAL)",
                "CREATE TABLE IF NOT EXISTS dependencies (direction_id TEXT,id TEXT,kind TEXT,target_id TEXT,target_revision INTEGER,blocking INTEGER,resolved INTEGER,PRIMARY KEY(direction_id,id))",
                "CREATE INDEX IF NOT EXISTS dependency_targets ON dependencies(kind,target_id)",
                "CREATE INDEX IF NOT EXISTS revision_scope ON revisions(kind,research_id,sequence)",
                "CREATE INDEX IF NOT EXISTS feedback_requests ON revisions(kind,request_id,sequence)",
            ):
                db.execute(statement)

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
    def _latest(db, kind, oid):
        return db.execute("SELECT r.* FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id AND r.revision=o.revision WHERE o.kind=? AND o.id=?", (kind, oid)).fetchone()

    @staticmethod
    def _reference(db, kind, oid, revision=None, research_id=None):
        row = (db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND revision=?", (kind, oid, revision)).fetchone()
               if revision is not None else NodeResearch._latest(db, kind, oid))
        if not row or research_id is not None and row["research_id"] != research_id:
            raise ResearchError(404, "research_reference_not_found", "referenced object/version is absent or belongs to a different research")
        return row

    @staticmethod
    def _operation(db, operation_id, request=None):
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row and request is not None and row["request"] != request:
            raise ResearchError(409, "research_operation_conflict", "operation ID already has different content")
        return json.loads(row["receipt"]) if row else None

    def operation(self, operation_id):
        with self._db() as db:
            value = self._operation(db, identifier(operation_id))
        if value is None:
            raise ResearchError(404, "research_operation_not_found", "no committed operation with this ID; do not automatically resubmit")
        return value

    def _bindings(self, data):
        output = []
        for ref in data["evidence_refs"]:
            binding = {"reference": ref, "identity_kind": "external"}
            kind = ref["kind"]
            if kind != "external":
                if ref["node_id"] != self.node_id:
                    raise ValueError("remote evidence must be an explicit external reference")
                try:
                    if kind == "job":
                        self.results._job_exists(ref["job_id"])
                        binding["identity_kind"] = "job_reference"
                    else:
                        row = self.results._row(ref["result_id"])
                        binding.update(identity_kind="result_reference", result_sequence=row["sequence"],
                                       result_registered_at=row["registered_at"])
                        if kind == "result_source":
                            observation = next((item for item in json.loads(row["observations"])
                                                if item["name"] == ref["source_name"]), None)
                            if observation is None:
                                raise ValueError("result source is absent")
                            binding["source_observation"] = {k: v for k, v in observation.items() if k != "stamp"}
                            binding["identity_kind"] = ("content_sha256" if observation.get("sha256") else
                                "registration_bound" if observation.get("stamp") else "unverified")
                except ValueError:
                    raise ResearchError(404, "research_reference_not_found", "referenced job, result or registered source is absent") from None
            output.append(binding)
        return output

    def _context_changed(self, db, research_id, context):
        parent = self._reference(db, "research", research_id)
        changes = []
        for direction in context["directions"]:
            current = self._reference(db, "direction", direction["id"], research_id=research_id)
            changes.append({"id": direction["id"], "expected_revision": direction["revision"],
                            "current_revision": current["revision"], "changed": current["revision"] != direction["revision"]})
        return {"research": parent["revision"] != context["research_revision"], "directions": changes}

    @staticmethod
    def _evaluation_sources(value, bindings):
        spec = value["evaluation"]
        for index in spec["slots"].values():
            binding = bindings[index]
            source = binding.get("source_observation") or {}
            if (source.get("kind") != "job_output" or source.get("status") != "available"
                    or binding.get("identity_kind") not in {"content_sha256", "registration_bound"}
                    or type(source.get("size")) is not int or source["size"] < 1):
                raise ResearchError(409, "research_media_unbound", "A/B slots require registered readable job-output snapshots")
            if source["size"] > LIMITS[spec["media_kind"]]["max_bytes"]:
                raise MediaError(413, "media_too_large", "registered media exceeds the evaluation byte limit")

    @staticmethod
    def _evaluation_binding(request):
        value, bindings = json.loads(request["data"]), json.loads(request["bindings"])
        spec = value.get("evaluation")
        if spec is None:
            raise ResearchError(409, "research_evaluation_required", "the original request revision has no A/B evaluation")
        return {**spec, "references": {slot: value["evidence_refs"][i] for slot, i in spec["slots"].items()},
                "evidence_bindings": {slot: bindings[i] for slot, i in spec["slots"].items()}}

    def media(self, oid, slot, revision):
        identifier(oid); positive(revision)
        if slot not in ("A", "B"):
            raise ValueError("evaluation media slot must be A or B")
        with self._db() as db:
            request = self._reference(db, "request", oid, revision)
            binding = self._evaluation_binding(request)
        ref = binding["references"][slot]
        return self.results.source(ref["result_id"], ref["source_name"], media_kind=binding["media_kind"])

    def _validate_links(self, db, kind, oid, value, old):
        observations = {}
        if kind == "research":
            return oid, observations
        if kind == "feedback":
            request = self._reference(db, "request", value["request_id"], value["request_revision"])
            rid = request["research_id"]
            latest = self._reference(db, "request", value["request_id"])
            context = json.loads(request["data"])["context"]
            observations = {"request_state_at_submit": latest["state"], "request_revision_at_submit": latest["revision"],
                            "context_changed_at_submit": self._context_changed(db, rid, context)}
            if "evaluation" in value:
                observations["evaluation_binding"] = self._evaluation_binding(request)
            if value["supersedes"]:
                prior = self._reference(db, "feedback", value["supersedes"], research_id=rid)
                if prior["request_id"] != value["request_id"]:
                    raise ValueError("superseded feedback must answer the same request")
            return rid, observations
        rid = value["research_id"]
        self._reference(db, "research", rid)
        if old and old["research_id"] != rid:
            raise ResearchError(409, "research_immutable", "an object cannot move to a different research")
        if kind == "request":
            if old:
                prior = json.loads(old["data"])
                if {k: v for k, v in prior.items() if k not in {"state", "close_reason"}} != {
                        k: v for k, v in value.items() if k not in {"state", "close_reason"}}:
                    raise ResearchError(409, "research_immutable", "request content is immutable; create a superseding request")
            context = value["context"]
            self._reference(db, "research", rid, context["research_revision"])
            for direction in context["directions"]:
                self._reference(db, "direction", direction["id"], direction["revision"], rid)
            if value["supersedes"]:
                self._reference(db, "request", value["supersedes"], research_id=rid)
            return rid, observations
        if value["next_experiment"] is not None:
            self._reference(db, "research", rid, value["next_experiment"]["research_revision"])
        for dep in value["dependencies"]:
            if dep["kind"] == "direction" and dep["target_id"] == oid:
                raise ResearchError(409, "research_dependency_cycle", "direction cannot depend on itself")
            self._reference(db, dep["kind"], dep["target_id"], dep["target_revision"], rid)
            for fid in (dep["resolution"] or {}).get("feedback_ids", []):
                feedback = self._reference(db, "feedback", fid, research_id=rid)
                if feedback["request_id"] != dep["target_id"]:
                    raise ValueError("dependency feedback belongs to a different request")
        for decision in value["feedback_decisions"]:
            self._reference(db, "feedback", decision["feedback_id"], research_id=rid)
        db.execute("DELETE FROM dependencies WHERE direction_id=?", (oid,))
        db.executemany("INSERT INTO dependencies VALUES (?,?,?,?,?,?,?)", [
            (oid, dep["id"], dep["kind"], dep["target_id"], dep["target_revision"], int(dep["blocking"]), int(dep["resolution"] is not None))
            for dep in value["dependencies"]])
        cycle = db.execute("""WITH RECURSIVE reachable(id) AS (
            SELECT target_id FROM dependencies WHERE direction_id=? AND kind='direction' AND blocking=1 AND resolved=0
            UNION SELECT d.target_id FROM dependencies d JOIN reachable r ON d.direction_id=r.id
              WHERE d.kind='direction' AND d.blocking=1 AND d.resolved=0)
            SELECT 1 FROM reachable WHERE id=? LIMIT 1""", (oid, oid)).fetchone()
        if cycle:
            raise ResearchError(409, "research_dependency_cycle", "unresolved blocking dependencies would form a cycle")
        return rid, observations

    def write(self, kind, oid, envelope, auth_channel):
        identifier(oid)
        object_fields(envelope, {"schema", "operation_id", "base_revision", "data", "actor_label", "reason"})
        if envelope.get("schema") != "phonefarm/research-write@1":
            raise ValueError("expected phonefarm/research-write@1")
        operation_id = identifier(envelope.get("operation_id"))
        base = envelope.get("base_revision")
        if base is not None:
            positive(base)
        actor = text(envelope.get("actor_label"), 128)
        reason = text(envelope.get("reason"), 16384, base is not None)
        value = normalize(kind, envelope.get("data"))
        request = encoded({"kind": kind, "id": oid, "base_revision": base, "data": value, "actor_label": actor, "reason": reason})
        with self._db() as db:
            receipt = self._operation(db, operation_id, request)
            if receipt is not None:
                return receipt
            previous = self._latest(db, kind, oid)
        bindings = (json.loads(previous["bindings"]) if previous and json.loads(previous["data"])["evidence_refs"] == value["evidence_refs"]
                    else self._bindings(value))
        if kind == "request" and "evaluation" in value:
            self._evaluation_sources(value, bindings)
        with self._db(True) as db:
            receipt = self._operation(db, operation_id, request)
            if receipt is not None:
                return receipt
            old = self._latest(db, kind, oid)
            current = old["revision"] if old else None
            if current != base:
                raise ResearchError(409, "research_revision_conflict", "object revision changed; keep the draft and merge explicitly", current_revision=current)
            if kind == "feedback" and old:
                raise ResearchError(409, "research_immutable", "feedback is immutable; append a correction with a new ID")
            rid, observations = self._validate_links(db, kind, oid, value, old)
            now, revision = time.time(), (current or 0) + 1
            audit = {"actor_label": actor, "auth_channel": auth_channel, "reason": reason, "at": now}
            sequence = db.execute("INSERT INTO events (research_id,kind,object_id,revision,operation_id,at) VALUES (?,?,?,?,?,?)",
                                  (rid, kind, oid, revision, operation_id, now)).lastrowid
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (sequence, kind, oid, revision, rid,
                       value.get("title"), value.get("state"), value.get("request_id"), encoded(value), encoded(audit),
                       encoded(bindings), encoded(observations), now))
            db.execute("INSERT INTO objects VALUES (?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at",
                       (kind, oid, rid, revision, now, now))
            receipt = {"schema_version": 1, "node_id": self.node_id, "operation_id": operation_id,
                       "object": {"kind": kind, "id": oid, "revision": revision}, "event_seq": sequence, "committed_at": now}
            db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, request, encoded(receipt)))
        return receipt

    @staticmethod
    def _kind(kind):
        if kind not in STATES:
            raise ValueError("unsupported research object kind")

    @staticmethod
    def _page(query, allowed=()):
        if set(query) - ({"limit", "cursor", "expected_node_id"} | set(allowed)) or any(len(v) != 1 for v in query.values()):
            raise ValueError("unsupported or repeated research query field")
        limit = int(query.get("limit", [20])[0])
        if not 1 <= limit <= 100:
            raise ValueError("research page limit must be 1 to 100")
        return limit

    def _cursor(self, query, binding, upper):
        if "cursor" not in query:
            return upper, None
        value = decode_cursor(query["cursor"][0])
        if (not isinstance(value, list) or len(value) != len(binding) + 2 or value[:-2] != binding
                or any(type(n) is not int or not 0 <= n <= (1 << 63) - 1 for n in value[-2:])):
            raise ValueError("research cursor belongs to another node or query")
        return value[-2], value[-1]

    @staticmethod
    def _upper(db):
        return db.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]

    def _derived(self, db, row, value):
        kind, oid = row["kind"], row["id"]
        if kind == "direction":
            pending = [dep for dep in value["dependencies"] if dep["blocking"] and dep["resolution"] is None]
            return {"unresolved_dependencies": pending, "metadata_blocked": bool(pending)}
        if kind == "feedback":
            return json.loads(row["observations"])
        if kind == "research":
            counts = db.execute("SELECT r.state,COUNT(*) AS n FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id AND r.revision=o.revision WHERE o.kind='direction' AND o.research_id=? GROUP BY r.state", (oid,)).fetchall()
            return {"direction_counts": {item["state"]: item["n"] for item in counts}}
        feedback = db.execute("SELECT COUNT(*) AS n,MAX(at) AS at FROM revisions WHERE kind='feedback' AND request_id=?", (oid,)).fetchone()
        impacted = db.execute("SELECT d.*,o.revision FROM dependencies d JOIN objects o ON o.kind='direction' AND o.id=d.direction_id WHERE d.kind='request' AND d.target_id=? ORDER BY d.direction_id,d.id", (oid,)).fetchall()
        derived = {"feedback_count": feedback["n"], "last_feedback_at": feedback["at"],
                "affected_directions": [{"id": item["direction_id"], "revision": item["revision"], "dependency_id": item["id"],
                                         "blocking": bool(item["blocking"]), "resolved": bool(item["resolved"])} for item in impacted],
                "context_changed": self._context_changed(db, row["research_id"], value["context"]),
                "overdue": value["state"] == "open" and value["due_at"] is not None and value["due_at"] < time.time()}
        if "evaluation" in value:
            spec = value["evaluation"]
            query = urlencode({"revision": row["revision"], "expected_node_id": self.node_id})
            derived["evaluation"] = {**spec, "media": {slot: "/research-state/request/" + quote(oid) + "/media/" + slot + "?" + query for slot in ("A", "B")},
                                     "limits": LIMITS[spec["media_kind"]], "blinding": "presentation_only"}
        return derived

    def _show(self, db, row):
        value = json.loads(row["data"])
        current = db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (row["kind"], row["id"])).fetchone()
        return {"schema_version": 1, "node_id": self.node_id, "kind": row["kind"], "id": row["id"],
                "research_id": row["research_id"], "revision": row["revision"], "sequence": row["sequence"],
                "created_at": current["created_at"], "updated_at": row["at"], "data": value,
                "audit": json.loads(row["audit"]), "evidence_bindings": json.loads(row["bindings"]),
                "derived": self._derived(db, row, value), "current_revision": current["revision"]}

    def get(self, kind, oid, revision=None):
        self._kind(kind); identifier(oid)
        if revision is not None:
            positive(revision)
        with self._db() as db:
            row = (db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND revision=?", (kind, oid, revision)).fetchone()
                   if revision is not None else self._latest(db, kind, oid))
            if row is None:
                raise ResearchError(404, "research_not_found", "research object or version was not found")
            return self._show(db, row)

    def list(self, kind, query):
        self._kind(kind)
        limit = self._page(query, {"research_id", "state", "request_id"})
        filters = {key: query[key][0] for key in ("research_id", "state", "request_id") if query.get(key) and query[key][0]}
        for key, value in filters.items():
            if key == "state":
                if value not in STATES[kind]:
                    raise ValueError("unsupported state filter for this kind")
            else:
                identifier(value)
                if key == "request_id" and kind != "feedback":
                    raise ValueError("request_id filter is only supported for feedback")
        binding = [self.node_id, "list", kind, filters]
        with self._db() as db:
            upper, after = self._cursor(query, binding, self._upper(db))
            terms = ["r.sequence IN (SELECT MAX(sequence) FROM revisions WHERE kind=? AND sequence<=? GROUP BY id)"]
            params = [kind, upper]
            for key, value in filters.items():
                terms.append("r." + key + "=?")
                params.append(value)
            if after is not None:
                terms.append("r.sequence<?"); params.append(after)
            rows = db.execute("SELECT r.* FROM revisions r WHERE " + " AND ".join(terms) + " ORDER BY r.sequence DESC LIMIT ?",
                              (*params, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                data = json.loads(row["data"])
                item = {"kind": kind, "id": row["id"], "research_id": row["research_id"], "revision": row["revision"],
                        "title": row["title"] or data.get("text", "")[:120], "state": row["state"],
                        "sequence": row["sequence"], "updated_at": row["at"]}
                if kind == "direction":
                    item["metadata_blocked"] = any(dep["blocking"] and dep["resolution"] is None for dep in data["dependencies"])
                elif kind == "request":
                    item["feedback_count"] = db.execute("SELECT COUNT(*) FROM revisions WHERE kind='feedback' AND request_id=? AND sequence<=?", (row["id"], upper)).fetchone()[0]
                items.append(item)
        cursor = encode_cursor(binding + [upper, rows[limit - 1]["sequence"]]) if len(rows) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": items, "next_cursor": cursor}

    def history(self, kind, oid, query):
        self._kind(kind); identifier(oid)
        limit = self._page(query)
        binding = [self.node_id, "history", kind, oid]
        with self._db() as db:
            if self._latest(db, kind, oid) is None:
                raise ResearchError(404, "research_not_found", "research object was not found")
            upper, after = self._cursor(query, binding, self._upper(db))
            rows = db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND sequence<=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                              (kind, oid, upper, after if after is not None else (1 << 63) - 1, limit + 1)).fetchall()
            items = [self._show(db, row) for row in rows[:limit]]
        cursor = encode_cursor(binding + [upper, rows[limit - 1]["sequence"]]) if len(rows) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": items, "next_cursor": cursor}

    def events(self, query):
        if set(query) - {"after", "limit", "research_id", "expected_node_id"} or any(len(v) != 1 for v in query.values()):
            raise ValueError("unsupported or repeated research event query")
        after, limit = int(query.get("after", [0])[0]), int(query.get("limit", [100])[0])
        if not 0 <= after <= (1 << 63) - 1 or not 1 <= limit <= 500:
            raise ValueError("invalid event sequence or page limit")
        rid = query.get("research_id", [None])[0]
        params, clause = [after], "sequence>?"
        if rid is not None:
            identifier(rid); params.append(rid); clause += " AND research_id=?"
        with self._db() as db:
            rows = db.execute("SELECT * FROM events WHERE " + clause + " ORDER BY sequence LIMIT ?", (*params, limit + 1)).fetchall()
        items = [{"sequence": row["sequence"], "research_id": row["research_id"], "kind": row["kind"], "id": row["object_id"],
                  "revision": row["revision"], "operation_id": row["operation_id"], "at": row["at"]} for row in rows[:limit]]
        return {"schema_version": 1, "node_id": self.node_id, "items": items,
                "next_after": items[-1]["sequence"] if items else after, "has_more": len(rows) > limit}
