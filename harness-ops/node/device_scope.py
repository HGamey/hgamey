"""Frozen PF bindings and pure scope gates; this module never owns a device."""
import copy
import re

from device_registry import DeviceRegistry, RegistryError, ERROR_SCHEMA, STATUS

DEVICE_BINS = {"phonefarm", "game_opt_loop", "adb", "fetch_apk"}
BINDING_SCHEMA = "phonefarm/device-binding@1"
SCOPES_SCHEMA = "phonefarm/device-scopes@1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
DOMAIN = re.compile(r"[0-9a-f]{32}\Z")
STATES = {"clear", "held", "blocked", "unknown"}
SESSION_VALUES = {"--out", "--serial", "--package", "--mode", "--so", "--actor-id",
                  "--evidence-origin", "--max-seconds", "--power-policy"}
LOCK_VALUES = {"--lock-label", "--lock-wait", "--lock-poll"}


def device_access(spec):
    return spec.get("device_access", "write" if spec.get("bin") in DEVICE_BINS else "none")


def valid_serial(value):
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))


def binding(value):
    if (not isinstance(value, dict) or set(value) != {
            "schema", "domain_id", "registry_revision", "device_id", "serial"}
            or value.get("schema") != BINDING_SCHEMA
            or not isinstance(value.get("domain_id"), str) or not DOMAIN.fullmatch(value["domain_id"])
            or type(value.get("registry_revision")) is not int or not 1 <= value["registry_revision"] <= 2**53 - 1
            or not isinstance(value.get("device_id"), str) or not IDENTIFIER.fullmatch(value["device_id"])
            or not valid_serial(value.get("serial"))):
        raise ValueError("invalid device_binding; use the configured node's devices-resolve response")
    return copy.deepcopy(value)


def resource(value):
    return "device:" + value["domain_id"] + ":" + value["device_id"]


def validate_job(spec, *, typed_recovery=False):
    """Pure early validation; no registry lookup before original-ID replay."""
    argv = spec.get("argv") or []
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a string array")
    scoped = "device_binding" in spec
    if not scoped and spec.get("bin") != "phonefarm":
        return None
    values = SESSION_VALUES | LOCK_VALUES | {"--request", "--request-id", "--session"}
    game = argv[:1] == ["game-deploy"]
    recovering = argv[:2] in (["gray-layer", "session-recover"], ["game-deploy", "recover"])
    if scoped and game:
        values = LOCK_VALUES | {"--request", "--session", "--serial", "--out"}
    serials, i = [], 2 if argv[:1] == ["gray-layer"] or game and recovering else 1 if game else 0
    while i < len(argv):
        key = argv[i]
        if key == "--device-binding" or key.startswith("--device-binding="):
            raise ValueError("device binding belongs in the job spec, not argv")
        if key in values:
            if i + 1 == len(argv):
                if scoped:
                    raise ValueError(key + " requires a value")
                break  # Preserve legacy parser errors at their original boundary.
            if key == "--serial":
                serials.append(argv[i + 1])
            i += 2
        else:
            if scoped and key not in {"--json", "--keep-prop"}:
                raise ValueError("unsupported scoped device argument: " + key)
            i += 1
    if not scoped:
        return None
    value = binding(spec["device_binding"])
    if typed_recovery:
        if (spec.get("bin") != "phonefarm" or spec.get("exe") is not None
                or device_access(spec) != "recovery" or not recovering):
            raise ValueError("bound recovery requires the original typed session or game recovery command")
        return value  # The typed producer also verifies the complete source/attempt spec.
    if (spec.get("bin") != "phonefarm" or spec.get("exe") is not None
            or device_access(spec) != "write" or spec.get("recovery_for") is not None
            or not (argv[:2] == ["gray-layer", "session"] or game and not recovering)):
        raise ValueError("device_binding requires a configured phonefarm session or game-deploy with write access and no exe")
    if serials != [value["serial"]]:
        raise ValueError("scoped session requires exactly one explicit serial matching device_binding")
    return value


def scope(spec):
    if "device_binding" in spec:
        return {"scope": "device", "binding": binding(spec["device_binding"])}
    return {"scope": "none" if device_access(spec) == "none" else "global"}


def conflicts(left, right):
    if "none" in (left["scope"], right["scope"]):
        return False
    if "global" in (left["scope"], right["scope"]):
        return True
    a, b = left["binding"], right["binding"]
    # Different configured domains cannot be assumed to be independent on one node.
    return a["domain_id"] != b["domain_id"] or a["device_id"] == b["device_id"]


def scopes(snapshot):
    value = snapshot.get("device_scopes")
    if (not isinstance(value, dict) or value.get("schema") != SCOPES_SCHEMA
            or not isinstance(value.get("domain_id"), str) or not DOMAIN.fullmatch(value["domain_id"])
            or type(value.get("registry_revision")) is not int or not 0 <= value["registry_revision"] <= 2**53 - 1
            or type(value.get("queue_waiting")) is not bool or type(value.get("unknown")) is not bool
            or not isinstance(value.get("devices"), list) or len(value["devices"]) > 128):
        raise ValueError("phonefarm device scopes are unavailable or incomplete")
    storage = snapshot.get("storage")
    if (not isinstance(storage, dict) or storage.get("readiness") != "ready" or storage.get("layout_version") != 2
            or storage.get("domain_id") != value["domain_id"]):
        raise ValueError("phonefarm scope and storage domains do not match")
    seen = set()
    for entry in [value.get("global"), *value["devices"]]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("status"), str) or entry["status"] not in STATES
                or "error" not in entry or not (entry["error"] is None or isinstance(entry["error"], str))):
            raise ValueError("phonefarm returned an invalid scope state")
    for entry in value["devices"]:
        key = entry.get("device_id")
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key) or key in seen:
            raise ValueError("phonefarm returned invalid or duplicate device scopes")
        seen.add(key)
    return value


def lease_problem(spec, snapshot, mode=None):
    """Combine PF facts only; recovery ownership is still checked by typed PF."""
    access = device_access(spec)
    mode = mode or ("recovery" if access == "recovery" else "start")
    declared = scope(spec)
    if declared["scope"] == "none":
        return None
    if declared["scope"] == "global":
        if mode == "recovery":  # Existing global recovery acquires its own original context.
            return None
        return None if snapshot.get("status") == "clear" else "global device lease is " + str(snapshot.get("status", "unknown"))
    try:
        current = scopes(snapshot)
    except ValueError as error:
        return str(error)
    target = declared["binding"]
    if current["domain_id"] != target["domain_id"]:
        return "frozen device binding belongs to a different lease domain"
    if mode == "start" and current["registry_revision"] != target["registry_revision"]:
        return "device registry changed; submit a new explicitly resolved binding"
    if current["unknown"]:
        return "device scope observation contains an unassigned or incomplete barrier"
    if conflicts(declared, {"scope": "global"}) and current["global"]["status"] != "clear":
        return current["global"]["error"] or "global device lease is " + current["global"]["status"]
    entry = next((item for item in current["devices"] if item["device_id"] == target["device_id"]), None)
    if entry is None:
        return "frozen device ID is absent from the complete scope observation"
    allowed = {"clear", "blocked"} if mode == "recovery" else {"clear"}
    if entry["status"] not in allowed:
        return entry["error"] or "device " + target["device_id"] + " lease is " + entry["status"]
    if mode == "start" and current["queue_waiting"]:
        return "a global phonefarm queue participant is waiting"
    return None


class DeviceScope:
    def __init__(self, jobs):
        self.jobs = jobs

    def resolve(self, serial):
        if not valid_serial(serial):
            raise RegistryError("invalid_request", "serial must be one explicit nonempty transport identifier")
        try:
            code, value = DeviceRegistry(self.jobs, getattr(self.jobs, "node_id", None))._run(
                ["lease", "resolve", "--serial", serial, "--json"])
        except OSError:
            raise RegistryError("unavailable", "configured phonefarm device resolution is unavailable") from None
        if code == 2 and isinstance(value, dict) and value.get("schema") == ERROR_SCHEMA:
            reason, message = value.get("code"), value.get("error")
            if isinstance(reason, str) and reason in STATUS and isinstance(message, str) and 0 < len(message) <= 2048:
                raise RegistryError(reason, message)
        try:
            result = binding(value)
        except ValueError:
            raise RegistryError("unavailable", "phonefarm device binding protocol is unavailable") from None
        if code != 0 or result["serial"] != serial:
            raise RegistryError("unavailable", "phonefarm did not resolve the requested serial")
        return result

    def admit(self, value):
        if self.resolve(value["serial"]) != value:
            raise RegistryError("conflict", "device registration no longer matches the frozen binding")
