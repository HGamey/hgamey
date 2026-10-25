"""Bounded host runtime observations, separate from execution or recovery proof."""
import copy
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time

from artifact_io import SafeRoot

LIMIT = 64 << 10
PROGRAMS = {"phonefarm", "game_opt_loop"}
GRAY_GATEWAY = "phonefarm/gray-control-gateway@1"


def is_gray_session(spec):
    """Match PF's first mode after queue flags, consuming flag values exactly."""
    argv = spec.get("argv") or []
    if spec.get("bin") != "phonefarm" or not isinstance(argv, list) or argv[:1] != ["gray-layer"]:
        return False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in {"--lock-label", "--lock-wait"}:
            index += 2
        elif arg == "--no-devlock" or isinstance(arg, str) and arg.startswith("--lock-label="):
            index += 1
        else:
            return arg == "session"
    return False


class ProgramCompatibilityError(ValueError):
    def __init__(self, status, code, reason, compatibility):
        super().__init__(code + ": " + reason)
        self.status = status
        self.payload = {"code": code, "error": reason, "program": "phonefarm",
                        "compatibility": compatibility, "command_started": False}


def gray_compatibility(record):
    required = [GRAY_GATEWAY]
    if not record or record.get("state") != "observed":
        return {"status": "unknown", "required": required, "missing": None,
                "reason": (record or {}).get("reason") or "configured phonefarm is not observed; refresh runtime explicitly"}
    missing = [item for item in required if item not in record["identity"]["supported_protocols"]]
    return {"status": "incompatible" if missing else "compatible", "required": required, "missing": missing,
            "reason": "configured phonefarm does not declare " + ", ".join(missing) if missing else
                      "observed program declares the gateway; owner actions and device eligibility remain separate"}


class ConfiguredPrograms:
    """One startup/explicit-refresh cache shared by reads and new-job admission."""
    def __init__(self, bins, env=None):
        self.bins, self.env = bins, env
        self._guard, self._refresh_guard = threading.Lock(), threading.Lock()
        self._records = {}
        self.refresh()

    def refresh(self):
        with self._refresh_guard:
            records = {name: observe_program(name, executable, cwd, self.env)
                       for name, (executable, cwd) in list(self.bins.items()) if name in PROGRAMS}
            with self._guard:
                self._records = records

    def snapshot(self):
        with self._guard:
            cached = copy.deepcopy(self._records)
        result = {}
        for name, (executable, cwd) in list(self.bins.items()):
            if name not in PROGRAMS:
                continue
            path, current = program_file(executable, cwd, self.env)
            record = cached.get(name)
            if record is None:
                record = {"state": "unknown", "resolved_path": path, "observed_at": None,
                          "file": None, "identity": None,
                          "reason": "configured program has not been observed; refresh explicitly"}
            elif (path, current) != (record["resolved_path"], record["file"]):
                record.update(state="stale", reason="configured program changed since observation; refresh explicitly")
            if name == "phonefarm":
                record["compatibility"] = {"gray_control": gray_compatibility(record)}
            result[name] = record
        return result

    def require_session(self, spec, prepared=None):
        if not is_gray_session(spec):
            return
        record = self.snapshot().get("phonefarm")
        compatibility = gray_compatibility(record)
        if spec.get("exe"):
            raise ProgramCompatibilityError(400, "program_override_unsupported",
                "new gray sessions require the configured phonefarm for both owner and control helper", compatibility)
        if compatibility["status"] != "compatible":
            known = compatibility["status"] == "incompatible"
            raise ProgramCompatibilityError(409 if known else 503,
                "program_protocol_unsupported" if known else "program_identity_unavailable",
                compatibility["reason"], compatibility)
        if prepared is not None:
            path, _ = program_file(prepared["command"][0], prepared["cwd"], self.env)
            if path != record["resolved_path"]:
                raise ProgramCompatibilityError(409, "program_configuration_changed",
                    "prepared session executable differs from the currently observed phonefarm; submit a new request",
                    compatibility)


def _text(value, nullable=False):
    return value is None and nullable or isinstance(value, str) and 0 < len(value) <= 1024


def read_build_info(directory):
    """Read an optional installation declaration; never infer it from git."""
    try:
        root = SafeRoot(directory)
        try:
            value = root.read_json("runtime-build.json", maximum=LIMIT)
        finally:
            root.close()
        if (isinstance(value, dict) and value.get("schema") == "hgamey/source-build@1"
                and _text(value.get("repository")) and "revision" in value and "dirty" in value
                and _text(value["revision"], nullable=True)
                and (value["dirty"] is None or type(value["dirty"]) is bool)):
            return value
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    return None


def runtime_snapshot(entry_path):
    path = os.path.abspath(os.fspath(entry_path))
    return {"entry_path": path, "python_version": platform.python_version(),
            "observed_at": time.time(), "build": read_build_info(os.path.dirname(path))}


def _identity(value, program=None):
    if (not isinstance(value, dict) or value.get("schema") != "phonefarm/runtime-identity@1"
            or not isinstance(value.get("program"), str) or value["program"] not in PROGRAMS
            or program is not None and value["program"] != program
            or not _text(value.get("package_version"))):
        return False
    build, protocols = value.get("build"), value.get("supported_protocols")
    return (isinstance(build, dict)
            and all(key in build for key in ("source_revision", "source_dirty", "target", "profile"))
            and all(_text(build[key], nullable=True) for key in ("source_revision", "target", "profile"))
            and (build["source_dirty"] is None or type(build["source_dirty"]) is bool)
            and isinstance(protocols, list) and len(protocols) <= 64
            and all(_text(item) for item in protocols))


def read_program_report(store, jid, child_pid, final=False):
    def result(state, reason=None, record=None):
        return {"state": state, "source": "program_self_report", "record": record, "reason": reason}

    if type(child_pid) is not int or child_pid <= 0:
        return result("unknown", "direct child identity is unavailable")
    try:
        # A contained child owns its output until handback. Use the same pinned,
        # no-follow file reader without requiring the management JSON's UID.
        value = json.loads(store.read_bytes(jid + "/out/.program-runtime.json", LIMIT))
    except FileNotFoundError:
        return result("unknown" if final else "pending", "program did not report its runtime identity")
    except (OSError, ValueError, TypeError, RecursionError):
        return result("unknown", "program runtime report is unreadable or invalid")
    if (not isinstance(value, dict) or value.get("schema") != "phonefarm/program-start@1"
            or value.get("job_id") != jid or type(value.get("pid")) is not int
            or value["pid"] != child_pid or not _identity(value.get("identity"))):
        return result("unknown", "program runtime report does not match the direct child and job")
    return result("reported", record=value)


def program_file(executable, cwd, env=None):
    """Cheap file observation; no execution and no whole-binary hash."""
    path = os.fspath(executable)
    if not os.path.isabs(path):
        environment = os.environ if env is None else env
        path = (os.path.join(cwd, path) if os.sep in path else
                shutil.which(path, path=environment.get("PATH", os.defpath)))
    if path is None:
        return None, None
    path = os.path.realpath(path)
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            return path, None
        return path, {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
                      "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
    except OSError:
        return path, None


def _runtime_info(path, cwd, env):
    # Only administrator-configured PF/GOL callers reach this helper. Temporary
    # files bound memory even if an older or broken program writes too much.
    environment = dict(os.environ if env is None else env)
    for key in list(environment):
        if key == "PF_RUNTIME_REPORT" or key.startswith("PF_JOB_"):
            environment.pop(key)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            proc = subprocess.Popen([path, "runtime-info", "--json"], cwd=cwd, env=environment,
                                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    start_new_session=True)
        except (OSError, ValueError):
            return None, "runtime-info could not start"
        deadline = time.monotonic() + 2
        problem = None
        while True:
            code = proc.poll()
            if max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) > LIMIT:
                problem = "runtime-info exceeded its 64 KiB output limit"
            elif code is None and time.monotonic() >= deadline:
                problem = "runtime-info exceeded its two-second deadline"
            if problem or code is not None:
                break
            time.sleep(.01)
        if problem:
            # Only signal the session while its owned leader is unreaped.
            # poll() may already have reaped an oversized successful response.
            if code is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            return None, problem
        if code != 0:
            return None, "runtime-info is unsupported or failed"
        stdout.seek(0)
        try:
            data = stdout.read(LIMIT + 1)
            if len(data) > LIMIT:
                return None, "runtime-info exceeded its 64 KiB output limit"
            return json.loads(data), None
        except (ValueError, RecursionError):
            return None, "runtime-info did not return valid identity JSON"


def observe_program(name, executable, cwd, env=None):
    if name not in PROGRAMS:
        raise ValueError("runtime-info is restricted to configured phonefarm/game_opt_loop")
    path, before = program_file(executable, cwd, env)
    observation = {"state": "unknown", "resolved_path": path, "observed_at": time.time(),
                   "file": before, "identity": None, "reason": None}
    if before is None:
        observation["reason"] = "configured program file is unavailable"
        return observation
    identity, reason = _runtime_info(path, cwd, env)
    after_path, after = program_file(executable, cwd, env)
    if (path, before) != (after_path, after):
        reason = "configured program changed during runtime-info; identity was not bound"
    elif reason is None and not _identity(identity, name):
        reason = "runtime-info identity is invalid or names another program"
    if reason is None:
        observation.update(state="observed", identity=identity)
    else:
        observation["reason"] = reason
    return observation
