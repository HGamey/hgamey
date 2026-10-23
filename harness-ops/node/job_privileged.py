"""Unprivileged FD handoff to an administrator-installed worker entry."""
import os
import sys


def worker_command(entry, jid, lock_fd):
    if not os.path.isabs(entry):
        raise ValueError("installed worker entry must be an absolute path")
    return [sys.executable, os.path.abspath(__file__), entry, jid, str(lock_fd)]


def main():
    if len(sys.argv) != 4:
        raise ValueError("expected installed entry, job ID and inherited lock FD")
    entry, jid, raw_fd = sys.argv[1:]
    fd = int(raw_fd)
    if fd < 3 or not os.path.isabs(entry):
        raise ValueError("invalid worker handoff")
    # Remap only in this dedicated child, never in the multithreaded HTTP node.
    if fd != 3:
        os.dup2(fd, 3, inheritable=True)
        os.close(fd)
    else:
        os.set_inheritable(3, True)
    os.execve("/usr/bin/sudo", ["sudo", "-n", "-C", "4", "--", entry, jid],
              {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


if __name__ == "__main__":
    main()
