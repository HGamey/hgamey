#!/usr/bin/env python3
"""Install only dash/steer credentials from one upload directory."""

import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("credential files must be ordinary non-hardlinked files: " + str(path))
    return info


def copy_file(source, target, *, mode=None):
    info = regular(source)
    durable_directory(target.parent, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=".install-secrets-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out, 1 << 20)
            os.fchmod(out.fileno(), stat.S_IMODE(info.st_mode) if mode is None else mode)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        sync_dir(target.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def durable_directory(path, mode=0o700):
    if not path.exists():
        durable_directory(path.parent, mode=mode)
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
    if path.is_symlink() or not path.is_dir():
        raise ValueError("directory path must be an ordinary directory")
    sync_dir(path)
    sync_dir(path.parent)


def private_directory(path):
    durable_directory(path, mode=0o700)


def install_secrets(upload):
    config = Path(os.path.expanduser(os.environ.get("HG_CONFIG_DIR") or "~/.config/hgamey"))
    if not config.is_absolute():
        config = Path.home() / "node" / config
    for name in ("dash.pass", "steer.pass"):
        source = upload / "secrets" / name
        if not source.is_file():
            continue
        regular(source)
        if not config.exists():
            private_directory(config)
        copy_file(source, config / name, mode=0o600)
        source.unlink()
        print("updated " + str(config / name), file=sys.stderr)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: install_secrets.py UPLOAD", file=sys.stderr)
        return 2
    try:
        install_secrets(Path(args[0]).resolve())
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        print("install-secrets: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
