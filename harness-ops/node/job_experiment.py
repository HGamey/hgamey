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
import subprocess
import sys

DEFAULT_URL = "https://github.com/HGamey/experiments.git"
COMMIT = re.compile(r"[0-9a-f]{40}")
ENTRY = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/)*[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:py|sh)")
CHECKOUT = "experiment"


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


def materialize(mirror, url, commit, dest, timeout=300):
    """Unpack `commit` of the experiments repository into `dest`, fetching it into `mirror` if needed."""
    os.makedirs(os.path.dirname(mirror), exist_ok=True)
    with open(mirror + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two jobs preparing at once share one mirror
        if not os.path.isdir(mirror):
            _git("init", "--bare", "-q", mirror)
        if _git("-C", mirror, "cat-file", "-e", commit + "^{commit}", check=False).returncode:
            _git("-C", mirror, "fetch", "-q", "--no-tags", "--depth=1", url, commit, timeout=timeout)
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
