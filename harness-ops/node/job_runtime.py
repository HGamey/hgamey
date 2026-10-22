"""Persistent job worker. Only this process signals the child it actually spawned."""
import argparse
import fcntl
import os
import signal
import stat
import subprocess
import sys
import time
import traceback

from artifact_io import SafeRoot


def _open_file(store, rel, flags):
    parent, name = store._parent(rel)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("job runtime files must be regular files")
    return fd


def _worker_command(job_dir, lock_fd):
    return [sys.executable, os.path.abspath(__file__), "--job-dir", job_dir,
            "--lock-fd", str(lock_fd)]


def launch(store, jid, job_dir, command, cwd, env):
    """Hand an already-held flock to a detached worker; never unlock its shared fd."""
    job_dir = os.path.abspath(job_dir)
    if os.path.basename(job_dir) != jid:
        raise ValueError("job directory does not match its ID")
    lock_fd = _open_file(store, jid + "/runner.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("job already has a live worker") from None
        if not store.publish_json_once(jid + "/launch.json", {"command": command, "cwd": os.fspath(cwd)}):
            raise ValueError("job was already launched; create a new job instead of replaying it")
        log_fd = _open_file(store, jid + "/log", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.close(log_fd)  # Make the raw log readable before the worker starts.
        diag_fd = _open_file(store, jid + "/runner.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            return subprocess.Popen(_worker_command(job_dir, lock_fd), env=env,
                                    stdin=subprocess.DEVNULL, stdout=diag_fd, stderr=subprocess.STDOUT,
                                    pass_fds=(lock_fd,), start_new_session=True)
        finally:
            os.close(diag_fd)
    finally:
        # flock belongs to the shared open file description passed to the worker.
        # LOCK_UN here would release the worker's lock as well.
        os.close(lock_fd)


def runner_alive(store, jid):
    """A held worker lock is authoritative; a saved PID is only diagnostic."""
    try:
        fd = store.open_fd(jid + "/runner.lock", directory=False)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def read_cancel(store, jid):
    """Read durable intent without changing it; escalation is monotonic."""
    try:
        request = store.read_json(jid + "/cancel.json")
    except FileNotFoundError:
        return None
    try:
        store.read_json(jid + "/cancel.force.json")
    except FileNotFoundError:
        return request
    return {**request, "mode": "escalate"}


def _cancellation(request, meta):
    prior = meta.get("cancel") or {}
    signals = prior.get("signals", [])
    status = "signaled" if signals else "not_applied" if meta.get("state") == "finished" else "requested"
    return {**request, "status": status, "signals": signals}


def request_cancel(store, jid, force=False):
    """Publish idempotent intent; services never signal a PID read from job files."""
    meta = store.read_json(jid + "/meta.json")
    store.publish_json_once(jid + "/cancel.json", {"mode": "graceful", "requested_at": time.time()})
    if force:
        store.publish_json_once(jid + "/cancel.force.json", {"requested_at": time.time()})
    return _cancellation(read_cancel(store, jid), meta)


def _finish(store, jid, meta, outcome, recovery, returncode=None, error=None):
    meta.update(state="finished", phase="finished", outcome=outcome, recovery=recovery, ended=time.time(),
                exit=None if returncode is None else returncode if returncode >= 0 else 128 - returncode)
    meta["execution"]["returncode"] = returncode
    if error:
        meta["error"] = error
    if returncode is not None:
        # JSON integers are also the legacy plain-integer exitcode format.
        store.write_json(jid + "/exitcode", meta["exit"])
    if "cancel" in meta and not meta["cancel"]["signals"]:
        meta["cancel"]["status"] = "not_applied"
    store.write_json(jid + "/meta.json", meta)


def _worker(job_dir, lock_fd, escalation_delays=(15.0, 15.0)):
    """Delay injection is for direct unit calls, never exposed in CLI or environment."""
    os.set_inheritable(lock_fd, False)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = SafeRoot(os.path.dirname(job_dir))
    jid = os.path.basename(job_dir)
    try:
        # Do not carry an HTTP launcher's ignored signal dispositions into the child.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        meta = store.read_json(jid + "/meta.json")
        meta.update(pid=os.getpid(), state="running", phase="starting")
        meta["execution"].update(child_pid=None, returncode=None)
        store.write_json(jid + "/meta.json", meta)
        request = read_cancel(store, jid)
        if request:
            meta["cancel"] = _cancellation(request, meta)
            _finish(store, jid, meta, "cancelled", {"status": "not_needed", "reason": "cancelled before command started"})
            return 0
        spec = store.read_json(jid + "/launch.json")
        log_fd = _open_file(store, jid + "/log", os.O_WRONLY | os.O_APPEND)
        try:
            try:
                child = subprocess.Popen(spec["command"], cwd=spec["cwd"],
                                         stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                                         start_new_session=True, close_fds=True)
            except (OSError, ValueError) as e:
                print(f"child could not start: {type(e).__name__}: {e}", flush=True)
                _finish(store, jid, meta, "failed", {"status": "not_needed", "reason": "command did not start"},
                        error=f"{type(e).__name__}: {e}")
                return 1
        finally:
            os.close(log_fd)
        meta["phase"] = "running"
        meta["execution"]["child_pid"] = child.pid
        store.write_json(jid + "/meta.json", meta)
        last_signal_at = None
        while True:
            request = read_cancel(store, jid)
            if request:
                cancellation = _cancellation(request, meta)
                if meta.get("cancel") != cancellation:
                    meta["cancel"] = cancellation
                    meta["phase"] = "cancelling"
                    store.write_json(jid + "/meta.json", meta)
            returncode = child.poll()
            if returncode is not None:
                signals = (meta.get("cancel") or {}).get("signals", [])
                outcome = "cancelled" if signals else "succeeded" if returncode == 0 else "failed"
                escalated = any(s["name"] in ("SIGTERM", "SIGKILL") for s in signals)
                recovery = {"status": "required" if escalated else "not_reported",
                            "reason": "command received TERM/KILL" if escalated else "command did not report device recovery"}
                _finish(store, jid, meta, outcome, recovery, returncode)
                return 0
            if request:
                signals = meta["cancel"]["signals"]
                sig = None
                if not signals:
                    sig = signal.SIGINT
                elif request["mode"] == "escalate":
                    delay = escalation_delays[0] if len(signals) == 1 else escalation_delays[1]
                    if len(signals) < 3 and time.monotonic() - last_signal_at >= delay:
                        sig = signal.SIGTERM if len(signals) == 1 else signal.SIGKILL
                if sig is not None:
                    try:
                        # child is our live Popen, with its own session/process group.
                        os.killpg(child.pid, sig)
                    except ProcessLookupError:
                        continue  # Child exited between poll and signal; reap it next.
                    last_signal_at = time.monotonic()
                    signals.append({"name": sig.name, "at": time.time()})
                    meta["cancel"]["status"] = "signaled"
                    store.write_json(jid + "/meta.json", meta)
            time.sleep(0.05)
    finally:
        store.close()
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    args = parser.parse_args()
    try:
        return _worker(args.job_dir, args.lock_fd)
    except BaseException:
        # No fabricated result or replay: the service detects a lost flock and
        # reports interrupted/recovery required. Diagnostics never enter child log.
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
