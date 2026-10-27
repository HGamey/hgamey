"""Directory-level atomic deployment for an existing ~/node-style installation.

The manager keeps full releases under ``<root>/releases/<run_id>`` and points a
symlink cursor at one of them. Activation is a single ``os.replace`` of a
temporary symlink, so a running service either sees the whole previous release
or the whole new one — never a half-published tree. This is an administrator
tool; it never restarts services and never touches the jobs root, tokens or
phonefarm/game_opt_loop data.

Layout (created on first activation):
    <root>/harness-ops -> releases/<run_id>/harness-ops   (symlink cursor)
    <root>/releases/<run_id>/harness-ops/...
    <root>/deploy-state/<run_id>/receipt.json

Typical use:
    python3 deploy_overlay.py activate --root ~/node --source <staged>/harness-ops
    python3 deploy_overlay.py status    --root ~/node
    python3 deploy_overlay.py rollback  --root ~/node            # previous release
    python3 deploy_overlay.py rollback  --root ~/node --run <id>
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

SCHEMA = "hgamey/node-deploy-overlay@1"
COMPONENT = "harness-ops"
SMOKE_ENTRY = "node/node_service.py"


class DeployError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def load_receipt(run_dir):
    receipt = run_dir / "receipt.json"
    try:
        value = json.loads(receipt.read_text())
    except (OSError, ValueError) as error:
        raise DeployError(500, f"deploy receipt is unreadable: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise DeployError(500, "deploy receipt has an unknown schema")
    return value


def record(run_dir, receipt, status=None):
    receipt["updated_at"] = time.time()
    if status:
        receipt["status"] = status
    temporary = run_dir / "receipt.json.tmp"
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=1))
    os.replace(temporary, run_dir / "receipt.json")
    directory = os.open(run_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def tree_digest(root):
    """Stable digest over relative paths and file contents; links are recorded, not followed."""
    digest = hashlib.sha256()
    count = 0
    for base, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(files):
            path = Path(base) / name
            relative = path.relative_to(root)
            if path.is_symlink():
                digest.update(f"l {relative} -> {os.readlink(path)}\n".encode())
                continue
            digest.update(f"f {relative} ".encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\n")
            count += 1
    return digest.hexdigest(), count


def cursor(root):
    return root / COMPONENT


def ensure_layout(root):
    (root / "releases").mkdir(parents=True, exist_ok=True)
    (root / "deploy-state").mkdir(parents=True, exist_ok=True)
    if cursor(root).is_symlink():
        return
    if cursor(root).exists():
        # First adoption: move the existing plain directory into releases as the
        # baseline release so rollback always has somewhere to go.
        run_id = time.strftime("baseline-%Y%m%dT%H%M%S")
        destination = root / "releases" / run_id / COMPONENT
        destination.mkdir(parents=True)
        # Move the existing tree into the standard release layout so rollback
        # always has somewhere to go.
        for entry in os.listdir(cursor(root)):
            os.rename(cursor(root) / entry, destination / entry)
        os.rmdir(cursor(root))
        digest, files = tree_digest(destination)
        receipt = {"schema": SCHEMA, "run_id": run_id, "created_at": time.time(),
                   "status": "baseline", "source": "existing directory", "digest": digest,
                   "files": files, "components": [COMPONENT]}
        run_dir = root / "deploy-state" / run_id
        run_dir.mkdir(parents=True)
        record(run_dir, receipt, "baseline")
        switch(root, run_id)
    else:
        raise DeployError(400, f"{cursor(root)} does not exist; nothing to manage")


def run_dir_name(run_id):
    return run_id


def switch(root, run_id):
    temporary = root / f".{COMPONENT}.cursor.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(f"releases/{run_id}/{COMPONENT}", temporary)
    os.replace(temporary, cursor(root))


def source_tree(source):
    if source.is_symlink() or not source.is_dir():
        raise DeployError(400, "source must be a real directory")
    if not (source / SMOKE_ENTRY).is_file():
        raise DeployError(400, f"source does not look like harness-ops ({SMOKE_ENTRY} missing)")
    return source


def smoke(source):
    import py_compile
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory(prefix="overlay-smoke-") as folder:
        target = os.path.join(folder, "smoke.pyc")
        for path in sorted((source / "node").glob("*.py")):
            try:
                py_compile.compile(str(path), cfile=target, doraise=True)
            except py_compile.PyCompileError as error:
                failures.append(str(error))
    if failures:
        raise DeployError(400, "smoke compile failed:\n" + "\n".join(failures[:5]))


def admission(args):
    """Cross-participant coordination: refuse to switch while jobs are running
    or another intake holder is mid-launch, unless the administrator forces it."""
    if not args.jobs_root:
        return {"kind": "administrator_asserted_quiet", "managed_jobs_checked": False}
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    from node_update import locked
    import sqlite3
    with locked(jobs_root / ".intake.lock", existing=True, wait_seconds=5):
        db = sqlite3.connect((jobs_root / ".queue.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT job_id FROM jobs WHERE state IN ('claimed','preparing','blocked')").fetchall()
        finally:
            db.close()
    if rows and not args.force:
        raise DeployError(409, f"{len(rows)} running/preparing/blocked job(s) prevent deployment; use --force to override")
    return {"kind": "node_intake", "jobs_root": str(jobs_root),
            "running": len(rows), "forced": bool(args.force)}


def cmd_activate(args):
    root = Path(args.root).expanduser().resolve()
    source = source_tree(Path(args.source).expanduser().resolve())
    smoke(source)
    (root / "releases").mkdir(parents=True, exist_ok=True)
    (root / "deploy-state").mkdir(parents=True, exist_ok=True)
    if args.check_only:
        digest, files = tree_digest(source)
        return {"schema": SCHEMA, "status": "checked", "digest": digest, "files": files}
    admission_fact = admission(args)
    ensure_layout(root)
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    release = root / "releases" / run_id
    release.mkdir()
    shutil.copytree(source, release / COMPONENT, symlinks=True)
    digest, files = tree_digest(release / COMPONENT)
    receipt = {"schema": SCHEMA, "run_id": run_id, "created_at": time.time(),
               "status": "staged", "source": str(source), "digest": digest, "files": files,
               "components": [COMPONENT], "previous": current_run(root),
               "admission": admission_fact}
    run_dir = root / "deploy-state" / run_id
    run_dir.mkdir()
    record(run_dir, receipt, "staged")
    switch(root, run_id)
    record(run_dir, receipt, "active")
    # Keep the last N releases; never delete the one the cursor points at.
    prune(root, keep=args.keep)
    return {"schema": SCHEMA, "run_id": run_id, "status": "active", "digest": digest,
            "files": files, "previous": receipt["previous"], "admission": admission_fact}


def prune(root, keep=5):
    releases = sorted((root / "releases").iterdir(), key=lambda p: p.name)
    active = current_run(root)
    for release in releases[:-keep] if len(releases) > keep else []:
        if release.name == active:
            continue
        shutil.rmtree(release, ignore_errors=True)
        state = root / "deploy-state" / release.name
        if state.exists():
            record(state, {**load_receipt(state), "status": "pruned"}, "pruned")


def current_run(root):
    try:
        return os.readlink(cursor(root)).split("/")[1]
    except OSError:
        return None


def cmd_status(args):
    root = Path(args.root).expanduser().resolve()
    active = current_run(root)
    out = {"schema": SCHEMA, "root": str(root), "active": active,
           "cursor_exists": cursor(root).is_symlink()}
    if active:
        receipt = load_receipt(root / "deploy-state" / active)
        out["digest"] = receipt.get("digest")
        out["files"] = receipt.get("files")
        out["created_at"] = receipt.get("created_at")
    releases = sorted(p.name for p in (root / "releases").iterdir()) if (root / "releases").exists() else []
    out["releases"] = releases
    return out


def cmd_rollback(args):
    root = Path(args.root).expanduser().resolve()
    active = current_run(root)
    if not active:
        raise DeployError(409, "no active deployment to roll back from")
    if args.run:
        target = args.run
    else:
        receipt = load_receipt(root / "deploy-state" / active)
        target = receipt.get("previous")
    if not target or not (root / "releases" / target / COMPONENT).is_dir():
        raise DeployError(404, "rollback target release does not exist")
    if target == active:
        raise DeployError(409, "rollback target equals the active release")
    switch(root, target)
    receipt = load_receipt(root / "deploy-state" / target)
    record(root / "deploy-state" / target, receipt, "active")
    return {"schema": SCHEMA, "rolled_back_to": target, "previous": active,
            "digest": receipt.get("digest")}


def parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("activate", "rollback", "status"):
        one = sub.add_parser(name)
        one.add_argument("--root", required=True, help="installation root, e.g. ~/node")
        if name == "activate":
            one.add_argument("--source", required=True, help="staged harness-ops tree to deploy")
            one.add_argument("--check-only", action="store_true", help="validate and digest without switching")
            one.add_argument("--keep", type=int, default=5, help="releases to retain (default 5)")
            one.add_argument("--jobs-root", help="node jobs root; refuse switching while jobs run")
            one.add_argument("--force", action="store_true", help="switch despite running jobs (administrator override)")
        if name == "rollback":
            one.add_argument("--run", help="specific release to roll back to (default: previous)")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = {"activate": cmd_activate, "status": cmd_status, "rollback": cmd_rollback}[args.command](args)
    except DeployError as error:
        print(json.dumps({"schema": SCHEMA, "error": {"status": error.status, "message": str(error)}}),
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(json.dumps({"schema": SCHEMA, "error": {"status": 500, "message": str(error)}}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
