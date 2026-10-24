#!/usr/bin/env python3
"""Prepare declared game resources on the host. This module never installs them."""
import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import http.client
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from artifact_io import SafeRoot, relative_path, stamp
from game_deploy_recipe import validate_recipe, validate_installation, validate_initialization
from job_runtime import runner_alive


CHUNK = 1 << 20
UPLOAD_READ = 256 << 10
UPLOAD_MAX = 8 << 20
RESERVE_BYTES = 64 << 20
RECEIPT_SCHEMA = "phonefarm/game-asset@1"
UPLOAD_SCHEMA = "phonefarm/game-upload@1"


class AssetError(ValueError):
    """An error safe for a public preparation report; never includes final CDN URLs."""


class AssetUploadError(AssetError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _upload_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise AssetUploadError(400, "invalid_upload_id", "invalid upload ID")
    return value


def _public_url(value, redirected=False):
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise AssetError("asset URL must be an explicit HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query and not redirected or "\\" in value):
            raise ValueError
        parsed.port
    except ValueError:
        raise AssetError("asset URL must be HTTP(S), without credentials, query or fragment") from None
    return value


def _relative(value):
    try:
        relative_path(value)
    except ValueError:
        raise AssetError("asset path must be relative and must not contain parent traversal") from None
    if not value or any(part in {"", ".", ".."} for part in value.split("/")) or any(ord(c) < 32 for c in value):
        raise AssetError("asset path must be a nonempty logical relative path")
    return value


def _asset(value):
    if not isinstance(value, dict) or set(value) - {"name", "role", "sha256", "size", "source", "target"}:
        raise AssetError("invalid asset fields")
    for key in ("name", "role"):
        if (not isinstance(value.get(key), str) or not value[key].strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in value[key])):
            raise AssetError("asset name and role must be nonempty text without control characters")
    if not isinstance(value.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise AssetError("asset sha256 must be 64 lowercase hexadecimal characters")
    if type(value.get("size")) is not int or value["size"] < 0:
        raise AssetError("asset size must be a nonnegative byte count")
    if "target" in value:
        _relative(value["target"])
    source = value.get("source")
    if not isinstance(source, dict):
        raise AssetError("asset source must be an object")
    if source.get("kind") == "url" and set(source) == {"kind", "url"}:
        _public_url(source["url"])
    elif source.get("kind") == "artifact" and set(source) == {"kind", "job_id", "path"}:
        if not isinstance(source["job_id"], str) or not re.fullmatch(r"[0-9a-f]{12}", source["job_id"]):
            raise AssetError("artifact source requires a job ID")
        _relative(source["path"])
    elif source.get("kind") == "unavailable" and set(source) == {"kind", "reason"}:
        if not isinstance(source["reason"], str) or not source["reason"].strip():
            raise AssetError("unavailable source requires a reason")
    else:
        raise AssetError("unsupported asset source")
    return value


def validate_manifest(value):
    metadata = {"application", "engine", "requirements", "scene", "provenance", "license", "deployment", "installation", "initialization"}
    if not isinstance(value, dict) or set(value) - ({"schema_version", "game_id", "version", "title", "assets"} | metadata):
        raise AssetError("unsupported game manifest fields")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise AssetError("unsupported game manifest schema")
    for key in ("game_id", "version"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value[key]):
            raise AssetError(key + " must be 1-64 lowercase letters, digits, dots, underscores or hyphens")
    if (not isinstance(value.get("title"), str) or not value["title"].strip()
            or any(ord(c) < 32 for c in value["title"])):
        raise AssetError("game title must be nonempty text")
    if any(not isinstance(value[key], dict) for key in metadata if key in value):
        raise AssetError("game descriptive metadata must be objects")
    if not isinstance(value.get("assets"), list) or not value["assets"]:
        raise AssetError("game manifest requires a nonempty assets list")
    names, sizes = set(), {}
    for asset in value["assets"]:
        _asset(asset)
        if asset["name"] in names:
            raise AssetError("asset names must be unique")
        names.add(asset["name"])
        if asset["sha256"] in sizes and sizes[asset["sha256"]] != asset["size"]:
            raise AssetError("the same content hash cannot declare different sizes")
        sizes[asset["sha256"]] = asset["size"]
    if "deployment" in value:
        validate_recipe(value["deployment"], value["assets"])
    if "installation" in value:
        validate_installation(value["installation"], value["assets"])
    if "initialization" in value:
        validate_initialization(value["initialization"], value["assets"])
    return copy.deepcopy(value)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl, redirected=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _observation(state, size, count=0, error=None, verified_at=None, **extra):
    return {"state": state, "bytes": count, "total": size, "verified_at": verified_at,
            "error": error, **extra}


class AssetCache:
    def __init__(self, cache_root):
        self.root = os.path.abspath(cache_root)

    def object_path(self, sha256):
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise AssetError("invalid content hash")
        return os.path.join(self.root, "blobs", sha256)

    @contextmanager
    def _storage(self, create=False):
        if create:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
        try:
            store = SafeRoot(self.root)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        try:
            if create:
                for directory in ("blobs", "partial", "receipts", "locks", "quarantine", "uploads"):
                    store.mkdir(directory)
            yield store
        finally:
            store.close()

    @staticmethod
    def _info(store, rel):
        try:
            with store.open_file(rel) as stream:
                info = os.fstat(stream.fileno())
                if info.st_nlink != 1:
                    raise AssetError("cache objects must not be hard links")
                return info
        except FileNotFoundError:
            return None

    def observe(self, asset):
        """Inspect metadata only; even a missing cache root is never created here."""
        _asset(asset)
        sha, size = asset["sha256"], asset["size"]
        try:
            with self._storage() as store:
                if store is not None:
                    info = self._info(store, "blobs/" + sha)
                    if info is not None:
                        try:
                            receipt = store.read_json("receipts/" + sha + ".json", maximum=64 << 10)
                        except FileNotFoundError:
                            receipt = {}
                        if (isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA and receipt.get("sha256") == sha
                                and receipt.get("size") == size == info.st_size
                                and receipt.get("identity") == list(stamp(info))
                                and isinstance(receipt.get("verified_at"), (int, float))):
                            return _observation("verified", size, size, verified_at=receipt["verified_at"])
                        return _observation("unknown", size, info.st_size, "completed object needs verification")
                    info = self._info(store, "partial/" + sha + ".part")
                    if info is not None:
                        if asset["source"]["kind"] == "unavailable":
                            return _observation("unavailable", size, info.st_size, asset["source"]["reason"])
                        return _observation("missing", size, info.st_size)
            if asset["source"]["kind"] == "unavailable":
                return _observation("unavailable", size, error=asset["source"]["reason"])
            return _observation("missing", size)
        except (OSError, ValueError, TypeError):
            return _observation("unknown", size, error="cache file or verification receipt is unreadable")

    @contextmanager
    def _lock(self, store, sha, wait=True, create=True):
        parent = store.open_fd("locks", directory=True)
        try:
            fd = os.open(sha + ".lock", os.O_RDWR | (os.O_CREAT if create else 0)
                         | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AssetError("content lock is not a private regular file")
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not wait:
                        raise AssetUploadError(409, "upload_busy", "content is busy; query the same upload before retrying") from None
                    time.sleep(0.1)  # SIGINT still interrupts waiting for another producer.
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _space(store, remaining):
        usage = os.fstatvfs(store.fd)
        if usage.f_bavail * usage.f_frsize < max(0, remaining) + RESERVE_BYTES:
            raise AssetError("insufficient cache space for remaining bytes and 64 MiB reserve")

    @staticmethod
    def _digest(stream):
        before, digest = os.fstat(stream.fileno()), hashlib.sha256()
        stream.seek(0)
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        if stamp(before) != stamp(os.fstat(stream.fileno())):
            raise AssetError("source changed during verification")
        return digest, before.st_size

    def _quarantine(self, store, rel, asset, reason, digest=None):
        info = self._info(store, rel)
        if info is None:
            raise AssetError("rejected content disappeared before quarantine")
        target = "quarantine/" + asset["sha256"] + "-" + uuid.uuid4().hex
        store.replace(rel, target)
        store.sync(target, directory=False)
        store.sync(os.path.dirname(rel), directory=True)
        record = {"path": target, "reason": reason, "size": info.st_size,
                  "actual_sha256": digest, "expected_sha256": asset["sha256"], "at": time.time()}
        store.write_json(target + ".json", record)
        return record

    def _complete(self, store, asset, rel, verified_identity):
        sha, size = asset["sha256"], asset["size"]
        destination = "blobs/" + sha
        fd = store.open_fd(rel, directory=False)
        try:
            original = os.fstat(fd)
            if stamp(original) != verified_identity or original.st_nlink != 1:
                raise AssetError("verified content changed before publication")
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            if rel != destination:
                store.replace(rel, destination)
                store.sync(os.path.dirname(rel), directory=True)
            store.sync(destination, directory=False)
            info = self._info(store, destination)
            if (info is None or info.st_size != size
                    or (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino)):
                raise AssetError("verified content changed before publication")
            at = time.time()
            store.write_json("receipts/" + sha + ".json", {"schema": RECEIPT_SCHEMA, "sha256": sha,
                             "size": size, "identity": list(stamp(info)), "verified_at": at})
            return _observation("verified", size, size, verified_at=at)
        finally:
            os.close(fd)

    @contextmanager
    def _partial(self, store, sha):
        parent = store.open_fd("partial", directory=True)
        try:
            fd = os.open(sha + ".part", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(fd, "r+b") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AssetError("partial content must be a private regular file")
            try:
                yield stream
            finally:
                stream.flush()
                os.fsync(stream.fileno())
                store.sync("partial", directory=True)

    @staticmethod
    def _read_upload(store, asset):
        try:
            record = store.read_json("uploads/" + asset["sha256"] + ".json", maximum=64 << 10)
        except FileNotFoundError:
            return None
        if (not isinstance(record, dict) or record.get("schema") != UPLOAD_SCHEMA
                or record.get("sha256") != asset["sha256"]
                or type(record.get("size")) is not int or record["size"] < 0
                or record.get("state") not in {"open", "sealed", "cancelled", "verified", "failed"}
                or not isinstance(record.get("upload_id"), str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", record["upload_id"])
                or record.get("error") is not None and not isinstance(record["error"], str)):
            raise AssetError("upload reservation is unreadable")
        if record["size"] != asset["size"]:
            raise AssetUploadError(409, "upload_size_conflict", "content hash already has a different declared size")
        if record["state"] == "sealed":
            identity = record.get("sealed_identity")
            if (not isinstance(identity, list) or len(identity) != 6
                    or any(type(value) is not int for value in identity) or identity[3] != asset["size"]):
                raise AssetError("sealed upload identity is unreadable")
        return record

    @staticmethod
    def _save_upload(store, record, state=None, **extra):
        updated = {**record, **extra, "updated_at": time.time()}
        if state is not None:
            updated["state"] = state
        store.write_json("uploads/" + updated["sha256"] + ".json", updated)
        return updated

    @contextmanager
    def _upload_locked(self, asset, upload_id, wait=False):
        _asset(asset)
        _upload_id(upload_id)
        try:
            with self._storage(create=True) as store, self._lock(store, asset["sha256"], wait=wait):
                yield store
        except AssetUploadError:
            raise
        except (OSError, ValueError):
            raise AssetUploadError(503, "upload_storage_error", "upload storage could not be read or saved") from None

    def _upload_state(self, store, asset, upload_id, record, busy=False):
        if record is None:
            if self.observe(asset)["state"] == "verified":
                return {"state": "verified", "offset": asset["size"], "error": None, "cache_hit": True}
            return {"state": "unclaimed", "offset": None, "error": None}
        if record["upload_id"] != upload_id:
            return {"state": "superseded", "offset": None, "error": "another upload owns this content"}
        state = record["state"]
        if busy:
            # A sealed record is published only after the complete prefix and
            # its directory entry have been synced, before any verifier starts.
            if state == "verified" and self.observe(asset)["state"] != "verified":
                return {"state": "unknown", "offset": None, "error": "verified content identity changed", "busy": True}
            if state in {"sealed", "verified"}:
                return {"state": state, "offset": asset["size"], "error": record.get("error"), "busy": True}
            return {"state": "busy", "offset": None, "error": None, "busy": True}
        if state in {"sealed", "verified"}:
            if self.observe(asset)["state"] == "verified":
                return {"state": "verified", "offset": asset["size"], "error": None, "cache_hit": True}
            info = self._info(store, "partial/" + asset["sha256"] + ".part")
            if state == "verified" or info is None or list(stamp(info)) != record["sealed_identity"]:
                return {"state": "unknown", "offset": None, "error": "sealed upload content identity changed"}
            return {"state": "sealed", "offset": asset["size"], "error": None}
        if state == "failed":
            return {"state": state, "offset": None, "error": record.get("error")}
        rel = "partial/" + asset["sha256"] + ".part"
        info = self._info(store, rel)
        if info is not None:
            # A killed writer may not have reached its finally block. Sync the
            # existing prefix before exposing a resumable offset; never create it.
            store.sync(rel, directory=False)
        return {"state": state, "offset": info.st_size if info is not None else 0, "error": record.get("error")}

    def upload_status(self, asset, upload_id):
        """Confirm the persisted prefix without creating paths or hashing.

        A nonblocking SHA lock excludes a writer. Existing open/cancelled bytes
        and their parent are then synced: fstat alone is not a durability proof
        after a writer crash or failed fsync. Any barrier failure returns unknown.
        """
        _asset(asset)
        _upload_id(upload_id)
        try:
            with self._storage() as store:
                if store is None:
                    return {"state": "unclaimed", "offset": None, "error": None}
                record = self._read_upload(store, asset)
                if record is None:
                    return self._upload_state(store, asset, upload_id, None)
                try:
                    with self._lock(store, asset["sha256"], wait=False, create=False):
                        return self._upload_state(store, asset, upload_id, self._read_upload(store, asset))
                except AssetUploadError as error:
                    if error.code != "upload_busy":
                        raise
                    return self._upload_state(store, asset, upload_id, self._read_upload(store, asset), busy=True)
        except (OSError, ValueError):
            return {"state": "unknown", "offset": None, "error": "upload storage is unreadable"}

    def upload_open(self, asset, upload_id):
        with self._upload_locked(asset, upload_id) as store:
            record = self._read_upload(store, asset)
            if record is not None and record["upload_id"] == upload_id:
                return self._upload_state(store, asset, upload_id, record)
            if record is not None and record["state"] in {"open", "sealed"}:
                raise AssetUploadError(409, "upload_owned", "another upload owns this content; cancel it before a new attempt")
            if self.observe(asset)["state"] == "verified":
                return {"state": "verified", "offset": asset["size"], "error": None, "cache_hit": True}
            if self._info(store, "blobs/" + asset["sha256"]) is not None:
                raise AssetUploadError(409, "cache_needs_verification", "existing completed content needs a preparation job before upload")
            partial = self._info(store, "partial/" + asset["sha256"] + ".part")
            if partial is not None and partial.st_size > asset["size"]:
                raise AssetUploadError(409, "upload_invalid_partial", "existing partial exceeds the declared size")
            self._space(store, asset["size"] - (partial.st_size if partial else 0))
            # Create/sync even an empty prefix before publishing its owner.
            with self._partial(store, asset["sha256"]):
                pass
            record = self._save_upload(store, {"schema": UPLOAD_SCHEMA, "upload_id": upload_id,
                "sha256": asset["sha256"], "size": asset["size"], "state": "open", "error": None})
            return self._upload_state(store, asset, upload_id, record)

    def _owned_upload(self, store, asset, upload_id, states):
        record = self._read_upload(store, asset)
        if record is None or record["upload_id"] != upload_id:
            raise AssetUploadError(409, "upload_not_owner", "upload no longer owns this content")
        if record["state"] not in states:
            raise AssetUploadError(409, "upload_not_writable", "upload state does not permit this operation")
        return record

    def upload_chunk(self, asset, upload_id, offset, length, stream):
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= UPLOAD_MAX:
            raise AssetUploadError(400, "invalid_upload_chunk", "chunk requires a nonnegative offset and 1 byte to 8 MiB length")
        with self._upload_locked(asset, upload_id) as store:
            record = self._owned_upload(store, asset, upload_id, {"open"})
            with self._partial(store, asset["sha256"]) as destination:
                actual = os.fstat(destination.fileno()).st_size
                if offset != actual:
                    raise AssetUploadError(409, "upload_offset_mismatch", "upload offset changed; query the same upload before retrying")
                if offset + length > asset["size"]:
                    raise AssetUploadError(409, "upload_size_exceeded", "chunk exceeds the declared asset size")
                self._space(store, asset["size"] - offset)
                destination.seek(offset)
                remaining = length
                try:
                    while remaining:
                        block = stream.read(min(UPLOAD_READ, remaining))
                        if not isinstance(block, bytes) or not block or len(block) > min(UPLOAD_READ, remaining):
                            raise AssetUploadError(400, "upload_incomplete", "chunk ended before its declared length; query the persisted offset")
                        destination.write(block)
                        remaining -= len(block)
                except (OSError, http.client.HTTPException):
                    raise AssetUploadError(400, "upload_interrupted", "chunk was interrupted; query the persisted offset") from None
            return self._upload_state(store, asset, upload_id, record)

    def upload_seal(self, asset, upload_id):
        with self._upload_locked(asset, upload_id) as store:
            record = self._read_upload(store, asset)
            if record is None and self.observe(asset)["state"] == "verified":
                return {"state": "verified", "offset": asset["size"], "error": None, "cache_hit": True}
            record = self._owned_upload(store, asset, upload_id, {"open", "sealed", "verified"})
            if record["state"] != "open":
                return self._upload_state(store, asset, upload_id, record)
            rel = "partial/" + asset["sha256"] + ".part"
            info = self._info(store, rel)
            if info is None or info.st_size != asset["size"]:
                raise AssetUploadError(409, "upload_incomplete", "upload has not received its declared size")
            store.sync(rel, directory=False)
            record = self._save_upload(store, record, "sealed", sealed_identity=list(stamp(info)))
            return self._upload_state(store, asset, upload_id, record)

    def upload_cancel(self, asset, upload_id):
        with self._upload_locked(asset, upload_id) as store:
            record = self._read_upload(store, asset)
            if record is None or record["upload_id"] != upload_id or record["state"] in {"cancelled", "verified", "failed"}:
                return self._upload_state(store, asset, upload_id, record)
            # Persist the prefix before releasing its reservation. An old queued
            # verifier must check this same owner/state after taking the SHA lock.
            rel = "partial/" + asset["sha256"] + ".part"
            if self._info(store, rel) is not None:
                store.sync(rel, directory=False)
            record = self._save_upload(store, record, "cancelled", error=None)
            return self._upload_state(store, asset, upload_id, record)

    def verify_upload(self, asset, upload_id, on_progress=None):
        _asset(asset)
        _upload_id(upload_id)

        def progress(state, count=0, **extra):
            value = _observation(state, asset["size"], count, **extra)
            if on_progress:
                on_progress(value)
            return value

        progress("waiting")
        with self._upload_locked(asset, upload_id, wait=True) as store:
            record = self._owned_upload(store, asset, upload_id, {"sealed", "verified"})
            observation = self.observe(asset)
            if observation["state"] == "verified":
                self._save_upload(store, record, "verified", error=None)
                return {**observation, "cache_hit": True}
            if record["state"] != "sealed":
                raise AssetUploadError(409, "upload_identity_changed", "previously verified content changed")
            rel = "partial/" + asset["sha256"] + ".part"
            info = self._info(store, rel)
            if info is None or list(stamp(info)) != record["sealed_identity"]:
                raise AssetUploadError(409, "upload_identity_changed", "sealed upload content identity changed")
            progress("verifying", info.st_size)
            with store.open_file(rel) as stream:
                digest, count = self._digest(stream)
                identity = stamp(os.fstat(stream.fileno()))
            if list(identity) != record["sealed_identity"]:
                raise AssetUploadError(409, "upload_identity_changed", "sealed upload changed during verification")
            if count != asset["size"] or digest.hexdigest() != asset["sha256"]:
                reason = "uploaded content failed size/hash verification"
                rejected = self._quarantine(store, rel, asset, reason, digest.hexdigest())
                self._save_upload(store, record, "failed", error=reason, quarantine=rejected)
                return progress("failed", count, error=reason, quarantine=[rejected])
            result = self._complete(store, asset, rel, identity)
            self._save_upload(store, record, "verified", error=None)
            return {**result, "cache_hit": False}

    @contextmanager
    def _artifact(self, jobs_root, source):
        with_root = SafeRoot(jobs_root)
        try:
            jid = source["job_id"]
            meta = with_root.read_json(jid + "/meta.json")
            if (not isinstance(meta, dict) or meta.get("id") != jid or meta.get("state") != "finished"
                    or runner_alive(with_root, jid)):
                raise AssetError("artifact source job must be finished without a live worker")
            with with_root.open_file(jid + "/out/" + source["path"]) as stream:
                yield stream
        finally:
            with_root.close()

    def _download(self, asset, stream, digest, offset, store, progress):
        headers = {"User-Agent": "phonefarm-assets/1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = "bytes=" + str(offset) + "-"
        request = urllib.request.Request(asset["source"]["url"], headers=headers)
        try:
            response = urllib.request.build_opener(_Redirect()).open(request, timeout=30)
        except urllib.error.HTTPError as error:
            error.close()
            raise AssetError("download returned HTTP " + str(error.code) + "; partial retained") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise AssetError("download connection failed; partial retained") from None
        with response:
            if response.status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if (not match or int(match[1]) != offset or int(match[2]) < offset
                        or int(match[3]) != asset["size"] or int(match[2]) != asset["size"] - 1):
                    raise AssetError("download Content-Range does not match the requested partial")
            elif response.status == 200:
                if offset:
                    self._space(store, asset["size"])
                    stream.seek(0)
                    stream.truncate()
                    digest, offset = hashlib.sha256(), 0
            else:
                raise AssetError("download returned an unsupported status; partial retained")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise AssetError("encoded download cannot be matched to declared byte offsets")
            count, resumed = offset, offset
            stream.seek(offset)
            progress("downloading", count, resumed_bytes=resumed, transferred_bytes=0)
            try:
                while True:
                    chunk = response.read1(CHUNK)
                    if not chunk:
                        break
                    stream.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
                    progress("downloading", count, resumed_bytes=resumed, transferred_bytes=count - resumed)
                    if count > asset["size"]:
                        break
            except (OSError, ValueError, http.client.HTTPException):
                raise AssetError("download interrupted; partial retained") from None
            if count < asset["size"]:
                raise AssetError("download ended before the declared size; partial retained")
            return digest, count, {"resumed_bytes": resumed, "transferred_bytes": count - resumed}

    def _import(self, asset, source, stream, offset, digest, progress):
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != asset["size"]:
            raise AssetError("artifact size does not match the declaration")
        # Existing bytes are hashed once; the final hash validates both this prefix
        # and the newly copied suffix against the declared content identity.
        source.seek(offset)
        stream.seek(offset)
        count = offset
        progress("importing", count, resumed_bytes=offset, transferred_bytes=0)
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            stream.write(chunk)
            digest.update(chunk)
            count += len(chunk)
            progress("importing", count, resumed_bytes=offset, transferred_bytes=count - offset)
        if stamp(before) != stamp(os.fstat(source.fileno())):
            raise AssetError("artifact changed during import; partial retained")
        return digest, count, {"resumed_bytes": offset, "transferred_bytes": count - offset}

    def prepare(self, asset, jobs_root, on_progress=None):
        return self._prepare(asset, jobs_root, on_progress)

    def import_file(self, asset, source, on_progress=None):
        """Legacy adapter only: caller supplies an already safely opened regular file."""
        return self._prepare(asset, None, on_progress, source)

    def _prepare(self, asset, jobs_root, on_progress, local_source=None):
        _asset(asset)
        sha, size = asset["sha256"], asset["size"]
        quarantine = []

        def progress(state, count=0, **extra):
            value = _observation(state, size, count, quarantine=list(quarantine), **extra)
            if on_progress:
                on_progress(value)
            return value

        progress("waiting")
        with self._storage(create=True) as store, self._lock(store, sha):
            upload = self._read_upload(store, asset)
            if upload is not None and upload["state"] in {"open", "sealed"}:
                raise AssetUploadError(409, "upload_owned", "content is reserved by an upload; finalize or cancel that upload first")
            observation = self.observe(asset)
            if observation["state"] == "verified":
                return {**observation, "cache_hit": True, "quarantine": []}
            complete, partial = "blobs/" + sha, "partial/" + sha + ".part"
            info = self._info(store, complete)
            if info is not None:
                progress("verifying", info.st_size)
                with store.open_file(complete) as stream:
                    digest, count = self._digest(stream)
                    identity = stamp(os.fstat(stream.fileno()))
                if count == size and digest.hexdigest() == sha:
                    return {**self._complete(store, asset, complete, identity), "cache_hit": True, "quarantine": []}
                quarantine.append(self._quarantine(store, complete, asset, "completed object failed size/hash verification", digest.hexdigest()))
                progress("missing")
            info = self._info(store, partial)
            if info is not None and info.st_size > size:
                quarantine.append(self._quarantine(store, partial, asset, "partial exceeds declared size"))
                progress("missing")
            with self._partial(store, sha) as stream:
                progress("verifying", os.fstat(stream.fileno()).st_size)
                digest, offset = self._digest(stream)
                prefix_identity = stamp(os.fstat(stream.fileno()))
                if offset == size:
                    if digest.hexdigest() == sha:
                        return {**self._complete(store, asset, partial, prefix_identity), "cache_hit": False,
                                "resumed_bytes": offset, "transferred_bytes": 0, "quarantine": quarantine}
                    # Close the fd before moving this rejected object, then open a
                    # fresh partial below. No content identity is inferred by size.
                    bad_partial = True
                else:
                    bad_partial = False
            if bad_partial:
                quarantine.append(self._quarantine(store, partial, asset, "complete partial failed hash verification", digest.hexdigest()))
                digest, offset = hashlib.sha256(), 0
                prefix_identity = None
                progress("missing")
            if asset["source"]["kind"] == "unavailable" and local_source is None:
                return _observation("unavailable", size, offset, asset["source"]["reason"], quarantine=quarantine)
            self._space(store, size - offset)
            with self._partial(store, sha) as stream:
                if prefix_identity is not None and stamp(os.fstat(stream.fileno())) != prefix_identity:
                    raise AssetError("partial changed between verification and transfer")
                if local_source is not None:
                    digest, count, transfer = self._import(asset, local_source, stream, offset, digest, progress)
                elif asset["source"]["kind"] == "url":
                    digest, count, transfer = self._download(asset, stream, digest, offset, store, progress)
                else:
                    with self._artifact(jobs_root, asset["source"]) as source:
                        digest, count, transfer = self._import(asset, source, stream, offset, digest, progress)
                stream.flush()
                os.fsync(stream.fileno())
                identity = stamp(os.fstat(stream.fileno()))
            progress("verifying", count, **transfer)
            if count != size or digest.hexdigest() != sha:
                record = self._quarantine(store, partial, asset, "received content failed size/hash verification", digest.hexdigest())
                quarantine.append(record)
                return _observation("failed", size, count, "received content failed size/hash verification",
                                    quarantine=quarantine, **transfer)
            return {**self._complete(store, asset, partial, identity), "cache_hit": False,
                    "quarantine": quarantine, **transfer}


def run_manifest(manifest, cache_root, jobs_root, out, verify_upload=None, asset_index=None):
    manifest = validate_manifest(manifest)
    selected = manifest["assets"]
    if verify_upload is not None:
        _upload_id(verify_upload)
        if type(asset_index) is not int or not 0 <= asset_index < len(selected):
            raise AssetError("upload verification requires a valid asset index")
        selected = [selected[asset_index]]
    elif asset_index is not None:
        raise AssetError("asset index is only valid for upload verification")
    cache, output = AssetCache(cache_root), SafeRoot(out)
    report = {"schema_version": 1, "game_id": manifest["game_id"], "version": manifest["version"],
              "state": "preparing", "updated_at": time.time(), "assets": [
                  {"name": asset["name"], "sha256": asset["sha256"], "size": asset["size"], **cache.observe(asset)}
                  for asset in selected]}
    if verify_upload is not None:
        report["scope"] = {"kind": "uploaded_asset", "asset_index": asset_index,
                           "asset_name": selected[0]["name"], "upload_id": verify_upload}
    last_save = [0.0]

    def save(force=False):
        now = time.monotonic()
        if force or now - last_save[0] >= 0.5:
            report["updated_at"] = time.time()
            output.write_json("preparation.json", report)
            last_save[0] = now

    try:
        save(True)
        for index, asset in enumerate(selected):
            def progress(value):
                changed = report["assets"][index]["state"] != value["state"]
                report["assets"][index].update(value)
                save(changed)

            try:
                result = (cache.verify_upload(asset, verify_upload, progress) if verify_upload is not None
                          else cache.prepare(asset, jobs_root, progress))
                progress(result)
            except KeyboardInterrupt:
                report["assets"][index].update(state="cancelled", error="preparation cancelled; partial retained")
                for remaining in report["assets"][index + 1:]:
                    if remaining["state"] != "verified":
                        remaining.update(state="cancelled", error="not attempted after cancellation")
                report["state"] = "cancelled"
                save(True)
                return 130
            except (OSError, ValueError) as error:
                progress({**cache.observe(asset), "state": "failed", "error": str(error) if isinstance(error, AssetError)
                          else "resource preparation failed; inspect partial and cache permissions"})
            save(True)
        report["state"] = "verified" if all(item["state"] == "verified" for item in report["assets"]) else "incomplete"
        save(True)
        return 0 if report["state"] == "verified" else 1
    finally:
        output.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cache-root", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--verify-upload")
    parser.add_argument("--asset-index", type=int)
    args = parser.parse_args(argv)
    try:
        with open(args.manifest, "rb") as stream:
            data = stream.read((64 << 10) + 1)
        if len(data) > 64 << 10:
            raise AssetError("manifest exceeds 64 KiB")
        return run_manifest(json.loads(data), args.cache_root, args.jobs_root, args.out,
                            args.verify_upload, args.asset_index)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(str(error) if isinstance(error, AssetError) else "game preparation could not read or save its inputs", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
