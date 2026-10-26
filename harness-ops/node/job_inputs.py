"""Small, explicit same-node input snapshots inside the existing preparation step."""
import copy
import errno
import hashlib
import json
import os
import re
import time

from artifact_io import stamp
from node_directory import DirectoryError


SCHEMA = "phonefarm/job-inputs@1"
RECEIPT = "phonefarm/job-input-preparation@1"
MAX_FILE = 8 << 20
MAX_TOTAL = 32 << 20
MAX_MANIFEST = 32 << 10
CHUNK = 256 << 10


class InputError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + " has missing or unsupported fields")


def _path(value):
    if (not isinstance(value, str) or not value or os.path.isabs(value)
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("input paths must be nonempty relative paths without dot or empty segments")
    return value


def _job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{12}", value):
        raise ValueError("input source job_id must be 12 lowercase hex characters")
    return value


def _pointer(value):
    if not isinstance(value, str) or (value and not value.startswith("/")):
        raise ValueError("binding pointer must be a JSON pointer")
    if re.search(r"~(?![01])", value):
        raise ValueError("binding pointer has an invalid JSON pointer escape")
    return [] if not value else [v.replace("~1", "/").replace("~0", "~") for v in value[1:].split("/")]


def validate(value, node_id):
    """Validate the complete declarative namespace without reading any artifacts."""
    _exact(value, ("schema", "node_id", "files", "bindings", "cwd"), "inputs")
    if (value["schema"] != SCHEMA or not isinstance(node_id, str) or not node_id
            or value["node_id"] != node_id):
        raise ValueError("inputs must use phonefarm/job-inputs@1 and identify the current node")
    if value["cwd"] not in ("default", "input"):
        raise ValueError("inputs.cwd must be default or input")
    files, bindings = value["files"], value["bindings"]
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise ValueError("inputs.files must contain 1 to 32 files")
    if not isinstance(bindings, list) or len(bindings) > 16:
        raise ValueError("inputs.bindings must contain at most 16 bindings")
    names, targets, total = set(), [], 0
    for item in files:
        _exact(item, ("name", "target", "source"), "input file")
        name = item["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name in names:
            raise ValueError("input names must be unique 1 to 64 character identifiers (letters, numbers, ._-)")
        names.add(name)
        targets.append(_path(item["target"]))
        source = item["source"]
        extra = set(source) - {"job_id", "path", "size", "sha256"}
        if extra not in (set(), {"node"}):
            raise ValueError("input source allows job_id, path, size, sha256 and an optional node name")
        _job_id(source["job_id"])
        _path(source["path"])
        if "node" in source and (not isinstance(source["node"], str)
                                 or not re.fullmatch(r"[a-z0-9_-]{1,64}", source["node"])
                                 or source["node"] == "local"):
            raise ValueError("input source node must name a shared directory entry (1-64 lowercase letters, digits, _ or -)")
        if type(source["size"]) is not int or not 0 <= source["size"] <= MAX_FILE:
            raise ValueError("each input size must be an integer from 0 to 8 MiB")
        if not isinstance(source["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError("input sha256 must be 64 lowercase hex characters")
        total += source["size"]
    if total > MAX_TOTAL:
        raise ValueError("declared inputs exceed 32 MiB")
    for binding in bindings:
        _exact(binding, ("source", "target", "values"), "input binding")
        if not isinstance(binding["source"], str) or binding["source"] not in names:
            raise ValueError("binding source must name a declared input")
        targets.append(_path(binding["target"]))
        values = binding["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= 128:
            raise ValueError("binding values must contain 1 to 128 replacements")
        pointers = set()
        for replacement in values:
            _exact(replacement, ("pointer", "input"), "binding value")
            _pointer(replacement["pointer"])
            if replacement["pointer"] in pointers:
                raise ValueError("binding pointers must not repeat")
            pointers.add(replacement["pointer"])
            if not isinstance(replacement["input"], str) or replacement["input"] not in names:
                raise ValueError("binding value must reference a declared input")
    for index, target in enumerate(targets):
        if any(target == other or target.startswith(other + "/") or other.startswith(target + "/")
               for other in targets[:index]):
            raise ValueError("input and binding targets must be distinct with no parent/child overlap")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("inputs must be valid UTF-8 JSON") from error
    if len(encoded) > MAX_MANIFEST:
        raise ValueError("inputs manifest exceeds 32 KiB")
    return copy.deepcopy(value)


def _node_id(jobs):
    value = getattr(jobs, "node_id", None)
    if not isinstance(value, str) or not value:
        raise InputError(503, "node_unavailable", "persistent node identity is unavailable")
    return value


def _source_ready(jobs, jid):
    from job_runtime import runner_alive

    _job_id(jid)
    try:
        raw = jobs._read_meta(jid)
        if not isinstance(raw, dict) or (raw.get("execution") or {}).get("kind") != "supervised-v1":
            raise InputError(409, "source_unavailable", "source job lacks verifiable host completion evidence")
        meta = jobs.status(jid)
        execution = meta.get("execution") or {}
        if (meta.get("state") != "finished" or execution.get("worker_alive") is not False
                or runner_alive(jobs._read_root, jid)):
            raise InputError(409, "source_unavailable", "source job must be finished without a live worker")
        policy = (execution.get("process_management") or {}).get("policy")
        row = jobs.dispatch.queue.get(jid)
        if policy == "contained" or (row and row["spec"].get("execution_policy") == "contained"):
            problem = jobs.host_completion_problem(jid, meta)
            if problem:
                raise InputError(409, "source_unavailable", "source host handback is unconfirmed: " + problem)
        elif (meta.get("phase") != "finished" or meta.get("lost")
              or (execution.get("owned_process_group") or {}).get("status") != "absent"):
            raise InputError(409, "source_unavailable", "source original process group completion is unconfirmed")
    except (FileNotFoundError, KeyError) as error:
        raise InputError(404, "source_not_found", "source job is unavailable") from error


def _source_file(jobs, source):
    _source_ready(jobs, source["job_id"])
    try:
        return jobs._read_root.open_file(source["job_id"] + "/out/" + source["path"])
    except FileNotFoundError as error:
        raise InputError(404, "source_not_found", "source output file does not exist") from error


def reference(jobs, jid, path):
    """Hash only the explicitly selected small, completed output through one FD."""
    _job_id(jid)
    _path(path)
    node_id = _node_id(jobs)
    source = {"job_id": jid, "path": path}
    with _source_file(jobs, source) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > MAX_FILE:
            raise InputError(413, "input_too_large", "input reference exceeds 8 MiB; use the game asset cache")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = stream.read(min(CHUNK, remaining))
            if not chunk:
                raise InputError(409, "input_mismatch", "source changed while creating its input reference")
            digest.update(chunk)
            remaining -= len(chunk)
        if stamp(before) != stamp(os.fstat(stream.fileno())):
            raise InputError(409, "input_mismatch", "source changed while creating its input reference")
    source.update(size=before.st_size, sha256=digest.hexdigest())
    return {"schema": "phonefarm/job-input-reference@1", "node_id": node_id, "source": source}


def _new_target(store, rel):
    # lstat catches every existing leaf, including a dangling symlink. Parent
    # traversal remains descriptor-relative and never follows an uploaded link.
    try:
        parent, name = store._parent(rel)
    except FileNotFoundError:
        return
    try:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise InputError(409, "target_conflict", "input target already exists: " + rel.split("/in/", 1)[-1])
    finally:
        os.close(parent)


def _parents(store, rel):
    parts = rel.split("/")[:-1]
    for count in range(3, len(parts) + 1):
        store.mkdir("/".join(parts[:count]))


def _remote_snapshot(jobs, source):
    """One bounded cross-node fetch; credentials stay inside the shared directory."""
    directory = getattr(jobs, "directory", None)
    if directory is None:
        raise InputError(503, "source_unavailable", "cross-node inputs require a shared directory on this node")
    try:
        data = directory.download_input(source["node"], source["job_id"], source["path"],
                                        source["size"] + 1)
    except DirectoryError as error:
        code = "source_not_found" if error.status == 404 else "source_unavailable"
        raise InputError(409, code, "cross-node input source is unavailable: " + str(error)) from error
    if len(data) != source["size"]:
        raise InputError(409, "input_mismatch", "remote source size differs from its declared input reference")
    if hashlib.sha256(data).hexdigest() != source["sha256"]:
        raise InputError(409, "input_mismatch", "remote source SHA-256 does not match its input reference")
    return data


def _snapshot(jobs, item):
    """The typed-input budget bounds all retained originals and derived bytes."""
    source = item["source"]
    if "node" in source:
        return _remote_snapshot(jobs, source)
    with _source_file(jobs, source) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != source["size"]:
            raise InputError(409, "input_mismatch", "source size differs from its declared input reference")
        data = stream.read(source["size"])
        if (len(data) != source["size"] or stamp(before) != stamp(os.fstat(stream.fileno()))
                or hashlib.sha256(data).hexdigest() != source["sha256"]):
            raise InputError(409, "input_mismatch", "source changed or SHA-256 does not match its input reference")
        return data


def _reject_constant(value):
    raise ValueError("non-JSON numeric constant: " + value)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("binding source JSON has duplicate object keys")
        value[key] = item
    return value


def _replace_pointer(value, pointer, replacement):
    parts = _pointer(pointer)
    if not parts:
        if not isinstance(value, str):
            raise ValueError("binding pointer must select an existing string")
        return replacement
    current = value
    for index, part in enumerate(parts):
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(current):
                raise ValueError("binding pointer does not select an existing array element")
            key = int(part)
        elif isinstance(current, dict) and part in current:
            key = part
        else:
            raise ValueError("binding pointer does not select an existing field")
        if index == len(parts) - 1:
            if not isinstance(current[key], str):
                raise ValueError("binding pointer must select an existing string")
            current[key] = replacement
        else:
            current = current[key]
    return value


def _derive(jobs, jid, binding, files, original, remaining):
    value = json.loads(original.decode("utf-8"),
                       parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    for replacement in binding["values"]:
        path = os.path.abspath(jobs.path(jid, "in", files[replacement["input"]]["target"]))
        value = _replace_pointer(value, replacement["pointer"], path)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_FILE:
        raise InputError(413, "input_too_large", "derived input JSON exceeds 8 MiB")
    if len(data) > remaining:
        raise InputError(413, "input_too_large", "original and derived inputs together exceed 32 MiB")
    return data


def _failure(error):
    from job_input_archive import Error as ArchiveError
    if isinstance(error, (InputError, ArchiveError)):
        return error
    if isinstance(error, FileExistsError):
        return InputError(409, "target_conflict", "input target already exists")
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT):
        failure = InputError(507, "input_write_space_failed", str(error))
        failure.details = {"errno": error.errno}
        return failure
    return InputError(409 if isinstance(error, ValueError) else 500, "input_preparation_failed", str(error))


def prepare(jobs, jid, manifest, *, before_write=None, write=None):
    """Snapshot bounded inputs once under intake, before any input file write.

    Failed partials remain in this never-started job. A retry with the same
    request ID returns that job; only an explicit new request can prepare anew.
    """
    _job_id(jid)
    node_id = _node_id(jobs)
    manifest = validate(manifest, node_id)
    receipt = {"schema": RECEIPT, "node_id": node_id, "job_id": jid, "status": "pending",
               "started_at": time.time(), "updated_at": time.time(), "error": None,
               "files": [{**item, "status": "pending"} for item in manifest["files"]],
               "bindings": [{**item, "status": "pending"} for item in manifest["bindings"]]}
    store, active = jobs._read_root, None
    receipt_path = jid + "/inputs.json"
    # Intake excludes other preparation publishers. An error after publishing
    # pending may still leave it visible; attempt to persist failed in that case.
    _new_target(store, receipt_path)
    owns_receipt = True
    try:
        # No replay/replacement of a previously attempted preparation.
        if not store.publish_json_once(receipt_path, receipt):
            owns_receipt = False
            raise InputError(409, "preparation_exists", "input preparation already has a persistent receipt")
        for item in manifest["files"] + manifest["bindings"]:
            _new_target(store, jid + "/in/" + item["target"])
        payloads = {}
        for item in receipt["files"]:
            active = item
            payloads[item["target"]] = _snapshot(jobs, item)
        files = {item["name"]: item for item in manifest["files"]}
        remaining = MAX_TOTAL - sum(len(data) for data in payloads.values())
        for binding in receipt["bindings"]:
            active = binding
            data = _derive(jobs, jid, binding, files,
                           payloads[files[binding["source"]]["target"]], remaining)
            binding["size"] = len(data)
            remaining -= len(data)
            payloads[binding["target"]] = data
        active = None
        if before_write is not None:
            before_write(payloads)
        for item in receipt["files"] + receipt["bindings"]:
            active = item
            target = jid + "/in/" + item["target"]
            _parents(store, target)
            with store.create_file(target) as destination:
                data = payloads[item["target"]]
                for offset in range(0, len(data), CHUNK):
                    chunk = data[offset:offset + CHUNK]
                    if write is None:
                        destination.write(chunk)
                    else:
                        write(destination, chunk)
            item["status"] = "ready"
        active = None
        # Jobs._prepare owns the single whole-input durability barrier before
        # queue.ready; this receipt does not claim that the command has started.
        receipt.update(status="ready", updated_at=time.time())
        store.write_json(receipt_path, receipt)
        return receipt
    except (OSError, ValueError, RecursionError) as error:
        if not owns_receipt:
            raise
        failure = _failure(error)
        detail = str(failure)
        if active is not None:
            active.update(status="failed", error=detail)
        receipt.update(status="failed", error=detail, updated_at=time.time())
        try:
            store.write_json(receipt_path, receipt)
        except (OSError, ValueError) as publish_error:
            raise InputError(500, "input_receipt_unavailable",
                             str(failure) + "; failed preparation receipt could not be persisted: " + str(publish_error)) from error
        raise failure from error


def record(jobs, jid):
    """Read declaration and receipt only; never prepare inputs or hash a file."""
    _job_id(jid)
    node_id = _node_id(jobs)
    row = jobs.dispatch.queue.get(jid)
    if row is None:
        try:
            jobs._read_meta(jid)
        except (KeyError, FileNotFoundError) as error:
            raise InputError(404, "job_not_found", "job does not exist") from error
    declared = (row["spec"].get("inputs") if row else None)
    preparation = {"status": "not_declared" if declared is None else "not_prepared"}
    if declared is not None:
        try:
            preparation = jobs._read_root.read_json(jid + "/inputs.json", maximum=128 << 10)
        except FileNotFoundError:
            pass
        else:
            if (not isinstance(preparation, dict) or preparation.get("schema") != RECEIPT
                    or preparation.get("job_id") != jid or preparation.get("node_id") != node_id
                    or preparation.get("status") not in ("pending", "ready", "failed")):
                raise InputError(409, "input_receipt_invalid", "input preparation receipt is invalid")
    return {"schema": "phonefarm/job-input-status@1", "node_id": node_id,
            "job_id": jid, "declared": declared, "preparation": preparation}
