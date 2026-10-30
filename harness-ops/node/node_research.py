"""Versioned research metadata and asynchronous feedback, independent of execution."""
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote, urlencode

from artifact_io import SafeRoot, describe_file
from node_read import encode_cursor, decode_cursor
from node_results import ResultError, encoded, identifier, object_fields, text
from node_result_compare import compare as compare_results
import node_research_criteria as criteria_rules
from node_media import LIMITS, MediaError, inspect_media

MAX_DATA = 64 << 10
MAX_WRITE = 128 << 10
ATTACHMENT_MAX = 16 << 20
ATTACHMENT_LIMITS = {"image/png": 16 << 20, "image/jpeg": 16 << 20, "image/svg+xml": 16 << 20,
                     "video/mp4": 128 << 20, "video/webm": 128 << 20}
ATTACHMENT_TYPES = set(ATTACHMENT_LIMITS)
AGENT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")  # same rule as node_agents.NAME
STATES = {
    "research": {"active", "idle", "paused", "closed", "delivered"},
    "direction": {"proposed", "preparing", "ready", "running", "waiting_device", "waiting_human",
                  "waiting_material", "awaiting_evaluation", "verified", "no_gain", "rejected", "deferred"},
    "request": {"open", "closed"}, "feedback": set(), "note": set(),
}
DECISIONS = {"adopt", "defer", "reject"}
# States that conclude work. An agent only reaches them after answering every human note in that research.
CONCLUDING = {"research": {"delivered", "closed"}, "direction": {"verified", "no_gain", "rejected", "deferred"}}


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


def field_names(names):
    return ", ".join(json.dumps(name, ensure_ascii=True) for name in sorted(names))


def refs(value):
    for ref in array(value):
        if not isinstance(ref, dict):
            raise ValueError("evidence reference must be an object")
        kind = ref.get("kind")
        fields = {"job": {"job_id", "path"}, "result": {"result_id"},
                  "result_source": {"result_id", "source_name"}, "external": {"locator"},
                  "attachment": {"attachment_id", "filename", "content_type", "sha256"}}
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
            path = ref.get("path")
            if path is not None:
                path = text(path, 2048, True)
                if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
                    raise ValueError("evidence job path must be a normalized relative path within job/out")
                ref["path"] = path
        elif kind == "attachment":
            if not re.fullmatch(r"[0-9a-f]{32}", ref.get("attachment_id", "")):
                raise ValueError("evidence attachment identity must have 32 hexadecimal characters")
            if not re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", "")):
                raise ValueError("evidence attachment requires a sha256")
            filename = ref.get("filename")
            if (not isinstance(filename, str) or not 1 <= len(filename) <= 128
                    or "/" in filename or "\\" in filename or "\x00" in filename):
                raise ValueError("evidence attachment requires a safe filename")
            if ref.get("content_type") not in ATTACHMENT_TYPES:
                raise ValueError("evidence attachment type must be PNG, JPEG, SVG, MP4 or WebM")
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
        schema = value.get("schema")
        if schema == "phonefarm/ab-evaluation@1":
            object_fields(value, {"schema", "media_kind", "slots"})
            if value.get("media_kind") not in LIMITS:
                raise ValueError("evaluation requires image/video")
            slots = object_fields(value.get("slots"), {"A", "B"})
            if set(slots) != {"A", "B"} or any(type(i) is not int or not 0 <= i < len(evidence_refs) for i in slots.values()):
                raise ValueError("evaluation slots must select two evidence reference indices")
            if slots["A"] == slots["B"] or any(evidence_refs[i]["kind"] != "result_source" for i in slots.values()):
                raise ValueError("evaluation slots must be distinct registered source reference indices")
        elif schema == "phonefarm/compare@1":
            object_fields(value, {"schema", "items", "pick"})
            items = value.get("items")
            if not isinstance(items, list) or not 2 <= len(items) <= 8:
                raise ValueError("comparison requires 2 to 8 items")
            if type(value.get("pick")) is not int or not 1 <= value["pick"] <= len(items):
                raise ValueError("comparison pick must be within the item count")
            selected = []
            for item in items:
                if type(item) is int:
                    index = item
                else:
                    object_fields(item, {"ref", "media_kind"})
                    index = item.get("ref")
                    if item.get("media_kind") not in LIMITS:
                        raise ValueError("comparison item requires image/video")
                if type(index) is not int or not 0 <= index < len(evidence_refs) or evidence_refs[index]["kind"] != "result_source":
                    raise ValueError("comparison items must select registered source reference indices")
                selected.append(index)
            if len(set(selected)) != len(selected):
                raise ValueError("comparison item indices must be distinct")
        else:
            raise ValueError("evaluation requires phonefarm/ab-evaluation@1 or phonefarm/compare@1")
    elif value.get("schema") == "phonefarm/compare-feedback@1":
        fields = {"schema", "picked", "unable", "difference", "client_display", "identity_revealed"}
        object_fields(value, fields)
        if set(value) != fields or not isinstance(value["picked"], list) or type(value["unable"]) is not bool:
            raise ValueError("comparison feedback requires picked and unable")
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
        "research": {"title", "objective", "game", "constraints", "budgets", "device_requirements", "state", "state_reason", "coverage", "criteria",
                     "owner", "deliverable", "follows"},
        "direction": {"research_id", "title", "hypothesis", "priority", "priority_reason", "prerequisites", "cost_expectation",
                      "expected_benefit", "next_experiment", "state", "state_reason", "dependencies", "feedback_decisions"},
        "request": {"research_id", "type", "title", "need", "why", "resume_hint", "context", "supersedes", "state", "close_reason", "due_at"},
        "feedback": {"request_id", "request_revision", "text", "supersedes"},
        # The research's discussion thread: humans and agents post, agents answer guidance with a decision.
        "note": {"research_id", "text", "reply_to", "decision", "ask", "to"},
    }
    if kind not in fields:
        raise ValueError("unsupported research object kind")
    if not isinstance(data, dict):
        raise ValueError(f"{kind} data must be a JSON object")
    allowed = fields[kind] | common
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{kind} data contains unsupported field(s): {field_names(unknown)}")
    if kind == "direction":
        missing = {name for name in ("research_id", "title", "hypothesis") if data.get(name) is None}
        if missing:
            raise ValueError(f"direction data is missing required field(s): {field_names(missing)}")
    value = copy.deepcopy(object_fields(data, allowed))
    value["evidence_refs"] = refs(value.get("evidence_refs", []))
    if kind not in ("feedback", "note"):
        try:
            value["title"] = text(value.get("title"), 240, True)
        except ValueError as error:
            raise ValueError(f"{kind} data.title: {error}") from None
        value.setdefault("state", {"research": "active", "direction": "proposed", "request": "open"}[kind])
        if value["state"] not in STATES[kind]:
            raise ValueError("unsupported declared object state")
    if kind in ("direction", "request", "note"):
        try:
            identifier(value.get("research_id"))
        except ValueError as error:
            raise ValueError(f"{kind} data.research_id: {error}") from None
    if kind in ("research", "direction"):
        value["state_reason"] = text(value.get("state_reason"), 16384)
        needs_reason = (kind == "research" and value["state"] not in {"active", "delivered"} or kind == "direction"
                        and value["state"] not in {"proposed", "preparing", "ready", "running"})
        if needs_reason and value["state_reason"] is None:
            raise ValueError("this declared state requires a reason")
    if kind == "research":
        value["objective"] = text(value.get("objective"), 16384, True)
        value["follows"] = identifier(value["follows"]) if value.get("follows") is not None else None
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
        if value.get("criteria") is not None:
            criteria_rules.validate(value["criteria"])
        owner = value.setdefault("owner", None)
        if owner is not None and (not isinstance(owner, str) or not AGENT_NAME.fullmatch(owner)):
            raise ValueError("owner must be a registered agent name or null")
        deliverable = value.setdefault("deliverable", None)
        if deliverable is not None:
            object_fields(deliverable, {"summary", "artifacts", "applies_to"})
            deliverable["summary"] = text(deliverable.get("summary"), 16384, True)
            deliverable["artifacts"] = refs(deliverable.get("artifacts", []))
            if not deliverable["artifacts"]:
                raise ValueError("a deliverable names at least one artifact (job, result or external locator)")
            applies = object_fields(deliverable.get("applies_to") or {}, {"game_version", "devices"})
            deliverable["applies_to"] = {"game_version": text(applies.get("game_version"), 128),
                                         "devices": strings(applies.get("devices", []))}
        if value["state"] == "delivered" and deliverable is None:
            raise ValueError("delivered research needs a deliverable: summary, artifacts and where it applies")
    elif kind == "direction":
        try:
            value["hypothesis"] = text(value.get("hypothesis"), 16384, True)
        except ValueError as error:
            raise ValueError(f"direction data.hypothesis: {error}") from None
        for key in ("priority_reason", "cost_expectation", "expected_benefit"):
            value[key] = text(value.get(key), 16384)
        value.setdefault("priority", None)
        if value["priority"] is not None and (type(value["priority"]) is not int or abs(value["priority"]) > (1 << 31) - 1):
            raise ValueError("priority must be a bounded declared integer or null")
        value["prerequisites"] = strings(value.get("prerequisites", []))
        value.setdefault("next_experiment", None)
        if value["next_experiment"] is not None:
            exp = value["next_experiment"]
            if not isinstance(exp, dict):
                raise ValueError("direction data.next_experiment must be an object")
            allowed_exp = {"title", "plan", "research_revision"}
            unknown = set(exp) - allowed_exp
            if unknown:
                raise ValueError("direction data.next_experiment contains unsupported field(s): " + field_names(unknown))
            missing = allowed_exp - set(exp)
            if missing:
                raise ValueError("direction data.next_experiment is missing required field(s): " + field_names(missing))
            for key, maximum in (("title", 240), ("plan", 16384)):
                try:
                    exp[key] = text(exp.get(key), maximum, True)
                except ValueError:
                    raise ValueError(f"direction data.next_experiment.{key} must be a non-empty string "
                                     f"of at most {maximum} characters") from None
            try:
                positive(exp.get("research_revision"))
            except ValueError:
                raise ValueError("direction data.next_experiment.research_revision must be a positive integer") from None
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
    elif kind == "note":
        value["text"] = text(value.get("text"), 16384, True)
        value["reply_to"] = identifier(value["reply_to"]) if value.get("reply_to") is not None else None
        value["to"] = identifier(value["to"]) if value.get("to") is not None else None
        value.setdefault("decision", None)
        if value["decision"] is not None and value["decision"] not in DECISIONS:
            raise ValueError("decision must be adopt, defer or reject")
        if value["decision"] is not None and value["reply_to"] is None:
            raise ValueError("a decision answers a note; set reply_to")
        value.setdefault("ask", False)
        if type(value["ask"]) is not bool:
            raise ValueError("ask must be a boolean")
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
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
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
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('stream_id',?)", (uuid.uuid4().hex,))
            self.stream_id = db.execute("SELECT value FROM metadata WHERE key='stream_id'").fetchone()[0]
        self.event_changed = threading.Condition()
        self.watch_slots = threading.BoundedSemaphore(16)

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
    def _unanswered_guidance(db, research_id, agent):
        """Human notes (written on the page) in `research_id` that `agent` has not replied to, oldest first."""
        rows = db.execute("SELECT r.id, r.data, r.audit FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id "
                          "AND r.revision=o.revision WHERE o.kind='note' AND r.research_id=? ORDER BY r.sequence",
                          (research_id,)).fetchall()
        human, answered = [], set()
        for row in rows:
            data, audit = json.loads(row["data"]), json.loads(row["audit"])
            if agent is not None and audit.get("actor_label") == agent and data.get("reply_to"):
                answered.add(data["reply_to"])
            elif audit.get("auth_channel") == "dashboard_steer" and data.get("to") in (None, agent):
                human.append(row["id"])
        return [note for note in human if note not in answered]

    @staticmethod
    def _unanswered_questions(db, research_id=None):
        """Agent questions that have no human reply, oldest first."""
        scope, params = (" AND r.research_id=?", (research_id,)) if research_id is not None else ("", ())
        rows = db.execute("SELECT r.id, r.data, r.audit FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id "
                          "AND r.revision=o.revision WHERE o.kind='note'" + scope + " ORDER BY r.sequence", params).fetchall()
        questions, answered = [], set()
        for row in rows:
            data, audit = json.loads(row["data"]), json.loads(row["audit"])
            if audit.get("auth_channel") in ("dashboard_steer", "admin_token") and data.get("reply_to"):
                answered.add(data["reply_to"])
            elif data.get("ask") is True and audit.get("auth_channel") != "dashboard_steer":
                questions.append(row["id"])
        return [note for note in questions if note not in answered]

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

    def upload_attachment(self, attachment_id, filename, content_type, raw):
        if not re.fullmatch(r"[0-9a-f]{32}", attachment_id or ""):
            raise ValueError("attachment ID must have 32 hexadecimal characters")
        if (not isinstance(filename, str) or not 1 <= len(filename) <= 128
                or filename in (".", "..") or "/" in filename or "\\" in filename or "\x00" in filename):
            raise ValueError("attachment filename must be a single safe filename")
        if content_type not in ATTACHMENT_TYPES:
            raise ValueError("attachments must be PNG, JPEG, SVG, MP4 or WebM")
        maximum = ATTACHMENT_LIMITS[content_type]
        if not isinstance(raw, bytes) or len(raw) > maximum:
            raise ValueError(f"attachment exceeds the {maximum >> 20} MiB limit")
        if content_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("attachment is not a PNG")
        if content_type == "image/jpeg" and not raw.startswith(b"\xff\xd8\xff"):
            raise ValueError("attachment is not a JPEG")
        if content_type == "image/svg+xml" and b"<svg" not in raw[:1024]:
            raise ValueError("attachment is not an SVG")
        if content_type == "video/mp4" and (len(raw) < 12 or raw[4:8] != b"ftyp"):
            raise ValueError("attachment is not an MP4")
        if content_type == "video/webm" and not raw.startswith(b"\x1a\x45\xdf\xa3"):
            raise ValueError("attachment is not a WebM")
        metadata = {"schema_version": 1, "node_id": self.node_id, "attachment_id": attachment_id,
                    "filename": filename, "content_type": content_type, "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest()}
        path = "_research/attachments/" + attachment_id
        root = SafeRoot(self.results.jobs.data_root)
        try:
            root.mkdir("_research/attachments")
            try:
                if root.read_json(path + ".json", 64 << 10) == metadata and root.read_bytes(path, maximum) == raw:
                    return metadata
                raise ResearchError(409, "research_operation_conflict", "attachment ID already has different content")
            except FileNotFoundError:
                pass
            with root.create_file(path) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            root.sync(path)
            if not root.publish_json_once(path + ".json", metadata):
                if root.read_json(path + ".json", 64 << 10) != metadata or root.read_bytes(path, maximum) != raw:
                    raise ResearchError(409, "research_operation_conflict", "attachment ID already has different content")
            root.sync("_research/attachments", directory=True)
            return metadata
        finally:
            root.close()

    def attachment(self, attachment_id):
        if not re.fullmatch(r"[0-9a-f]{32}", attachment_id or ""):
            raise ResearchError(404, "research_attachment_not_found", "no such attachment")
        root = SafeRoot(self.results.jobs.data_root)
        try:
            try:
                metadata = root.read_json("_research/attachments/" + attachment_id + ".json", 64 << 10)
            except FileNotFoundError:
                raise ResearchError(404, "research_attachment_not_found", "no such attachment") from None
            stream = root.open_file("_research/attachments/" + attachment_id)
            info = describe_file(stream)
            if info["sha256"] != metadata["sha256"] or info["size"] != metadata["size"]:
                stream.close()
                raise ValueError("attachment bytes differ from their immutable metadata")
            return stream, {"kind": "file", "path": metadata["filename"], **info,
                            "media": {"mime": metadata["content_type"]}}
        finally:
            root.close()

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
                        if ref.get("path") is not None:
                            try:
                                with self.results.jobs.center.artifacts.open_output(ref["job_id"], ref["path"]):
                                    pass
                            except (OSError, ValueError):
                                raise ResearchError(404, "research_reference_not_found",
                                                    "referenced job artifact is absent or outside job/out") from None
                        binding["identity_kind"] = "job_reference"
                    elif kind == "attachment":
                        stream, info = self.attachment(ref["attachment_id"])
                        stream.close()
                        if (ref["filename"] != info["path"] or ref["content_type"] != info["media"]["mime"]
                                or ref["sha256"] != info["sha256"]):
                            raise ValueError("attachment evidence does not match its immutable bytes")
                        binding.update(identity_kind="attachment_reference", attachment_sha256=info["sha256"])
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

    def _criteria_bindings(self, value, previous):
        criteria = value.get("criteria")
        if criteria is None:
            return None
        spec = criteria["comparison"]
        if spec["node_id"] != self.node_id:
            raise ValueError("criteria comparison must belong to this node")
        if previous:
            prior = json.loads(previous["data"]).get("criteria")
            if prior and prior["comparison"] == spec:
                return json.loads(previous["observations"])["criteria_bindings"]
        cache, bindings = {}, []
        for column in spec["columns"]:
            rid = column["result_id"]
            if rid not in cache:
                try:
                    row = self.results._row(rid)
                    cache[rid] = {"status": "registration_bound", "result_sequence": row["sequence"],
                                  "result_registered_at": row["registered_at"]}
                except ResultError as error:
                    if error.status != 404:
                        raise
                    cache[rid] = {"status": "declared_pending", "result_sequence": None, "result_registered_at": None}
            bindings.append({"column_id": column["id"], "node_id": self.node_id, "result_id": rid, **cache[rid]})
        return bindings

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
        if spec["schema"] == "phonefarm/ab-evaluation@1":
            selected = [(index, spec["media_kind"]) for index in spec["slots"].values()]
        else:
            selected = [((item if type(item) is int else item["ref"]),
                         None if type(item) is int else item["media_kind"]) for item in spec["items"]]
        for index, media_kind in selected:
            binding = bindings[index]
            source = binding.get("source_observation") or {}
            if (source.get("kind") not in {"job_output", "attachment"} or source.get("status") != "available"
                    or binding.get("identity_kind") not in {"content_sha256", "registration_bound"}
                    or type(source.get("size")) is not int or source["size"] < 1):
                raise ResearchError(409, "research_media_unbound", "comparison slots require registered readable media snapshots")
            if media_kind is not None and source["size"] > LIMITS[media_kind]["max_bytes"]:
                raise MediaError(413, "media_too_large", "registered media exceeds the evaluation byte limit")

    @staticmethod
    def _evaluation_binding(request):
        value, bindings = json.loads(request["data"]), json.loads(request["bindings"])
        spec = value.get("evaluation")
        if spec is None:
            raise ResearchError(409, "research_evaluation_required", "the original request revision has no evaluation")
        if spec["schema"] == "phonefarm/compare@1":
            slots = {"ABCDEFGH"[position]: (item if type(item) is int else item["ref"])
                     for position, item in enumerate(spec["items"])}
        else:
            slots = spec["slots"]
        return {**spec, "references": {slot: value["evidence_refs"][i] for slot, i in slots.items()},
                "evidence_bindings": {slot: bindings[i] for slot, i in slots.items()}}

    def media(self, oid, slot, revision):
        identifier(oid); positive(revision)
        if slot not in tuple("ABCDEFGH"):
            raise ValueError("evaluation media slot must be A through H")
        with self._db() as db:
            request = self._reference(db, "request", oid, revision)
            binding = self._evaluation_binding(request)
            spec = json.loads(request["data"])["evaluation"]
        if slot not in binding["references"]:
            raise ValueError("evaluation media slot is absent from this comparison")
        ref = binding["references"][slot]
        if spec["schema"] == "phonefarm/compare@1":
            item = spec["items"]["ABCDEFGH".index(slot)]
            media_kind = None if type(item) is int else item["media_kind"]
        else:
            media_kind = spec["media_kind"]
        stream, info = self.results.source(ref["result_id"], ref["source_name"], media_kind=media_kind)
        if media_kind is None:
            try:
                header = stream.read(12)
                stream.seek(0)
                media_kind = "video" if header.startswith(b"\x1a\x45\xdf\xa3") or header[4:8] == b"ftyp" else "image"
                info["media"] = inspect_media(stream, media_kind)
            except BaseException:
                stream.close()
                raise
        return stream, info

    def _validate_links(self, db, kind, oid, value, old):
        observations = {}
        if kind == "research":
            follows = value.get("follows")
            if follows == oid:
                raise ValueError("research cannot follow itself")
            if follows is not None:
                self._reference(db, "research", follows)
            return oid, observations
        if kind == "feedback":
            request = self._reference(db, "request", value["request_id"], value["request_revision"])
            rid = request["research_id"]
            latest = self._reference(db, "request", value["request_id"])
            context = json.loads(request["data"])["context"]
            observations = {"request_state_at_submit": latest["state"], "request_revision_at_submit": latest["revision"],
                            "context_changed_at_submit": self._context_changed(db, rid, context)}
            if "evaluation" in value:
                answer, spec = value["evaluation"], json.loads(request["data"]).get("evaluation")
                if answer["schema"] == "phonefarm/compare-feedback@1":
                    if not spec or spec.get("schema") != "phonefarm/compare@1":
                        raise ResearchError(409, "research_evaluation_required", "comparison feedback requires a comparison request")
                    picked = answer["picked"]
                    letters = set("ABCDEFGH"[:len(spec["items"])])
                    if (len(set(picked)) != len(picked) or any(not isinstance(slot, str) or slot not in letters for slot in picked)
                            or not answer["unable"] and picked and len(picked) != spec["pick"]):
                        raise ValueError("picked must contain distinct comparison slots and match pick, unless unable")
                elif not spec or spec.get("schema") != "phonefarm/ab-evaluation@1":
                    raise ResearchError(409, "research_evaluation_required", "A/B feedback requires an A/B request")
                observations["evaluation_binding"] = self._evaluation_binding(request)
            if value["supersedes"]:
                prior = self._reference(db, "feedback", value["supersedes"], research_id=rid)
                if prior["request_id"] != value["request_id"]:
                    raise ValueError("superseded feedback must answer the same request")
            return rid, observations
        if kind == "note":
            rid = value["research_id"]
            self._reference(db, "research", rid)
            if value["reply_to"]:
                self._reference(db, "note", value["reply_to"], research_id=rid)
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

    def _auto_reopen(self, db, kind, oid, rid, auth_channel, agent, actor, now):
        if kind == "note" and auth_channel == "dashboard_steer":
            cause = f"人工留言 {oid} 重新打开任务"
        elif kind == "direction" and agent is not None and self._latest(db, "direction", oid) is None:
            cause = f"负责人新建方向 {oid} 重新打开任务"
        else:
            return
        research = self._latest(db, "research", rid)
        data = json.loads(research["data"])
        if data["state"] not in {"closed", "delivered"} or kind == "direction" and data.get("owner") != agent:
            return
        data["state"], data["state_reason"] = "active", cause
        operation_id = "auto-reopen-" + uuid.uuid4().hex
        revision = research["revision"] + 1
        request = encoded({"kind": "research", "id": rid, "base_revision": research["revision"],
                           "data": data, "actor_label": actor, "reason": cause})
        audit = {"actor_label": actor, "auth_channel": auth_channel, "reason": cause, "at": now}
        sequence = db.execute("INSERT INTO events (research_id,kind,object_id,revision,operation_id,at) VALUES (?,?,?,?,?,?)",
                              (rid, "research", rid, revision, operation_id, now)).lastrowid
        db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sequence, "research", rid, revision, rid, data.get("title"), "active", None,
                    encoded(data), encoded(audit), research["bindings"], research["observations"], now))
        db.execute("INSERT INTO objects VALUES (?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at",
                   ("research", rid, rid, revision, now, now))
        receipt = {"schema_version": 1, "node_id": self.node_id, "operation_id": operation_id,
                   "object": {"kind": "research", "id": rid, "revision": revision},
                   "event_seq": sequence, "committed_at": now}
        db.execute("INSERT INTO operations VALUES (?,?,?)", (operation_id, request, encoded(receipt)))

    def write(self, kind, oid, envelope, auth_channel, agent=None):
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
        criteria_bindings = self._criteria_bindings(value, previous) if kind == "research" else None
        deliverable_bindings = None
        if kind == "research" and value["deliverable"] is not None:
            deliverable_bindings = self._bindings({"evidence_refs": value["deliverable"]["artifacts"]})
        if kind == "research" and value["owner"] is not None:
            registry = getattr(self.results.jobs, "agents", None)
            if registry is not None:
                try:
                    registry.get(value["owner"])
                except ValueError:
                    raise ResearchError(404, "research_owner_unknown", "owner must be a registered agent") from None
        with self._db(True) as db:
            receipt = self._operation(db, operation_id, request)
            if receipt is not None:
                return receipt
            old = self._latest(db, kind, oid)
            current = old["revision"] if old else None
            if current != base:
                raise ResearchError(409, "research_revision_conflict", "object revision changed; keep the draft and merge explicitly", current_revision=current)
            if kind in ("feedback", "note") and old:
                raise ResearchError(409, "research_immutable", kind + " is immutable; append a correction with a new ID")
            if kind == "research" and agent is not None:
                before = json.loads(old["data"]).get("owner") if old else None
                if value["owner"] != before and before not in (None, agent):
                    raise ResearchError(409, "research_owned", f"this research belongs to {before}; a human reassigns it",
                                        owner=before)
            rid, observations = self._validate_links(db, kind, oid, value, old)
            if agent is not None and kind in ("research", "direction"):
                research = self._latest(db, "research", rid)
                owner = json.loads(research["data"]).get("owner") if research else None
                if owner is not None and owner != agent:
                    raise ResearchError(409, "research_owned",
                                        f"this research belongs to {owner}; use a note instead", owner=owner)
            if (agent is not None and value.get("state") in CONCLUDING.get(kind, ())
                    and (json.loads(old["data"])["state"] if old else None) != value["state"]):
                waiting = self._unanswered_guidance(db, rid, agent)
                if waiting:
                    raise ResearchError(409, "research_guidance_unanswered",
                                        "answer the human notes in this research before concluding it", notes=waiting)
            if criteria_bindings is not None:
                observations["criteria_bindings"] = criteria_bindings
            if deliverable_bindings is not None:
                observations["deliverable_bindings"] = deliverable_bindings
            now = time.time()
            self._auto_reopen(db, kind, oid, rid, auth_channel, agent, actor, now)
            revision = (current or 0) + 1
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
        # Commit precedes notification. Readers hold this condition across their final
        # empty read and wait registration, so a concurrent commit cannot be missed.
        with self.event_changed:
            self.event_changed.notify_all()
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
        if kind == "note":
            replies = db.execute("SELECT id FROM revisions WHERE kind='note' AND json_extract(data,'$.reply_to')=? ORDER BY sequence",
                                 (oid,)).fetchall()
            return {"replies": [item["id"] for item in replies]}
        if kind == "research":
            counts = db.execute("SELECT r.state,COUNT(*) AS n FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id AND r.revision=o.revision WHERE o.kind='direction' AND o.research_id=? GROUP BY r.state", (oid,)).fetchall()
            derived = {"direction_counts": {item["state"]: item["n"] for item in counts},
                       "note_count": db.execute("SELECT COUNT(*) FROM objects WHERE kind='note' AND research_id=?", (oid,)).fetchone()[0]}
            if value.get("criteria") is not None:
                derived["criteria_bindings"] = json.loads(row["observations"])["criteria_bindings"]
            if value.get("deliverable") is not None:
                derived["deliverable_bindings"] = json.loads(row["observations"]).get("deliverable_bindings")
            return derived
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
            if spec["schema"] == "phonefarm/compare@1":
                slots = list("ABCDEFGH"[:len(spec["items"])])
                limits = {slot: LIMITS.get(item.get("media_kind")) if isinstance(item, dict) else None
                          for slot, item in zip(slots, spec["items"])}
                blinding = "until_feedback"
            else:
                slots, limits, blinding = ["A", "B"], LIMITS[spec["media_kind"]], "presentation_only"
            derived["evaluation"] = {**spec, "media": {slot: "/research-state/request/" + quote(oid) + "/media/" + slot + "?" + query for slot in slots},
                                     "limits": limits, "blinding": blinding}
        return derived

    def _show(self, db, row, blind=False):
        value, bindings = json.loads(row["data"]), json.loads(row["bindings"])
        if blind and row["kind"] == "request" and (value.get("evaluation") or {}).get("schema") in {
                "phonefarm/ab-evaluation@1", "phonefarm/compare@1"}:
            answered = db.execute("SELECT 1 FROM revisions WHERE kind='feedback' AND request_id=? LIMIT 1", (row["id"],)).fetchone()
            if not answered:
                value["evidence_refs"], bindings = [], []
        current = db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (row["kind"], row["id"])).fetchone()
        return {"schema_version": 1, "node_id": self.node_id, "kind": row["kind"], "id": row["id"],
                "research_id": row["research_id"], "revision": row["revision"], "sequence": row["sequence"],
                "created_at": current["created_at"], "updated_at": row["at"], "data": value,
                "audit": json.loads(row["audit"]), "evidence_bindings": bindings,
                "derived": self._derived(db, row, value), "current_revision": current["revision"]}

    def get(self, kind, oid, revision=None, blind=False):
        self._kind(kind); identifier(oid)
        if revision is not None:
            positive(revision)
        with self._db() as db:
            row = (db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND revision=?", (kind, oid, revision)).fetchone()
                   if revision is not None else self._latest(db, kind, oid))
            if row is None:
                raise ResearchError(404, "research_not_found", "research object or version was not found")
            return self._show(db, row, blind)

    def open_questions(self):
        with self._db() as db:
            pending = set(self._unanswered_questions(db))
            if not pending:
                return []
            rows = db.execute("SELECT r.* FROM objects o JOIN revisions r ON r.kind=o.kind AND r.id=o.id "
                              "AND r.revision=o.revision WHERE o.kind='note' ORDER BY r.sequence DESC").fetchall()
            output = []
            for row in rows:
                if row["id"] not in pending:
                    continue
                data, audit = json.loads(row["data"]), json.loads(row["audit"])
                research = self._latest(db, "research", row["research_id"])
                output.append({"id": row["id"], "research_id": row["research_id"],
                               "research_title": research["title"] if research else row["research_id"],
                               "text": data["text"], "actor_label": audit.get("actor_label"), "updated_at": row["at"]})
                if len(output) == 50:
                    break
            return output

    def assessment(self, oid, revision):
        record = self.get("research", oid, positive(revision))
        criteria = record["data"].get("criteria")
        if criteria is None:
            raise ResearchError(409, "research_criteria_required", "this research revision has no numerical criteria")
        bindings = record["derived"]["criteria_bindings"]
        comparison = compare_results(criteria_rules.BoundResults(self.results, bindings), self.node_id, criteria["comparison"])
        return {"schema_version": 1, "node_id": self.node_id, "research_id": oid, "revision": record["revision"],
                "current_revision": record["current_revision"], "audit": record["audit"], "criteria": criteria,
                "criteria_bindings": bindings, **criteria_rules.assess(criteria, comparison)}

    def list(self, kind, query):
        self._kind(kind)
        limit = self._page(query, {"research_id", "state", "request_id", "follows"})
        filters = {key: query[key][0] for key in ("research_id", "state", "request_id", "follows") if query.get(key) and query[key][0]}
        for key, value in filters.items():
            if key == "state":
                if value not in STATES[kind]:
                    raise ValueError("unsupported state filter for this kind")
            else:
                identifier(value)
                if key == "request_id" and kind != "feedback":
                    raise ValueError("request_id filter is only supported for feedback")
                if key == "follows" and kind != "research":
                    raise ValueError("follows filter is only supported for research")
        binding = [self.node_id, "list", kind, filters]
        with self._db() as db:
            upper, after = self._cursor(query, binding, self._upper(db))
            terms = ["r.sequence IN (SELECT MAX(sequence) FROM revisions WHERE kind=? AND sequence<=? GROUP BY id)"]
            params = [kind, upper]
            for key, value in filters.items():
                terms.append("json_extract(r.data,'$.follows')=?" if key == "follows" else "r." + key + "=?")
                params.append(value)
            if after is not None:
                terms.append("r.sequence<?"); params.append(after)
            rows = db.execute("SELECT r.* FROM revisions r WHERE " + " AND ".join(terms) + " ORDER BY r.sequence DESC LIMIT ?",
                              (*params, limit + 1)).fetchall()
            items, guidance, questions = [], {}, {}
            for row in rows[:limit]:
                data = json.loads(row["data"])
                item = {"kind": kind, "id": row["id"], "research_id": row["research_id"], "revision": row["revision"],
                        "title": row["title"] or data.get("text", "")[:120], "state": row["state"],
                        "sequence": row["sequence"], "updated_at": row["at"]}
                if kind == "research":
                    item["owner"], item["follows"] = data.get("owner"), data.get("follows")
                if kind == "note":
                    audit = json.loads(row["audit"])
                    rid = row["research_id"]
                    if rid not in guidance:
                        research = self._latest(db, "research", rid)
                        owner = json.loads(research["data"]).get("owner") if research else None
                        guidance[rid] = set(self._unanswered_guidance(db, rid, owner))
                        questions[rid] = set(self._unanswered_questions(db, rid))
                    item.update(text=data["text"], reply_to=data["reply_to"], decision=data["decision"], to=data.get("to"),
                                evidence_refs=data["evidence_refs"], actor_label=audit.get("actor_label"),
                                auth_channel=audit.get("auth_channel"), ask=data.get("ask", False),
                                unanswered=row["id"] in guidance[rid] if audit.get("auth_channel") == "dashboard_steer" else None,
                                question_unanswered=row["id"] in questions[rid] if data.get("ask") is True else None)
                if kind == "direction":
                    item["hypothesis"] = data["hypothesis"]
                    item["metadata_blocked"] = any(dep["blocking"] and dep["resolution"] is None for dep in data["dependencies"])
                elif kind == "request":
                    item["feedback_count"] = db.execute("SELECT COUNT(*) FROM revisions WHERE kind='feedback' AND request_id=? AND sequence<=?", (row["id"], upper)).fetchone()[0]
                items.append(item)
        cursor = encode_cursor(binding + [upper, rows[limit - 1]["sequence"]]) if len(rows) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": items, "next_cursor": cursor}

    def history(self, kind, oid, query, blind=False):
        self._kind(kind); identifier(oid)
        limit = self._page(query)
        binding = [self.node_id, "history", kind, oid]
        with self._db() as db:
            if self._latest(db, kind, oid) is None:
                raise ResearchError(404, "research_not_found", "research object was not found")
            upper, after = self._cursor(query, binding, self._upper(db))
            rows = db.execute("SELECT * FROM revisions WHERE kind=? AND id=? AND sequence<=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                              (kind, oid, upper, after if after is not None else (1 << 63) - 1, limit + 1)).fetchall()
            items = [self._show(db, row, blind) for row in rows[:limit]]
        cursor = encode_cursor(binding + [upper, rows[limit - 1]["sequence"]]) if len(rows) > limit else None
        return {"schema_version": 1, "node_id": self.node_id, "items": items, "next_cursor": cursor}

    def _event_page(self, after, limit, rid, cursor):
        params, clause = [after], "sequence>?"
        if rid is not None:
            params.append(rid)
            clause += " AND research_id=?"
        with self._db() as db:
            head = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
            if after > head:
                raise ResearchError(409, "research_cursor_ahead", "event position is beyond this stream; preserve the original cursor")
            anchor = db.execute("SELECT operation_id FROM events WHERE sequence=?", (after,)).fetchone() if after else None
            if after and anchor is None or cursor is not None and cursor["operation_id"] != (anchor[0] if anchor else None):
                raise ResearchError(409, "research_cursor_history_changed", "original event position no longer matches; preserve the original cursor")
            rows = db.execute("SELECT * FROM events WHERE " + clause + " ORDER BY sequence LIMIT ?", (*params, limit + 1)).fetchall()
        items = [{"sequence": row["sequence"], "research_id": row["research_id"], "kind": row["kind"], "id": row["object_id"],
                  "revision": row["revision"], "operation_id": row["operation_id"], "at": row["at"]} for row in rows[:limit]]
        position = items[-1]["sequence"] if items else after
        operation = items[-1]["operation_id"] if items else anchor[0] if anchor else None
        next_cursor = encode_cursor({"v": 1, "node_id": self.node_id, "stream_id": self.stream_id,
                                     "research_id": rid, "after": position, "operation_id": operation})
        return {"schema_version": 1, "node_id": self.node_id, "stream_id": self.stream_id, "research_id": rid,
                "items": items, "head": head, "next_after": position, "next_cursor": next_cursor, "has_more": len(rows) > limit}

    def events(self, query):
        allowed = {"after", "limit", "research_id", "expected_node_id", "cursor", "wait_seconds"}
        if set(query) - allowed or any(len(v) != 1 for v in query.values()):
            raise ValueError("unsupported or repeated research event query")
        after, limit = int(query.get("after", [0])[0]), int(query.get("limit", [100])[0])
        wait = int(query.get("wait_seconds", [0])[0])
        if not 0 <= after <= (1 << 63) - 1 or not 1 <= limit <= 500 or not 0 <= wait <= 15:
            raise ValueError("invalid event sequence, page limit or wait_seconds")
        rid = query.get("research_id", [None])[0]
        if rid is not None:
            identifier(rid)
        cursor = None
        if "cursor" in query:
            if "after" in query:
                raise ValueError("cursor and after are mutually exclusive")
            cursor = decode_cursor(query["cursor"][0])
            if (not isinstance(cursor, dict) or set(cursor) != {"v", "node_id", "stream_id", "research_id", "after", "operation_id"}
                    or type(cursor["v"]) is not int or cursor["v"] != 1 or type(cursor["after"]) is not int
                    or not 0 <= cursor["after"] <= (1 << 63) - 1):
                raise ValueError("invalid research event cursor")
            if cursor["node_id"] != self.node_id or cursor["stream_id"] != self.stream_id:
                raise ResearchError(409, "research_cursor_stream_changed", "cursor belongs to another node or stream; preserve the original cursor")
            if rid is not None and rid != cursor["research_id"]:
                raise ResearchError(409, "research_cursor_scope_changed", "cursor research scope differs; choose a separate position for the new scope")
            rid, after = cursor["research_id"], cursor["after"]
            if rid is not None:
                identifier(rid)
            if cursor["operation_id"] is not None:
                identifier(cursor["operation_id"])
        value = self._event_page(after, limit, rid, cursor)
        if value["items"] or wait == 0:
            return {**value, "wait_result": "events" if value["items"] else "immediate"}
        if not self.watch_slots.acquire(blocking=False):
            raise ResearchError(429, "research_watch_busy", "research event wait capacity is occupied; retry explicitly later", retry_after_seconds=15)
        deadline = time.monotonic() + wait
        try:
            with self.event_changed:
                while True:
                    # Close the first-read/admission race, and reread after wake or
                    # deadline. No SQLite transaction remains open while waiting.
                    value = self._event_page(after, limit, rid, cursor)
                    if value["items"]:
                        return {**value, "wait_result": "events"}
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {**value, "wait_result": "timeout"}
                    self.event_changed.wait(remaining)
        finally:
            self.watch_slots.release()
