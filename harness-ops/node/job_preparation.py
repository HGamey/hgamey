"""Bound Node-controlled input preparation, independently of worker output."""
import os
import time

import job_input_archive
import job_inputs

SCHEMA = "phonefarm/job-input-write-limits@1"
RECEIPT = "input-write-limits.json"


def check_space(fd, required, reserve):
    stat = os.fstatvfs(fd)
    space = {"available_bytes": stat.f_bavail * stat.f_frsize,
             "required_bytes": required, "reserve_bytes": reserve, "basis": "f_bavail"}
    if space["available_bytes"] < required + reserve:
        error = job_inputs.InputError(507, "input_space_insufficient", "insufficient space for prepared inputs and reserve")
        error.details = space
        raise error
    return space


def prepare(jobs, jid, body, manifest=None):
    store = jobs._read_root
    limits = dict(job_input_archive.POLICY)
    receipt = {"schema": SCHEMA, "status": "pending", "limits": limits,
               "declared_bytes": None, "written_bytes": 0, "tar_stream_bytes": None,
               "entries": None, "space": None, "error": None,
               "started_at": time.time(), "updated_at": time.time()}
    path = jid + "/" + RECEIPT
    if not store.publish_json_once(path, receipt):
        raise ValueError("input write preparation already exists; use the original job record")
    directory = None
    try:
        directory = store.open_fd(jid + "/in", directory=True)

        def write(destination, data):
            # Each new file is written only through this descriptor, never via
            # its buffered Python wrapper. Count returned writes, not allocation.
            if receipt["written_bytes"] + len(data) > limits["expanded_bytes"]:
                raise job_inputs.InputError(413, "input_budget_exceeded", "prepared input payload exceeds the node limit")
            observe_space(len(data))
            view = memoryview(data)
            while view:
                count = os.write(destination.fileno(), view)
                if count <= 0:
                    raise OSError("input write made no progress")
                receipt["written_bytes"] += count
                view = view[count:]

        def observe_space(required):
            try:
                value = check_space(directory, required, limits["min_free_bytes"])
            except job_inputs.InputError as error:
                receipt["space"] = error.details
                raise
            return value

        def before_write(payloads):
            archive = job_input_archive.inspect(body, typed_targets=payloads.keys())
            total = archive.payload_bytes + sum(len(data) for data in payloads.values())
            receipt.update(declared_bytes=total, tar_stream_bytes=archive.stream_bytes, entries=archive.entries)
            if total > limits["expanded_bytes"]:
                error = job_inputs.InputError(413, "input_budget_exceeded", "archive, referenced and derived inputs exceed the node limit")
                error.details = {"dimension": "expanded_bytes", "requested": total, "limit": limits["expanded_bytes"]}
                raise error
            if archive.entries:
                receipt["space"] = observe_space(total)
            archive.extract(store, jid, write)

        if manifest is None:
            before_write({})
        else:
            job_inputs.prepare(jobs, jid, manifest, before_write=before_write, write=write)
        receipt.update(status="ready", updated_at=time.time())
        store.write_json(path, receipt)
    except (OSError, ValueError, RecursionError) as error:
        failure = job_inputs._failure(error)
        receipt.update(status="failed", updated_at=time.time(), error={
            "code": failure.code,
            "status": failure.status, "message": str(failure),
            "details": getattr(failure, "details", {})})
        # Failure to persist propagates to the existing unstarted/durability
        # path; the last saved pending receipt must remain visibly unresolved.
        store.write_json(path, receipt)
        raise
    finally:
        if directory is not None:
            os.close(directory)


def record(store, jid, *, expected=False):
    try:
        return store.read_json(jid + "/" + RECEIPT, maximum=32 << 10)
    except FileNotFoundError:
        if not expected:
            return None
        return {"status": "unknown", "error": {"code": "input_receipt_unavailable",
                "message": "expected input write receipt is missing"}}
    except (OSError, ValueError):
        return {"status": "unknown", "error": {"code": "input_receipt_unavailable",
                "message": "input write receipt could not be read"}}
