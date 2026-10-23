"""Installed privileged entry; requests name a job, never root paths or programs."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import traceback

from artifact_io import SafeRoot
import job_runtime


MODULES = ("artifact_io.py", "job_policy.py", "job_runtime.py", "job_cgroup.py",
           "job_privileged.py", "worker_entry.py")
CONFIG_NAME = "worker.json"


def trusted_path(path, directory=False):
    """Installation ancestors and files cannot be replaced by the node account."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("trusted worker paths must be absolute and not symbolic links")
    for item in (path, *path.parents):
        st = item.lstat()
        if st.st_uid != 0 or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
            raise ValueError("worker installation is not root-owned and immutable to other users: " + str(item))
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ValueError("unexpected trusted worker path type")


def configuration(prefix):
    trusted_path(prefix, directory=True)
    for name in (*MODULES, "phonefarm-worker", CONFIG_NAME):
        trusted_path(prefix / name)
    with (prefix / CONFIG_NAME).open() as f:
        config = json.load(f)
    if config.get("schema") != "phonefarm/worker-install@1":
        raise ValueError("unsupported installed worker configuration")
    for key in ("node_uid", "node_gid", "uid", "gid"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError("installed node/job identities must be non-root integers")
    if config["node_uid"] == config["uid"] or config["node_gid"] == config["gid"]:
        raise ValueError("node and job identities must be distinct")
    for key in ("jobs_root", "cgroup_root"):
        if not isinstance(config.get(key), str) or not os.path.isabs(config[key]):
            raise ValueError(key + " must be an absolute administrator-configured path")
    return config


def run(prefix, jid):
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
        if len(sys.argv) != 2:
            raise ValueError("installed worker accepts only a job ID; configuration paths are fixed")
        return run(Path(__file__).parent, sys.argv[1])
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
