"""Persistent remote-node directory; observations never reserve a remote device."""
from collections import deque
from contextlib import contextmanager
import copy
import http.client
import json
import os
import re
import socket
import stat
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

from artifact_io import SafeRoot
from job_dispatch import file_lock


SCHEMA = "phonefarm/node-directory@1"
REFRESH_S = 30
DEADLINE_S = 8
MAX_RESPONSE = 2 << 20
PUBLIC_FIELDS = ("name", "url", "tags", "note", "ssh", "goals_dir", "enabled", "revision",
                 "node_id", "created_at", "updated_at", "connection", "snapshot")
MUTABLE_FIELDS = {"url", "tags", "note", "ssh", "goals_dir", "enabled", "token"}


class DirectoryError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _ProbeError(Exception):
    def __init__(self, status, message, reachable=True):
        self.status, self.message, self.reachable = status, message, reachable


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", value) or value == "local":
        raise DirectoryError(400, "name must be 1-64 lowercase letters, digits, underscores or hyphens; local is reserved")
    return value


def _url(value):
    if not isinstance(value, str) or not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise DirectoryError(400, "url must be an explicit HTTP or HTTPS endpoint")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "\\" in value):
            raise ValueError
        parsed.port  # Reject invalid ports before any probe is scheduled.
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise DirectoryError(400, "url must not contain credentials, a query, a fragment or an invalid port") from None


def _fields(body, create=False):
    allowed = (MUTABLE_FIELDS - {"enabled"}) | {"name"} if create else MUTABLE_FIELDS | {"revision"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise DirectoryError(400, "unsupported directory fields")
    if create and ("name" not in body or "url" not in body):
        raise DirectoryError(400, "name and url are required")
    out = copy.deepcopy(body)
    if "name" in out:
        _name(out["name"])
    if "url" in out:
        out["url"] = _url(out["url"])
    if "enabled" in out and type(out["enabled"]) is not bool:
        raise DirectoryError(400, "enabled must be a boolean")
    if "revision" in out and (type(out["revision"]) is not int or out["revision"] < 1):
        raise DirectoryError(400, "revision must be a positive integer")
    if "tags" in out:
        if (not isinstance(out["tags"], list) or len(out["tags"]) > 64 or any(
                not isinstance(tag, str) or not tag.strip() or len(tag) > 128
                or any(ord(c) < 32 for c in tag) for tag in out["tags"])):
            raise DirectoryError(400, "tags must be a list of nonempty short strings")
        out["tags"] = list(dict.fromkeys(out["tags"]))
    for key in ("note", "ssh", "goals_dir"):
        if key in out and (not isinstance(out[key], str) or len(out[key]) > 4096 or "\x00" in out[key]):
            raise DirectoryError(400, key + " must be a string of at most 4096 characters")
    if "token" in out and out["token"] is not None:
        token = out["token"]
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise DirectoryError(400, "token must be null or a nonempty printable credential without whitespace")
    return out


def _connection(status, previous=None):
    previous = previous or {}
    return {"status": status, "reachable": None, "attempted_at": previous.get("attempted_at"),
            "last_seen_at": previous.get("last_seen_at"), "error": None}


class NodeDirectory:
    def __init__(self, data_root, local_node_id):
        if not isinstance(local_node_id, str) or not re.fullmatch(r"[0-9a-f]{32}", local_node_id):
            raise DirectoryError(500, "local node identity is invalid")
        self.local_node_id = local_node_id
        self._guard = threading.Condition(threading.RLock())
        self._pending = deque()
        self._inflight = {}
        self._workers = 0
        self._closed = False
        self._write_errors = {}
        root = SafeRoot(data_root)
        try:
            root.mkdir("_node")
            root.mkdir("_node/directory")
            fd = root.open_fd("_node/directory", directory=True)
            try:
                if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                    raise DirectoryError(500, "node directory storage must be private (0700)")
            finally:
                os.close(fd)
        finally:
            root.close()
        directory = os.path.join(os.path.realpath(data_root), "_node", "directory")
        self._store = SafeRoot(directory)
        self._lock_path = os.path.join(directory, "registry.lock")
        with self._guard, file_lock(self._lock_path):
            self._store.publish_json_once("registry.json", {"schema": SCHEMA, "nodes": []})
            self._read()

    def _read(self):
        with self._store.open_file("registry.json") as stream:
            if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o600:
                raise DirectoryError(500, "node directory registry must be private (0600)")
            raw = stream.read((128 << 20) + 1)
        if len(raw) > 128 << 20:
            raise DirectoryError(500, "node directory registry exceeds its read limit")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("nodes"), list):
            raise DirectoryError(500, "node directory registry format is invalid")
        return value

    @contextmanager
    def _locked(self):
        with self._guard:
            if self._closed:
                raise DirectoryError(503, "node directory is closed")
            try:
                with file_lock(self._lock_path):
                    yield self._read()
            except DirectoryError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise DirectoryError(500, "node directory storage is unavailable") from None

    @staticmethod
    def _find(state, name):
        entry = next((entry for entry in state["nodes"] if entry["name"] == name), None)
        if entry is None:
            raise DirectoryError(404, "registered node was not found")
        return entry

    def _public(self, entry):
        value = copy.deepcopy({key: entry[key] for key in PUBLIC_FIELDS})
        value["credential_present"] = bool(entry.get("token"))
        value["refreshing"] = self._inflight.get(entry["name"]) == entry["revision"]
        if self._write_errors.get(entry["name"]) == entry["revision"]:
            value["connection"] = {**value["connection"], "status": "unavailable",
                                   "error": "directory observation could not be saved"}
        snapshot = value["snapshot"]
        value["stale"] = (not entry["enabled"] or value["connection"]["status"] != "ready"
                          or snapshot is None or time.time() - snapshot["at"] > REFRESH_S)
        return value

    def list(self):
        with self._locked() as state:
            return [self._public(entry) for entry in state["nodes"]]

    def get(self, name):
        _name(name)
        with self._locked() as state:
            return self._public(self._find(state, name))

    def download_input(self, name, job_id, remote_path, limit):
        """One bounded credential-bearing artifact fetch for an explicit cross-node input.

        Returns at most `limit` bytes; the caller compares size and hash. The
        entry credential never leaves this method.
        """
        _name(name)
        from job_inputs import _job_id, _path
        _job_id(job_id)
        _path(remote_path)
        if type(limit) is not int or not 0 <= limit <= (16 << 20):
            raise DirectoryError(400, "download limit must be a small nonnegative integer")
        with self._locked() as state:
            entry = self._find(state, name)
            if not entry["enabled"]:
                raise DirectoryError(409, "registered node is disabled")
            if not entry.get("token"):
                raise DirectoryError(409, "registered node has no read credential")
            frozen = {key: entry[key] for key in ("url", "token", "node_id")}
        endpoint = urlsplit(frozen["url"])
        cls = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
        connection = cls(endpoint.hostname, endpoint.port, timeout=DEADLINE_S)
        try:
            connection.connect()
            connection.sock.settimeout(DEADLINE_S)
            headers = {"Authorization": "Bearer " + frozen["token"], "User-Agent": "pf-directory/1"}
            if frozen["node_id"]:
                headers["X-Expected-Node-ID"] = frozen["node_id"]
            path = "/jobs/" + job_id + "/out?path=" + quote(remote_path, safe="")
            connection.request("GET", endpoint.path + path, headers=headers)
            response = connection.getresponse()
            if response.status in (401, 403):
                raise DirectoryError(409, "remote node rejected the registered credential")
            if response.status == 404:
                raise DirectoryError(404, "remote source output is missing")
            if response.status != 200:
                raise DirectoryError(502, "remote node returned an unsuccessful status for the input source")
            chunks, size = [], 0
            while True:
                chunk = response.read1(min(64 << 10, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    break
            return b"".join(chunks)
        except DirectoryError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise DirectoryError(502, "remote input source could not be read: " + str(error)) from error
        finally:
            connection.close()

    def read_target(self, name, node_id, revision):
        """Private frozen read configuration; never expose its credential to clients."""
        _name(name)
        with self._locked() as state:
            entry = self._find(state, name)
            if entry["node_id"] != node_id:
                raise _ProbeError("identity_mismatch", "registered node does not match the selected identity")
            if entry["revision"] != revision:
                raise _ProbeError("configuration_changed", "registered node configuration changed; select it again explicitly")
            if not entry["enabled"]:
                raise _ProbeError("disabled", "registered node is disabled")
            if not entry.get("token"):
                raise _ProbeError("no_credentials", "registered node has no read credential")
            return copy.deepcopy(entry)

    def create(self, body):
        fields = _fields(body, create=True)
        with self._locked() as state:
            if any(entry["name"] == fields["name"] for entry in state["nodes"]):
                raise DirectoryError(409, "a node with this name is already registered")
            now = time.time()
            entry = {"tags": [], "note": "", "ssh": "", "goals_dir": "", "enabled": True,
                     "token": None, **fields, "revision": 1, "node_id": None,
                     "created_at": now, "updated_at": now, "snapshot": None}
            status = "disabled" if not entry["enabled"] else "unverified" if entry["token"] else "no_credentials"
            entry["connection"] = _connection(status)
            state["nodes"].append(entry)
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def update(self, name, body):
        _name(name)
        fields = _fields(body)
        if "revision" not in fields:
            raise DirectoryError(400, "revision is required")
        with self._locked() as state:
            entry = self._find(state, name)
            if fields.pop("revision") != entry["revision"]:
                raise DirectoryError(409, "node configuration revision changed; read it again")
            changed = any(entry.get(key) != fields[key] for key in ("url", "token", "enabled") if key in fields)
            entry.update(fields)
            entry["revision"] += 1
            entry["updated_at"] = time.time()
            if changed:
                status = "disabled" if not entry["enabled"] else "unverified" if entry["token"] else "no_credentials"
                entry["connection"] = _connection(status, entry["connection"])
                entry["connection"]["attempted_at"] = None
            self._store.write_json("registry.json", state)
            return self._public(entry)

    def refresh(self, name=None, devices=False):
        if name is not None:
            _name(name)
        if type(devices) is not bool or devices and name is None:
            raise DirectoryError(400, "device refresh requires one explicitly selected node")
        with self._locked() as state:
            entries = [self._find(state, name)] if name is not None else state["nodes"]
            if devices and name in self._inflight:
                raise DirectoryError(409, "node refresh already in progress; query it before requesting device sampling")
            scheduled = []
            now = time.time()
            for entry in entries:
                attempted = entry["connection"]["attempted_at"]
                if (not entry["enabled"] or not entry.get("token") or entry["name"] in self._inflight
                        or not devices and attempted is not None and now - attempted < REFRESH_S):
                    continue
                entry["connection"]["attempted_at"] = now
                scheduled.append((copy.deepcopy(entry), devices))
            if scheduled:
                self._store.write_json("registry.json", state)
                for entry, device_refresh in scheduled:
                    self._inflight[entry["name"]] = entry["revision"]
                    self._pending.append((entry, device_refresh))
                while self._workers < min(4, len(self._inflight)):
                    self._workers += 1
                    threading.Thread(target=self._worker, daemon=True, name="node-directory").start()
                self._guard.notify_all()
            public = [self._public(entry) for entry in entries]
            return public[0] if name is not None else public

    def close(self):
        with self._guard:
            self._closed = True
            self._pending.clear()
            self._inflight.clear()
            self._guard.notify_all()
            if not self._workers:
                self._store.close()

    def _worker(self):
        try:
            while True:
                with self._guard:
                    self._guard.wait_for(lambda: self._closed or self._pending)
                    if self._closed:
                        return
                    entry, devices = self._pending.popleft()
                try:
                    # A queued refresh may have been disabled or reconfigured.
                    with self._locked() as state:
                        current = self._find(state, entry["name"])
                        if current["revision"] != entry["revision"] or not current["enabled"]:
                            continue
                    self._probe(entry, devices)
                except (DirectoryError, OSError, ValueError, KeyError, TypeError):
                    with self._guard:
                        self._write_errors[entry["name"]] = entry["revision"]
                finally:
                    with self._guard:
                        self._inflight.pop(entry["name"], None)
                        self._guard.notify_all()
        finally:
            with self._guard:
                self._workers -= 1
                if self._closed and not self._workers:
                    self._store.close()
                self._guard.notify_all()

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _request(self, entry, path, deadline, expected_node_id=None, result_not_found=False):
        endpoint = urlsplit(entry["url"])
        cls = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
        connection = cls(endpoint.hostname, endpoint.port, timeout=self._remaining(deadline))
        socket_ref = [None]

        def expire():
            sock = socket_ref[0] or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(self._remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        try:
            # DNS may outlast socket timeouts; never send credentials after it
            # returns past the deadline. The timer also bounds trickling bodies.
            connection.connect()
            sock = socket_ref[0] = connection.sock
            sock.settimeout(self._remaining(deadline))
            headers = {"Authorization": "Bearer " + entry["token"], "User-Agent": "pf-directory/1"}
            if expected_node_id is not None:
                headers["X-Expected-Node-ID"] = expected_node_id
            connection.request("GET", endpoint.path + path, headers=headers)
            sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if response.status in (401, 403):
                raise _ProbeError("unauthorized", "remote node rejected the credential")
            if 300 <= response.status < 400:
                raise _ProbeError("incompatible", "remote node redirected the request; redirects are not followed")
            if response.status == 404 and not result_not_found:
                raise _ProbeError("incompatible", "remote node does not support the required read API")
            if expected_node_id is not None and response.status == 409:
                raise _ProbeError("identity_mismatch", "remote node rejected the selected identity")
            if expected_node_id is not None and response.status == 400 and "cursor=" in path:
                raise _ProbeError("invalid_cursor", "source rejected the cursor or its original filters; do not restart automatically")
            if response.status != 200 and not (response.status == 404 and result_not_found):
                raise _ProbeError("unavailable", "remote node returned an unsuccessful HTTP status")
            chunks, size = [], 0
            while True:
                if response.isclosed():
                    break
                sock.settimeout(self._remaining(deadline))
                chunk = response.read1(min(64 << 10, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise _ProbeError("incompatible", "remote node response exceeds 2 MiB")
            self._remaining(deadline)
            try:
                value = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise _ProbeError("incompatible", "remote node did not return valid JSON") from None
            if response.status == 404:
                if isinstance(value, dict) and value.get("code") == "result_not_found":
                    raise _ProbeError("not_found", "source has no registered result with this ID")
                raise _ProbeError("incompatible", "remote node does not support the required result detail API")
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise _ProbeError("incompatible", "remote node returned an incompatible read protocol")
            return value
        finally:
            timer.cancel()
            connection.close()

    def _pin(self, entry, node_id):
        if not isinstance(node_id, str) or not re.fullmatch(r"[0-9a-f]{32}", node_id):
            raise _ProbeError("incompatible", "remote node identity is invalid")
        with self._locked() as state:
            current = self._find(state, entry["name"])
            if current["revision"] != entry["revision"] or not current["enabled"]:
                return False
            if (node_id == self.local_node_id or current["node_id"] not in (None, node_id)
                    or any(other["name"] != entry["name"] and other["node_id"] == node_id for other in state["nodes"])):
                raise _ProbeError("identity_mismatch", "remote node identity does not match this registration or is already registered")
            if current["node_id"] is None:
                current["node_id"] = node_id
                self._store.write_json("registry.json", state)
            return True

    def _probe(self, entry, devices):
        deadline = time.monotonic() + DEADLINE_S
        snapshot, failure, reachable = None, None, False
        try:
            identity = self._request(entry, "/node", deadline)
            reachable = True
            node_id = identity.get("node_id")
            if not self._pin(entry, node_id):
                return
            capabilities = identity.get("capabilities")
            if not isinstance(capabilities, list) or "fleet.cached-v1" not in capabilities:
                raise _ProbeError("incompatible", "remote node must support fleet.cached-v1")
            fleet = self._request(entry, "/fleet?wait=5" if devices else "/fleet?cached=1", deadline)
            if fleet.get("node_id") != node_id:
                raise _ProbeError("identity_mismatch", "remote read responses disagree about node identity")
            queue = self._request(entry, "/queue", deadline)
            if queue.get("node_id") != node_id:
                raise _ProbeError("identity_mismatch", "remote read responses disagree about node identity")
            if ((fleet.get("devices") is not None and not isinstance(fleet.get("devices"), list))
                    or not isinstance(fleet.get("lock"), dict)
                    or fleet["lock"].get("status") not in {"free", "held", "unknown"}
                    or "devices" not in fleet or not isinstance(queue.get("items"), list)):
                raise _ProbeError("incompatible", "remote node returned an invalid fleet or queue snapshot")
            snapshot = {"at": time.time(), "url": entry["url"], "identity": identity, "fleet": fleet, "queue": queue}
        except _ProbeError as error:
            failure, reachable = error, error.reachable
        except (OSError, http.client.HTTPException):
            failure = _ProbeError("unavailable", "remote node could not be read before the deadline", reachable)
        with self._locked() as state:
            current = self._find(state, entry["name"])
            if current["revision"] != entry["revision"] or not current["enabled"]:
                return
            connection = current["connection"]
            if snapshot is not None:
                current["snapshot"] = snapshot
                connection.update(status="ready", reachable=True, last_seen_at=snapshot["at"], error=None)
            else:
                connection.update(status=failure.status, reachable=reachable, error=failure.message)
            self._store.write_json("registry.json", state)
            self._write_errors.pop(entry["name"], None)
