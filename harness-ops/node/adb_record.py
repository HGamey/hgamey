"""What a job sends to a phone, as the platform saw it.

A job's processes find `adb` first in tools/adb-record, which runs this: one JSON line per call goes to the job's
device record (PF_ADB_RECORD, in its output directory), then the real adb (PF_REAL_ADB) runs in its place with the
same arguments, input and output. Recording never stops a call: any failure to record is skipped.

A line names the phone and what the call does: a file pushed or a package installed (local path, size, SHA-256), a
shell command, a file pulled, or any other adb command with its arguments.
"""
import hashlib
import json
import os
import sys
import time

RECORD_MAX = 4 << 20          # a script that polls adb in a loop stops being recorded here, not the job
HASH_MAX = 1 << 30
TEXT_MAX = 2000
GLOBAL_WITH_VALUE = {"-s", "-t", "-H", "-P", "-L"}
UNSENT = {"devices", "version", "start-server", "kill-server", "get-state", "get-serialno", "get-devpath", "help",
          "reconnect", "keygen", "mdns", "wait-for-device", "wait-for-usb-device", "wait-for-local-device"}


def describe_file(path):
    item = {"path": path}
    try:
        size = os.path.getsize(path)
        item["size"] = size
        if size <= HASH_MAX and os.path.isfile(path):
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            item["sha256"] = digest.hexdigest()
    except OSError:
        pass
    return item


def describe(args, now=None):
    """The record line for one adb call, or None for calls that do not reach a phone."""
    serial, rest = os.environ.get("ANDROID_SERIAL"), list(args)
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in GLOBAL_WITH_VALUE and rest:
            value = rest.pop(0)
            if flag == "-s":
                serial = value
    if not rest or rest[0] in UNSENT or rest[0].startswith("wait-for-"):
        return None
    command, operands = rest[0], rest[1:]
    line = {"at": round(now if now is not None else time.time(), 3), "serial": serial, "command": command}
    plain = [arg for arg in operands if not arg.startswith("-")]
    if command == "push" and len(plain) >= 2:
        line.update(files=[describe_file(path) for path in plain[:-1]], to=plain[-1])
    elif command in ("install", "install-multiple") and plain:
        line.update(command="install", files=[describe_file(path) for path in plain],
                    flags=[arg for arg in operands if arg.startswith("-")])
    elif command in ("shell", "exec-out", "exec-in"):
        line["text"] = " ".join(arg for arg in operands if arg not in ("-t", "-T", "-n", "-x"))[:TEXT_MAX]
    elif command == "pull" and plain:
        line.update(source=plain[0], to=plain[1] if len(plain) > 1 else None)
    else:
        line["args"] = [arg[:200] for arg in operands[:20]]
    return line


def record(path, line):
    try:
        if os.path.exists(path) and os.path.getsize(path) >= RECORD_MAX:
            return
        data = json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data.encode())
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        pass


def main(args):
    real = os.environ.get("PF_REAL_ADB")
    if not real:
        sys.stderr.write("adb-record: PF_REAL_ADB is not set\n")
        return 127
    path = os.environ.get("PF_ADB_RECORD")
    if path:
        try:
            line = describe(args)
        except Exception:  # noqa: BLE001 - recording must never stop the call
            line = None
        if line:
            record(path, line)
    os.execv(real, [real, *args])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
