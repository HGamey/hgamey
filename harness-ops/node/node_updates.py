"""Read-only installation status, bound to this service's startup observation."""
import copy
import os
from pathlib import Path
import stat
import time

import node_update


def _path(value):
    # Resolve the containing directory, but never hide a symlink at the file.
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("updater paths must be absolute")
    return path.parent.resolve() / path.name


def _fingerprint(path, executable=False):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK | (os.X_OK if executable else 0)):
        raise ValueError("updater is not an accessible ordinary file")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# Keep the module observation from import, not from a later API request. This
# establishes a local source/path observation, not a signed build attestation.
try:
    _MODULE_PATH = _path(node_update.__file__)
    _MODULE_CODE_PATH = _path(node_update.maintenance_record.__code__.co_filename)
    _MODULE_FINGERPRINT = _fingerprint(_MODULE_PATH)
except (OSError, ValueError, TypeError, AttributeError):
    _MODULE_PATH = _MODULE_CODE_PATH = _MODULE_FINGERPRINT = None


class NodeUpdates:
    def __init__(self, layout, executable, service_directory):
        self.layout = copy.deepcopy(layout)
        self.observed_at = time.time()
        self.executable = executable if isinstance(executable, str) else None
        self.files = None
        self.reason = "configured updater is not the current service's observed safe update entry"
        try:
            directory = Path(service_directory).resolve()
            script = _path(self.executable)
            module = directory / "node_update.py"
            if (script != directory / "update_node.sh" or module != _MODULE_PATH
                    or module != _MODULE_CODE_PATH or _MODULE_FINGERPRINT is None
                    or _fingerprint(module) != _MODULE_FINGERPRINT):
                return
            self.files = ((script, _fingerprint(script, executable=True)), (module, _MODULE_FINGERPRINT))
            self.reason = None
        except (OSError, ValueError, TypeError):
            pass

    def updater(self):
        state, reason = "unknown", self.reason
        if self.files is not None:
            state, reason = "observed", None
            try:
                if any(_fingerprint(path, executable=(index == 0)) != identity
                       for index, (path, identity) in enumerate(self.files)):
                    state = "stale"
            except (OSError, ValueError, TypeError):
                state = "stale"
            if state == "stale":
                reason = "updater files changed since startup; review the installation and restart the service explicitly"
        return {"available": state == "observed", "reason": reason, "executable": self.executable,
                "identity_state": state, "observed_at": self.observed_at,
                "contract": "hgamey/node-update@1"}

    def report(self, node_id):
        now = time.time()
        maintenance = {"state": "unknown", "run_id": None,
                       "reason": "installation maintenance record is unavailable or invalid",
                       "observed_at": now, "all_participants_verified": False}
        try:
            marker = node_update.maintenance_record(self.layout)
            if marker is None:
                maintenance.update(state="clear", reason="no recorded installation maintenance obligation")
            else:
                maintenance.update(state="blocked", run_id=marker["run_id"],
                                   reason="installation update requires explicit rollback of the recorded run")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
        return {"schema": "hgamey/node-updates@1", "node_id": node_id, "observed_at": now,
                "scope": "host-only", "layout": copy.deepcopy(self.layout), "updater": self.updater(),
                "maintenance": maintenance,
                "limitations": ["clear means no recorded maintenance obligation, not that all participants are stopped",
                                "updater identity is a startup file observation; this read does not execute or refresh it",
                                "no device, lease, update history, or live job qualification is checked",
                                "publication, rollback, command exit, and service restart are separate facts"]}
