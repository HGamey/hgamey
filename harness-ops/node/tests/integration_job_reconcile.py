#!/usr/bin/env python3
"""One lost privileged worker, read-only domain check, then explicit recovery.

Run only as root in an isolated Linux VM. The node, fixed sudo entry, queue,
cgroup and Unix identities are real. No PF, ADB, public contained submission or
device recovery is involved. All system resources belong to this invocation.
"""
import argparse
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
import traceback

from integration_job_privileged import PrivilegedFixture
from integration_job_cgroup import NODE, events, membership, save, until
from artifact_io import SafeRoot
from job_runtime import runner_alive


WORKLOAD = r'''
import errno,json,os,pathlib,subprocess,sys,time
mode,raw_uid,raw_gid,*rest=sys.argv[1:]
assert os.getuid()==int(raw_uid) and os.getgid()==int(raw_gid)
out=pathlib.Path(os.environ['PF_JOB_OUT'])
def facts():
    return {'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0),
            'uid':os.getuid(),'gid':os.getgid(),
            'cgroup':pathlib.Path('/proc/self/cgroup').read_text()}
def write(name,value):
    (out/name).write_text(json.dumps(value))
if mode=='descendant':
    write('descendant.json',facts())
    deadline=time.monotonic()+90
    while time.monotonic()<deadline: time.sleep(.1)
    write('descendant-deadline.json',{'error':'test failed to reconcile within bounded child lifetime'})
    sys.exit(1)
write('workload-started.json',facts())
if mode=='source':
    raw=(pathlib.Path(os.environ['PF_JOB_IN'])/'input.txt').read_bytes()
    (out/'input-copy.txt').write_bytes(raw)
    write('leader.json',facts())
    child=subprocess.Popen([sys.executable,'-I',__file__,'descendant',raw_uid,raw_gid],
                           stdin=subprocess.DEVNULL,start_new_session=True)
    deadline=time.monotonic()+4
    while not (out/'descendant.json').exists() and time.monotonic()<deadline: time.sleep(.02)
    assert (out/'descendant.json').exists(),'owned child did not report readiness'
    print('RECONCILE_SOURCE_STDOUT',flush=True)
    print('RECONCILE_SOURCE_STDERR',file=sys.stderr,flush=True)
elif mode=='reuse':
    old=pathlib.Path(rest[0])
    try: old.read_bytes()
    except OSError as error:
        assert error.errno in (errno.EACCES,errno.EPERM),repr(error)
        write('reuse.json',dict(facts(),denied=str(old),errno=error.errno))
    else: raise AssertionError('later same-UID command could read prior private evidence')
else: raise AssertionError('unknown workload mode')
'''


NODE_HELPER = r'''
import errno,hashlib,http.client,io,json,os,pathlib,sys,tarfile,threading,time
from http.server import ThreadingHTTPServer
operation,plan_path=sys.argv[1:]
plan=json.loads(pathlib.Path(plan_path).read_text())
control=pathlib.Path(plan['control'])
def answer(value):
    print(json.dumps({'helper_pid':os.getpid(),'helper_uid':os.getuid(),**value}),flush=True)
source=pathlib.Path(plan['jobs_root'])/plan['jid'] if plan.get('jid') else None
if operation=='job-read':
    assert os.getuid()==plan['job_uid']
    denied=[]
    for path in (source/'meta.json',source/'out/input-copy.txt'):
        try: path.read_bytes()
        except OSError as error:
            assert error.errno in (errno.EACCES,errno.EPERM),repr(error)
            denied.append(str(path))
        else: raise AssertionError('job identity reopened sealed history')
    answer({'denied':denied}); sys.exit(0)
assert os.getuid()==plan['node_uid'] and os.getgid()==plan['node_gid']
os.umask(0o077)
sys.path[:0]=[plan['prefix'],plan['node_source']]
from node_service import Jobs,make_handler
from job_dispatch import file_lock
import job_runtime as runtime
jobs=Jobs(plan['node_root'],{'python':('/usr/bin/python3',plan['node_root'])},
          worker_prefix=plan['prefix'])
store=jobs._read_root
def keep(name,value):
    (control/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    return value
def bounded(check,seconds=25):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=check()
        if value: return value
        time.sleep(.05)
    raise AssertionError('bounded reconcile fixture wait expired')
def accident(meta):
    return {key:meta.get(key) for key in ('lost','phase','outcome','exit','recovery')}
def finalize(jid):
    with file_lock(jobs.dispatch.intake_path):
        assert jobs.dispatch.finalize(jobs.dispatch.queue.get(jid)),'terminal storage was not committed'
    return jobs.status(jid)

if operation.startswith('prepare-'):
    role=operation.removeprefix('prepare-')
    assert role in ('source','candidate','successor')
    argv=['-I','@in/workload.py','source' if role=='source' else 'reuse',
          str(plan['job_uid']),str(plan['job_gid'])]
    if role!='source': argv.append(str(source/'out/input-copy.txt'))
    spec={'bin':'python','argv':argv,'label':'isolated reconciliation '+role,
          'execution_policy':'contained','device_access':'none','resources':['fixture-'+role]}
    body=io.BytesIO()
    with tarfile.open(fileobj=body,mode='w:gz') as archive:
        for name,payload,mode in [('workload.py',pathlib.Path(plan['workload_template']).read_bytes(),0o700),
                                  ('input.txt',b'original-reconciliation-output\n',0o600)]:
            member=tarfile.TarInfo(name); member.size=len(payload); member.mode=mode
            archive.addfile(member,io.BytesIO(payload))
    data=body.getvalue()
    digest=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()+b'\0'+data).hexdigest()
    # Only this explicit administrator fixture bypasses public policy admission.
    # Reservation, preparation, resource selection and finalization remain real.
    with file_lock(jobs.dispatch.intake_path):
        row,created=jobs.dispatch.queue.reserve('reconcile-'+role+'-'+plan['nonce'],digest,spec,spec['resources'])
        assert created
        jid=row['job_id']; jobs._prepare(row,data)
        assert jobs.dispatch.queue.ready(jid)['state']=='queued'
        claimed=jobs.dispatch.queue.claim_next(1)
        assert claimed['job_id']==jid and claimed['state']=='claimed',claimed
        assert claimed['host_capacity_release'] is None,claimed
        meta=jobs._read_meta(jid)
        meta.update(phase='starting',recovery={'status':'not_reported','reason':'execution has not reported recovery'})
        meta['execution']['launch_deadline']=time.time()+30
        jobs._write_meta(jid,meta)
    answer({'jid':jid,'row':claimed}); sys.exit(0)
if operation.startswith('launch-'):
    role=operation.removeprefix('launch-'); jid=plan[role+'_jid']
    prepared=store.read_json(jid+'/prepared.json')
    proc=runtime.launch(store,jid,jobs.path(jid),prepared['command'],prepared['cwd'],dict(os.environ),
                        execution_policy='contained',privileged_entry=plan['entry'])
    answer({'launcher_pid':os.getpid(),'worker_handle_pid':proc.pid})
    store.close(); os._exit(0)
if operation.startswith('finalize-'):
    role=operation.removeprefix('finalize-'); jid=plan[role+'_jid']
    bounded(lambda:not runtime.runner_alive(store,jid))
    meta=keep(operation,finalize(jid)); row=jobs.dispatch.queue.get(jid)
    if role=='source':
        assert meta.get('lost') is True and meta['phase']=='interrupted' and meta['outcome']=='interrupted',meta
        assert meta['exit']==-1 and meta['recovery']['status']=='required' and row['state']=='blocked',meta
        assert row['host_capacity_release'] is None and meta['queue']['host_capacity']['state']=='reserved',meta
        keep('original-accident',accident(meta))
    else:
        assert row['host_capacity_release'] is not None and meta['queue']['host_capacity']['state']=='released',meta
    answer({'job':meta,'row':row}); sys.exit(0)
if operation=='final':
    meta=jobs.status(plan['jid'])
    assert accident(meta)==json.loads((control/'original-accident.json').read_text()),meta
    assert jobs.dispatch.queue.get(plan['jid'])['state']=='blocked'
    release=jobs.dispatch.queue.get(plan['jid'])['host_capacity_release']
    assert release['basis']=='owned_cgroup_completed' and meta['queue']['host_capacity']['state']=='released',meta
    assert not runtime.runner_alive(store,plan['jid'])
    assert meta['execution']['reconciliation']['state']=='complete'
    assert meta['execution']['containment']['handoff']['status']=='returned'
    assert meta['execution']['owned_cgroup']['status']=='absent'
    assert (source/'out/input-copy.txt').read_bytes()==b'original-reconciliation-output\n'
    assert jobs.dispatch.queue.capacity_snapshot()['reserved']=={'cpu':0,'gpu':0,'io':0}
    successor=pathlib.Path(plan['jobs_root'])/plan['successor_jid']
    answer({'job':meta,'row':jobs.dispatch.queue.get(plan['jid']),
            'raw_output':(source/'out/input-copy.txt').read_text(),
            'reuse':json.loads((successor/'out/reuse.json').read_text())}); sys.exit(0)

assert operation in ('probe','force')
token='isolated-reconcile-http-token'
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(None,jobs,token))
server.daemon_threads=True
thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
def request(method,path,value=None):
    headers={'Authorization':'Bearer '+token,'Connection':'close'}
    body=None if value is None else json.dumps(value).encode()
    if body is not None: headers['Content-Type']='application/json'
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=8)
    try:
        conn.request(method,path,body=body,headers=headers)
        reply=conn.getresponse(); raw=reply.read(); code=reply.status
    finally: conn.close()
    result=json.loads(raw)
    with (control/'http.jsonl').open('a') as log:
        log.write(json.dumps({'method':method,'path':path,'body':value,'status':code,'response':result})+'\n')
    return code,result
try:
    code,node=request('GET','/node')
    assert code==200 and 'jobs.execution-reconcile-v1' in node['capabilities'],(code,node)
    assert node['execution']['process_policies']['contained']['available'] is False,node
    assert node['execution']['host_capacity']['slots']=={'cpu':2,'gpu':0,'io':0},node
    route='/jobs/'+plan['jid']
    force=operation=='force'
    code,submitted=request('POST',route+'/execution/reconcile',{'force':force})
    keep(operation+'-accepted',submitted)
    assert code==200 and submitted['accepted'] is True,(code,submitted)
    def settled():
        code,meta=request('GET',route)
        assert code==200,(code,meta)
        state=(meta.get('execution',{}).get('reconciliation') or {}).get('state')
        return meta if state in ('blocked','complete','failed') and not runtime.runner_alive(store,plan['jid']) else None
    observed=keep(operation+'-observed',bounded(settled))
    assert observed['execution']['reconciliation']['state']==('complete' if force else 'blocked'),observed
    meta=keep(operation+'-committed',finalize(plan['jid']))
    assert accident(meta)==json.loads((control/'original-accident.json').read_text()),meta
    assert jobs.dispatch.queue.get(plan['jid'])['state']=='blocked'
    if not force:
        code,rejected=request('POST',route+'/queue-release',{'note':'isolated fixture: live domain has not been recovered'})
        assert code==409,(code,rejected)
        assert not (source/'queue-release.json').exists()
        assert meta['execution']['containment']['handoff']['status']=='pending'
        assert jobs.dispatch.queue.get(plan['jid'])['host_capacity_release'] is None
        assert meta['queue']['host_capacity']['state']=='reserved',meta
    else:
        assert meta['execution']['owned_cgroup']['status']=='absent'
        assert meta['execution']['containment']['handoff']['status']=='returned'
        assert jobs.dispatch.queue.get(plan['jid'])['host_capacity_release']['basis']=='owned_cgroup_completed'
        assert meta['queue']['host_capacity']['state']=='released',meta
        assert not (source/'queue-release.json').exists()
        assert (source/'out/input-copy.txt').read_bytes()==b'original-reconciliation-output\n'
    answer({'job':meta,'row':jobs.dispatch.queue.get(plan['jid'])})
finally:
    server.shutdown(); server.server_close(); thread.join(timeout=2)
    # The service's finite finalizer may still finish its same-row storage
    # commit. Leave its shared SafeRoot open until this helper process exits.
'''


class ReconcileFixture(PrivilegedFixture):
    def __init__(self, artifact_dir):
        super().__init__(artifact_dir)
        self.report['scope']='one real Linux worker loss and explicit host-domain reconciliation; no device recovery or reboot'
        self.jobs = {}

    def helper(self, operation, *, job_identity=False):
        uid,gid=(self.uid,self.gid) if job_identity else (self.node_uid,self.node_gid)
        proc=subprocess.Popen(['/usr/bin/python3','-I',str(self.scratch/'node-helper.py'),
                               operation,str(self.scratch/'node-plan.json')],
            user=uid,group=gid,extra_groups=[],cwd='/',env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.helper_handles.append(proc); self.known_pids.add(proc.pid)
        try:
            stdout,stderr=proc.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            proc.kill(); stdout,stderr=proc.communicate(timeout=3)
            (self.artifacts/(operation+'.stdout')).write_text(stdout)
            (self.artifacts/(operation+'.stderr')).write_text(stderr)
            raise
        (self.artifacts/(operation+'.stdout')).write_text(stdout)
        (self.artifacts/(operation+'.stderr')).write_text(stderr)
        assert proc.returncode==0,(operation,proc.returncode,stderr)
        value=json.loads(stdout); assert value['helper_uid']==uid,value
        return value

    def setup(self):
        if sys.platform!='linux' or os.geteuid()!=0:
            raise RuntimeError('isolated Linux root integration required; no skip')
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise RuntimeError('real kernel pidfd ownership is required; no PID fallback')
        mount=Path('/sys/fs/cgroup')
        assert any(line.split()[4]==str(mount) and line.split(' - ')[1].split()[0]=='cgroup2'
                   for line in Path('/proc/self/mountinfo').read_text().splitlines() if ' - ' in line)
        self.cgroup_root=mount/('phonefarm-reconcile-'+self.nonce)
        self.cgroup_root.mkdir(mode=0o755); self.cgroup_created=True
        assert all((self.cgroup_root/name).exists() for name in ('cgroup.events','cgroup.procs','cgroup.kill'))
        assert events(self.cgroup_root)['populated']=='0'
        for name in (self.node_account,self.username):
            try: pwd.getpwnam(name)
            except KeyError: pass
            else: raise RuntimeError('fixture account already exists')
            self.command(['/usr/sbin/useradd','--system','--user-group','--no-create-home',
                          '--shell','/usr/sbin/nologin',name])
            if name==self.node_account: self.node_created=True
            else: self.account_created=True
        node,job=pwd.getpwnam(self.node_account),pwd.getpwnam(self.username)
        self.node_uid,self.node_gid=node.pw_uid,node.pw_gid
        self.uid,self.gid=job.pw_uid,job.pw_gid
        assert min(self.node_uid,self.node_gid,self.uid,self.gid)>0 and self.node_uid!=self.uid and self.node_gid!=self.gid
        self.report['identity']={'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,'job_gid':self.gid}
        self.scratch=Path(tempfile.mkdtemp(prefix='phonefarm-reconcile-',dir='/tmp')); self.scratch.chmod(0o755)
        node_root=self.scratch/'node'; node_root.mkdir(mode=0o755); os.chown(node_root,self.node_uid,self.node_gid)
        jobs_root=node_root/'_jobs'; jobs_root.mkdir(mode=0o755); os.chown(jobs_root,self.node_uid,self.node_gid)
        control=self.scratch/'control'; control.mkdir(mode=0o700); os.chown(control,self.node_uid,self.node_gid)
        # Runner checkout ancestors may be private. Copy only actual source,
        # never chmod public ancestors or substitute node/worker behavior.
        self.install_root=Path(tempfile.mkdtemp(prefix='phonefarm-reconcile-',dir='/')); self.install_root.chmod(0o755)
        node_source=self.install_root/'node-source'; node_source.mkdir(mode=0o755)
        for path in NODE.glob('*.py'):
            dest=node_source/path.name; shutil.copyfile(path,dest); dest.chmod(0o644)
        prefix=self.install_root/'worker'
        self.command(['/usr/bin/python3',str(node_source/'install_worker.py'),'--prefix',str(prefix),
                      '--jobs-root',str(jobs_root),'--cgroup-root',str(self.cgroup_root),
                      '--node-uid',str(self.node_uid),'--node-gid',str(self.node_gid),
                      '--job-uid',str(self.uid),'--job-gid',str(self.gid)])
        installed=json.loads(self.report['commands'][-1]['stdout'])
        assert installed['entry']==str(prefix/'phonefarm-worker') and installed['config']==str(prefix/'worker.json')
        config=(prefix/'worker.json').stat()
        assert (config.st_uid,config.st_gid,stat.S_IMODE(config.st_mode))==(0,self.node_gid,0o640)
        assert not json.loads((prefix/'worker.json').read_text()).get('pf_env')
        save(self.artifacts/'installation.json',installed)
        self.sudoers_path=Path('/etc/sudoers.d')/('phonefarm-reconcile-'+self.nonce)
        fd=os.open(self.sudoers_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o440); self.sudoers_created=True
        with os.fdopen(fd,'w') as stream: stream.write(installed['sudoers'].rstrip()+'\n')
        self.command(['/usr/sbin/visudo','-cf',str(self.sudoers_path)])
        for name,content in [('workload.py',WORKLOAD),('node-helper.py',NODE_HELPER)]:
            (self.scratch/name).write_text(content); (self.scratch/name).chmod(0o644)
        self.plan={'nonce':self.nonce,'prefix':str(prefix),'entry':installed['entry'],
                   'node_source':str(node_source),'node_root':str(node_root),'jobs_root':str(jobs_root),
                   'control':str(control),'workload_template':str(self.scratch/'workload.py'),
                   'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,'job_gid':self.gid}
        save(self.scratch/'node-plan.json',self.plan)
        self.store=SafeRoot(jobs_root)

    def prepare_launch(self, role):
        reserved=self.helper('prepare-'+role)
        jid=reserved['jid']; self.jobs[role]=jid; self.plan[role+'_jid']=jid
        if role=='source': self.jid=jid; self.plan['jid']=jid
        save(self.scratch/'node-plan.json',self.plan)
        launched=self.helper('launch-'+role)
        self.known_pids.add(launched['worker_handle_pid'])
        assert not Path('/proc/'+str(launched['launcher_pid'])).exists()
        self.report['checks'][role+'_actual_queue_launch']={'reservation':reserved,'launch':launched}
        return jid

    def observe(self, jid):
        meta=self.store.read_json(jid+'/meta.json')
        execution=meta.get('execution') or {}
        for pid in (meta.get('pid'),execution.get('child_pid')):
            if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
        return meta

    def terminal(self, role):
        jid=self.jobs[role]
        def done():
            meta=self.observe(jid)
            return meta if meta.get('state')=='finished' and not runner_alive(self.store,jid) else None
        raw=until(done,seconds=15)
        save(self.artifacts/(role+'-raw-terminal.json'),raw)
        return self.helper('finalize-'+role)

    def run(self):
        self.setup()
        jid=self.prepare_launch('source')
        source=Path(self.plan['jobs_root'])/jid
        def collecting():
            meta=self.observe(jid)
            assert meta.get('state')!='finished',meta
            return meta if meta.get('phase')=='collecting' else None
        meta=until(collecting)
        save(self.artifacts/'source-collecting.json',meta)
        assert meta['execution']['returncode']==0 and meta.get('exit') is None
        assert runner_alive(self.store,jid)
        assert meta['execution']['owned_cgroup']['status']=='present'
        containment=meta['execution']['containment']
        assert containment['schema']=='phonefarm/job-containment@1' and containment['handoff']['status']=='pending'
        assert (containment['uid'],containment['gid'],containment['node_uid'],containment['node_gid'])==(
            self.uid,self.gid,self.node_uid,self.node_gid)
        domain=Path(meta['execution']['owned_cgroup']['path'])
        assert domain.parent==self.cgroup_root and domain.name=='job-'+jid
        assert (domain.stat().st_ino,domain.stat().st_dev)==(containment['inode'],containment['dev'])
        leader=json.loads((source/'out/leader.json').read_text())
        child=json.loads((source/'out/descendant.json').read_text())
        self.known_pids.update((leader['pid'],child['pid']))
        assert child['pid']==child['sid']==child['pgid'] and child['pgid']!=leader['pgid']
        assert membership(child['pid'])=='/'+str(domain.relative_to('/sys/fs/cgroup'))
        raw=(source/'out/input-copy.txt').read_bytes()
        assert raw==b'original-reconciliation-output\n'
        log=(source/'log').read_text()
        assert 'RECONCILE_SOURCE_STDOUT' in log and 'RECONCILE_SOURCE_STDERR' in log
        # Bind a kernel handle to the currently verified outside worker. This
        # deliberately kills our worker, never a historical PID or process group.
        worker_pid=meta['pid']; handle=os.pidfd_open(worker_pid)
        try:
            info=Path('/proc/'+str(worker_pid))
            before=(info/'stat').read_text().rsplit(')',1)[1].split()[19]
            argv=(info/'cmdline').read_bytes().split(b'\0')
            assert self.plan['entry'].encode() in argv and jid.encode() in argv,argv
            assert info.stat().st_uid==0 and membership(worker_pid)!=membership(child['pid'])
            assert self.observe(jid)['pid']==worker_pid and runner_alive(self.store,jid)
            assert (info/'stat').read_text().rsplit(')',1)[1].split()[19]==before
            signal.pidfd_send_signal(handle,signal.SIGKILL)
        finally: os.close(handle)
        until(lambda:not runner_alive(self.store,jid))
        assert events(domain)['populated']=='1' and Path('/proc/'+str(child['pid'])).exists()
        self.report['checks']['worker_lost_descendant_retained']={
            'worker_pid':worker_pid,'worker_start':before,'leader':leader,'descendant':child,
            'domain_events':events(domain),'lost':self.helper('finalize-source')}
        probed=self.helper('probe')
        assert Path('/proc/'+str(child['pid'])).exists() and events(domain)['populated']=='1'
        assert membership(child['pid'])=='/'+str(domain.relative_to('/sys/fs/cgroup'))
        assert (source/'out').stat().st_uid==self.uid
        assert not (source/'out/descendant-deadline.json').exists()
        self.report['checks']['plain_http_check_kept_descendant_and_blocked_release']=probed
        candidate=self.prepare_launch('candidate')
        refused=self.terminal('candidate'); candidate_job=Path(self.plan['jobs_root'])/candidate
        assert refused['job']['outcome']=='failed' and refused['job']['recovery']['status']=='not_needed',refused
        assert refused['job']['execution'].get('child_pid') is None
        assert refused['job']['execution'].get('containment') is None
        assert refused['row']['host_capacity_release']['basis']=='not_started',refused
        assert refused['job']['queue']['host_capacity']['state']=='released',refused
        assert not (candidate_job/'out/workload-started.json').exists()
        assert all(path.stat().st_uid==self.node_uid and stat.S_IMODE(path.stat().st_mode)==0o700
                   for path in (candidate_job,candidate_job/'out'))
        assert not (self.cgroup_root/('job-'+candidate)).exists()
        assert events(domain)['populated']=='1'
        self.report['checks']['same_uid_start_refused_before_workload']=refused
        forced=self.helper('force')
        assert events(self.cgroup_root)['populated']=='0' and not domain.exists()
        until(lambda:not Path('/proc/'+str(child['pid'])).exists())
        assert (source/'out/input-copy.txt').read_bytes()==raw
        assert not (source/'out/descendant-deadline.json').exists()
        for path,mode in [(source,0o700),(source/'out',0o700),(source/'out/input-copy.txt',0o600)]:
            info=path.stat()
            assert (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))==(self.node_uid,self.node_gid,mode)
        self.report['checks']['force_http_empty_domain_and_handback_preserved_accident']=forced
        self.report['checks']['sealed_history_denied_to_job_uid']=self.helper('job-read',job_identity=True)
        self.prepare_launch('successor')
        successor=self.terminal('successor')
        assert successor['job']['outcome']=='succeeded' and successor['job']['exit']==0,successor
        assert successor['job']['execution']['containment']['handoff']['status']=='returned'
        assert successor['row']['state']=='done'
        assert successor['row']['host_capacity_release']['basis']=='owned_cgroup_completed',successor
        assert successor['job']['queue']['host_capacity']['state']=='released',successor
        final=self.helper('final')
        assert final['reuse']['uid']==self.uid and final['reuse']['gid']==self.gid
        self.known_pids.add(final['reuse']['pid'])
        assert events(self.cgroup_root)['populated']=='0'
        assert not [path for path in self.cgroup_root.iterdir() if path.is_dir()]
        self.report['checks']['same_uid_successor_cannot_read_old_output']=successor
        self.report['checks']['source_accident_and_queue_block_preserved']=final
        self.report['actual_jobs']={'source_executed':jid,'candidate_refused':candidate,
                                    'successor_executed':self.jobs['successor']}
        self.report['passed']=True

    def cleanup(self):
        # Include identities observed on an early failure, without signaling
        # numbers recovered from records. Base cleanup acts only on our handles
        # and newly created cgroup, then preserves the complete raw data tree.
        if self.store:
            for jid in self.jobs.values():
                try: self.observe(jid)
                except (OSError,ValueError): pass
                for name in ('leader.json','descendant.json','reuse.json'):
                    try:
                        value=self.store.read_json(jid+'/out/'+name)
                        pid=value.get('pid')
                        if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
                    except (OSError,ValueError): pass
        super().cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir',type=Path,required=True)
    args=parser.parse_args()
    fixture=ReconcileFixture(args.artifact_dir.absolute())
    def interrupted(signum,frame): raise TimeoutError(f'integration interrupted by signal {signum}')
    for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,interrupted)
    signal.alarm(120)
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
