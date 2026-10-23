"""Installed privileged entry; requests name a job, never root paths or programs."""
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
import traceback

from artifact_io import SafeRoot
import job_runtime


from worker_config import configuration


def run(prefix, jid, *, reconcile=False, force=False):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("installed worker requires its configured privileged launcher")
    if not re.fullmatch(r"[0-9a-f]{12}", jid):
        raise ValueError("expected one 12-hex job ID")
    config = configuration(prefix)
    # sudo supplies this field itself. A direct root administrator may also call
    # the entry, but a different sudo-authorized account cannot select this node.
    if "SUDO_UID" in os.environ and int(os.environ["SUDO_UID"]) != config["node_uid"]:
        raise ValueError("sudo caller does not match the configured node identity")
    os.environ.clear()
    os.environ.update(PATH="/usr/bin:/bin", LANG="C.UTF-8")
    os.umask(0o077)
    owner = (config["node_uid"], config["node_gid"])
    store = SafeRoot(config["jobs_root"], json_owner=owner)
    try:
        root = os.fstat(store.fd)
        if root.st_uid != owner[0] or root.st_mode & 0o022:
            raise ValueError("jobs root must belong to the configured node without group/other write access")
        job_fd = store.open_fd(jid, directory=True)
        try:
            st = os.fstat(job_fd)
            if st.st_uid != owner[0] or st.st_mode & 0o022:
                raise ValueError("job directory does not belong to this node")
        finally:
            os.close(job_fd)
        expected_fd = store.open_fd(jid + "/runner.lock", directory=False)
        try:
            expected, received = os.fstat(expected_fd), os.fstat(3)
            if (not stat.S_ISREG(received.st_mode) or received.st_uid != owner[0] or received.st_nlink != 1
                    or (expected.st_dev, expected.st_ino) != (received.st_dev, received.st_ino)):
                raise ValueError("inherited FD 3 does not identify this job's runner lock")
        finally:
            os.close(expected_fd)
        # This succeeds on the same inherited open description, or claims a
        # currently free lock. Another active worker's description blocks it.
        fcntl.flock(3, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(3, False)
        if reconcile:
            from job_cgroup import reconcile as reconcile_domain
            return reconcile_domain(store, jid, os.path.join(config["jobs_root"], jid), config, force=force)
        meta = store.read_json(jid + "/meta.json")
        execution = meta.get("execution") or {}
        if (meta.get("state") != "running" or meta.get("phase") not in ("preparing", "starting")
                or meta.get("pid") is not None or execution.get("child_pid") is not None):
            raise ValueError("job already started or reached a terminal state; create a new job instead of replaying it")
        if store.read_json(jid + "/launch.json").get("execution_policy") != "contained":
            raise ValueError("installed worker refuses non-contained launch records")
        # Keep this pinned root. Do not resolve a mutable node path again after
        # validation; runtime also requires contained on its own fresh spec read.
        return job_runtime._worker(os.path.join(config["jobs_root"], jid), 3,
                                   contained_config=config, record_owner=owner, _store=store)
    finally:
        store.close()


def main():
    try:
        args = sys.argv[1:]
        if not args or args[1:] not in ([], ["--reconcile"], ["--reconcile", "--force"]):
            raise ValueError("expected job ID with optional --reconcile [--force]; configuration paths are fixed")
        return run(Path(__file__).parent, args[0], reconcile="--reconcile" in args, force="--force" in args)
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
