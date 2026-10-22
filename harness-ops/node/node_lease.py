"""Read phonefarm's host lease contract; observations never grant ownership."""
import json
import subprocess
import threading
import time


DEVICE_BINS = {"phonefarm", "game_opt_loop", "adb", "fetch_apk"}


def device_access(spec):
    return spec.get("device_access", "write" if spec.get("bin") in DEVICE_BINS else "none")


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
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                value = {"schema_version": 1, "status": "unknown", "bridge_held": None,
                         "entries": [], "error": f"lease status unavailable: {type(error).__name__}: {error}"}
            self._snapshot = {**value, "sampled_at": time.time()}
            self._next = time.monotonic() + 1
            return dict(self._snapshot)
