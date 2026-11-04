"""Experiments live outside the platform repository (HGamey/experiments by default).

A job with bin `experiment` runs one entry script of that repository at a pinned commit. The node
fetches the commit into a local mirror and unpacks it into the job's input directory, so every run
names the exact code that produced it. Experiments find the node's programs through PHONEFARM_BIN,
GAME_OPT_LOOP_BIN and PF_ROOT, and prefix their own device-lock labels with DEVLOCK_LABEL so the
dashboard can tie a held phone back to the job.
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys

DEFAULT_URL = "https://github.com/HGamey/experiments.git"
COMMIT = re.compile(r"[0-9a-f]{40}")
ENTRY = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh)")
CHECKOUT = "experiment"
# Branches and tags of the experiments repository, kept under their own namespace in the mirror.
REFS = "refs/experiments/"
REFSPECS = ("+refs/heads/*:" + REFS + "heads/*", "+refs/tags/*:" + REFS + "tags/*")


def validate(spec):
    """Check the experiment part of a job spec and return the normalized `experiment` field."""
    value = spec.get("experiment")
    if (not isinstance(value, dict) or set(value) != {"commit"} or not isinstance(value["commit"], str)
            or not COMMIT.fullmatch(value["commit"])):
        raise ValueError("experiment jobs need experiment.commit: the full 40-character commit to run")
    argv = spec.get("argv") or []
    if not argv or not ENTRY.fullmatch(argv[0]):
        raise ValueError("experiment argv[0] must be a .py or .sh path inside the experiments repository")
    if spec.get("exe") or "inputs" in spec:
        raise ValueError("experiment jobs take their code from the pinned commit; exe and inputs do not apply")
    return {"commit": value["commit"]}


def command(checkout, argv):
    """Interpreter and arguments for an entry script unpacked under `checkout`."""
    entry = os.path.join(checkout, argv[0])
    interpreter = sys.executable if entry.endswith(".py") else "/bin/bash"
    return [interpreter, entry, *argv[1:]]


def fetch_refs(mirror, url, timeout=300):
    """Fetch every branch and tag of `url` into the bare repository `mirror`, pruning deleted ones.

    Only history reachable from those refs arrives. The node never fetches a bare commit id: GitHub serves
    any commit of the fork network through the parent's URL, which would skip the repository's branch rules.
    """
    if os.path.isdir(mirror) and _git("-C", mirror, "rev-parse", "--is-shallow-repository",
                                      check=False).stdout.strip() == "true":
        shutil.rmtree(mirror)  # older nodes fetched single commits with --depth=1; such a cache cannot answer reachability
    if not os.path.isdir(mirror):
        _git("init", "--bare", "-q", mirror)
    _git("-C", mirror, "fetch", "-q", "--prune", "--no-tags", url, *REFSPECS, timeout=timeout)


def containing_refs(mirror, commit):
    """Branches and tags of the mirror whose history contains `commit`; empty when the mirror lacks it."""
    done = _git("-C", mirror, "for-each-ref", "--format=%(refname)", "--contains", commit, REFS, check=False)
    return done.stdout.split() if not done.returncode else []


def unreachable(url, commit):
    return (f"实验提交 {commit} 不在实验仓库 {url} 的任何分支或标签上，拒绝运行。"
            f"只能运行实验仓库分支或标签里的提交：先推到实验仓库的分支（外部账号用 ext/<团队>/<主题>），再按分支名或提交号提交")


def materialize(mirror, url, commit, dest, timeout=300):
    """Unpack `commit` of the experiments repository into `dest` after checking a branch or tag reaches it."""
    os.makedirs(os.path.dirname(mirror), exist_ok=True)
    with open(mirror + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two jobs preparing at once share one mirror
        try:
            fetch_refs(mirror, url, timeout=timeout)
        except ValueError as error:  # offline: refs fetched earlier still decide, they came from the same URL
            if not os.path.isdir(mirror) or not containing_refs(mirror, commit):
                raise ValueError(f"{error}；{unreachable(url, commit)}") from None
        if not containing_refs(mirror, commit):
            raise ValueError(unreachable(url, commit))
        os.makedirs(dest)
        archive = subprocess.Popen(["git", "-C", mirror, "archive", "--format=tar", commit],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        untar = subprocess.run(["tar", "-x", "-f", "-", "-C", dest], stdin=archive.stdout, capture_output=True)
        archive.stdout.close()
        with archive.stderr:
            problem = archive.stderr.read()
        if archive.wait() or untar.returncode:
            raise ValueError(f"experiment commit {commit} could not be unpacked: "
                             f"{(problem or untar.stderr).decode(errors='replace').strip()[-300:]}")


def _git(*args, check=True, timeout=60):
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:  # the launcher reports ValueError as a failed preparation
        raise ValueError(f"experiment repository: git {args[0] if args[0] != '-C' else args[2]} timed out") from None
    if check and done.returncode:
        raise ValueError(f"experiment repository: git {' '.join(a for a in args if not a.startswith('/'))[:80]} "
                         f"failed: {done.stderr.strip()[-300:]}")
    return done
