#!/usr/bin/env python3
"""Deploy one commit of this repository to a node: build, publish, restart, check.

    python3 deploy.py rhodes1                 # origin/main
    python3 deploy.py rhodes1 --ref <commit>  # any commit; deploying the previous commit is the rollback
    python3 deploy.py rhodes1 --status        # installed commit and what the running nodes report

One commit is one version. phonefarm and game_opt_loop are built from the commit with its SHA stamped in
(`runtime-info`). harness-ops is published as a complete release behind the <root>/harness-ops cursor
(harness-ops/node/deploy_overlay.py) and declares the same SHA in node/runtime-build.json. The services are
stopped while files change and started again; the deploy succeeds only when every configured node reports
that SHA for the service, phonefarm and game_opt_loop.

Node data is never written: _jobs, _research, _node, tasks/ and the other RESERVED names stay put, and files
that exist only in the installation are left alone. Deployment refuses while a configured jobs root holds a
claimed, preparing or blocked job (--force overrides). Per-node settings live in deploy/<node>.json: paths
and service commands only, no secrets.
"""
import argparse
import fcntl
import filecmp
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCHEMA, CONFIG_SCHEMA = "hgamey/deploy@1", "hgamey/deploy-node@1"
# Same rule as harness-ops/node/node_update.py: these path parts hold execution data, never code.
RESERVED = {".git", "_jobs", "jobs", "runs", "lease-receipts", "secrets", ".updates", ".target", "__pycache__", "tasks"}
# (component directory, crate directory, executable)
PROGRAMS = (("phonefarm", "phonefarm/src", "phonefarm"), ("game_opt_loop", "game_opt_loop", "game_opt_loop"))
GRAY_LAYER = ("libVkLayer_refknobs.so", "VkLayer_refknobs.json")
BUSY = ("claimed", "preparing", "blocked")
KEEP = 5


class DeployError(RuntimeError):
    pass


def say(message):
    print("deploy: " + message, file=sys.stderr, flush=True)


def path(value):
    return Path(os.path.expanduser(value))


def run(argv, **kwargs):
    say("$ " + " ".join(map(str, argv)))
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def load_config(name):
    file = REPO / "deploy" / (name + ".json")
    try:
        config = json.loads(file.read_text())
    except (OSError, ValueError) as error:
        raise DeployError(f"cannot read {file}: {error}")
    required = {"schema", "root", "work", "jobs_roots", "stop", "start", "nodes", "gray_layer"}
    if config.get("schema") != CONFIG_SCHEMA or not required <= set(config):
        raise DeployError(f"{file} needs schema {CONFIG_SCHEMA} and keys {sorted(required)}")
    return config


def reports(config, wait):
    """What each configured node says it runs, read from GET /node."""
    found = []
    for node in config["nodes"]:
        token = path(node["token_file"]).read_text().strip()
        request = urllib.request.Request(node["url"].rstrip("/") + "/node", headers={"Authorization": "Bearer " + token})
        deadline = time.monotonic() + wait
        while True:
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    value = json.load(response)
                break
            except (OSError, ValueError) as error:
                if time.monotonic() >= deadline:
                    value, failure = None, str(error)
                    break
                time.sleep(2)
        if value is None:
            found.append({"url": node["url"], "error": failure})
            continue
        programs = (value.get("execution") or {}).get("programs") or {}

        def stamped(name):
            return (((programs.get(name) or {}).get("identity") or {}).get("build") or {}).get("source_revision")

        found.append({"url": node["url"], "node_id": value.get("node_id"),
                      "service": (value.get("service") or {}).get("revision"),
                      "phonefarm": stamped("phonefarm"), "game_opt_loop": stamped("game_opt_loop")})
    return found


def busy(config):
    found = []
    for root in config["jobs_roots"]:
        database = path(root) / ".queue.sqlite3"
        if not database.exists():
            continue
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT job_id, state FROM jobs WHERE state IN (?, ?, ?)", BUSY).fetchall()
        finally:
            connection.close()
        found += [{"jobs_root": root, "job_id": job, "state": state} for job, state in rows]
    return found


def extract(sha, destination):
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(REPO), "archive", "--format=tar", sha], stdout=subprocess.PIPE)
    # -m gives fresh mtimes, so cargo never takes an older commit's file for one it already built.
    unpack = subprocess.run(["tar", "-x", "-m", "-f", "-", "-C", str(destination)], stdin=archive.stdout)
    archive.stdout.close()
    if archive.wait() or unpack.returncode:
        raise DeployError("could not extract " + sha)


def identity(executable, name):
    out = subprocess.run([str(executable), "runtime-info", "--json"], cwd=executable.parent,
                         capture_output=True, timeout=10, check=True).stdout
    value = json.loads(out)
    if value.get("program") != name:
        raise DeployError(f"{executable} reports program {value.get('program')!r}, expected {name}")
    return value.get("build") or {}


def build(sha, source, work, config):
    builds = work / "builds"
    result = builds / sha
    wanted = [executable for _, _, executable in PROGRAMS] + (list(GRAY_LAYER) if config["gray_layer"] else [])
    if all((result / name).is_file() for name in wanted):
        say("reusing the build of " + sha[:12])
        return result
    partial = builds / (sha + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    env = dict(os.environ, HGAMEY_BUILD_REVISION=sha, HGAMEY_BUILD_DIRTY="false")
    env["PATH"] = os.pathsep.join([os.path.expanduser(p) for p in config.get("path_prepend", [])] + [env.get("PATH", os.defpath)])
    env.update({key: os.path.expanduser(value) for key, value in config.get("build_env", {}).items()})
    for name, crate, executable in PROGRAMS:
        target = work / "target" / name
        run(["cargo", "build", "--release", "--locked", "-q"], cwd=source / crate, env=dict(env, CARGO_TARGET_DIR=str(target)))
        shutil.copy2(target / "release" / executable, partial / executable)
        if sys.platform == "darwin":
            run(["codesign", "--force", "--sign", "-", partial / executable])
        stamped = identity(partial / executable, name)
        if stamped.get("source_revision") != sha or stamped.get("source_dirty") is not False:
            raise DeployError(f"{name} build reports {stamped}, expected a clean {sha}")
    if config["gray_layer"]:
        layer = source / "phonefarm" / "knobs" / "gray" / "build"
        run([layer / "build_layer.sh"], cwd=source / "phonefarm", env=env)
        for name in GRAY_LAYER:
            shutil.copy2(layer / "out" / name, partial / name)
    if result.exists():
        shutil.rmtree(result)
    os.replace(partial, result)
    return result


class Publication:
    """Replaces installation files one by one, keeping what it overwrote so a failure can put it back."""

    def __init__(self, backup):
        self.backup, self.replaced, self.created = backup, [], []

    def put(self, source, target, mode):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise DeployError(f"{target} is not an ordinary file; not replacing it")
        if target.is_file() and target.stat().st_mode & 0o777 == mode and filecmp.cmp(source, target, shallow=False):
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            saved = self.backup / str(len(self.replaced))
            shutil.copy2(target, saved)
            self.replaced.append((target, saved))
        else:
            self.created.append(target)
        temporary = target.with_name("." + target.name + ".deploy")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        return True

    def component(self, source, target):
        changed = 0
        for base, directories, files in os.walk(source):
            directories[:] = sorted(d for d in directories if d not in RESERVED)
            for name in sorted(set(files) - RESERVED):
                file = Path(base) / name
                if file.is_symlink():
                    raise DeployError(f"{file} is a symlink; deploy publishes ordinary files only")
                mode = 0o755 if file.stat().st_mode & 0o111 else 0o644
                changed += self.put(file, target / file.relative_to(source), mode)
        return changed

    def undo(self):
        for target, saved in reversed(self.replaced):
            temporary = target.with_name("." + target.name + ".deploy")
            shutil.copy2(saved, temporary)
            os.replace(temporary, target)
        for target in reversed(self.created):
            if target.exists():
                target.unlink()

    def save(self):
        (self.backup / "manifest.json").write_text(json.dumps(
            {"replaced": [[str(t), str(s)] for t, s in self.replaced], "created": [str(t) for t in self.created]}, indent=1))


def prune(directory, keep, protect):
    entries = sorted((p for p in directory.iterdir() if p.name != protect), key=lambda p: p.stat().st_mtime)
    for old in entries[:-keep] if len(entries) > keep else []:
        shutil.rmtree(old, ignore_errors=True)


def deploy(name, config, ref, fetch, force):
    root, work = path(config["root"]), path(config["work"])
    work.mkdir(parents=True, exist_ok=True)
    with open(work / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("another deploy is running on this node")
        if fetch:
            run(["git", "-C", REPO, "fetch", "-q", "origin"])
        sha = git("rev-parse", "--verify", ref + "^{commit}")
        started = time.time()
        say(f"deploying {sha[:12]} ({ref}) to {name}")
        before = reports(config, wait=0)
        source = work / "src"
        extract(sha, source)
        built = build(sha, source, work, config)
        jobs = busy(config)
        if jobs and not force:
            raise DeployError(f"jobs are running or blocked, deploy refused (--force overrides): {jobs}")
        for argv in config["stop"]:
            run(argv)
        jobs = busy(config)  # nothing can start a job now; this is the check that counts
        if jobs and not force:
            for argv in config["start"]:
                run(argv)
            raise DeployError(f"jobs are running or blocked, deploy refused (--force overrides): {jobs}")
        backup = work / "backups" / time.strftime("%Y%m%dT%H%M%S")
        backup.mkdir(parents=True)
        publication, changed = Publication(backup), {}
        cursor = root / "harness-ops"
        release_before = os.readlink(cursor).split("/")[1] if cursor.is_symlink() else None
        try:
            for component, _, executable in PROGRAMS:
                changed[component] = publication.component(source / component, root / component)
                changed[component] += publication.put(built / executable, root / component / executable, 0o755)
            if config["gray_layer"]:
                for file in GRAY_LAYER:
                    changed["phonefarm"] += publication.put(built / file, root / "phonefarm/knobs/gray/build/out" / file, 0o644)
            (source / "harness-ops/node/runtime-build.json").write_text(json.dumps(
                {"schema": "hgamey/source-build@1", "repository": "HGamey/hgamey", "revision": sha, "dirty": False}) + "\n")
            overlay = json.loads(subprocess.run(
                [sys.executable, str(source / "harness-ops/node/deploy_overlay.py"), "activate", "--root", str(root),
                 "--source", str(source / "harness-ops"), "--keep", "10"],
                check=True, capture_output=True, text=True).stdout)
        except BaseException:
            say("publication failed; putting the previous files back")
            publication.undo()
            if release_before and os.readlink(cursor).split("/")[1] != release_before:
                run([sys.executable, source / "harness-ops/node/deploy_overlay.py", "rollback", "--root", root, "--run", release_before])
            for argv in config["start"]:
                run(argv)
            raise
        finally:
            publication.save()
        for argv in config["start"]:
            run(argv)
        after = reports(config, wait=120)
        ok = all(report.get(key) == sha for report in after for key in ("service", "phonefarm", "game_opt_loop"))
        receipt = {"schema": SCHEMA, "node": name, "commit": sha, "ref": ref, "status": "succeeded" if ok else "check_failed",
                   "started_at": started, "finished_at": time.time(), "harness_release": overlay["run_id"],
                   "files_changed": changed, "backup": str(backup), "forced_over": jobs, "before": before, "after": after}
        (root / "deploy-state" / overlay["run_id"] / "deploy.json").write_text(json.dumps(receipt, indent=1) + "\n")
        prune(work / "builds", KEEP, sha)
        prune(work / "backups", KEEP, backup.name)
        print(json.dumps(receipt, indent=1))
        if not ok:
            say("a node does not report the deployed commit; see 'after' above. To go back: "
                f"python3 deploy.py {name} --ref <previous commit>")
        return 0 if ok else 1


def status(name, config):
    root = path(config["root"])
    cursor = root / "harness-ops"
    release = os.readlink(cursor).split("/")[1] if cursor.is_symlink() else None
    receipt = root / "deploy-state" / release / "deploy.json" if release else None
    installed = json.loads(receipt.read_text()) if receipt and receipt.is_file() else None
    print(json.dumps({"node": name, "harness_release": release,
                      "installed": installed and {k: installed[k] for k in ("commit", "status", "finished_at")},
                      "running": reports(config, wait=0)}, indent=1))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("node", help="node name; settings come from deploy/<node>.json")
    parser.add_argument("--ref", default="origin/main", help="commit, branch or tag to deploy (default origin/main)")
    parser.add_argument("--no-fetch", action="store_true", help="do not git fetch origin first")
    parser.add_argument("--force", action="store_true", help="deploy even while jobs are running or blocked")
    parser.add_argument("--status", action="store_true", help="show the installed commit and what the nodes report")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.node)
        if args.status:
            return status(args.node, config)
        return deploy(args.node, config, args.ref, not args.no_fetch, args.force)
    except (DeployError, OSError, ValueError, subprocess.CalledProcessError, sqlite3.Error) as error:
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            say(error.stderr if isinstance(error.stderr, str) else error.stderr.decode(errors="replace"))
        say(f"failed: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
