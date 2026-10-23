"""Opt-in, privileged Linux worker backend. Not a public node capability.

The administrator supplies an empty cgroup v2 root and a dedicated job UID/GID.
This manages local descendants, not access to remote daemons or device recovery.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from artifact_io import SafeRoot, open_child


CGROUP2_SUPER_MAGIC = 0x63677270
GATE = 'IFS= read -r gate && [ "$gate" = GO ] || exit 125; unset PWD OLDPWD SHLVL _; exec "$@" </dev/null'


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    return libc


def _cgroup_filesystem(fd, libc):
    # Linux statfs starts with f_type. Keep ample space for the ABI's remaining
    # fields, which this backend does not interpret.
    buf = (ctypes.c_long * 32)()
    if libc.fstatfs(fd, ctypes.byref(buf)) != 0:
        raise OSError(ctypes.get_errno(), "fstatfs")
    if buf[0] != CGROUP2_SUPER_MAGIC:
        raise ValueError("configured root is not a cgroup v2 filesystem")


def _root_owned(fd, label):
    st = os.fstat(fd)
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise ValueError(f"{label} must be root-owned and not group/other writable")


def _populated(fd):
    fields = dict(line.split() for line in os.pread(fd, 4096, 0).decode().splitlines())
    if fields.get("populated") not in ("0", "1"):
        raise ValueError("cgroup.events has no valid populated field")
    return fields["populated"] == "1"


class Domain:
    def __init__(self, config_path, jid):
        self.root = self.group = None
        self.job_fd = None
        self.events = self.procs = self.kill_fd = None
        self.name = "job-" + jid
        self.created = False
        self.unconfirmed_gate = False
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ValueError("contained worker requires Linux and a privileged worker identity")
        self.libc = _libc()  # Load before the single-threaded worker forks.
        with SafeRoot(Path(config_path).parent).open_file(Path(config_path).name) as f:
            _root_owned(f.fileno(), "contained configuration")
            config = json.load(f)
        self.uid, self.gid = config["uid"], config["gid"]
        if any(type(v) is not int or v <= 0 for v in (self.uid, self.gid)):
            raise ValueError("contained job UID/GID must be nonzero integers")
        root_path = Path(config["cgroup_root"])
        if not root_path.is_absolute():
            raise ValueError("cgroup_root must be absolute")
        self.root_path = root_path.resolve(strict=True)
        self.path = self.root_path / self.name
        try:
            self.root = SafeRoot(self.root_path)
            _cgroup_filesystem(self.root.fd, self.libc)
            _root_owned(self.root.fd, "cgroup root")
            # One configured identity/root is used serially. A prior populated
            # domain still blocks reuse even if its worker has already died.
            fcntl.flock(self.root.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.root.read_bytes("cgroup.type", 64).strip() != b"domain":
                raise ValueError("contained root must be a domain cgroup")
            with self.root.open_file("cgroup.events") as f:
                if _populated(f.fileno()):
                    raise ValueError("contained root still has members; recovery is required")
            if any(stat.S_ISDIR(os.stat(name, dir_fd=self.root.fd, follow_symlinks=False).st_mode)
                   for name in os.listdir(self.root.fd)):
                raise ValueError("prior contained domain remains; reconcile its records before identity reuse")
            os.mkdir(self.name, mode=0o755, dir_fd=self.root.fd)
            self.created = True
            self.group = self.root.open_fd(self.name, directory=True)
            self.events = open_child(self.group, "cgroup.events", directory=False)
            self.procs = os.open("cgroup.procs", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.group)
            self.kill_fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.group)
            # Check access using the *dropped* identity in preexec, including ACLs.
            # Ancestor migration entrypoints must be inaccessible to that identity.
            self.protected = []
            path = self.path
            while (path / "cgroup.procs").exists():
                self.protected += [str(path), str(path / "cgroup.procs"), str(path / "cgroup.threads")]
                path = path.parent
        except BaseException:
            self.close()
            raise

    def prepare_files(self, store, jid, job_dir):
        def readonly(fd):
            st = os.fstat(fd)
            _root_owned(fd, "contained input")
            if stat.S_ISDIR(st.st_mode):
                for name in os.listdir(fd):
                    child = open_child(fd, name)
                    try:
                        readonly(child)
                    finally:
                        os.close(child)
                os.fchmod(fd, 0o555)
            else:
                if st.st_nlink != 1:
                    raise ValueError("contained input cannot be a hard link")
                os.fchmod(fd, 0o555 if st.st_mode & 0o111 else 0o444)

        fd = store.open_fd(jid, directory=True)
        try:
            _root_owned(fd, "job directory")
            self.job_fd = os.dup(fd)
            for name in os.listdir(fd):
                if name in ("in", "out"):
                    continue
                child = open_child(fd, name, directory=False)
                try:
                    _root_owned(child, "worker record")
                    if os.fstat(child).st_nlink != 1:
                        raise ValueError("worker record cannot be a hard link")
                    os.fchmod(child, 0o600)
                finally:
                    os.close(child)
                self.protected.append(os.path.join(job_dir, name))
            inp = open_child(fd, "in", directory=True)
            try:
                readonly(inp)
            finally:
                os.close(inp)
            out = open_child(fd, "out", directory=True)
            try:
                _root_owned(out, "initial job output")
                if os.listdir(out):
                    raise ValueError("contained worker requires a new, empty output directory")
                os.fchown(out, self.uid, self.gid)
                os.fchmod(out, 0o700)
            finally:
                os.close(out)
            os.fchmod(fd, 0o711)
            self.protected.append(job_dir)
            # A writable non-sticky parent would let the job rename the whole
            # management directory. Do not repair private deployment ancestors.
            self.parents = list(Path(job_dir).parents)
        finally:
            os.close(fd)

    def _restrict(self):
        # Called only in this independent, single-threaded worker's child, after
        # Popen has applied user/group/extra_groups and before the fixed gate exec.
        if os.getuid() != self.uid or os.getgid() != self.gid or os.getgroups():
            raise ValueError("job identity was not dropped")
        if self.libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        if any(int(status[key].strip(), 16) for key in ("CapEff", "CapPrm", "CapAmb")):
            raise ValueError("job identity retains capabilities")
        if any(os.access(path, os.W_OK, effective_ids=True) for path in self.protected):
            raise ValueError("job identity can write worker records or cgroup migration entrypoints")
        if any(os.access(path, os.W_OK, effective_ids=True) and not path.stat().st_mode & stat.S_ISVTX
               for path in self.parents):
            raise ValueError("job identity can rename a management directory ancestor")
        if not os.access(".", os.X_OK, effective_ids=True):
            raise ValueError("job working directory is not accessible to its identity")

    def start_gate(self, command, cwd, job_dir, jid, log_fd):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": os.path.join(job_dir, "out"),
               "TMPDIR": os.path.join(job_dir, "out"), "PF_JOB_ID": jid,
               "PF_JOB_IN": os.path.join(job_dir, "in"), "PF_JOB_OUT": os.path.join(job_dir, "out")}
        child = subprocess.Popen(["/bin/sh", "-c", GATE, "phonefarm-gate", *command],
                                 cwd=cwd, env=env, stdin=subprocess.PIPE, bufsize=0,
                                 stdout=log_fd, stderr=subprocess.STDOUT, close_fds=True,
                                 start_new_session=True, user=self.uid, group=self.gid,
                                 extra_groups=[], preexec_fn=self._restrict, umask=0o077)
        self.unconfirmed_gate = True
        try:
            # Do not reap before attachment: even a gate that exited unexpectedly
            # cannot have its PID reused while we still own its wait obligation.
            os.write(self.procs, str(child.pid).encode())
            members = os.pread(self.procs, 1 << 16, 0).decode().split()
            if str(child.pid) not in members or child.poll() is not None:
                raise ValueError("startup gate did not remain in its owned cgroup")
            self.unconfirmed_gate = False
            return child
        except BaseException:
            child.stdin.close()  # EOF never permits execution.
            child.wait(timeout=5)
            self.unconfirmed_gate = False
            raise

    def observe(self):
        try:
            status, error = ("present" if _populated(self.events) else "absent"), None
        except (OSError, ValueError) as exc:
            status, error = "unknown", f"{type(exc).__name__}: {exc}"
        return {"scope": "owned_cgroup", "status": status, "path": str(self.path),
                "checked_at": time.time(), "error": error}

    def kill(self):
        os.lseek(self.kill_fd, 0, os.SEEK_SET)
        os.write(self.kill_fd, b"1")

    def seal_files(self):
        if self.job_fd is not None:
            # A serially reused UID must not retain access to previous evidence.
            # Leave producer file ownership intact; revoke traversal at the root.
            os.fchmod(self.job_fd, 0o700)
            os.fsync(self.job_fd)

    def close(self):
        empty = not self.unconfirmed_gate and self.events is not None and self.observe()["status"] == "absent"
        if empty:
            self.seal_files()
        for name in ("events", "procs", "kill_fd", "group"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        if self.root is not None:
            try:
                if self.created and empty:
                    os.rmdir(self.name, dir_fd=self.root.fd)
            finally:
                self.root.close()
                self.root = None
        if self.job_fd is not None:
            os.close(self.job_fd)
            self.job_fd = None


def run(store, jid, job_dir, meta, spec, config_path, escalation_delays):
    from job_runtime import _open_file, _cancellation, _finish, read_cancel

    domain, child, released = None, None, False
    try:
        try:
            domain = Domain(config_path, jid)
            domain.prepare_files(store, jid, job_dir)
            log_fd = _open_file(store, jid + "/log", os.O_WRONLY | os.O_APPEND)
            try:
                child = domain.start_gate(spec["command"], spec["cwd"], job_dir, jid, log_fd)
            finally:
                os.close(log_fd)
        except subprocess.TimeoutExpired:
            # The startup gate was not reaped. Let the worker-loss path retain
            # resources rather than publish an unproven not_needed recovery.
            raise
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            if domain is not None:
                domain.seal_files()
            _finish(store, jid, meta, "failed", {"status": "not_needed", "reason": "command did not start"},
                    error=f"contained startup failed: {type(error).__name__}: {error}")
            return 1
        meta["execution"].pop("owned_process_group", None)
        meta["execution"].update(child_pid=child.pid, process_management={
            "policy": "contained", "backend": "linux_cgroup_v2", "completion_scope": "owned_cgroup",
            "detached_descendants": "contained"}, owned_cgroup=domain.observe())
        # Publish actual membership before allowing the target to execute.
        store.write_json(jid + "/meta.json", meta)
        os.write(child.stdin.fileno(), b"GO\n")
        released = True
        child.stdin.close()
        meta["phase"] = "running"
        store.write_json(jid + "/meta.json", meta)
        last_signal_at = None
        while True:
            request = read_cancel(store, jid)
            if request:
                cancellation = _cancellation(request, meta)
                if meta.get("cancel") != cancellation:
                    meta["cancel"], meta["phase"] = cancellation, "cancelling"
                    store.write_json(jid + "/meta.json", meta)
            rc = child.poll()
            group = domain.observe()
            if group["status"] == "unknown":
                meta["execution"]["owned_cgroup"] = group
                _finish(store, jid, meta, "interrupted", {"status": "required", "reason": "owned cgroup status is unknown"}, rc)
                return 1
            if group["status"] == "absent" and rc is not None:
                signals = (meta.get("cancel") or {}).get("signals", [])
                escalated = any(s["name"] in ("SIGTERM", "SIGKILL") for s in signals)
                meta["execution"]["owned_cgroup"] = group
                recovery = {"status": "required" if escalated else "not_reported",
                            "reason": "command domain received TERM/KILL" if escalated else "command did not report device recovery"}
                domain.seal_files()
                _finish(store, jid, meta, "cancelled" if signals else "succeeded" if rc == 0 else "failed", recovery, rc)
                return 0
            if rc is not None and meta["execution"].get("returncode") is None:
                meta["execution"].update(returncode=rc, owned_cgroup=group)
                if not request:
                    meta["phase"] = "collecting"
                store.write_json(jid + "/meta.json", meta)
            if request:
                signals = meta["cancel"]["signals"]
                sig = None
                if request["mode"] == "escalate" and rc is not None and not any(s["name"] == "SIGKILL" for s in signals):
                    sig = signal.SIGKILL
                elif rc is None:
                    if not signals:
                        sig = signal.SIGINT
                    elif request["mode"] == "escalate" and len(signals) < 3:
                        delay = escalation_delays[0] if len(signals) == 1 else escalation_delays[1]
                        if time.monotonic() - last_signal_at >= delay:
                            sig = signal.SIGTERM if len(signals) == 1 else signal.SIGKILL
                if sig is not None:
                    if sig == signal.SIGKILL:
                        domain.kill()
                    else:
                        try:
                            os.killpg(child.pid, sig)  # Only while the directly owned child is live.
                        except ProcessLookupError:
                            continue
                    last_signal_at = time.monotonic()
                    signals.append({"name": sig.name, "scope": "owned_cgroup" if sig == signal.SIGKILL else "original_process_group",
                                    "at": time.time()})
                    meta["cancel"]["status"] = "signaled"
                    store.write_json(jid + "/meta.json", meta)
            time.sleep(0.05)
    finally:
        if child is not None and not released:
            child.stdin.close()
            child.wait(timeout=5)
        if domain is not None:
            domain.close()
