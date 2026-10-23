#!/usr/bin/python3
"""Install an immutable Linux worker copy; print, but never install, sudo policy.

Run explicitly as an administrator after reviewing the source. Existing prefixes
are not overwritten; service activation and live upgrades are separate actions.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import sys

from worker_entry import CONFIG_NAME, MODULES, configuration, trusted_path


BOOTSTRAP = '''#!/usr/bin/python3 -I
import os, stat, sys
from pathlib import Path
prefix = Path(__file__).absolute().parent
for path in (prefix, *prefix.parents, *prefix.iterdir()):
    info = path.lstat()
    if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
        raise SystemExit("untrusted worker installation")
sys.path.insert(0, str(prefix))
from worker_entry import main
sys.exit(main())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path("/usr/local/libexec/phonefarm-worker"))
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--cgroup-root", type=Path, required=True)
    for key in ("node-uid", "node-gid", "job-uid", "job-gid"):
        parser.add_argument("--" + key, type=int, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("installation requires an explicit root administrator on Linux")
    prefix = args.prefix
    if not prefix.is_absolute() or any(c in str(prefix) for c in " \t\n,:=!*?\\"):
        parser.error("prefix must be an absolute path without sudoers metacharacters")
    trusted_path(prefix.parent, directory=True)
    config = {"schema": "phonefarm/worker-install@1", "jobs_root": str(args.jobs_root),
              "cgroup_root": str(args.cgroup_root), "node_uid": args.node_uid, "node_gid": args.node_gid,
              "uid": args.job_uid, "gid": args.job_gid}
    # Account selection is an administrator operation; do not create or modify
    # accounts, cgroups, data roots, sudoers or an existing installation here.
    account = pwd.getpwuid(args.node_uid)
    if not all(v > 0 for v in (args.node_uid, args.node_gid, args.job_uid, args.job_gid)):
        parser.error("node and job identities must be non-root")
    if args.node_uid == args.job_uid or args.node_gid == args.job_gid:
        parser.error("node and job identities must differ")
    if not args.jobs_root.is_absolute() or not args.cgroup_root.is_absolute():
        parser.error("configured roots must be absolute")
    prefix.mkdir(mode=0o755)  # Never overwrite an installed/running version.
    try:
        prefix.chmod(0o755)
        source = Path(__file__).parent
        for name in MODULES:
            target = prefix / name
            shutil.copyfile(source / name, target)
            target.chmod(0o644)
        entry = prefix / "phonefarm-worker"
        entry.write_text(BOOTSTRAP)
        entry.chmod(0o755)
        target = prefix / CONFIG_NAME
        target.write_text(json.dumps(config, indent=2) + "\n")
        target.chmod(0o600)
        configuration(prefix)
        sudoers = (f"Defaults!{entry} closefrom_override\n"
                   f"{account.pw_name} ALL=(root) NOPASSWD: {entry} *\n")
        print(json.dumps({"prefix": str(prefix), "entry": str(entry), "config": str(target),
                          "sudoers": sudoers, "activated": False}, indent=2))
    except BaseException:
        # This invocation created the prefix exclusively; no previous install is
        # touched. Installation failures do not leave an apparently usable entry.
        shutil.rmtree(prefix)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
