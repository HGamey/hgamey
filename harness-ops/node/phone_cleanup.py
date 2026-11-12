"""Leftover processes on an idle phone. A process of the shell user whose parent is init and that keeps using the CPU
was left behind by something that has ended: a job's backgrounded command, an adb session that dropped. Nothing
stops it, and it can keep a phone hot and its battery from charging for days. While nothing holds a phone (no job,
no remote control session), the node's sampler looks for such processes now and then, stops them, and keeps a record
of each clean-up per phone. Recovery only: no job is held back and nothing is checked at submission."""
import json
import os
import subprocess
import threading
import time

PS = ["shell", "ps", "-A", "-o", "PID,PPID,USER,TIME,ETIME,ARGS"]
MIN_CPU_S = 60        # a minute of CPU in all: a process a job started moments ago never counts
MIN_SHARE = 0.2       # and on the CPU for a fifth of its life
EVERY_S = 300         # each idle phone is looked at once per five minutes
KEEP_S = 86400        # a clean-up shows on the pages for a day


def seconds(text):
    """[[dd-]hh:]mm:ss as toybox ps prints TIME and ETIME."""
    days, _, rest = text.rpartition("-")
    parts = [int(part) for part in rest.split(":")]
    if not 1 <= len(parts) <= 3 or any(part < 0 for part in parts):
        raise ValueError(text)
    hours, minutes, secs = ([0, 0] + parts)[-3:]
    return int(days or 0) * 86400 + hours * 3600 + minutes * 60 + secs


def leftovers(listing):
    """The leftover processes in `ps -A -o PID,PPID,USER,TIME,ETIME,ARGS` output."""
    found = []
    for line in listing.splitlines()[1:]:
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        pid, ppid, user, cpu, elapsed, args = fields
        try:
            pid, ppid, cpu, elapsed = int(pid), int(ppid), seconds(cpu), seconds(elapsed)
        except ValueError:
            continue
        if user == "shell" and ppid == 1 and cpu >= MIN_CPU_S and cpu >= MIN_SHARE * max(elapsed, 1):
            found.append({"pid": pid, "name": args.strip()[:120], "cpu_s": cpu, "elapsed_s": elapsed})
    return found


class Cleaner:
    """Looks at idle adb phones for leftovers and stops them; the record of each clean-up is kept in `log` (JSON lines)."""

    def __init__(self, adb, log, env=None):
        self.adb, self.log, self.env = adb, log, env
        self._lock = threading.Lock()
        self._next = {}
        self._records = []
        try:
            with open(log, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and isinstance(record.get("at"), (int, float)):
                        self._records.append(record)
        except FileNotFoundError:
            pass
        self._forget()

    def _forget(self):
        since = time.time() - KEEP_S
        self._records = [record for record in self._records if record["at"] >= since]

    def run(self, devices, idle):
        """Look at each online adb phone that `idle(serial)` says nothing holds, at most once per EVERY_S."""
        for device in devices:
            serial = device.get("serial")
            if not serial or device.get("online") is not True or device.get("transport", "adb") != "adb" or not idle(serial):
                continue
            now = time.monotonic()
            if now < self._next.get(serial, 0):
                continue
            self._next[serial] = now + EVERY_S
            try:
                self._look(serial)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass   # an unread phone is looked at again next time

    def _adb(self, serial, *args):
        return subprocess.run([self.adb, "-s", serial, *args], capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL, env=self.env, check=True).stdout

    def _look(self, serial):
        found = leftovers(self._adb(serial, *PS))
        if not found:
            return
        self._adb(serial, "shell", "kill", "-9", *(str(process["pid"]) for process in found))
        record = {"at": time.time(), "serial": serial, "processes": found}
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        with open(self.log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        with self._lock:
            self._records.append(record)

    def recent(self, serial):
        """This phone's clean-ups in the last day, oldest first."""
        with self._lock:
            self._forget()
            return [{"at": record["at"], "processes": record["processes"]} for record in self._records
                    if record.get("serial") == serial]
