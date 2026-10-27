"""Registered agent identities; same persistence pattern as the node directory."""
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

from artifact_io import SafeRoot
from job_dispatch import file_lock

SCHEMA = "phonefarm/agent-registry@1"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


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
    """Persistent agent registry; agent identity is declared, not authenticated."""

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
        return {**entry, "credential_present": False}

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
            if any(entry["name"] == fields["name"] for entry in state["agents"]):
                raise AgentError(409, "an agent with this name is already registered")
            now = time.time()
            entry = {"tags": [], "note": "", "enabled": True,
                     **fields, "revision": 1, "node_id": self.local_node_id,
                     "created_at": now, "updated_at": now}
            state["agents"].append(entry)
            self._store.write_json("registry.json", state)
            return self._public(entry)

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

    def close(self):
        self._store.close()
