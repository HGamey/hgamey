"""Unprivileged FD handoff to an administrator-installed worker entry."""
import os
import sys


def worker_command(entry, jid, lock_fd, operation="run"):
    if not os.path.isabs(entry):
        raise ValueError("installed worker entry must be an absolute path")
    if operation not in {"run", "reconcile", "reconcile-force"}:
        raise ValueError("unknown fixed worker operation")
    return [sys.executable, os.path.abspath(__file__), entry, jid, str(lock_fd), operation]


def main():
    if len(sys.argv) != 5:
        raise ValueError("expected installed entry, job ID and inherited lock FD")
    entry, jid, raw_fd, operation = sys.argv[1:]
    fd = int(raw_fd)
    if fd < 3 or not os.path.isabs(entry):
        raise ValueError("invalid worker handoff")
    # Remap only in this dedicated child, never in the multithreaded HTTP node.
    if fd != 3:
        os.dup2(fd, 3, inheritable=True)
        os.close(fd)
    else:
        os.set_inheritable(3, True)
    if operation not in {"run", "reconcile", "reconcile-force"}:
        raise ValueError("unknown fixed worker operation")
    args = [jid] if operation == "run" else [jid, "--reconcile", *(["--force"] if operation == "reconcile-force" else [])]
    os.execve("/usr/bin/sudo", ["sudo", "-n", "-C", "4", "--", entry, *args],
              {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


if __name__ == "__main__":
    main()
