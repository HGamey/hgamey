"""Read phonefarm's host lease contract; observations never grant ownership."""
import json
import os
import re
import subprocess
import threading
import time


DEVICE_BINS = {"phonefarm", "game_opt_loop", "adb", "fetch_apk"}


def device_access(spec):
    return spec.get("device_access", "write" if spec.get("bin") in DEVICE_BINS else "none")


def prepare_receipt_directory(store, jid):
    """Also covers queued jobs prepared by an older node before the upgrade."""
    fd = store.open_fd(jid + "/out", directory=True)
    try:
        try:
            os.mkdir("lease-receipts", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
    finally:
        os.close(fd)
    os.close(store.open_fd(jid + "/out/lease-receipts", directory=True))


def job_lease_receipts(store, jid):
    """Read scoped producer evidence; never infer job recovery or follow evidence paths."""
    result = {"schema_version": 1, "job_id": jid, "status": "not_reported", "entries": [], "errors": []}
    directory = jid + "/out/lease-receipts"
    try:
        fd = store.open_fd(directory, directory=True)
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as error:
        return {**result, "status": "unknown", "errors": [{"path": "lease-receipts", "error": str(error)}]}
    try:
        # Atomic writers use non-JSON temporary names. Do not hide malformed final files.
        with os.scandir(fd) as files:
            names = []
            for entry in files:
                if not entry.name.endswith(".json"):
                    continue
                if len(names) == 128:
                    result["errors"].append({"path": "lease-receipts", "error": "more than 128 receipt files; listing is incomplete"})
                    break
                names.append(entry.name)
        for name in sorted(names):
            path = "lease-receipts/" + name
            try:
                value = store.read_json(jid + "/out/" + path, maximum=64 << 10)
                if not isinstance(value, dict) or value.get("schema") != "phonefarm/job-lease@1":
                    raise ValueError("unsupported job lease receipt schema")
                lease_id = value.get("lease_id")
                if (value.get("job_id") != jid or not isinstance(lease_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", lease_id) or name != lease_id + ".json"):
                    raise ValueError("receipt job or lease identity does not match its location")
                recovery = value.get("recovery")
                if (value.get("state") not in ("active", "released", "retained")
                        or not isinstance(value.get("serial"), str) or not value["serial"]
                        or not isinstance(recovery, dict)
                        or recovery.get("status") not in ("not_reported", "required", "reported_complete")):
                    raise ValueError("invalid lease or recovery state")
                # Only the public contract is returned, never raw ownership/token fields.
                public = {key: value.get(key) for key in (
                    "schema", "job_id", "lease_id", "serial", "label", "state", "updated_at")}
                public["recovery"] = {key: recovery.get(key) for key in (
                    "status", "reason", "context", "scope", "evidence_origin", "evidence")}
                evidence = recovery.get("evidence")
                if evidence is not None and (not isinstance(evidence, list) or any(
                        not isinstance(path, str) or not path for path in evidence)):
                    raise ValueError("invalid evidence references")
                if recovery["status"] == "reported_complete" and (not evidence or any(
                        not isinstance(recovery.get(key), str) or not recovery[key].strip()
                        for key in ("scope", "evidence_origin"))):
                    raise ValueError("reported completion requires scope, evidence origin and reference")
                prior = value.get("recovery_of")
                if prior is not None:
                    if not isinstance(prior, dict):
                        raise ValueError("invalid prior lease reference")
                    prior = {key: prior.get(key) for key in ("job_id", "lease_id")}
                public.update(recovery_of=prior, artifact_path=path)
                result["entries"].append(public)
            except (OSError, ValueError) as error:
                result["errors"].append({"path": path, "error": str(error)})
    except OSError as error:
        result["errors"].append({"path": "lease-receipts", "error": str(error)})
    finally:
        os.close(fd)
    result["status"] = ("partial" if result["entries"] else "unknown") if result["errors"] else (
        "recorded" if result["entries"] else "not_reported")
    return result


def storage_root(value):
    """Resolve only from the configured phonefarm, never from a Python fallback."""
    storage = value.get("storage")
    if not isinstance(storage, dict) or storage.get("schema_version") != 1:
        raise ValueError("lease storage contract unavailable; upgrade phonefarm")
    if storage.get("readiness") != "ready":
        raise ValueError(storage.get("error") or "lease storage requires explicit initialization")
    root = storage.get("devlock_dir")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ValueError("phonefarm returned an invalid devlock directory")
    return root


class LeaseState:
    def __init__(self, bins):
        self.bins = bins
        self._lock = threading.Lock()
        self._next = 0
        self._snapshot = {"schema_version": 1, "status": "unknown", "bridge_held": None,
                          "entries": [], "error": "lease status has not been read", "sampled_at": None}

    def snapshot(self, refresh=False, force=False):
        with self._lock:
            if not refresh or not force and time.monotonic() < self._next:
                return dict(self._snapshot)
            value = None
            try:
                if "phonefarm" not in self.bins:
                    raise ValueError("phonefarm lease reader is not configured")
                executable, cwd = self.bins["phonefarm"]
                result = subprocess.run([executable, "lease", "status", "--json"], cwd=cwd,
                                        capture_output=True, timeout=3, stdin=subprocess.DEVNULL, check=True)
                value = json.loads(result.stdout)
                if (not isinstance(value, dict) or value.get("schema_version") != 1
                        or value.get("status") not in {"clear", "held", "blocked", "unknown"}
                        or not isinstance(value.get("entries"), list)):
                    raise ValueError("incompatible lease status; upgrade phonefarm")
                try:
                    storage_root(value)
                except ValueError as error:
                    value.update(status="unknown", error=str(error))
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                value = {"schema_version": 1, "status": "unknown", "bridge_held": None,
                         "entries": [], "error": f"lease status unavailable: {type(error).__name__}: {error}"}
            self._snapshot = {**value, "sampled_at": time.time()}
            self._next = time.monotonic() + 1
            return dict(self._snapshot)
