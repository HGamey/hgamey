"""Opt-in, privileged Linux worker backend. Not a public node capability.

The administrator supplies an empty cgroup v2 root and a dedicated job UID/GID.
This manages local descendants, not access to remote daemons or device recovery.
"""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

from artifact_io import SafeRoot, open_child


CGROUP2_SUPER_MAGIC = 0x63677270
GATE = 'IFS= read -r gate && [ "$gate" = GO ] || exit 125; unset PWD OLDPWD SHLVL _; exec "$@" </dev/null'
CONTAINMENT_SCHEMA = "phonefarm/job-containment@1"


def _boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise ValueError("host boot identity is unavailable")
    return value


def _binding(value):
    if not isinstance(value, dict) or value.get("schema") != CONTAINMENT_SCHEMA:
        raise ValueError("contained job has no supported persistent domain identity")
    if (not isinstance(value.get("host_boot_id"), str)
            or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["host_boot_id"])):
        raise ValueError("contained job has no valid host boot identity")
    for key in ("uid", "gid", "node_uid", "node_gid", "inode", "dev"):
        if type(value.get(key)) is not int or value[key] < (0 if key in ("node_uid", "node_gid", "dev") else 1):
            raise ValueError("invalid contained identity: " + key)
    if (not isinstance(value.get("cgroup_root"), str) or not os.path.isabs(value["cgroup_root"])
            or not isinstance(value.get("name"), str) or not re.fullmatch(r"job-[0-9a-f]{12}", value["name"])):
        raise ValueError("invalid contained domain location")
    handoff = value.get("handoff")
    if (not isinstance(handoff, dict) or handoff.get("status") not in ("pending", "returned")
            or type(handoff.get("at")) not in (int, float)):
        raise ValueError("invalid contained output handoff")
    return value


def completion_problem(meta):
    """Local execution/handback evidence only; never certifies device recovery."""
    execution = meta.get("execution") or {}
    if not isinstance(execution, dict):
        return "invalid contained execution record"
    group = execution.get("owned_cgroup") or {}
    if not isinstance(group, dict):
        return "invalid owned cgroup observation"
    value = execution.get("containment")
    if value is None:
        reconciliation = execution.get("reconciliation") or {}
        if (not group and not execution.get("process_management")
                and isinstance(reconciliation, dict) and reconciliation.get("state") == "complete"
                and reconciliation.get("basis") == "unstarted_empty_domain"):
            return None
        # A refused admission or pre-start cancellation never handed out the UID.
        if (not group and (execution.get("process_management") or {}).get("policy") != "contained"
                and meta.get("phase") == "finished" and not meta.get("lost")
                and (meta.get("recovery") or {}).get("status") == "not_needed"):
            return None
        if meta.get("phase") == "finished" and not meta.get("lost") and group.get("status") == "absent":
            return None  # Pre-02B3 normal completion; caller also checks private output.
        return "contained execution lacks confirmed domain completion"
    try:
        value = _binding(value)
    except ValueError as error:
        return str(error)
    if (group.get("status") != "absent" or group.get("scope") != "owned_cgroup"
            or group.get("path") != os.path.join(value["cgroup_root"], value["name"])):
        return "owned cgroup has not been confirmed absent"
    if value["handoff"]["status"] != "returned":
        return "contained output handoff has not completed"
    reconciliation = execution.get("reconciliation")
    if (meta.get("lost") or meta.get("phase") == "interrupted") and (
            not isinstance(reconciliation, dict) or reconciliation.get("state") != "complete"):
        return "interrupted contained job requires execution reconciliation"
    if reconciliation and (not isinstance(reconciliation, dict) or reconciliation.get("state") != "complete"):
        return "contained domain reconciliation has not completed"
    return None


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
    def __init__(self, config_path, jid, existing=None, unstarted=False):
        self.root = self.group = None
        self.job_fd = self.output_fd = None
        self.store = self.meta = self.binding = None
        self.jid = jid
        self.output_pending = False
        self.events = self.procs = self.kill_fd = None
        self.absence_basis = None
        self.name = "job-" + jid
        self.created = False
        self.unconfirmed_gate = False
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ValueError("contained worker requires Linux and a privileged worker identity")
        if not re.fullmatch(r"[0-9a-f]{12}", jid):
            raise ValueError("contained job ID must be 12 lowercase hex characters")
        self.host_boot_id = _boot_id()
        self.libc = _libc()  # Load before the single-threaded worker forks.
        if isinstance(config_path, dict):
            # Only the fixed privileged entry passes an already validated config.
            config = config_path
        else:
            with SafeRoot(Path(config_path).parent).open_file(Path(config_path).name) as f:
                _root_owned(f.fileno(), "contained configuration")
                config = json.load(f)
        self.uid, self.gid = config["uid"], config["gid"]
        if any(type(v) is not int or v <= 0 for v in (self.uid, self.gid)):
            raise ValueError("contained job UID/GID must be nonzero integers")
        self.node_uid, self.node_gid = config.get("node_uid", 0), config.get("node_gid", 0)
        if any(type(v) is not int or v < 0 for v in (self.node_uid, self.node_gid)):
            raise ValueError("contained node UID/GID must be nonnegative integers")
        if self.uid == self.node_uid or self.gid == self.node_gid:
            raise ValueError("contained job and node identities must differ")
        from worker_config import phonefarm_environment
        self.pf_env = phonefarm_environment(config)
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
            children = [name for name in os.listdir(self.root.fd)
                        if stat.S_ISDIR(os.stat(name, dir_fd=self.root.fd, follow_symlinks=False).st_mode)]
            if existing is None and not unstarted:
                with self.root.open_file("cgroup.events") as f:
                    if _populated(f.fileno()):
                        raise ValueError("contained root still has members; recovery is required")
                if children:
                    raise ValueError("prior contained domain remains; reconcile its records before identity reuse")
                os.mkdir(self.name, mode=0o755, dir_fd=self.root.fd)
                self.created = True
                self._open_group()
            elif unstarted:
                if children != [self.name] or self.root.read_bytes("cgroup.procs", 1 << 16).strip():
                    raise ValueError("unstarted cleanup requires only the named empty domain")
                self.output_pending = True  # No cleanup until private paths are proven.
                self._open_group()
                if self.observe()["status"] != "absent":
                    raise ValueError("unstarted domain is not confirmed empty")
            else:
                self.binding = _binding(existing)
                expected = {"uid": self.uid, "gid": self.gid, "node_uid": self.node_uid,
                            "node_gid": self.node_gid, "cgroup_root": str(self.root_path), "name": self.name}
                if any(existing[key] != value for key, value in expected.items()):
                    raise ValueError("saved domain identity differs from the fixed worker configuration")
                if any(name != self.name for name in children) or self.root.read_bytes("cgroup.procs", 1 << 16).strip():
                    raise ValueError("another contained domain or root member prevents reconciliation")
                self.output_pending = True
                if self.name in children:
                    info = os.stat(self.name, dir_fd=self.root.fd, follow_symlinks=False)
                    if (existing["host_boot_id"] != self.host_boot_id
                            or (existing["dev"], existing["inode"]) != (info.st_dev, info.st_ino)):
                        raise ValueError("current cgroup does not match the saved boot/inode identity")
                    self._open_group()
                    self.created = True  # Only this verified domain may be retired.
                elif existing["host_boot_id"] != self.host_boot_id:
                    self.absence_basis = "previous_boot"
                elif existing["handoff"]["status"] == "returned":
                    self.absence_basis = "previously_returned"
                else:
                    raise ValueError("saved cgroup is missing in the same boot; ownership is unknown")
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

    def _open_group(self):
        self.group = self.root.open_fd(self.name, directory=True)
        _root_owned(self.group, "contained domain")
        if self.root.read_bytes(self.name + "/cgroup.type", 64).strip() != b"domain":
            raise ValueError("owned cgroup is not a domain")
        self.events = open_child(self.group, "cgroup.events", directory=False)
        self.procs = os.open("cgroup.procs", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.group)
        self.kill_fd = os.open("cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.group)

    def retire_unstarted(self, store):
        for rel in (self.jid, self.jid + "/out"):
            fd = store.open_fd(rel, directory=True)
            try:
                info = os.fstat(fd)
                if info.st_uid != self.node_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("unstarted cleanup requires private node-owned job and output")
            finally:
                os.close(fd)
        if self.observe()["status"] != "absent":
            raise ValueError("unstarted domain is not confirmed empty")
        # No output was handed out, and GO could not precede the absent actual
        # membership record. Only retire this empty directory, not producer data.
        self.created, self.output_pending = True, False
        self.retire()

    def check_reuse(self, store):
        """The cgroup root flock also serializes the persistent UID reuse check."""
        for jid in os.listdir(store.fd):
            if jid == self.jid or not re.fullmatch(r"[0-9a-f]{12}", jid):
                continue
            job = store.open_fd(jid, directory=True)
            try:
                info = os.fstat(job)
                private = info.st_uid == self.node_uid and stat.S_IMODE(info.st_mode) == 0o700
                job_owned_output = False
                try:
                    out = open_child(job, "out", directory=True)
                except FileNotFoundError:
                    output_private = private
                else:
                    try:
                        info = os.fstat(out)
                        job_owned_output = info.st_uid == self.uid
                        output_private = info.st_uid == self.node_uid and stat.S_IMODE(info.st_mode) == 0o700
                    finally:
                        os.close(out)
            finally:
                os.close(job)
            try:
                meta = store.read_json(jid + "/meta.json")
            except FileNotFoundError:
                if private and output_private:
                    continue  # Input preparation never handed this directory out.
                raise ValueError("unclassified unsealed job prevents identity reuse: " + jid)
            execution = meta.get("execution") or {}
            if not isinstance(execution, dict):
                raise ValueError("invalid execution record prevents identity reuse: " + jid)
            record = execution.get("containment")
            managed = (execution.get("process_management") or {}).get("policy") == "contained"
            if record is not None:
                _binding(record)
                if (record["uid"], record["gid"], record["node_uid"], record["node_gid"]) != (
                        self.uid, self.gid, self.node_uid, self.node_gid) or record["name"] != "job-" + jid or record["cgroup_root"] != str(self.root_path):
                    raise ValueError("prior contained identity differs from configured identities: " + jid)
                problem = completion_problem(meta)
                if problem or not private or not output_private:
                    raise ValueError("prior contained job requires reconciliation: " + jid + ": " + (problem or "output is not private"))
            elif managed or execution.get("owned_cgroup") is not None:
                if completion_problem(meta) or not private or not output_private:
                    raise ValueError("legacy contained job has no confirmed private completion: " + jid)
            elif not private or job_owned_output or meta.get("execution_policy") == "contained" and not output_private:
                raise ValueError("unsealed contained job lacks persistent identity: " + jid)

    def bind_files(self, store, jid, meta, new=False):
        """Pin output and publish the obligation before any identity/permission change."""
        self.job_fd = store.open_fd(jid, directory=True)
        info = os.fstat(self.job_fd)
        if info.st_uid != self.node_uid or info.st_mode & 0o022:
            raise ValueError("job directory does not belong to the configured node")
        self.output_fd = open_child(self.job_fd, "out", directory=True)
        if os.fstat(self.output_fd).st_uid not in (self.uid, self.node_uid):
            raise ValueError("contained output belongs to an unrelated identity")
        self.store, self.meta = store, meta
        if new:
            info = os.fstat(self.group)
            self.binding = {"schema": CONTAINMENT_SCHEMA, "host_boot_id": self.host_boot_id,
                            "uid": self.uid, "gid": self.gid, "node_uid": self.node_uid, "node_gid": self.node_gid,
                            "cgroup_root": str(self.root_path), "name": self.name, "inode": info.st_ino, "dev": info.st_dev,
                            "handoff": {"status": "pending", "at": time.time()}}
        self.output_pending = True
        meta["execution"]["containment"] = self.binding
        store.write_json(jid + "/meta.json", meta)

    def prepare_files(self, store, jid, job_dir):
        def node_owned(fd, label):
            st = os.fstat(fd)
            if st.st_uid != self.node_uid or st.st_mode & 0o022:
                raise ValueError(f"{label} must be node-owned and not group/other writable")

        def readonly(fd):
            st = os.fstat(fd)
            node_owned(fd, "contained input")
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

        fd = os.dup(self.job_fd)
        try:
            node_owned(fd, "job directory")
            for name in os.listdir(fd):
                if name in ("in", "out"):
                    continue
                child = open_child(fd, name, directory=False)
                try:
                    node_owned(child, "worker record")
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
            out = os.dup(self.output_fd)
            try:
                node_owned(out, "initial job output")
                if os.listdir(out):
                    raise ValueError("contained worker requires a new, empty output directory")
                if self.pf_env is not None:
                    from worker_config import PF_ROOTS
                    for key in PF_ROOTS:
                        root = SafeRoot(self.pf_env[key])
                        try:
                            st = os.fstat(root.fd)
                            if st.st_gid != self.gid or st.st_mode & 0o2777 != 0o2770:
                                raise ValueError(key + " must be an existing dedicated group directory with mode 02770")
                        finally:
                            root.close()
                self.output_pending = True
                os.fchown(out, self.uid, self.gid)
                os.fchmod(out, 0o2770 if self.pf_env is not None else 0o700)
                if self.pf_env is not None:
                    os.mkdir("lease-receipts", mode=0o700, dir_fd=out)
                    receipts = open_child(out, "lease-receipts", directory=True)
                    try:
                        os.fchown(receipts, self.uid, self.gid)
                        os.fchmod(receipts, 0o2770)
                        os.fsync(receipts)
                    finally:
                        os.close(receipts)
                    os.fsync(out)
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
               "PF_RUNTIME_REPORT": os.path.join(job_dir, "out", ".program-runtime.json"),
               "PF_JOB_IN": os.path.join(job_dir, "in"), "PF_JOB_OUT": os.path.join(job_dir, "out")}
        if self.pf_env is not None:
            env.update(self.pf_env)
            env.update(PF_JOB_LEASE_DIR=os.path.join(job_dir, "out", "lease-receipts"),
                       DEVLOCK_LABEL="job-" + jid)
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
        if self.absence_basis is not None:
            return {"scope": "owned_cgroup", "status": "absent", "path": str(self.path),
                    "checked_at": time.time(), "error": None, "basis": self.absence_basis}
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
            os.fchmod(self.job_fd, 0o700)
            os.fsync(self.job_fd)

    def return_outputs(self):
        """Return completed output through pinned FDs, never through producer links."""
        # Even a failed empty-domain check or seal must retain this domain.
        self.output_pending = True
        if self.unconfirmed_gate or self.observe()["status"] != "absent":
            raise ValueError("cannot return output before the owned processes are confirmed absent")
        self.seal_files()

        def hand_back(fd, depth=0):
            if depth > 64:
                raise ValueError("contained output directory nesting exceeds 64")
            st = os.fstat(fd)
            if st.st_uid not in (self.uid, self.node_uid):
                raise ValueError("contained output must belong to its job or node identity")
            if stat.S_ISDIR(st.st_mode):
                for name in sorted(os.listdir(fd)):
                    child = open_child(fd, name)
                    try:
                        hand_back(child, depth + 1)
                    finally:
                        os.close(child)
                mode = 0o700
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                mode = 0o600 | (st.st_mode & 0o100)
            else:
                raise ValueError("contained output cannot contain links or special files")
            os.fchown(fd, self.node_uid, self.node_gid)
            os.fchmod(fd, mode)
            os.fsync(fd)

        if self.output_fd is not None:
            hand_back(self.output_fd)
        if self.job_fd is not None:
            os.fsync(self.job_fd)
        if self.binding is not None and self.store is not None:
            self.binding["handoff"] = {"status": "returned", "at": time.time()}
            self.meta["execution"]["containment"] = self.binding
            self.meta["execution"]["owned_cgroup"] = self.observe()
            self.store.write_json(self.jid + "/meta.json", self.meta)
        # Keep this pending after any failure, including a visible but unsynced
        # ownership change. close() must preserve the domain for reconciliation.
        self.output_pending = False

    def retire(self):
        empty = not self.unconfirmed_gate and self.events is not None and self.observe()["status"] == "absent"
        if self.root is not None and self.created and empty and not self.output_pending:
            current, opened = os.stat(self.name, dir_fd=self.root.fd, follow_symlinks=False), os.fstat(self.group)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("owned cgroup changed before retirement")
            os.rmdir(self.name, dir_fd=self.root.fd)
            self.created = False

    def close(self):
        empty = not self.unconfirmed_gate and self.events is not None and self.observe()["status"] == "absent"
        try:
            if empty:
                self.seal_files()
            self.retire()
        finally:
            for name in ("events", "procs", "kill_fd", "group", "job_fd", "output_fd"):
                fd = getattr(self, name, None)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)
            if self.root is not None:
                self.root.close()
                self.root = None


def reconcile(store, jid, job_dir, config, force=False):
    """Caller holds the real runner lock. Never execute the saved command."""
    if type(force) is not bool or os.path.basename(os.path.abspath(job_dir)) != jid:
        raise ValueError("invalid domain reconciliation request")
    meta = store.read_json(jid + "/meta.json")
    execution = meta.setdefault("execution", {})
    request = execution.get("reconciliation") or {}
    execution["reconciliation"] = {"state": "running", "force": force,
                                   "requested_at": request.get("requested_at", time.time()),
                                   "finished_at": None, "error": None}
    store.write_json(jid + "/meta.json", meta)
    domain = None

    def result(state, error=None):
        execution["reconciliation"].update(state=state, error=error, finished_at=time.time())
        store.write_json(jid + "/meta.json", meta)
        return 0 if state == "complete" else 1

    try:
        if execution.get("containment") is None:
            if (execution.get("process_management") is not None or execution.get("owned_cgroup") is not None
                    or store.read_json(jid + "/launch.json").get("execution_policy") != "contained"):
                raise ValueError("missing domain binding cannot be inferred from an executed job")
            domain = Domain(config, jid, unstarted=True)
            domain.retire_unstarted(store)
            execution["reconciliation"]["basis"] = "unstarted_empty_domain"
            return result("complete")
        domain = Domain(config, jid, existing=_binding(execution.get("containment")))
        previous = execution.get("owned_cgroup") or {}
        if domain.absence_basis == "previously_returned" and (
                previous.get("status") != "absent" or previous.get("scope") != "owned_cgroup"
                or previous.get("path") != str(domain.path)):
            raise ValueError("missing same-boot domain has no matching completed observation")
        domain.bind_files(store, jid, meta)
        observed = domain.observe()
        execution["owned_cgroup"] = observed
        if observed["status"] == "unknown":
            return result("blocked", observed["error"])
        if observed["status"] == "present":
            if not force:
                return result("blocked", "owned cgroup still has members; explicit force is required")
            domain.kill()
            deadline = time.monotonic() + 15
            while True:
                observed = domain.observe()
                execution["owned_cgroup"] = observed
                if observed["status"] != "present" or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if observed["status"] != "absent":
                return result("blocked", observed["error"] or "owned cgroup did not become empty before the deadline")
        domain.return_outputs()
        domain.retire()
        return result("complete")
    except (OSError, ValueError, KeyError) as error:
        return result("failed", f"{type(error).__name__}: {error}")
    finally:
        if domain is not None:
            domain.close()


def run(store, jid, job_dir, meta, spec, config_path, escalation_delays, budget, disk):
    from job_runtime import _open_file, _finish, OwnerStop

    domain, child, released = None, None, False
    try:
        try:
            domain = Domain(config_path, jid)
            domain.check_reuse(store)
            domain.bind_files(store, jid, meta, new=True)
            domain.prepare_files(store, jid, job_dir)
            log_fd = _open_file(store, jid + "/log", os.O_WRONLY | os.O_APPEND)
            try:
                disk.prepare(log_fd)
                child = domain.start_gate(spec["command"], spec["cwd"], job_dir, jid, log_fd)
            finally:
                os.close(log_fd)
        except subprocess.TimeoutExpired:
            # The startup gate was not reaped. Let the worker-loss path retain
            # resources rather than publish an unproven not_needed recovery.
            raise
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            if domain is not None:
                try:
                    domain.return_outputs()
                except (OSError, ValueError) as handback_error:
                    _finish(store, jid, meta, "interrupted", {"status": "required", "reason": "contained startup output handback is unconfirmed"},
                            error=f"contained startup failed: {error}; output handback failed: {handback_error}")
                    return 1
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
        budget.start()
        disk.start()
        child.stdin.close()
        meta["phase"] = "running"
        owner = OwnerStop(store, jid, meta)
        owner.poll()
        owner.save()
        last_signal_at = None
        while True:
            rc = child.poll()
            group = domain.observe()
            if group["status"] == "unknown":
                meta["execution"]["owned_cgroup"] = group
                _finish(store, jid, meta, "interrupted", {"status": "required", "reason": "owned cgroup status is unknown"}, rc)
                return 1
            if group["status"] == "absent" and rc is not None:
                budget.finish()
                disk.finish()
                signals = (meta.get("cancel") or {}).get("signals", [])
                escalated = any(s["name"] in ("SIGTERM", "SIGKILL") for s in signals)
                meta["execution"]["owned_cgroup"] = group
                recovery = {"status": "required" if escalated else "not_reported",
                            "reason": "command domain received TERM/KILL" if escalated else "command did not report device recovery"}
                try:
                    domain.return_outputs()
                except (OSError, ValueError) as error:
                    _finish(store, jid, meta, "interrupted", {"status": "required", "reason": "contained output handback is unconfirmed"},
                            rc, error=f"output handback failed: {type(error).__name__}: {error}")
                    return 1
                outcome = "interrupted" if owner.interrupted else "cancelled" if signals else "succeeded" if rc == 0 else "failed"
                _finish(store, jid, meta, outcome, recovery, rc)
                return 0
            owner.poll()
            owner.observe_program()
            budget.check(owner.request_cancel)
            disk.check(owner.request_cancel)
            request = owner.request
            if rc is not None and meta["execution"].get("returncode") is None:
                meta["execution"].update(returncode=rc, owned_cgroup=group)
                if not request:
                    meta["phase"] = "collecting"
                owner.save()
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
                    owner.save()
            time.sleep(0.05)
    finally:
        if child is not None and not released:
            child.stdin.close()
            child.wait(timeout=5)
        if domain is not None:
            domain.close()
