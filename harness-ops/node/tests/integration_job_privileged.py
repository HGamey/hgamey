#!/usr/bin/env python3
"""One real non-root node -> fixed sudo entry -> privileged worker chain.

Explicit integration test for an isolated Linux VM. Accounts, sudoers fragment,
installation prefix and cgroup belong only to this invocation. No API capability
is enabled and a missing prerequisite fails rather than skipping the test.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback

from integration_job_cgroup import KernelFixture, WORKLOAD, NODE, events, until, save, membership
from artifact_io import SafeRoot
from job_runtime import runner_alive


NODE_HELPER = r'''
import errno,fcntl,json,os,pathlib,subprocess,sys
operation, plan_path = sys.argv[1:]
plan = json.loads(pathlib.Path(plan_path).read_text())
jid = plan['probe_jid'] if operation in ('prepare-probe','bad-fd','bad-policy') else plan['jid']
job = pathlib.Path(plan['jobs_root']) / jid
def answer(value):
    print(json.dumps({'helper_pid':os.getpid(),'helper_uid':os.getuid(),**value}),flush=True)
if operation == 'job-read':
    assert os.getuid() == plan['job_uid']
    denied=[]
    for path in (job/'meta.json', job/'out/input-copy.txt'):
        try: path.read_bytes()
        except OSError as error:
            assert error.errno in (errno.EACCES,errno.EPERM), repr(error)
            denied.append(str(path))
        else: raise AssertionError('job identity can read old protected evidence')
    answer({'denied':denied})
    sys.exit(0)
assert os.getuid() == plan['node_uid'] and os.getgid() == plan['node_gid']
if operation in ('bad-fd','bad-policy','terminal-replay'):
    if operation == 'bad-policy':
        sys.path.insert(0,plan['prefix'])
        from artifact_io import SafeRoot
        store = SafeRoot(plan['jobs_root'])
        try:
            store.write_json(jid+'/launch.json',{'execution_policy':'process_group',
                'command':['/usr/bin/python3','-I','-c',
                    'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("unexpected root execution")',
                    plan['probe_marker']], 'cwd':str(job/'out')})
        finally: store.close()
    fd = os.open(plan['wrong_fd'] if operation == 'bad-fd' else job/'runner.lock',
                 os.O_CREAT|os.O_RDWR,0o600)
    if operation != 'bad-fd': fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if fd != 3:
        os.dup2(fd,3); os.close(fd)
    os.set_inheritable(3,True)
    proc = subprocess.Popen(['/usr/bin/sudo','-n','-C4',plan['entry'],jid],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(3,),text=True)
    try: stdout,stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill(); proc.communicate(timeout=3); raise
    finally: os.close(3)
    answer({'entry_pid':proc.pid,'exit':proc.returncode,'stdout':stdout,'stderr':stderr})
    sys.exit(0)
sys.path.insert(0,plan['prefix'])
from artifact_io import SafeRoot
import job_runtime as runtime
store = SafeRoot(plan['jobs_root'])
try:
    if operation in ('prepare','prepare-probe'):
        (job/'in').mkdir(parents=True,mode=0o700)
        job.chmod(0o700)
        (job/'out').mkdir(mode=0o700)
        (job/'in/input.txt').write_text('real-input-payload\n')
        (job/'in/input.txt').chmod(0o600)
        (job/'in/workload.py').write_text(pathlib.Path(plan['workload_template']).read_text())
        (job/'in/workload.py').chmod(0o700)
        (job/'in/plan.json').write_text(json.dumps(plan['workload_plan']))
        (job/'in/plan.json').chmod(0o600)
        store.write_json(jid+'/meta.json',{'id':jid,'pid':None,'state':'running','phase':'starting',
            'execution_policy':'contained','execution_policy_source':'explicit','execution':{'kind':'supervised-v1'}})
        (job/'runner.lock').touch(mode=0o600)
        answer({'prepared':True})
    elif operation == 'launch':
        env=dict(os.environ,PF_NODE_TOKEN='fixture-node-secret-not-for-job',
                 AWS_SECRET_ACCESS_KEY='fixture-credential-not-for-job')
        process=runtime.launch(store,plan['jid'],str(job),
            ['/usr/bin/python3','-I',str(job/'in/workload.py'),str(job/'in/plan.json')],
            str(job/'out'),env,execution_policy='contained',privileged_entry=plan['entry'])
        receipt={'launcher_pid':os.getpid(),'launcher_uid':os.getuid(),'worker_handle_pid':process.pid}
        pathlib.Path(plan['launcher_receipt']).write_text(json.dumps(receipt))
        answer(receipt)
        store.close()
        os._exit(0)  # The real node launcher exits; only the worker retains its flock.
    elif operation in ('inspect','cancel','final'):
        meta=store.read_json(plan['jid']+'/meta.json')
        result={'meta':meta,'runner_alive':runtime.runner_alive(store,plan['jid']),
                'log':store.read_bytes(plan['jid']+'/log',65536).decode()}
        if operation == 'cancel':
            result['cancel']=runtime.request_cancel(store,plan['jid'],force=True)
            result['cancel_owner']=(job/'cancel.json').stat().st_uid
        if operation == 'final':
            result['output']=(job/'out/input-copy.txt').read_text()
            result['ownership']={name:{'uid':(job/name).stat().st_uid,'gid':(job/name).stat().st_gid,
                'mode':(job/name).stat().st_mode & 0o777} for name in
                ('meta.json','runner.lock','log','cancel.json','out','out/input-copy.txt')}
        answer(result)
    else: raise AssertionError('unknown node helper operation')
finally:
    store.close()
'''


class PrivilegedFixture(KernelFixture):
    def __init__(self, artifact_dir):
        super().__init__(artifact_dir)
        self.node_account = "pfnd" + self.nonce
        self.node_created = self.sudoers_created = False
        self.install_root = self.sudoers_path = None
        self.helper_handles = []
        self.report["scope"] = "one non-root node launch through installed fixed sudo entry; no public contained admission"

    def helper(self, operation, *, job_identity=False):
        uid, gid = (self.uid,self.gid) if job_identity else (self.node_uid,self.node_gid)
        env = {"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8", "HOME":str(self.scratch),
               "PF_NODE_TOKEN":"fixture-node-secret-not-for-job"}
        proc = subprocess.Popen(["/usr/bin/python3","-I",str(self.scratch/'node-helper.py'),
                                 operation,str(self.scratch/'node-plan.json')],
            user=uid,group=gid,extra_groups=[],cwd="/",env=env,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.helper_handles.append(proc)
        self.known_pids.add(proc.pid)
        try:
            stdout,stderr = proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate(timeout=3)
            raise
        (self.artifacts/(operation+'.stdout')).write_text(stdout)
        (self.artifacts/(operation+'.stderr')).write_text(stderr)
        assert proc.returncode == 0, (operation,proc.returncode,stderr)
        value = json.loads(stdout)
        assert value["helper_uid"] == uid
        return value

    def run(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("isolated Linux root integration required; no skip")
        mount = Path('/sys/fs/cgroup')
        mounts = Path('/proc/self/mountinfo').read_text()
        assert any(line.split()[4] == str(mount) and line.split(' - ')[1].split()[0] == 'cgroup2'
                   for line in mounts.splitlines() if ' - ' in line)
        self.cgroup_root = mount/('phonefarm-privileged-'+self.nonce)
        self.cgroup_root.mkdir(mode=0o755)
        self.cgroup_created = True
        assert all((self.cgroup_root/name).exists() for name in ('cgroup.events','cgroup.procs','cgroup.kill'))
        assert events(self.cgroup_root)['populated'] == '0'
        for name in (self.node_account,self.username):
            try: pwd.getpwnam(name)
            except KeyError: pass
            else: raise RuntimeError('fixture account already exists')
            self.command(['/usr/sbin/useradd','--system','--user-group','--no-create-home',
                          '--shell','/usr/sbin/nologin',name])
            if name == self.node_account: self.node_created = True
            else: self.account_created = True
        node,job = pwd.getpwnam(self.node_account),pwd.getpwnam(self.username)
        self.node_uid,self.node_gid = node.pw_uid,node.pw_gid
        self.uid,self.gid = job.pw_uid,job.pw_gid
        assert min(self.node_uid,self.node_gid,self.uid,self.gid)>0 and self.node_uid!=self.uid and self.node_gid!=self.gid
        self.report['identity']={'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,'job_gid':self.gid}
        self.scratch = Path(tempfile.mkdtemp(prefix='phonefarm-privileged-',dir='/tmp'))
        self.scratch.chmod(0o755)
        jobs_root = self.scratch/'jobs'; jobs_root.mkdir(mode=0o755)
        os.chown(jobs_root,self.node_uid,self.node_gid)
        control = self.scratch/'control'; control.mkdir(mode=0o700)
        os.chown(control,self.node_uid,self.node_gid)
        # Runner /opt may be writable by its regular user. Create only our own
        # root child; never weaken trust checks or chmod a shared ancestor.
        self.install_root = Path(tempfile.mkdtemp(prefix='phonefarm-integration-',dir='/'))
        self.install_root.chmod(0o755)
        prefix = self.install_root/'worker'
        argv = ['/usr/bin/python3',str(NODE/'install_worker.py'),'--prefix',str(prefix),
                '--jobs-root',str(jobs_root),'--cgroup-root',str(self.cgroup_root),
                '--node-uid',str(self.node_uid),'--node-gid',str(self.node_gid),
                '--job-uid',str(self.uid),'--job-gid',str(self.gid)]
        self.command(argv)
        installed = json.loads(self.report['commands'][-1]['stdout'])
        assert installed['entry'] == str(prefix/'phonefarm-worker') and installed['prefix'] == str(prefix)
        assert installed['config'] == str(prefix/'worker.json')
        assert (prefix/'job_runtime.py').is_file() and (prefix/'job_privileged.py').is_file()
        assert all((prefix/name).stat().st_uid == 0 for name in ('phonefarm-worker','worker.json','job_runtime.py'))
        assert stat.S_IMODE((prefix/'worker.json').stat().st_mode) == 0o640
        assert (prefix/'worker.json').stat().st_gid == self.node_gid
        save(self.artifacts/'installation.json',installed)
        self.sudoers_path = Path('/etc/sudoers.d')/('phonefarm-integration-'+self.nonce)
        fd = os.open(self.sudoers_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o440)
        self.sudoers_created = True
        with os.fdopen(fd,'w') as stream:
            stream.write(installed['sudoers'].rstrip()+'\n')
        self.command(['/usr/sbin/visudo','-cf',str(self.sudoers_path)])
        (self.artifacts/'sudoers-fragment.txt').write_text(installed['sudoers'])
        self.jid = self.nonce[:12]; self.job = jobs_root/self.jid
        probe_jid = self.nonce[-12:]
        assert probe_jid != self.jid
        probe_job = jobs_root/probe_jid
        self.store = SafeRoot(jobs_root)
        (self.scratch/'workload.py').write_text(WORKLOAD)
        (self.scratch/'workload.py').chmod(0o644)
        (self.scratch/'node-helper.py').write_text(NODE_HELPER)
        (self.scratch/'node-helper.py').chmod(0o644)
        workload_plan = {'uid':self.uid,'gid':self.gid,'jid':self.jid,'input':str(self.job/'in/input.txt'),
            'migration_target':str(self.cgroup_root/'cgroup.procs'),
            'denied_write_paths':{name:str(self.job/name) for name in ('meta.json','launch.json','runner.lock')}}
        workload_plan['denied_write_paths']['input'] = str(self.job/'in/input.txt')
        plan = {'prefix':str(prefix),'entry':installed['entry'],'jobs_root':str(jobs_root),'jid':self.jid,
            'probe_jid':probe_jid,'probe_marker':str(control/'unexpected-root-execution'),
            'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,
            'workload_template':str(self.scratch/'workload.py'),'workload_plan':workload_plan,
            'wrong_fd':str(control/'wrong-fd'),'launcher_receipt':str(control/'launcher.json')}
        save(self.scratch/'node-plan.json',plan)
        self.helper('prepare-probe')
        before = (probe_job/'meta.json').read_bytes()
        rejected = self.helper('bad-fd')
        self.known_pids.add(rejected['entry_pid'])
        assert rejected['exit'] != 0 and 'inherited FD 3 does not identify' in rejected['stderr'], rejected
        assert (probe_job/'meta.json').read_bytes() == before and not (probe_job/'launch.json').exists()
        assert not list((probe_job/'out').iterdir()) and events(self.cgroup_root)['populated']=='0'
        assert not [p for p in self.cgroup_root.iterdir() if p.is_dir()]
        self.report['checks']['wrong_fd_refused_before_job_start'] = rejected
        rejected = self.helper('bad-policy')
        self.known_pids.add(rejected['entry_pid'])
        assert rejected['exit'] != 0 and 'refuses non-contained launch records' in rejected['stderr'], rejected
        assert (probe_job/'meta.json').read_bytes() == before and not Path(plan['probe_marker']).exists()
        assert not list((probe_job/'out').iterdir()) and events(self.cgroup_root)['populated']=='0'
        assert not [p for p in self.cgroup_root.iterdir() if p.is_dir()]
        self.report['checks']['correct_fd_noncontained_refused_before_job_start'] = rejected
        self.helper('prepare')
        launched = self.helper('launch')
        self.known_pids.add(launched['worker_handle_pid'])
        assert not Path(f"/proc/{launched['launcher_pid']}").exists()
        def collecting():
            meta = self.store.read_json(self.jid+'/meta.json')
            for pid in (meta.get('pid'),meta.get('execution',{}).get('child_pid')):
                if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
            if meta.get('state') == 'finished':
                raise AssertionError('privileged worker ended before collection: '+json.dumps(meta))
            return meta if meta.get('phase')=='collecting' else None
        meta = until(collecting)
        save(self.artifacts/'collecting.json',meta)
        inspect = self.helper('inspect')
        assert inspect['runner_alive'] and inspect['meta']['phase']=='collecting'
        assert inspect['meta']['execution']['returncode']==0
        assert 'CGROUP_LEADER_STDOUT' in inspect['log'] and 'CGROUP_LEADER_STDERR' in inspect['log']
        detached = json.loads((self.job/'out/descendant.json').read_text())
        self.known_pids.add(detached['pid'])
        owned = meta['execution']['owned_cgroup']
        assert owned['status']=='present' and Path(owned['path']).parent==self.cgroup_root
        assert membership(detached['pid'])=='/'+str(Path(owned['path']).relative_to(mount))
        self.report['checks']['launcher_exit_kept_worker_lock'] = {'launcher':launched,'node_observation':inspect,
            'detached_pid':detached['pid']}
        cancelled = self.helper('cancel')
        assert cancelled['cancel_owner']==self.node_uid
        def finished():
            current=self.store.read_json(self.jid+'/meta.json')
            return current if current.get('state')=='finished' and not runner_alive(self.store,self.jid) else None
        done = until(finished)
        assert done['outcome']=='cancelled' and done['execution']['owned_cgroup']['status']=='absent'
        assert done['recovery']['status']=='required' and done['exit']==0
        identity = done['execution']['program_identity']
        assert identity['state']=='reported' and identity['source']=='program_self_report', identity
        assert identity['record']['job_id']==self.jid
        assert identity['record']['pid']==done['execution']['child_pid']
        assert identity['record']['identity']['build']['source_revision']=='a'*40
        assert done['execution']['worker_identity']['entry_path']==str(prefix/'job_runtime.py')
        assert done['execution']['service_at_launch'] is None
        assert events(self.cgroup_root)['populated']=='0'
        final = self.helper('final')
        assert not final['runner_alive'] and final['output']=='real-input-payload\n'
        for name,entry in final['ownership'].items():
            assert entry['uid']==self.node_uid and entry['gid']==self.node_gid, (name,entry)
            assert entry['mode']==(0o700 if name=='out' else 0o600), (name,entry)
        assert self.job.stat().st_uid==self.node_uid and stat.S_IMODE(self.job.stat().st_mode)==0o700
        denied = self.helper('job-read',job_identity=True)
        assert len(denied['denied'])==2
        self.report['checks']['node_cancel_terminal_and_output_handback'] = final
        self.report['checks']['job_cannot_reopen_old_output_or_records'] = denied
        before = (self.job/'meta.json').read_bytes()
        rejected = self.helper('terminal-replay')
        self.known_pids.add(rejected['entry_pid'])
        assert rejected['exit'] != 0 and 'job already started or reached a terminal state' in rejected['stderr'], rejected
        assert (self.job/'meta.json').read_bytes() == before and events(self.cgroup_root)['populated']=='0'
        assert not [p for p in self.cgroup_root.iterdir() if p.is_dir()]
        assert (self.job/'out/input-copy.txt').read_text() == final['output']
        self.report['checks']['terminal_same_job_replay_refused'] = rejected
        self.report['passed'] = True

    def cleanup(self):
        # Helpers have actual Popen ownership. The detached privileged worker is
        # allowed to finish after only our cgroup is emptied; never signal its PID.
        for proc in self.helper_handles:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=3)
        super().cleanup()
        errors = self.report['cleanup']['errors']
        if self.sudoers_created:
            try: self.sudoers_path.unlink()
            except OSError as error: errors.append('sudoers cleanup: '+repr(error))
        if self.install_root and not self.report['cleanup']['remaining_known_pids']:
            shutil.rmtree(self.install_root)
        if self.node_created:
            try:
                self.command(['/usr/sbin/userdel',self.node_account])
                try: grp.getgrnam(self.node_account)
                except KeyError: pass
                else: self.command(['/usr/sbin/groupdel',self.node_account])
            except Exception as error: errors.append('node account cleanup: '+repr(error))
        self.report['cleanup'].update(sudoers_removed=bool(self.sudoers_created and not self.sudoers_path.exists()),
            prefix_removed=bool(self.install_root and not self.install_root.exists()))
        if errors: self.report['passed']=False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir',type=Path,required=True)
    args=parser.parse_args()
    fixture=PrivilegedFixture(args.artifact_dir.absolute())
    def interrupted(signum,frame): raise TimeoutError(f'integration interrupted by signal {signum}')
    for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,interrupted)
    signal.alarm(60)
    try: fixture.run()
    except BaseException as error:
        fixture.report['failure']=repr(error)
        (fixture.artifacts/'failure.txt').write_text(traceback.format_exc())
    finally:
        signal.alarm(0)
        fixture.cleanup()
        save(fixture.artifacts/'result.json',fixture.report)
    print(json.dumps(fixture.report,indent=2))
    return 0 if fixture.report['passed'] else 1


if __name__=='__main__':
    sys.exit(main())
