"""Fixed worker installation and shared PF environment contract."""
import json
import os
from pathlib import Path
import stat

MODULES = ("artifact_io.py", "job_policy.py", "job_runtime.py", "job_cgroup.py",
           "job_privileged.py", "worker_entry.py", "worker_config.py")
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
    phonefarm_environment(config)
    return config


PF_ROOTS = ("PHONEFARM_STATE_DIR", "DEVLOCK_DIR", "PHONEFARM_LEASE_DIR")
PF_TOOLS = ("ADB_BIN", "DEVLOCK_BIN")


def phonefarm_environment(config):
    """Only administrator-selected PF paths and the dedicated job group cross UIDs."""
    value = config.get("pf_env")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - set((*PF_ROOTS, *PF_TOOLS, "PHONEFARM_SHARED_GID")):
        raise ValueError("unsupported fixed phonefarm environment")
    for key in PF_ROOTS:
        if not isinstance(value.get(key), str) or not os.path.isabs(value[key]):
            raise ValueError(key + " must be an explicit absolute path")
    for key in PF_TOOLS:
        if key in value and (not isinstance(value[key], str) or not os.path.isabs(value[key])):
            raise ValueError(key + " must be an absolute administrator-selected tool")
    if value.get("PHONEFARM_SHARED_GID") != str(config["gid"]):
        raise ValueError("PF sharing group must be the dedicated job group")
    jobs = os.path.realpath(config["jobs_root"])
    for key in PF_ROOTS:
        if os.path.commonpath((jobs, os.path.realpath(value[key]))) == jobs:
            raise ValueError("persistent PF roots cannot be inside the jobs directory")
    return {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **value}


def node_environment(prefix, jobs_root=None):
    """Read the same immutable, non-secret configuration as the fixed worker."""
    config = configuration(Path(prefix))
    if os.geteuid() != config["node_uid"] or os.getegid() != config["node_gid"]:
        raise ValueError("node identity does not match installed worker configuration")
    if config["gid"] not in {*os.getgroups(), os.getegid()}:
        raise ValueError("node process must already belong to the dedicated PF sharing group")
    if jobs_root is not None and os.path.realpath(jobs_root) != os.path.realpath(config["jobs_root"]):
        raise ValueError("node jobs root does not match installed worker configuration")
    env = phonefarm_environment(config)
    if env is None:
        raise ValueError("installed worker has no shared PF profile")
    return env
