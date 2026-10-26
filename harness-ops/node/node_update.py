#!/usr/bin/env python3
"""Stage node code, then publish a reversible file overlay under the node intake lock.

This is not a multi-repository transaction, a process restarter, or a device lock.
Unmanaged installation data and administrator-installed privileged workers stay put.
"""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid


SCHEMA = "hgamey/node-update@1"
KEYS = {"phonefarm", "game_opt_loop", "harness_ops", "state_dir"}
COMPONENTS = {"phonefarm": "phonefarm", "game_opt_loop": "game_opt_loop", "harness-ops": "harness_ops"}
MACHO = {bytes.fromhex(v) for v in ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}
# These names belong to execution/data, never to a code overlay.
RESERVED = {".git", "_jobs", "jobs", "runs", "lease-receipts", "secrets", ".updates", ".target", "__pycache__", "tasks"}


class UpdateError(ValueError):
    pass


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".update-record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, ensure_ascii=False, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def read(path, limit=32 << 20):
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise UpdateError("update record exceeds its limit")
    return json.loads(value)


def absolute(value, label):
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise UpdateError(label + " must be an absolute path")
    return Path(value).resolve()


def state_directory(layout_without_state):
    roots = {key: str(absolute(str(layout_without_state[key]), key)) for key in sorted(KEYS - {"state_dir"})}
    identity = hashlib.sha256(json.dumps(roots, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return Path(roots["phonefarm"]).parent / ".updates" / identity


def layout_for(args):
    declared = os.environ.get("HG_UPDATE_LAYOUT")
    managed = bool(os.environ.get("PF_JOB_ID"))
    if declared is not None:
        if args.install_root:
            raise UpdateError("configured installation paths cannot be overridden")
        try:
            result = json.loads(declared)
        except ValueError:
            raise UpdateError("HG_UPDATE_LAYOUT must contain JSON") from None
        if not isinstance(result, dict) or set(result) != KEYS:
            raise UpdateError("HG_UPDATE_LAYOUT requires exactly phonefarm/game_opt_loop/harness_ops/state_dir")
        result = {k: absolute(v, k) for k, v in result.items()}
    else:
        if managed:
            raise UpdateError("managed code updates require the service's configured installation paths")
        base = absolute(args.install_root, "--install-root") if args.install_root else Path.home() / "node"
        result = {"phonefarm": base / "phonefarm", "game_opt_loop": base / "game_opt_loop",
                  "harness_ops": base / "harness-ops"}
        result = {k: p.resolve() for k, p in result.items()}
        result["state_dir"] = state_directory(result)
    if result["state_dir"] != state_directory(result):
        raise UpdateError("state_dir must use the installation identity returned by state_directory")
    paths = list(result.values())
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise UpdateError("installation and updater state directories must be disjoint")
    return result


def jobs_root_for(args):
    configured = os.environ.get("HG_UPDATE_JOBS_ROOT")
    if os.environ.get("PF_JOB_ID") and (args.offline or args.jobs_root):
        raise UpdateError("managed updates cannot override the service execution guard")
    if configured and args.jobs_root:
        raise UpdateError("configured jobs root cannot be overridden")
    if args.offline:
        if configured:
            raise UpdateError("--offline cannot bypass a configured jobs root")
        return None
    return absolute(configured or args.jobs_root, "--jobs-root (or explicitly --offline)")


@contextmanager
def locked(path, *, existing=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if not existing:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UpdateError("update lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("another update or node intake operation is in progress; retry explicitly") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def publication_guard(jobs_root):
    if jobs_root is None:
        yield {"kind": "administrator_asserted_offline", "managed_jobs_checked": False,
               "all_participants_verified": False}
        return
    # Same stable intake lock used around every actual node worker launch. No
    # database writes or schema creation, and no PID-based guesses about old jobs.
    with locked(jobs_root / ".intake.lock", existing=True):
        jid = os.environ.get("PF_JOB_ID")
        db = sqlite3.connect((jobs_root / ".queue.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute("SELECT job_id,state,spec FROM jobs WHERE state IN ('claimed','blocked')").fetchall()
            if jid:
                own = [r for r in rows if r[0] == jid]
                if len(own) != 1 or own[0][1] != "claimed" or json.loads(own[0][2]).get("bin") != "update_node":
                    raise UpdateError("current updater is not the claimed update_node job in this queue")
            if any(r[0] != jid for r in rows):
                raise UpdateError("other active or blocked jobs prevent code publication")
            # Legacy on-disk running jobs are not represented by SQL ownership.
            known = {r[0] for r in db.execute("SELECT job_id FROM jobs")}
            for path in jobs_root.iterdir():
                if re.fullmatch(r"[0-9a-f]{12}", path.name) and path.name not in known:
                    raise UpdateError("untracked historical job requires administrator review before update")
        finally:
            db.close()
        yield {"kind": "node_intake", "jobs_root": str(jobs_root), "job_id": jid,
               "managed_jobs_checked": True, "all_participants_verified": False}


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UpdateError("code files must be ordinary non-hardlinked files: " + str(path))
    return info


def fingerprint(path):
    if not os.path.lexists(path):
        return None
    before = regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise UpdateError("code file changed while being inspected")
    return {"sha256": digest.hexdigest(), "size": after.st_size, "mode": stat.S_IMODE(after.st_mode)}


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or any(p in {".", "..", ""} for p in path.parts) or any(p in RESERVED for p in path.parts):
        raise UpdateError("non-code or unsafe publication path")
    return path


def destination(root, relative):
    relative = safe_relative(relative)
    cursor = root
    if os.path.lexists(cursor) and (cursor.is_symlink() or not cursor.is_dir()):
        raise UpdateError("installation root is not an ordinary directory")
    for part in relative.parts[:-1]:
        cursor /= part
        if os.path.lexists(cursor) and (cursor.is_symlink() or not cursor.is_dir()):
            raise UpdateError("installation path traverses a link or non-directory")
    return root / relative


def copy_file(source, target, *, mode=None):
    info = regular(source)
    durable_directory(target.parent, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=".node-update-", dir=target.parent)
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


def files_under(root):
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(directory) / name
            if path.is_symlink():
                raise UpdateError("code staging does not follow directory links")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root)
            safe_relative(str(relative))
            regular(path)
            result.append(relative)
    return sorted(result)


def verify_macho(path, *, sign=False):
    with path.open("rb") as stream:
        macho = stream.read(4) in MACHO
    if macho:
        if sign:
            subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True, stdout=2, stderr=2)
        subprocess.run(["codesign", "--verify", "--strict", str(path)], check=True, stdout=2, stderr=2)


def validate_stage(stage, name):
    files = files_under(stage)
    if not files:
        raise UpdateError("empty code component: " + name)
    for rel in files:
        path = stage / rel
        # Code staging never imports set-ID bits or world-writable modes.
        path.chmod(0o755 if regular(path).st_mode & 0o111 else 0o644)
        # This covers binaries carried inside either source/upload overlays.
        verify_macho(path, sign=True)
    return files


def import_upload(source, stage):
    if source.is_symlink() or not source.is_dir():
        raise UpdateError("uploaded component must be an ordinary directory")
    durable_directory(stage)
    for rel in files_under(source):
        copy_file(source / rel, stage / rel)


def durable_directory(path, mode=0o700):
    """Persist a new directory's parent entry before creating children in it.

    Also sync the immediate parent on reuse, including a retry after mkdir
    succeeded but its parent sync failed. We never chmod existing directories.
    """
    if not path.exists():
        durable_directory(path.parent, mode=mode)
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
    if path.is_symlink() or not path.is_dir():
        raise UpdateError("directory path must be an ordinary directory")
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


def load_receipt(path):
    value = read(path)
    states = {"preparing", "staged", "failed", "publishing", "succeeded", "partial", "rolling_back", "restored", "rollback_required"}
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("run_id") != path.parent.name or not re.fullmatch(r"[0-9a-f]{32}", path.parent.name)
            or not isinstance(value.get("status"), str) or value["status"] not in states
            or not isinstance(value.get("layout"), dict) or set(value["layout"]) != KEYS
            or not isinstance(value.get("files"), list) or not isinstance(value.get("components"), list)):
        raise UpdateError("invalid update receipt schema or structure")
    for key, target in value["layout"].items():
        absolute(target, key)
    if value["layout"]["state_dir"] != str(state_directory(value["layout"])):
        raise UpdateError("invalid update receipt installation identity")
    def valid_fingerprint(item):
        return (isinstance(item, dict) and set(item) == {"sha256", "size", "mode"}
                and isinstance(item["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                and type(item["size"]) is int and item["size"] >= 0
                and type(item["mode"]) is int and 0 <= item["mode"] <= 0o7777)
    seen = set()
    for item in value["files"]:
        if (not isinstance(item, dict) or set(item) != {"component", "path", "before", "after", "backup", "state"}
                or not isinstance(item["component"], str) or item["component"] not in KEYS - {"state_dir"} or not isinstance(item["path"], str)
                or not isinstance(item["state"], str) or item["state"] not in {"pending", "published", "restored"}
                or not valid_fingerprint(item["after"])
                or (item["before"] is not None and not valid_fingerprint(item["before"]))):
            raise UpdateError("invalid update file receipt")
        safe_relative(item["path"])
        key = item["component"], item["path"]
        if key in seen or (item["before"] is None and item["backup"] is not None) or (item["before"] is not None and
                (not isinstance(item["backup"], str) or not re.fullmatch(r"backups/[0-9]+", item["backup"]))):
            raise UpdateError("invalid update backup receipt")
        seen.add(key)
    for item in value["components"]:
        if (not isinstance(item, dict) or not isinstance(item.get("component"), str) or item.get("component") not in KEYS - {"state_dir"}
                or item.get("status") != "published" or type(item.get("file_count")) is not int or item["file_count"] < 1):
            raise UpdateError("invalid update component receipt")
    if value["status"] in {"preparing", "staged", "failed"} and (value["files"] or value["components"]):
        raise UpdateError("unfinished publication cannot be treated as an unstarted update")
    expected = "restored" if value["status"] == "restored" else "published"
    if value["status"] in {"succeeded", "partial", "restored"} and any(item["state"] != expected for item in value["files"]):
        raise UpdateError("terminal update receipt has unfinished files")
    return value


def maintenance_record(layout):
    if not isinstance(layout, dict) or set(layout) != KEYS:
        raise UpdateError("invalid installation layout")
    layout = {key: str(absolute(str(value), key)) for key, value in layout.items()}
    if layout["state_dir"] != str(state_directory(layout)):
        raise UpdateError("invalid installation state directory")
    path = Path(layout["state_dir"]) / "maintenance.json"
    try:
        regular(path)
        value = read(path, 65536)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or set(value) != {"schema", "run_id", "layout", "at"}
            or value.get("schema") != "hgamey/node-update-maintenance@1"
            or not isinstance(value.get("run_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            or value.get("layout") != {k: str(v) for k, v in layout.items()}):
        raise UpdateError("invalid or mismatched installation maintenance record")
    return value


def maintenance_problem(layout, spec):
    """Read-only dispatch gate. A corrupt marker grants no rollback exception."""
    try:
        marker = maintenance_record(layout)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {"kind": "node_update", "reason": "installation maintenance state is unavailable or invalid", "blocking_runs": []}
    if marker is None:
        return None
    if (isinstance(spec, dict) and spec.get("bin") == "update_node" and "exe" not in spec
            and spec.get("argv") == ["--rollback", marker["run_id"]]):
        return None
    return {"kind": "node_update", "reason": "installation update requires explicit rollback",
            "blocking_runs": [marker["run_id"]]}


def begin_maintenance(layout, run_id):
    previous = maintenance_record(layout)
    if previous is not None and previous["run_id"] != run_id:
        raise UpdateError("another unfinished update owns this installation")
    save(Path(layout["state_dir"]) / "maintenance.json", {"schema": "hgamey/node-update-maintenance@1",
        "run_id": run_id, "layout": {k: str(v) for k, v in layout.items()}, "at": time.time()})


def end_maintenance(layout, run_id):
    marker = maintenance_record(layout)
    if marker is None or marker["run_id"] != run_id:
        raise UpdateError("installation maintenance ownership changed")
    path = Path(layout["state_dir"]) / "maintenance.json"
    path.unlink()
    sync_dir(path.parent)


def unresolved(state, excluding=None):
    for item in sorted((state / "runs").iterdir()):
        if item.name == excluding:
            continue
        path = item / "receipt.json"
        try:
            receipt = load_receipt(path)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise UpdateError("unreadable or invalid previous update receipt requires administrator review: " + item.name) from None
        if receipt["status"] in {"publishing", "rollback_required", "rolling_back"}:
            raise UpdateError("unfinished publication requires explicit rollback: " + item.name)


def record(receipt_path, receipt, status=None):
    if status is not None:
        receipt["status"] = status
    receipt["updated_at"] = time.time()
    save(receipt_path, receipt)


def rollback_files(run, receipt, layout):
    entries = receipt["files"]
    # Preflight all touched paths before reverting any of them. A later edit is
    # not ours to overwrite, and backup corruption is not a rollback success.
    for item in entries:
        target = destination(layout[item["component"]], item["path"])
        current = fingerprint(target)
        if current not in (item["before"], item["after"]):
            raise UpdateError("rollback target changed since publication: " + item["path"])
        if item["before"] is not None and fingerprint(run / item["backup"]) != item["before"]:
            raise UpdateError("rollback backup is unavailable or changed")
        if item["before"] is not None:
            verify_macho(run / item["backup"])
    for item in reversed(entries):
        target = destination(layout[item["component"]], item["path"])
        if fingerprint(target) != item["before"]:
            if item["before"] is None:
                target.unlink()
                sync_dir(target.parent)
            else:
                copy_file(run / item["backup"], target)
        item["state"] = "restored"
        record(run / "receipt.json", receipt, "rolling_back")


def publish(run, receipt, layout, stages):
    receipt_path = run / "receipt.json"
    # Every component was staged and validated before any installation writes.
    for component, stage, files in stages:
        root = layout[component]
        for rel in files:
            target = destination(root, str(rel))
            before = fingerprint(target)
            after = fingerprint(stage / rel)
            if before == after:
                continue
            index = len(receipt["files"])
            backup = Path("backups") / str(index)
            if before is not None:
                copy_file(target, run / backup)
                if fingerprint(run / backup) != before:
                    raise UpdateError("installation changed during backup")
            item = {"component": component, "path": str(rel), "before": before, "after": after,
                    "backup": str(backup) if before is not None else None, "state": "pending"}
            receipt["files"].append(item)
            record(receipt_path, receipt, "publishing")
            copy_file(stage / rel, target)
            item["state"] = "published"
            record(receipt_path, receipt)
        receipt["components"].append({"component": component, "status": "published", "file_count": len(files)})
        record(receipt_path, receipt)
    record(receipt_path, receipt, "succeeded")


def result(run, receipt):
    # Full per-file backups stay in the private update state, not the job log.
    print(json.dumps({"schema": SCHEMA, "run_id": receipt["run_id"], "status": receipt["status"],
                      "receipt": str(run / "receipt.json"), "components": receipt.get("components", []),
                      "error": receipt.get("error"), "restart_required": receipt["status"] in {"succeeded", "partial"},
                      "atomic_scope": "individual_file", "data_migrated": False}, ensure_ascii=False), flush=True)


def parser():
    ap = argparse.ArgumentParser(description="Stage, validate and reversibly publish node code; no automatic service restart")
    ap.add_argument("--install-root", help="manual conventional sibling installation root (absolute)")
    ap.add_argument("--jobs-root", help="existing node jobs root to guard publication (absolute)")
    ap.add_argument("--offline", action="store_true", help="administrator asserts the installation is offline; not a participant audit")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--git", action="store_true", help="stage selected repositories before publication")
    modes.add_argument("--rollback", metavar="RUN_ID", help="restore a recorded update, refusing changed destinations")
    ap.add_argument("targets", nargs="*", help="repo[@ref] with --git, otherwise one uploaded directory")
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    run = receipt = None
    try:
        if args.rollback and args.targets:
            raise UpdateError("rollback does not accept update targets")
        upload = None
        if not args.git and not args.rollback:
            if len(args.targets) != 1:
                raise UpdateError("supply --git, --rollback RUN_ID, or one uploaded directory")
            upload = Path(args.targets[0]).resolve()
            selected = [(name, upload / name) for name in COMPONENTS if (upload / name).exists()]
            if not selected:
                # Existing credential-only workflow intentionally needs no code
                # maintenance gate and never acquires permission to replace code.
                install_secrets(upload)
                return 0
        layout = layout_for(args)
        jobs_root = jobs_root_for(args)
        state = layout["state_dir"]
        private_directory(state)
        durable_directory(state / "runs")
        with locked(state / ".update.lock"):
            if args.rollback:
                if not re.fullmatch(r"[0-9a-f]{32}", args.rollback):
                    raise UpdateError("invalid rollback run ID")
                run = state / "runs" / args.rollback
                receipt = load_receipt(run / "receipt.json")
                if receipt.get("schema") != SCHEMA or receipt.get("layout") != {k: str(v) for k, v in layout.items()}:
                    raise UpdateError("rollback receipt does not match the configured installation")
                if receipt.get("status") not in {"succeeded", "partial", "preparing", "staged", "failed", "publishing", "rollback_required", "rolling_back", "restored"}:
                    raise UpdateError("this update has no publication to roll back")
                unresolved(state, excluding=args.rollback)
                with publication_guard(jobs_root) as guard:
                    receipt["rollback_guard"] = guard
                    begin_maintenance(layout, run.name)
                    record(run / "receipt.json", receipt, "rolling_back")
                    try:
                        rollback_files(run, receipt, layout)
                        record(run / "receipt.json", receipt, "restored")
                    except BaseException:
                        record(run / "receipt.json", receipt, "rollback_required")
                        raise
                    end_maintenance(layout, run.name)
                result(run, receipt)
                return 0
            unresolved(state)
            if maintenance_record(layout) is not None:
                raise UpdateError("unfinished publication requires explicit rollback")
            run = state / "runs" / uuid.uuid4().hex
            durable_directory(run)
            receipt = {"schema": SCHEMA, "run_id": run.name, "status": "preparing", "started_at": time.time(),
                       "layout": {k: str(v) for k, v in layout.items()}, "files": [], "components": [],
                       "error": None, "sources": [], "atomic_scope": "individual_file", "preserves_unmanaged_files": True}
            record(run / "receipt.json", receipt)
            stages = []
            if args.git:
                selected = []
                for target in args.targets or ["phonefarm", "game_opt_loop"]:
                    name, _, ref = target.partition("@")
                    if name not in COMPONENTS or ("@" in target and not ref) or ref.startswith("-"):
                        raise UpdateError("invalid repository or revision")
                    if any(v[0] == name for v in selected):
                        raise UpdateError("duplicate update component")
                    selected.append((name, ref or "origin/main"))
            for name, source in selected:
                stage = run / ("stage-" + name)
                if args.git:
                    subprocess.run(["/bin/zsh", str(Path(__file__).with_name("update_node.sh")), "--prepare-internal",
                                    name, source, str(stage), str(state / "work")], check=True,
                                   env=dict(os.environ, HG_UPDATE_INTERNAL_STAGE=str(stage)), stdout=2, stderr=2)
                else:
                    import_upload(source, stage)
                receipt["sources"].append({"component": COMPONENTS[name],
                    "requested_ref": source if args.git else None,
                    "revision": (state / "work" / ("revision-" + name)).read_text().strip() if args.git else None,
                    "kind": "git" if args.git else "uploaded_overlay"})
                if name == "harness-ops" and not (stage / "runtime-build.json").exists():
                    # An undeclared upload must not retain the old build identity.
                    (stage / "runtime-build.json").write_text("null\n")
                stages.append((COMPONENTS[name], stage, validate_stage(stage, name)))
            record(run / "receipt.json", receipt, "staged")
            with publication_guard(jobs_root) as guard:
                receipt["guard"] = guard
                begin_maintenance(layout, run.name)
                try:
                    publish(run, receipt, layout, stages)
                except BaseException as error:
                    receipt["error"] = f"{type(error).__name__}: {error}"
                    try:
                        rollback_files(run, receipt, layout)
                        record(run / "receipt.json", receipt, "restored")
                    except BaseException as rollback_error:
                        receipt["rollback_error"] = f"{type(rollback_error).__name__}: {rollback_error}"
                        record(run / "receipt.json", receipt, "rollback_required")
                    else:
                        end_maintenance(layout, run.name)
                    raise
                else:
                    # A clear failure occurs after the complete publication is
                    # durable. Do not start a new mutation/rollback without a marker.
                    end_maintenance(layout, run.name)
            if upload is not None:
                try:
                    install_secrets(upload)
                except (OSError, ValueError):
                    record(run / "receipt.json", receipt, "partial")
                    raise
            result(run, receipt)
            return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError, sqlite3.Error, KeyboardInterrupt) as error:
        message = f"{type(error).__name__}: {error}"
        if receipt is not None and run is not None:
            receipt["error"] = message
            if receipt["status"] in {"preparing", "staged"}:
                receipt["status"] = "failed"
            elif receipt["status"] == "succeeded":
                receipt["status"] = "partial"
            try:
                record(run / "receipt.json", receipt)
                result(run, receipt)
            except (OSError, ValueError):
                print("update receipt could not be saved; inspect the original run " + str(run), file=sys.stderr)
        print("node-update: " + message, file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
