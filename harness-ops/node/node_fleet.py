"""One demand-driven device sampler shared by the node API and dashboard."""
import collections
import json
import os
import subprocess
import threading
import time
from node_lease import LeaseState, queue_directory

VIEW_IDLE_S = 90
FLEET_EVERY_FREE_S = 30
FLEET_EVERY_HELD_S = 60
FREE_GRACE_S = 0
SHOT_EVERY_S = 60
STALE_AFTER_S = 90


def read_lock(directory):
    """Read historical events only; static owner and queue files are retired."""
    out = dict(status="unknown", held_by=None, since=None, heartbeat_age_s=None,
               stale=False, queue=[], recent=[], free_for_s=None, error="current PF lock status unavailable")
    try:
        with open(os.path.join(directory, "devlock.log"), "rb") as f:
            offset = max(0, f.seek(0, 2) - 4096)
            f.seek(offset)
            lines = f.read().decode(errors="replace").splitlines()
        events = []
        for line in lines[1:] if offset else lines:
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("acquire", "release", "steal"):
                label = parts[4] if parts[2:4] == ["steal", "by"] and len(parts) > 4 else parts[3]
                events.append(dict(at=f"{parts[0]} {parts[1]}", action=parts[2], label=label))
        out["recent"] = events[::-1][:12]
    except (OSError, ValueError):
        pass
    return out


class FleetState:
    def __init__(self, phonefarm_root, lock_dir, shot_dir, *, lease_state=None):
        self.root, self.lock_dir, self.shot_dir = phonefarm_root, lock_dir, shot_dir
        self.leases = lease_state
        if lock_dir is None and self.leases is None:
            self.leases = LeaseState({"phonefarm": (os.path.join(phonefarm_root, "phonefarm"), phonefarm_root)})
        self._condition = threading.Condition()
        self._thread = None
        self._closed = False
        self._active_until = self._shots_until = self._next_at = 0.0
        self._attempt = 0
        self._fleet = None
        self._sampled_at = None
        self._attempted_at = None
        self._error = self._deferred = None
        self._refreshing = False
        self._history = collections.deque(maxlen=720)

    def request(self, wait=0, *, observe=False):
        """Activate bounded sampling. Metadata requests never enable screenshots."""
        with self._condition:
            if self._closed:
                return self._snapshot()
            now = time.monotonic()
            self._active_until = now + VIEW_IDLE_S
            if observe:
                self._shots_until = now + VIEW_IDLE_S
            attempt = self._attempt
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="node-fleet")
                self._thread.start()
            self._condition.notify_all()
            if wait and (self._refreshing or now >= self._next_at):
                self._condition.wait_for(lambda: self._attempt != attempt or self._closed, timeout=wait)
            return self._snapshot()

    def snapshot(self):
        """Read cached state without activating the device sampler."""
        with self._condition:
            return self._snapshot()

    def _snapshot(self):
        now = time.time()
        age = max(0, now - self._sampled_at) if self._sampled_at is not None else None
        return dict(schema_version=1, now=now, lock=self._read_lock(),
                    devices=self._fleet["devices"] if self._fleet is not None else None,
                    notes=self._fleet.get("notes", []) if self._fleet is not None else [],
                    sampled_at=self._sampled_at, attempted_at=self._attempted_at, age_s=age,
                    stale=age is None or age > STALE_AFTER_S, refreshing=self._refreshing,
                    error=self._error, deferred=self._deferred,
                    reclaimed=self.leases.reclaimed if self.leases is not None else None,
                    policy=dict(fleet_every_free_s=FLEET_EVERY_FREE_S,
                                fleet_every_held_s=FLEET_EVERY_HELD_S,
                                free_grace_s=FREE_GRACE_S, stale_after_s=STALE_AFTER_S))

    def _read_lock(self):
        if self.leases is None:
            return read_lock(self.lock_dir)
        lease = self.leases.snapshot(refresh=True)
        lock = dict(status="unknown", held_by=None, since=None, heartbeat_age_s=None,
                    stale=False, queue=[], recent=[], free_for_s=None, error=lease.get("error"), lease=lease)
        # Existing logs remain history; they never decide current occupancy.
        try:
            lock["recent"] = read_lock(queue_directory(lease))["recent"]
        except ValueError:
            pass
        if lease["status"] == "clear":
            lock.update(status="free", error=None)
        elif lease["status"] == "held":
            owner = next(iter(lease.get("entries", [])), {})
            lock.update(status="held", held_by=owner.get("label"), since=owner.get("since"))
        return lock

    def history(self):
        with self._condition:
            return list(self._history)

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _run(self):
        while True:
            with self._condition:
                now = time.monotonic()
                while not self._closed and (now >= self._active_until or now < self._next_at):
                    self._condition.wait(None if now >= self._active_until else self._next_at - now)
                    now = time.monotonic()
                if self._closed:
                    return
                self._attempted_at = time.time()
                lock = self._read_lock()
                self._refreshing, self._deferred = True, None
                # PF tries each device independently; one busy phone must not
                # disable screenshots of every idle phone.
                screenshots = now < self._shots_until
            executable, cwd = (self.leases.bins["phonefarm"] if self.leases else
                               (os.path.join(self.root, "phonefarm"), self.root))
            configured = self.leases.env if self.leases and self.leases.env is not None else os.environ
            env = {k: v for k, v in configured.items() if k not in {"DEVLOCK_LABEL", "DEVLOCK_INSTANCE_ID"}}
            command = [executable, "fleet", "--json"]
            if self.leases is None:
                command += ["--lock-dir", self.lock_dir]
            if screenshots:
                command += ["--screenshot-dir", self.shot_dir, "--screenshot-every", str(SHOT_EVERY_S)]
            fleet, error, deferred = None, None, None
            try:
                # The PF supervisor bounds and cleans up its own observation group.
                result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, start_new_session=True,
                                        timeout=40, stdin=subprocess.DEVNULL, check=False)
                value = json.loads(result.stdout)
                if not isinstance(value, dict) or value.get("schema") != "phonefarm/fleet@1":
                    raise ValueError("incompatible fleet schema")
                observation = value.get("observation", "sampled")
                if result.returncode == 3 and observation in ("deferred", "error"):
                    reason = value.get("reason")
                    if value.get("devices", "missing") is not None or not isinstance(reason, str) or not reason.strip():
                        raise ValueError("deferred/error fleet response requires a reason and null devices")
                    if observation == "deferred":
                        deferred = reason
                    else:
                        error = f"fleet 采样失败: {reason}"
                else:
                    if result.returncode not in (0, 1) or observation != "sampled":
                        raise ValueError(f"unexpected fleet observation/exit: {observation}/{result.returncode}")
                    if not isinstance(value.get("devices"), list):
                        raise ValueError("fleet response must contain a devices array")
                    if (result.returncode == 1) != (not value["devices"]):
                        raise ValueError("fleet exit code does not match its device count")
                    for device in value["devices"]:
                        if not isinstance(device, dict):
                            raise ValueError("fleet devices must be objects")
                        for field in ("battery", "thermal"):
                            if device.get(field) is not None and not isinstance(device[field], dict):
                                raise ValueError(f"fleet device {field} must be an object or null")
                    if value.get("lock") is not None and not isinstance(value["lock"], dict):
                        raise ValueError("fleet lock must be an object or null")
                    fleet = value
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                error = f"fleet 采样失败: {type(e).__name__} {str(e)[:200]}"
            with self._condition:
                if fleet is not None:
                    self._fleet, self._sampled_at = fleet, time.time()
                    self._record(fleet)
                self._error, self._deferred, self._refreshing = error, deferred, False
                interval = FLEET_EVERY_HELD_S if lock["status"] == "held" else FLEET_EVERY_FREE_S
                self._next_at = time.monotonic() + (5 if deferred else interval)
                self._attempt += 1
                self._condition.notify_all()

    def _record(self, fleet):
        device = next((d for d in fleet["devices"] if d.get("online")), None)
        if device:
            battery, thermal = device.get("battery") or {}, device.get("thermal") or {}
            self._history.append(dict(t=int(self._sampled_at), soc=thermal.get("soc_max_c"),
                                      gpu=thermal.get("gpu_c"), bat=battery.get("level_pct"),
                                      bat_c=battery.get("temp_c"),
                                      held=bool((fleet.get("lock") or {}).get("held_by"))))
