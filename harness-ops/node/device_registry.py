"""Bounded host-only access to PF's authoritative device registry."""
import json
import os
import signal
import subprocess
import tempfile
import time

LIMIT = 64 << 10
SCHEMA = "phonefarm/device-registry@1"
ERROR_SCHEMA = "phonefarm/device-registry-error@1"
STATUS = {"invalid_request": 400, "conflict": 409, "busy": 409, "unavailable": 503}


def _reject_constant(value):
    raise ValueError("non-JSON number")


class RegistryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.status = STATUS[code]
        self.payload = {"schema": ERROR_SCHEMA, "code": code, "error": message}


def _record(value):
    """Check the response protocol, leaving registry rules and CAS to PF."""
    if not (isinstance(value, dict) and value.get("schema") == SCHEMA
            and type(value.get("revision")) is int and 0 <= value["revision"] <= 2**53 - 1
            and value.get("identity_basis") == "operator_declared"
            and value.get("physical_identity_verified") is False
            and value.get("execution_scope") == "global"
            and "updated_at" in value
            and (value["updated_at"] is None or isinstance(value["updated_at"], str))
            and isinstance(value.get("devices"), list) and len(value["devices"]) <= 128):
        return False
    return all(isinstance(device, dict)
               and isinstance(device.get("device_id"), str)
               and isinstance(device.get("label"), str)
               and isinstance(device.get("aliases"), list)
               and all(isinstance(alias, str) for alias in device["aliases"])
               and type(device.get("enabled")) is bool for device in value["devices"])


class DeviceRegistry:
    def __init__(self, jobs, node_id):
        self.jobs, self.node_id = jobs, node_id

    def read(self):
        return self._call(None)

    def write(self, body):
        if not isinstance(body, bytes) or not 0 < len(body) <= LIMIT:
            raise RegistryError("invalid_request", "device registration requires at most 64 KiB of JSON")
        return self._call(body)

    def _call(self, body):
        try:
            return self._invoke(body)
        except OSError:
            raise RegistryError("unavailable", "device registry response unavailable; read the registry before any explicit retry") from None

    def _invoke(self, body):
        arguments = ["lease", "devices", "--json"] if body is None else [
            "lease", "devices-set", "--request", "-", "--json"]
        code, value = self._run(arguments, body)
        if code == 2 and isinstance(value, dict) and value.get("schema") == ERROR_SCHEMA:
            reason, message = value.get("code"), value.get("error")
            if isinstance(reason, str) and reason in STATUS and isinstance(message, str) and 0 < len(message) <= 2048:
                raise RegistryError(reason, message)
        if code != 0 or not _record(value):
            raise RegistryError("unavailable", "phonefarm registry protocol unavailable; upgrade or check the configured program")
        return {**value, "node_id": self.node_id}

    def _run(self, arguments, body=None):
        """One bounded configured host helper, shared by registry and scope reads."""
        configured = self.jobs.bins.get("phonefarm")
        if not configured:
            raise RegistryError("unavailable", "phonefarm is not configured for this node")
        executable, cwd = configured
        environment = dict(os.environ if self.jobs.pf_env is None else self.jobs.pf_env)
        for key in list(environment):
            if key.startswith("PF_JOB_") or key in (
                    "PF_RUNTIME_REPORT", "PF_RECOVERY_OUT", "DEVLOCK_LABEL", "DEVLOCK_INSTANCE_ID"):
                environment.pop(key)
        argv = [executable, *arguments]
        unknown = "device registry response unavailable; read the registry before any explicit retry"
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            if body is not None:
                source.write(body)
                source.seek(0)
            try:
                proc = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=source,
                                        stdout=output, stderr=error, start_new_session=True)
            except (OSError, ValueError):
                raise RegistryError("unavailable", "configured phonefarm registry helper could not start") from None
            deadline = time.monotonic() + 5
            while True:
                code = proc.poll()
                exceeded = max(os.fstat(output.fileno()).st_size, os.fstat(error.fileno()).st_size) > LIMIT
                if exceeded or code is None and time.monotonic() >= deadline:
                    # Never signal a PID after poll has reaped its owned leader.
                    if code is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                    raise RegistryError("unavailable", unknown)
                if code is not None:
                    break
                time.sleep(.01)
            output.seek(0)
            try:
                value = json.loads(output.read(LIMIT + 1), parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise RegistryError("unavailable", "phonefarm registry protocol unavailable; upgrade or check the configured program") from None
        return code, value
