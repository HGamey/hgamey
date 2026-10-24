"""Host-only gateway to the configured PF remote owner and its durable mailbox."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from artifact_io import SafeRoot


class OwnerError(Exception):
    pass


class PFRemote:
    def __init__(self, executable, cwd, env, root):
        self.executable, self.cwd = executable, cwd
        self.env = env if env is not None else {k: v for k, v in os.environ.items()
                                                if not k.startswith("PF_JOB_") and k not in ("DEVLOCK_LABEL", "DEVLOCK_INSTANCE_ID")}
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = SafeRoot(str(self.root))
        self.processes = {}
        self.available, self.actions = False, []
        self.input_contract = None
        self.reason = "configured PF lacks strong remote owner; raw device fallback disabled"
        try:
            cap = self._call(["capabilities", "--json"])
            self.available = (cap.get("schema") == "phonefarm/remote-control-capabilities@1"
                              and cap.get("ownership") == "strong-lease-v1")
            if self.available:
                self.actions = cap["actions"]
                self.input_contract = cap.get("input_contract")
                self.reason = None
        except (OwnerError, KeyError):
            pass

    def _call(self, args, request=None):
        # Helpers do host file I/O only. A timed-out submit may have been accepted;
        # callers retain its ID and query, never submit it under a new ID.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error, tempfile.TemporaryFile() as source:
            if request is not None:
                source.write(json.dumps(request).encode())
                source.seek(0)
            try:
                proc = subprocess.Popen([self.executable, "remote-control", *args], cwd=self.cwd, env=self.env,
                                        stdin=source, stdout=output, stderr=error)
            except OSError as exc:
                raise OwnerError(str(exc)) from exc
            deadline = time.monotonic() + 5
            while proc.poll() is None:
                if time.monotonic() >= deadline or max(os.fstat(output.fileno()).st_size,
                                                       os.fstat(error.fileno()).st_size) > (256 << 10):
                    proc.kill()
                    proc.wait()
                    raise OwnerError("PF host helper exceeded its response budget; query original request")
                time.sleep(.02)
            output.seek(0)
            try:
                result = json.loads(output.read((256 << 10) + 1))
                if not isinstance(result, dict):
                    raise ValueError("expected object")
            except ValueError as exc:
                raise OwnerError("PF helper did not return a valid record") from exc
            if proc.returncode and not result.get("schema"):
                raise OwnerError(str(result.get("error") or "PF helper rejected request"))
            return result

    def load(self):
        refs = []
        for path in sorted(self.root.glob("*/ref.json")):
            with path.open() as source:
                ref = json.load(source)
            if ref.get("schema") != "hgamey/remote-owner-ref@1" or ref.get("id") != path.parent.name:
                raise OwnerError("invalid saved remote owner reference: " + str(path))
            refs.append(ref)
        return refs

    def _path(self, ref):
        return self.root / ref["id"] / "owner"

    def start(self, serial, operator, ip, max_seconds, acquisition_id):
        if not self.available:
            raise OwnerError(self.reason)
        ref = {"schema": "hgamey/remote-owner-ref@1", "id": uuid.uuid4().hex, "serial": serial,
               "operator": operator, "ip": ip, "created_at": time.time(), "max_seconds": max_seconds,
               "acquisition_id": acquisition_id}
        work = self.root / ref["id"]
        self.store.mkdir(ref["id"])
        self.store.write_json(ref["id"] + "/ref.json", ref)
        # Publish the reference before spawn, so loss of the HTTP request/node
        # cannot erase the location of a possibly running owner.
        with (work / "stdout").open("wb") as output, (work / "stderr").open("wb") as error:
            try:
                proc = subprocess.Popen([self.executable, "remote-control", "owner", "--serial", serial,
                    "--out", str(work / "owner"), "--ttl-seconds", "300", "--max-seconds", str(max_seconds),
                    "--json"], cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                    stdout=output, stderr=error, start_new_session=True)
            except OSError as exc:
                ref["not_started"] = str(exc)
                self.store.write_json(ref["id"] + "/ref.json", ref)
                return ref
        self.processes[ref["id"]] = proc
        deadline = time.monotonic() + 3
        while proc.poll() is None and time.monotonic() < deadline:
            with (work / "stdout").open("rb") as source:
                line = source.readline(65537)
            if line.endswith(b"\n"):
                break
            time.sleep(.025)
        return ref

    def status(self, ref):
        if ref.get("not_started"):
            return {"schema": "phonefarm/remote-control@1", "session_id": None,
                    "serial": ref["serial"], "phase": "closed", "live_owner": False,
                    "not_started": True,
                    "capabilities": [], "error": ref["not_started"], "recovery": {"required": False}}
        try:
            value = self._call(["status", "--session", str(self._path(ref)), "--json"])
            if (value.get("schema") == "phonefarm/remote-control-startup@1"
                    and value.get("not_started") is True and value.get("device_io_started") is False
                    and value.get("serial") == ref["serial"]
                    and value.get("context") == str(self._path(ref).resolve())
                    and value.get("live_owner") is False):
                proc = self.processes.get(ref["id"])
                if proc is None or proc.poll() is not None:
                    ref["not_started"] = value.get("reason") or "PF did not acquire ownership"
                    ref["startup_evidence"] = value
                    self.store.write_json(ref["id"] + "/ref.json", ref)
                    if proc is not None:
                        if proc.stdin:
                            proc.stdin.close()
                        self.processes.pop(ref["id"], None)
                    return self.status(ref)
            if (value.get("schema") != "phonefarm/remote-control@1"
                    or value.get("serial") != ref["serial"]):
                raise OwnerError("PF session identity/descriptor unavailable")
        except OwnerError as exc:
            return {"schema": "phonefarm/remote-control@1", "session_id": None,
                    "serial": ref["serial"], "phase": "unknown", "live_owner": None,
                    "capabilities": [], "error": str(exc), "recovery": {"required": True}}
        proc = self.processes.get(ref["id"])
        if proc is not None and proc.poll() is not None:
            if proc.stdin:
                proc.stdin.close()
            self.processes.pop(ref["id"], None)
        return value

    def submit(self, ref, request):
        return self._call(["action", "--session", str(self._path(ref)), "--request", "-", "--json"], request)

    def receipt(self, ref, request_id):
        return self._call(["status", "--session", str(self._path(ref)), "--request-id", request_id, "--json"])

    def image(self, ref, receipt):
        result = receipt.get("result") or {}
        if receipt.get("status") != "completed" or result.get("mime") not in ("image/png", "image/jpeg"):
            raise OwnerError("request has no completed image")
        root = self._path(ref).resolve()
        rel = result.get("relative_path")
        if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise OwnerError("invalid PF image reference")
        store = SafeRoot(str(root))
        try:
            data = store.read_bytes(rel, 8 << 20)
            if not data or len(data) != result.get("bytes"):
                raise OwnerError("PF image size mismatch")
            return data, result["mime"]
        finally:
            store.close()

    def close(self):
        # EOF is intentionally abnormal: PF retains a recovery hold. Node exit
        # must never manufacture a normal close receipt or kill a saved PID.
        for proc in self.processes.values():
            if proc.stdin:
                proc.stdin.close()
        self.processes.clear()
