"""Registered agent identities and their own tokens; same persistence pattern as the node directory."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from artifact_io import SafeRoot
from job_dispatch import file_lock

SCHEMA = "phonefarm/agent-registry@1"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TOKEN = re.compile(r"hgag_([0-9a-f]{8})_[A-Za-z0-9_-]{43}\Z")


class AgentError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise AgentError(400, "agent name must be 1-64 lowercase letters, digits, dots, underscores or hyphens")
    return value


def _fields(body):
    if not isinstance(body, dict) or set(body) - {"name", "note", "tags", "enabled"}:
        raise AgentError(400, "unsupported agent fields")
    out = dict(body)
    if "name" in out:
        _name(out["name"])
    if "note" in out and (not isinstance(out["note"], str) or len(out["note"]) > 4096):
        raise AgentError(400, "note must be a string of at most 4096 characters")
    if "tags" in out:
        if (not isinstance(out["tags"], list) or len(out["tags"]) > 32
                or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 64 for tag in out["tags"])):
            raise AgentError(400, "tags must be a list of nonempty short strings")
        out["tags"] = list(dict.fromkeys(out["tags"]))
    if "enabled" in out and type(out["enabled"]) is not bool:
        raise AgentError(400, "enabled must be a boolean")
    return out


class NodeAgents:
    """Persistent agent registry. An agent that holds its own token is identified by it;
    only the token's sha256 is stored."""

    def __init__(self, data_root, local_node_id):
        self.local_node_id = local_node_id
        self._guard = threading.Lock()
        root = SafeRoot(data_root)
        try:
            root.mkdir("_node")
            root.mkdir("_node/agents")
            fd = root.open_fd("_node/agents", directory=True)
            try:
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                    raise AgentError(500, "agent registry storage must be private (0700)")
            finally:
                os.close(fd)
        finally:
            root.close()
        directory = os.path.join(os.path.realpath(data_root), "_node", "agents")
        self._store = SafeRoot(directory)
        self._lock_path = os.path.join(directory, "registry.lock")
        with file_lock(self._lock_path):
            self._store.publish_json_once("registry.json", {"schema": SCHEMA, "agents": []})
            self._read()

    def _read(self):
        with self._store.open_file("registry.json") as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                raise AgentError(500, "agent registry file must be private (0600)")
            value = json.load(stream)
        if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("agents"), list):
            raise AgentError(500, "agent registry storage is invalid")
        return value

    def _locked(self):
        from contextlib import contextmanager
        return contextmanager(lambda: self._locked_gen())()
    
    def _locked_gen(self):
        with self._guard:
            with file_lock(self._lock_path):
                yield self._read()

    def _public(self, entry):
        return {**{k: v for k, v in entry.items() if k != "credential"}, "credential_present": "credential" in entry}

    def _entry(self, state, name):
        entry = next((e for e in state["agents"] if e["name"] == name), None)
        if entry is None:
            raise AgentError(404, "registered agent was not found")
        return entry

    def issue_token(self, name):
        """Mint the agent's own token. Issuing again replaces the previous token."""
        _name(name)
        with self._locked() as state:
            entry = self._entry(state, name)
            if not entry.get("enabled", True):
                raise AgentError(409, "this agent is disabled")
            token = self._mint_token(entry)
            entry["revision"] += 1
            self._store.write_json("registry.json", state)
            return {"agent": self._public(entry), "token": token}

    def _mint_token(self, entry):
        token_id = secrets.token_hex(4)
        token = f"hgag_{token_id}_{secrets.token_urlsafe(32)}"
        now = time.time()
        entry["credential"] = {"id": token_id, "sha256": hashlib.sha256(token.encode()).hexdigest(), "issued_at": now}
        entry["updated_at"] = now
        return token

    def invite(self, hours=24, note=""):
        _fields({"note": note})
        try:
            seconds = float(hours) * 3600
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        if not math.isfinite(seconds) or seconds <= 0:
            raise AgentError(400, "hours must be a positive finite number")
        code = "hginv_" + secrets.token_urlsafe(32)
        now = time.time()
        entry = {"id": secrets.token_hex(8), "sha256": hashlib.sha256(code.encode()).hexdigest(),
                 "created_at": now, "expires_at": now + seconds, "note": note}
        with self._locked() as state:
            state.setdefault("invites", []).append(entry)
            self._store.write_json("registry.json", state)
        return {**{k: v for k, v in entry.items() if k != "sha256"}, "code": code}

    def invites(self):
        with self._locked() as state:
            return [{k: v for k, v in entry.items() if k != "sha256"}
                    for entry in state.get("invites", []) if "used_at" not in entry]

    def revoke_invite(self, invite_id):
        with self._locked() as state:
            state["invites"] = [entry for entry in state.get("invites", []) if entry["id"] != invite_id]
            self._store.write_json("registry.json", state)
        return {"id": invite_id, "revoked": True}

    def join(self, body):
        code = body.get("invite")
        if not isinstance(code, str) or not code:
            raise AgentError(400, "invite is required")
        fields = _fields({"name": body.get("name"), "note": body.get("note", "")})
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self._locked() as state:
            invite = next((entry for entry in state.get("invites", [])
                           if hmac.compare_digest(entry["sha256"], digest)), None)
            if invite is None:
                raise AgentError(404, "invite not found")
            if "used_at" in invite:
                raise AgentError(409, "invite already used")
            if time.time() >= invite["expires_at"]:
                raise AgentError(410, "invite expired")
            entry = self._create(state, fields)
            token = self._mint_token(entry)
            invite.update(used_at=time.time(), agent=entry["name"])
            # Identity, token hash and consumption are one registry replacement.
            self._store.write_json("registry.json", state)
            return {"node_id": self.local_node_id, "agent": self._public(entry), "token": token}

    def verify(self, token):
        """Name of the enabled agent this token belongs to, or None."""
        match = TOKEN.fullmatch(token or "")
        if not match:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._locked() as state:
            for entry in state["agents"]:
                credential = entry.get("credential") or {}
                if (credential.get("id") == match.group(1) and entry.get("enabled", True)
                        and hmac.compare_digest(credential["sha256"], digest)):
                    return entry["name"]
        return None

    def record_checkin(self, name, seen_event_seq):
        """Remember when the agent last checked in and how far it has read the research stream."""
        with self._locked() as state:
            entry = self._entry(state, name)
            previous = entry.get("checkin") or {}
            now = time.time()
            entry["checkin"] = {"at": now, "previous_at": previous.get("at"),
                                "seen_event_seq": max(previous.get("seen_event_seq", 0), seen_event_seq)}
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def list(self):
        with self._locked() as state:
            return [self._public(entry) for entry in state["agents"]]

    def get(self, name):
        _name(name)
        with self._locked() as state:
            entry = next((e for e in state["agents"] if e["name"] == name), None)
            if entry is None:
                raise AgentError(404, "registered agent was not found")
            return self._public(entry)

    def create(self, body):
        fields = _fields(body)
        if "name" not in fields:
            raise AgentError(400, "name is required")
        with self._locked() as state:
            entry = self._create(state, fields)
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def _create(self, state, fields):
        if any(entry["name"] == fields["name"] for entry in state["agents"]):
            raise AgentError(409, "an agent with this name is already registered")
        now = time.time()
        entry = {"tags": [], "note": "", "enabled": True,
                 **fields, "revision": 1, "node_id": self.local_node_id,
                 "created_at": now, "updated_at": now}
        state["agents"].append(entry)
        return entry

    def update(self, name, body):
        _name(name)
        if not isinstance(body, dict) or "revision" not in body:
            raise AgentError(400, "revision is required")
        revision = body.get("revision")
        fields = _fields({k: v for k, v in body.items() if k != "revision"})
        with self._locked() as state:
            entry = next((e for e in state["agents"] if e["name"] == name), None)
            if entry is None:
                raise AgentError(404, "registered agent was not found")
            if revision != entry["revision"]:
                raise AgentError(409, "agent revision changed; read it again")
            entry.update(fields)
            entry["revision"] += 1
            entry["updated_at"] = time.time()
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def post_status(self, name, body):
        """Agent 定期上传目的/方向/在做什么——声明式，不认证。"""
        _name(name)
        if not isinstance(body, dict) or "text" not in body:
            raise AgentError(400, "status requires a text field")
        text = body["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > 1024:
            raise AgentError(400, "status text must be 1-1024 characters")
        text = text.strip()
        detail = body.get("detail")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 4096):
            raise AgentError(400, "status detail must be a string of at most 4096 characters")
        now = time.time()
        with self._locked() as state:
            entry = next((e for e in state["agents"] if e["name"] == name), None)
            if entry is None:
                raise AgentError(404, "registered agent was not found")
            entry["status"] = {"text": text, "at": now, **({"detail": detail} if detail else {})}
            entry.setdefault("status_history", []).append({"text": text, "at": now})
            entry["status_history"] = entry["status_history"][-50:]
            entry["updated_at"] = now
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def close(self):
        self._store.close()
