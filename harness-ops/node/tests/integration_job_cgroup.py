#!/usr/bin/env python3
"""One real Linux cgroup-v2 worker chain; missing kernel privileges fail, never skip.

Run explicitly as root on an isolated VM. This is intentionally outside normal
unittest discovery and does not enable contained requests on the node/API.
"""
import argparse
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

NODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NODE))
from artifact_io import SafeRoot
import job_runtime as runtime


WORKLOAD = r'''
import errno, json, os, pathlib, subprocess, sys, time
plan = json.loads(pathlib.Path(sys.argv[1]).read_text())
out = pathlib.Path(os.environ['PF_JOB_OUT'])
def save(name, value):
    temporary = out / (name + '.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(out / name)
def cgroup():
    return next(line[3:] for line in pathlib.Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
def facts():
    status = dict(line.split(':', 1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
    return {'pid':os.getpid(), 'pgid':os.getpgrp(), 'sid':os.getsid(0),
            'uid':list(os.getresuid()), 'gid':list(os.getresgid()), 'groups':os.getgroups(), 'cgroup':cgroup(),
            'no_new_privs':status['NoNewPrivs'].strip(),
            'caps':{key:status[key].strip() for key in ('CapInh','CapPrm','CapEff','CapAmb')},
            'environment_keys':sorted(os.environ)}
if len(sys.argv) == 3 and sys.argv[2] == 'descendant':
    print('CGROUP_DESCENDANT_STDOUT', flush=True)
    save('descendant.json', facts())
    # Independent finite lifetime in case the outer harness or CI is interrupted.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(.05)
    save('descendant-deadline.json', {'deadline_expired':True})
    sys.exit(1)
value = facts()
assert value['uid'] == [plan['uid']]*3 and value['gid'] == [plan['gid']]*3, value
assert value['groups'] == [] and value['no_new_privs'] == '1', value
assert all(int(v,16) == 0 for v in value['caps'].values()), value
expected = {'PATH','LANG','HOME','TMPDIR','PF_JOB_IN','PF_JOB_OUT','PF_JOB_ID','PF_RUNTIME_REPORT'}
assert set(os.environ) == expected, value
report = pathlib.Path(os.environ.pop('PF_RUNTIME_REPORT'))
assert report == out/'.program-runtime.json'
temporary = out/'.program-runtime.tmp'
temporary.write_text(json.dumps({'schema':'phonefarm/program-start@1',
    'job_id':plan['jid'],'pid':os.getpid(),'identity':{
        'schema':'phonefarm/runtime-identity@1','program':'game_opt_loop','package_version':'fixture',
        'build':{'source_revision':'a'*40,'source_dirty':False,'target':'fixture-linux','profile':'test'},
        'supported_protocols':['phonefarm/runtime-identity@1','phonefarm/program-start@1']}}))
os.link(temporary,report)
temporary.unlink()
assert os.environ['PATH'] == '/usr/bin:/bin' and os.environ['LANG'] == 'C.UTF-8'
assert os.environ['HOME'] == os.environ['TMPDIR'] == str(out)
assert os.environ['PF_JOB_ID'] == plan['jid']
assert pathlib.Path(os.environ['PF_JOB_IN']) == pathlib.Path(plan['input']).parent
assert pathlib.Path(plan['input']).read_text() == 'real-input-payload\n'
assert pathlib.Path(plan['input']).stat().st_mode & 0o777 == 0o444
assert pathlib.Path(__file__).stat().st_mode & 0o777 == 0o555
assert pathlib.Path(plan['input']).parent.stat().st_mode & 0o777 == 0o555
assert out.stat().st_uid == plan['uid'] and out.stat().st_gid == plan['gid']
assert out.stat().st_mode & 0o777 == 0o700
(out/'input-copy.txt').write_text(pathlib.Path(plan['input']).read_text())
denied = {}
for name, path in plan['denied_write_paths'].items():
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as error:
        assert error.errno in (errno.EACCES, errno.EPERM), (name, repr(error))
        denied[name] = error.errno
    else:
        os.close(fd)
        raise AssertionError('job can open protected path for writing: ' + name)
# A real attempted migration outside the per-job domain must fail. The target
# is our fixture's parent domain, not an unrelated system or runner cgroup.
try:
    with open(plan['migration_target'], 'w') as target:
        target.write(str(os.getpid()))
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM), repr(error)
    denied['migration'] = error.errno
else:
    raise AssertionError('job migrated out of its owned cgroup')
value['denied'] = denied
save('leader.json', value)
child = subprocess.Popen([sys.executable, '-I', __file__, sys.argv[1], 'descendant'],
                         start_new_session=True, stdin=subprocess.DEVNULL, close_fds=True)
deadline = time.monotonic()+5
while not (out/'descendant.json').exists() and time.monotonic()<deadline:
    time.sleep(.02)
assert (out/'descendant.json').exists() and child.poll() is None
print('CGROUP_LEADER_STDOUT', flush=True)
print('CGROUP_LEADER_STDERR', file=sys.stderr, flush=True)
'''


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o644)


def until(check, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("bounded cgroup integration wait expired")


def events(root):
    return dict(line.split() for line in (root / "cgroup.events").read_text().splitlines())


def membership(pid):
    return next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))


class KernelFixture:
    def __init__(self, artifact_dir):
        self.artifacts = artifact_dir
        self.artifacts.mkdir(parents=True, exist_ok=False)
        self.artifacts.chmod(0o755)
        self.nonce = uuid.uuid4().hex[:16]
        self.username = "pfcg" + self.nonce
        self.account_created = False
        self.cgroup_created = False
        self.scratch = self.cgroup_root = self.store = self.worker = None
        self.report = {"passed": False, "scope": "one real privileged Linux worker; node/API contained remains unavailable",
                       "commands": [], "checks": {}, "cleanup": {}}
        self.known_pids = set()

    def command(self, argv):
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin", "LANG":"C.UTF-8"}, timeout=5)
        self.report["commands"].append({"argv":argv, "exit":result.returncode,
                                        "stdout":result.stdout, "stderr":result.stderr})
        if result.returncode:
            raise RuntimeError(f"fixture command failed: {argv[0]} exit {result.returncode}: {result.stderr}")

    def run(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("this integration test requires root on an isolated Linux VM; it does not skip")
        mount = Path("/sys/fs/cgroup")
        mounts = Path("/proc/self/mountinfo").read_text()
        if not any(line.split()[4] == str(mount) and line.split(" - ")[1].split()[0] == "cgroup2"
                   for line in mounts.splitlines() if " - " in line):
            raise RuntimeError("/sys/fs/cgroup must be a real cgroup-v2 mount")
        self.report["kernel"] = {"uname":list(os.uname()), "test_cgroup":membership(os.getpid())}
        # Create only our own directory, enabling no controllers or system units.
        self.cgroup_root = mount / ("phonefarm-integration-" + self.nonce)
        self.cgroup_root.mkdir(mode=0o755)
        self.cgroup_created = True
        for name in ("cgroup.procs", "cgroup.events", "cgroup.kill", "cgroup.type"):
            if not (self.cgroup_root / name).exists():
                raise RuntimeError("required kernel interface missing: " + name)
        assert (self.cgroup_root / "cgroup.type").read_text().strip() == "domain"
        assert events(self.cgroup_root)["populated"] == "0"
        try:
            pwd.getpwnam(self.username)
        except KeyError:
            pass
        else:
            raise RuntimeError("fixture account name unexpectedly exists")
        self.command(["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home",
                      "--shell", "/usr/sbin/nologin", self.username])
        self.account_created = True
        account = pwd.getpwnam(self.username)
        self.uid, self.gid = account.pw_uid, account.pw_gid
        assert self.uid > 0 and self.gid > 0 and grp.getgrgid(self.gid).gr_name == self.username
        self.report["identity"] = {"account":self.username, "uid":self.uid, "gid":self.gid}
        # This fresh parent is deliberately traversable; never chmod the caller's
        # private directory or rely on giving the job access to the checkout.
        self.scratch = Path(tempfile.mkdtemp(prefix="phonefarm-cgroup-", dir="/tmp"))
        self.scratch.chmod(0o755)
        jobs_root = self.scratch / "jobs"
        jobs_root.mkdir(mode=0o755)
        self.store = SafeRoot(jobs_root)
        self.jid = self.nonce[:12]
        self.job = jobs_root / self.jid
        (self.job / "in").mkdir(parents=True, mode=0o700)
        (self.job / "out").mkdir(mode=0o700)
        (self.job / "in/input.txt").write_text("real-input-payload\n")
        (self.job / "in/input.txt").chmod(0o600)
        (self.job / "in/workload.py").write_text(WORKLOAD)
        (self.job / "in/workload.py").chmod(0o700)
        self.store.write_json(self.jid + "/meta.json", {"id":self.jid, "pid":None, "state":"running", "phase":"starting",
            "execution_policy":"contained", "execution_policy_source":"explicit", "execution":{"kind":"supervised-v1"}})
        config = self.scratch / "config.json"
        config.write_text(json.dumps({"cgroup_root":str(self.cgroup_root), "uid":self.uid, "gid":self.gid}))
        config.chmod(0o600)
        plan = {"uid":self.uid, "gid":self.gid, "jid":self.jid, "input":str(self.job / "in/input.txt"),
                "migration_target":str(self.cgroup_root / "cgroup.procs"),
                "denied_write_paths":{name:str(self.job / name) for name in ("meta.json","launch.json","runner.lock")}}
        plan["denied_write_paths"]["input"] = str(self.job / "in/input.txt")
        (self.job / "in/plan.json").write_text(json.dumps(plan))
        (self.job / "in/plan.json").chmod(0o600)
        env = dict(os.environ, PF_NODE_TOKEN="fixture-secret-must-not-reach-job",
                   AWS_SECRET_ACCESS_KEY="fixture-credential-must-not-reach-job",
                   PF_JOB_ID=self.jid, PF_JOB_IN=str(self.job / "in"), PF_JOB_OUT=str(self.job / "out"))
        self.worker = runtime.launch(self.store, self.jid, str(self.job),
            ["/usr/bin/python3", "-I", str(self.job / "in/workload.py"), str(self.job / "in/plan.json")],
            str(self.job / "out"), env, execution_policy="contained", contained_config=str(config))
        self.known_pids.add(self.worker.pid)
        def collecting():
            meta = self.store.read_json(self.jid + "/meta.json")
            child_pid = meta.get("execution", {}).get("child_pid")
            if isinstance(child_pid, int) and child_pid > 1:
                self.known_pids.add(child_pid)
            if meta.get("state") == "finished":
                raise AssertionError("worker terminated before real cgroup collection: " + json.dumps(meta))
            return meta if meta.get("phase") == "collecting" else None
        meta = until(collecting)
        save(self.artifacts / "collecting.json", meta)
        assert meta["state"] == "running" and meta["execution"]["returncode"] == 0
        assert meta.get("exit") is None and not (self.job / "exitcode").exists()
        assert runtime.runner_alive(self.store, self.jid) and self.worker.poll() is None
        assert meta["execution"]["process_management"] == {"policy":"contained", "backend":"linux_cgroup_v2",
            "completion_scope":"owned_cgroup", "detached_descendants":"contained"}
        owned = meta["execution"]["owned_cgroup"]
        assert owned["scope"] == "owned_cgroup" and owned["status"] == "present", owned
        domain = Path(owned["path"])
        assert domain.parent == self.cgroup_root and domain.is_dir(), owned
        leader = json.loads((self.job / "out/leader.json").read_text())
        detached = json.loads((self.job / "out/descendant.json").read_text())
        self.known_pids.update([leader["pid"], detached["pid"]])
        assert leader["pgid"] != detached["pgid"] == detached["pid"] == detached["sid"]
        relative = "/" + str(domain.relative_to(mount))
        assert leader["cgroup"] == detached["cgroup"] == membership(detached["pid"]) == relative
        assert membership(self.worker.pid) != relative and not membership(self.worker.pid).startswith(relative + "/")
        assert events(domain)["populated"] == events(self.cgroup_root)["populated"] == "1"
        assert str(detached["pid"]) in (domain / "cgroup.procs").read_text().splitlines()
        assert self.job.stat().st_uid == 0 and stat.S_IMODE(self.job.stat().st_mode) == 0o711
        for name in ("meta.json", "launch.json", "runner.lock"):
            assert (self.job/name).stat().st_uid == 0 and stat.S_IMODE((self.job/name).stat().st_mode) == 0o600
        self.report["checks"]["privilege_environment_io_and_protected_records"] = leader
        self.report["checks"]["setsid_retained_after_direct_exit"] = {"descendant":detached, "cgroup":owned,
            "worker_outside_job_domain":True, "pending_with_lock":True}
        runtime.request_cancel(self.store, self.jid, force=True)
        self.worker.wait(timeout=8)
        done = self.store.read_json(self.jid + "/meta.json")
        save(self.artifacts / "finished.json", done)
        assert done["state"] == "finished" and done["outcome"] == "cancelled", done
        assert done["execution"]["returncode"] == 0 and done["exit"] == 0
        assert done["execution"]["owned_cgroup"]["status"] == "absent"
        assert done["recovery"]["status"] == "required", done
        assert not runtime.runner_alive(self.store, self.jid)
        assert events(self.cgroup_root)["populated"] == "0"
        assert not (self.job / "out/descendant-deadline.json").exists()
        assert (self.job / "out/input-copy.txt").read_text() == "real-input-payload\n"
        log = (self.job / "log").read_text()
        assert all(text in log for text in ("CGROUP_LEADER_STDOUT", "CGROUP_LEADER_STDERR", "CGROUP_DESCENDANT_STDOUT")), log
        assert stat.S_IMODE(self.job.stat().st_mode) == 0o700
        # Reuse the numeric job identity only for one short read probe, not a
        # second worker. It must not reopen evidence after the root is sealed.
        probe = """
import errno,json,os,pathlib,sys
denied=[]
for path in sys.argv[1:]:
    try:
        if pathlib.Path(path).suffix: pathlib.Path(path).read_bytes()
        else: list(pathlib.Path(path).iterdir())
    except OSError as error:
        assert error.errno in (errno.EACCES,errno.EPERM), repr(error)
        denied.append(path)
    else: raise AssertionError('old job evidence remained accessible: '+path)
print(json.dumps({'pid':os.getpid(),'uid':os.getuid(),'gid':os.getgid(),'denied':denied}))
"""
        helper = subprocess.Popen(["/usr/bin/python3", "-I", "-c", probe,
            str(self.job / "out"), str(self.job / "out/input-copy.txt")],
            user=self.uid, group=self.gid, extra_groups=[], cwd="/", env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.known_pids.add(helper.pid)
        try:
            stdout, stderr = helper.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            helper.kill(); helper.communicate(timeout=3)
            raise
        (self.artifacts / "sealed-read-probe.stdout").write_text(stdout)
        (self.artifacts / "sealed-read-probe.stderr").write_text(stderr)
        assert helper.returncode == 0, (helper.returncode, stderr)
        sealed = json.loads(stdout)
        assert sealed["uid"] == self.uid and sealed["gid"] == self.gid and len(sealed["denied"]) == 2
        assert (self.job / "out/input-copy.txt").read_text() == "real-input-payload\n"
        self.report["checks"]["completed_evidence_sealed_against_same_uid"] = sealed
        self.report["checks"]["force_emptied_owned_domain"] = {"root_events":events(self.cgroup_root),
            "preserved_direct_exit":0, "device_recovery":"required", "raw_output_preserved":True}
        self.report["passed"] = True

    def cleanup(self):
        errors = []
        # Stop our actual worker handle first on failure, so it cannot create a
        # new per-job domain after the final empty-domain observation.
        if self.worker:
            if self.worker.poll() is None:
                self.worker.terminate()
                try:
                    self.worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.worker.kill(); self.worker.wait(timeout=3)
            else:
                self.worker.wait()
        # Only this freshly created dedicated domain, never a colliding path.
        if self.cgroup_created and self.cgroup_root.exists():
            try:
                if events(self.cgroup_root)["populated"] != "0":
                    (self.cgroup_root / "cgroup.kill").write_text("1")
                    until(lambda: events(self.cgroup_root)["populated"] == "0", seconds=5)
                self.report["cleanup"]["owned_root_empty"] = True
            except Exception as error:
                errors.append("owned cgroup cleanup: " + repr(error))
        if self.scratch:
            # Copy raw diagnostics before removing scratch. Only copies become
            # readable for artifact upload; tested worker files keep their modes.
            try:
                target = self.artifacts / "raw"
                target.mkdir(mode=0o755)
                for source in self.scratch.rglob("*"):
                    dest = target / source.relative_to(self.scratch)
                    if source.is_dir():
                        dest.mkdir(mode=0o755, exist_ok=True)
                    elif source.is_file() and not source.is_symlink():
                        shutil.copyfile(source, dest)
                        dest.chmod(0o644)
            except Exception as error:
                errors.append("artifact collection: " + repr(error))
        if self.store:
            self.store.close()
        if self.cgroup_created and self.cgroup_root.exists():
            try:
                for current, directories, _ in os.walk(self.cgroup_root, topdown=False):
                    for name in directories:
                        (Path(current)/name).rmdir()
                self.cgroup_root.rmdir()
            except Exception as error:
                errors.append("owned cgroup removal: " + repr(error))
        deadline = time.monotonic() + 3
        while any(Path(f"/proc/{pid}").exists() for pid in self.known_pids) and time.monotonic() < deadline:
            time.sleep(.025)
        remaining = [pid for pid in sorted(self.known_pids) if Path(f"/proc/{pid}").exists()]
        if remaining:
            errors.append("known fixture processes remain: " + repr(remaining))
        if self.account_created:
            try:
                self.command(["/usr/sbin/userdel", self.username])
                try:
                    grp.getgrnam(self.username)
                except KeyError:
                    pass
                else:
                    self.command(["/usr/sbin/groupdel", self.username])
                self.report["cleanup"]["temporary_account_removed"] = True
            except Exception as error:
                errors.append("temporary account cleanup: " + repr(error))
        if self.scratch and not errors:
            shutil.rmtree(self.scratch)
        self.report["cleanup"].update(known_pids=sorted(self.known_pids), errors=errors,
            remaining_known_pids=remaining,
            cgroup_removed=bool(self.cgroup_created and not self.cgroup_root.exists()),
            scratch_retained=str(self.scratch) if self.scratch and self.scratch.exists() else None)
        if errors:
            self.report["passed"] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    fixture = KernelFixture(args.artifact_dir.absolute())
    def interrupted(signum, frame):
        raise TimeoutError(f"integration interrupted by signal {signum}")
    for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    signal.alarm(60)
    try:
        fixture.run()
    except BaseException as error:
        fixture.report["failure"] = repr(error)
        (fixture.artifacts / "failure.txt").write_text(traceback.format_exc())
    finally:
        signal.alarm(0)
        fixture.cleanup()
        save(fixture.artifacts / "result.json", fixture.report)
    print(json.dumps(fixture.report, indent=2))
    return 0 if fixture.report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
