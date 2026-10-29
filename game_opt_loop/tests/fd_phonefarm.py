#!/usr/bin/env python3
"""Selected-PF fixture: the actual socket/fd wire contract, no device transport."""
import array
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys


def handoff():
    arguments = sys.argv[2:]
    wire = socket.socket(fileno=int(arguments[0]))
    root = Path(__file__).parent
    inherited = os.environ.get("PF_LOCK_FDS")
    if inherited:
        fds = [int(fd) for fd in inherited.split(",")]
        scope = json.loads(os.environ["PF_LOCK_SCOPE"])
    else:
        fd = os.open(root / "device.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds = [fd]
        serial = arguments[arguments.index("--serial") + 1] if "--serial" in arguments else None
        scope = {"key": serial, "serial": serial, "binding": None}
    payload = json.dumps(scope).encode()
    wire.sendmsg([b"x"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
    wire.sendall(struct.pack("!I", len(payload)) + payload)
    wire.recv(1)


if __name__ == "__main__":
    handoff()
