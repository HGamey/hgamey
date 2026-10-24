#!/usr/bin/env python3
"""One contained battery session, failed typed recovery, then successful recovery.

Explicit isolated Linux/root integration only. The PF, node, installed worker,
queue, Unix permissions and cgroup are real; all Android responses are fake.
No public contained admission or production device/lease is used.
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

from integration_pf_shared import SharedFixture, FAKE_ADB, PKG
from integration_job_cgroup import NODE, events, save
from artifact_io import SafeRoot
from pf_recovery_fake import compose


CHARGE_NODE = '/sys/class/power_supply/battery/input_suspend'

NODE_HELPER = r'''
import errno,hashlib,http.client,io,json,os,pathlib,stat,sys,tarfile,threading,time
from http.server import ThreadingHTTPServer
from urllib.parse import quote
operation, plan_path = sys.argv[1:]
plan=json.loads(pathlib.Path(plan_path).read_text())
control=pathlib.Path(plan['control'])
def answer(value):
    print(json.dumps({'helper_pid':os.getpid(),'helper_uid':os.getuid(),**value}),flush=True)
job=pathlib.Path(plan['jobs_root'])/plan['jid'] if plan.get('jid') else None
if operation=='job-read':
    assert os.getuid()==plan['job_uid']
    denied=[]
    for path in (job/'meta.json',job/'out/session-control.json',job/'out'):
        try:
            list(path.iterdir()) if path.suffix=='' else path.read_bytes()
        except OSError as error:
            assert error.errno in (errno.EACCES,errno.EPERM),repr(error)
            denied.append(str(path))
        else: raise AssertionError('job identity can reopen sealed history: '+str(path))
    answer({'denied':denied}); sys.exit(0)
assert os.getuid()==plan['node_uid'] and plan['job_gid'] in os.getgroups()
os.umask(0o077)
sys.path[:0]=[plan['prefix'],plan['node_source']]
from node_service import Jobs,make_handler
from node_lease import job_lease_receipts
from job_dispatch import file_lock
from worker_config import node_environment
import job_runtime as runtime
env=node_environment(plan['prefix'],jobs_root=plan['jobs_root'])
assert env['PHONEFARM_SHARED_GID']==str(plan['job_gid'])
assert env['PHONEFARM_STATE_DIR']==plan['state_root']
assert env['DEVLOCK_DIR']==plan['lock_root'] and env['PHONEFARM_LEASE_DIR']==plan['device_lease_root']
assert env['ADB_BIN']==plan['fake_adb'] and env['DEVLOCK_BIN']==plan['devlock']
jobs=Jobs(plan['node_root'],{'phonefarm':(plan['phonefarm'],plan['node_root'])},worker_prefix=plan['prefix'])
store=jobs._read_root
def keep(name,value):
    (control/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    return value
def bounded(check,seconds=45):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=check()
        if value: return value
        time.sleep(.1)
    raise AssertionError('bounded recovery fixture wait expired')
def lease():
    value=jobs.leases.snapshot(refresh=True,force=True)
    assert value['storage']['readiness']=='ready',value
    assert value['storage']['devlock_dir']==plan['lock_root'],value
    assert value['storage']['device_lease_dir']==plan['device_lease_root'],value
    return value
def ownership(path):
    value=path.stat()
    return {'uid':value.st_uid,'gid':value.st_gid,'mode':stat.S_IMODE(value.st_mode)}
def terminal(jid):
    meta=jobs._read_meta(jid)
    keep('observed-'+jid,meta)
    if meta.get('state')!='finished' or runtime.runner_alive(store,jid): return None
    with file_lock(jobs.dispatch.intake_path):
        row=jobs.dispatch.queue.get(jid)
        assert jobs.dispatch.finalize(row),'could not commit actual worker terminal records'
    return jobs.status(jid)

if operation=='prepare':
    argv=['gray-layer','session','--mode','frameaudit','--serial',plan['serial'],
          '--package',plan['package'],'--out','@out','--so','@in/synthetic.so',
          '--actor-id','agent','--max-seconds','90','--power-policy','battery',
          '--evidence-origin','synthetic','--lock-wait','0s','--json']
    spec={'bin':'phonefarm','argv':argv,'label':'isolated battery recovery source',
          'execution_policy':'contained','device_access':'write','resources':['fixture-device:'+plan['serial']]}
    body=io.BytesIO()
    with tarfile.open(fileobj=body,mode='w:gz') as archive:
        payload=b'explicit synthetic SO fixture'
        member=tarfile.TarInfo('synthetic.so'); member.size=len(payload); member.mode=0o600
        archive.addfile(member,io.BytesIO(payload))
    data=body.getvalue()
    digest=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()+b'\0'+data).hexdigest()
    # This administrator fixture cannot use public contained submission. Its
    # source reservation/preparation/claim are genuine, not a fake blocked row.
    with file_lock(jobs.dispatch.intake_path):
        row,created=jobs.dispatch.queue.reserve('pf-recovery-source-'+plan['nonce'],digest,spec,spec['resources'])
        assert created
        jid=row['job_id']; jobs._prepare(row,data)
        assert jobs.dispatch.queue.ready(jid)['state']=='queued'
        claimed=jobs.dispatch.queue.claim_next(1)
        assert claimed['job_id']==jid and claimed['state']=='claimed'
        meta=jobs._read_meta(jid)
        meta.update(phase='starting',recovery={'status':'not_reported','reason':'execution has not reported recovery'})
        meta['execution']['launch_deadline']=time.time()+30
        jobs._write_meta(jid,meta)
    assert not list((pathlib.Path(jobs.path(jid))/'out').iterdir())
    current=lease(); assert current['status']=='clear',current
    answer({'jid':jid,'row':claimed,'lease':current}); sys.exit(0)
if operation=='launch':
    prepared=store.read_json(plan['jid']+'/prepared.json')
    process=runtime.launch(store,plan['jid'],str(job),prepared['command'],prepared['cwd'],
        dict(os.environ,PF_NODE_TOKEN='fixture-secret-must-not-reach-job'),
        execution_policy='contained',privileged_entry=plan['entry'])
    value={'launcher_pid':os.getpid(),'worker_handle_pid':process.pid,'argv':prepared['command']}
    keep('launch',value); answer(value); store.close(); os._exit(0)
if operation=='cleanup':
    rows=jobs.dispatch.queue.rows(('preparing','queued','claimed','blocked','done'))
    requested=[]
    for row in rows:
        jid=row['job_id']
        if runtime.runner_alive(store,jid):
            requested.append(jid); jobs.cancel(jid,force=True)
    bounded(lambda: all(not runtime.runner_alive(store,r['job_id']) for r in rows),seconds=40)
    answer({'cancel_requested':requested,'runner_locks_released':True}); sys.exit(0)

token='isolated-recovery-http-token'
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(None,jobs,token))
server.daemon_threads=True
thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
def request(method,path,value=None,request_id=None):
    body=None if value is None else json.dumps(value).encode()
    headers={'Authorization':'Bearer '+token,'Connection':'close'}
    if value is not None: headers['Content-Type']='application/json'
    if request_id: headers['X-Request-ID']=request_id
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=8)
    try:
        conn.request(method,path,body=body,headers=headers)
        reply=conn.getresponse(); raw=reply.read(); status=reply.status
    finally: conn.close()
    result=json.loads(raw)
    with (control/'http.jsonl').open('a') as log:
        log.write(json.dumps({'method':method,'path':path,'request_id':request_id,'body':value,
                              'status':status,'response':result})+'\n')
    return status,result
def get(path):
    code,value=request('GET',path); assert code==200,(code,value)
    return value
session_url='/jobs/'+plan['jid']+'/session'
def ready():
    meta=jobs._read_meta(plan['jid'])
    assert meta.get('state')!='finished',meta
    code,value=request('GET',session_url)
    if code==404: return None
    assert code==200,(code,value)
    assert value.get('phase')!='recovery_required',value
    return value if value.get('phase')=='ready' and value.get('live_owner') else None
def action(descriptor,rid,name,params,expected='completed'):
    msg={'schema':'phonefarm/gray-control-action@1','request_id':rid,'session_id':descriptor['session_id'],
         'actor_id':descriptor['controller']['actor_id'],'control_epoch':descriptor['controller']['epoch'],
         'action':name,'params':params}
    code,published=request('POST',session_url+'/actions',msg)
    assert code==200,(code,published)
    keep(rid+'-published',published)
    def done():
        value=get(session_url+'/actions/'+rid)
        return value if value.get('status') in ('completed','failed','unknown','not_executed') else None
    value=keep(rid,bounded(done))
    assert value['status']==expected,value
    return value
def preserve(paths,name='preserved-originals'):
    directory=control/('negative-originals' if name=='preserved-originals' else name)
    directory.mkdir()
    result=[]
    for index,path in enumerate(sorted(set(paths))):
        dest=directory/(str(index)+'-'+path.name)
        dest.write_bytes(path.read_bytes())
        result.append({'source':str(path),'copy':str(dest)})
    return keep(name,result)
try:
    capabilities=get('/node')['capabilities']
    assert 'jobs.session-recovery-v1' in capabilities,capabilities
    assert 'jobs.session-recovery-chain-v1' in capabilities,capabilities
    if operation=='source':
        descriptor=keep('ready',bounded(ready))
        assert descriptor['power']['policy']=='battery'
        live=keep('source-active-lease',lease())
        assert live['status'] in ('held','blocked') and live['bridge_held'] is True,live
        live_receipts=keep('source-active-receipts',job_lease_receipts(store,plan['jid']))
        assert live_receipts['status']=='recorded' and len(live_receipts['entries'])==1,live_receipts
        sample=action(descriptor,'battery-sample','sample',{'seconds':1})
        result=sample['result']
        assert result['collection']['status']=='complete' and result['requested_seconds']==1,result
        assert result['resource_health']['status']=='ready' and result['power']['policy']=='battery'
        assert all(result['power'][when]['on_battery'] is True and
                   result['power'][when]['control_modified_by_session'] is True for when in ('before','after'))
        assert result['metrics']['energy_per_frame_mj'] is None
        descriptor=bounded(ready)
        stopped=action(descriptor,'battery-stop','stop',{},expected='failed')
        done=keep('source-terminal',bounded(lambda:terminal(plan['jid'])))
        assert done['state']=='finished' and done['exit']==2 and done['outcome']=='failed',done
        assert done['execution']['owned_cgroup']['status']=='absent',done
        assert jobs.dispatch.queue.get(plan['jid'])['state']=='blocked'
        descriptor=keep('source-required',get(session_url))
        assert descriptor['phase']=='recovery_required' and not descriptor['live_owner']
        assert descriptor['power']['stage']=='restoring' and descriptor['recovery']['status']=='required'
        session=job/'out'/descriptor['session_path']
        parent=json.loads((session/'eval-session.json').read_text())
        base=json.loads((session/'session.json').read_text())
        assert parent['schema']=='phonefarm/gray-control-resources@3' and len(parent['rounds'])==1
        assert parent['pending'] is None and parent['charging']['stage']=='restoring'
        assert base['write_pending'] is None and base['transport_uncertain'] is False
        record=pathlib.Path(parent['charging']['record_path'])
        assert record.parent==pathlib.Path(plan['state_root'])
        # This read is genuinely performed as node UID after output handback;
        # the global record remains owned by the old, distinct job UID.
        raw=record.read_bytes(); binding=pathlib.Path(parent['charging']['binding']['record_evidence'])
        assert raw==binding.read_bytes()
        assert ownership(record)=={'uid':plan['job_uid'],'gid':plan['job_gid'],'mode':0o640}
        (control/'charge-before.raw').write_bytes(raw)
        keep('charge-before',{'path':str(record),'binding':str(binding),'ownership':ownership(record),'read_as_uid':os.getuid()})
        # Public recovery references and the sample originals are immutable;
        # live manifests/descriptor legitimately advance during recovery.
        paths=[binding]+[job/'out'/p for p in descriptor['recovery']['reports']]
        paths += [p for p in pathlib.Path(parent['rounds'][0]['out']).iterdir() if p.is_file()]
        preserved=preserve(paths)
        downloaded=get('/jobs/'+plan['jid']+'/out?path='+quote(result['path'],safe=''))
        assert downloaded==json.loads((job/'out'/result['path']).read_text())
        receipts=keep('source-required-receipts',job_lease_receipts(store,plan['jid']))
        assert receipts['status']=='recorded' and receipts['entries'][0]['recovery']['status']=='required'
        current=keep('source-retained-lease',lease())
        assert current['status']=='blocked',current
        assert ownership(job)=={'uid':plan['node_uid'],'gid':plan['node_gid'],'mode':0o700}
        assert ownership(job/'out')['mode']==0o700 and ownership(session/'session.json')['mode']==0o600
        answer({'sample':sample,'stop':stopped,'meta':done,'descriptor':descriptor,'receipts':receipts,
                'lease':current,'preserved_count':len(preserved),'global_record':ownership(record)})
    elif operation in ('recover-fail','recover'):
        failing=operation=='recover-fail'
        name='failed-recovery' if failing else 'recovery'
        previous=None if failing else json.loads((control/'failed-recovery-id.json').read_text())
        target=plan['jid'] if failing else previous['id']
        endpoint='/jobs/'+target+'/session/recover'
        before=keep('before-'+name+'-lease',lease()); assert before['status']=='blocked',before
        saved=json.loads((control/'source-required.json').read_text())
        proposed=keep(name+'-plan',get(session_url+'/recovery'))
        assert proposed['supported'] and proposed['eligible'],proposed
        assert proposed['source_job_id']==plan['jid'] and proposed['target_job_id']==target,proposed
        assert proposed['session_id']==saved['session_id'] and proposed['serial']==plan['serial']
        assert proposed['session_path']==saved['session_path']
        assert [item['job_id'] for item in proposed['attempts']]==([] if failing else [target]),proposed
        if not failing:
            from_leaf=get('/jobs/'+target+'/session/recovery')
            assert from_leaf['job_id']==target and from_leaf['source_job_id']==plan['jid']
            assert from_leaf['eligible'] and from_leaf['target_job_id']==target
        request_id='pf-recovery-'+('r1-' if failing else 'r2-')+plan['nonce']
        code,submitted=request('POST',endpoint,{},request_id)
        keep(name+'-submitted',submitted); assert code==200,(code,submitted)
        jid=submitted['id']; assert jid!=plan['jid']
        keep(name+'-id',{'id':jid,'request_id':request_id,'target_job_id':target})
        code,repeated=request('POST',endpoint,{},request_id)
        assert code==200 and repeated['id']==jid,(code,repeated)
        done=keep(name+'-terminal',bounded(lambda:terminal(jid),seconds=60))
        assert done['state']=='finished' and done['exit']==(2 if failing else 0),done
        assert done['outcome']==('failed' if failing else 'succeeded'),done
        assert done['execution_policy']=='process_group' and done['device_access']=='recovery'
        assert done['recovery_for']==target
        # The ordinary worker reports process completion, not aggregate PF recovery.
        assert done['recovery']['status']=='not_reported',done
        assert done['queue']['state']=='blocked' and jobs.dispatch.queue.get(jid)['state']=='blocked',done
        assert done['execution']['process_management']['completion_scope']=='original_process_group'
        assert done['execution']['owned_process_group']['status']=='absent'
        launch=store.read_json(jid+'/launch.json')
        assert launch['execution_policy']=='process_group'
        command=launch['command']
        assert 'session-recover' in command and command[command.index('--serial')+1]==plan['serial']
        assert command[command.index('--session')+1]==str(job/'out'/saved['session_path'])
        descriptor=keep(name+'-source',get(session_url))
        assert descriptor['job_id']==plan['jid'] and descriptor['session_id']==saved['session_id']
        assert descriptor['session_path']==saved['session_path'] and not descriptor['live_owner']
        info=json.loads((control/'charge-before.json').read_text())
        assert pathlib.Path(info['binding']).read_bytes()==(control/'charge-before.raw').read_bytes()
        for original in json.loads((control/'preserved-originals.json').read_text()):
            assert pathlib.Path(original['source']).read_bytes()==pathlib.Path(original['copy']).read_bytes(),original
        old_stop=get(session_url+'/actions/battery-stop')
        assert old_stop==json.loads((control/'battery-stop.json').read_text()),'original failed action was rewritten'
        assert jobs._read_meta(plan['jid'])['exit']==2 and jobs.dispatch.queue.get(plan['jid'])['state']=='blocked'
        receipts=keep(name+'-receipts',job_lease_receipts(store,jid))
        assert receipts['status']=='recorded' and len(receipts['entries'])==1,receipts
        entry=receipts['entries'][0]
        old=json.loads((control/('source-required-receipts.json' if failing else 'failed-recovery-receipts.json')).read_text())['entries'][0]
        assert entry['job_id']==jid and entry['lease_id']!=old['lease_id'],entry
        assert entry['state']==('retained' if failing else 'released'),entry
        assert entry['recovery']['status']==('required' if failing else 'reported_complete'),entry
        assert entry['serial']==plan['serial'] and entry['recovery']['context']==old['recovery']['context']
        # This R1 failed after acquiring the lease. The receipt's actual prior
        # marker therefore belongs to R1 for R2; do not derive it from queue ancestry.
        assert entry['recovery_of']=={'job_id':old['job_id'],'lease_id':old['lease_id']},entry
        out=pathlib.Path(jobs.path(jid,'out'))
        for directory in (out,out/'lease-receipts'):
            assert ownership(directory)=={'uid':plan['node_uid'],'gid':plan['job_gid'],'mode':0o2770}
        receipt_path=out/entry['artifact_path']
        assert ownership(receipt_path)=={'uid':plan['node_uid'],'gid':plan['job_gid'],'mode':0o640}
        downloaded=get('/jobs/'+jid+'/out?path='+quote(entry['artifact_path'],safe=''))
        assert downloaded==json.loads(receipt_path.read_text())
        current=keep(name+'-lease',lease())
        assert ownership(job)['mode']==0o700 and ownership(job/'out')['mode']==0o700
        assert ownership(job/'out'/saved['session_path']/'session.json')['mode']==0o600
        code,repeated=request('POST',endpoint,{},request_id)
        assert code==200 and repeated['id']==jid,(code,repeated)
        rows=jobs.dispatch.queue.rows(('preparing','queued','claimed','blocked','done'))
        assert len(rows)==(2 if failing else 3) and all(r['state']=='blocked' for r in rows),rows
        releases=[]
        if failing:
            assert descriptor['phase']=='recovery_required' and descriptor['recovery']['status']=='required',descriptor
            assert descriptor['power']['stage']=='restoring' and current['status']=='blocked'
            assert pathlib.Path(info['path']).read_bytes()==(control/'charge-before.raw').read_bytes()
            next_plan=keep('after-failed-recovery-plan',get(session_url+'/recovery'))
            assert next_plan['eligible'] and next_plan['target_job_id']==jid,next_plan
            preserve([job/'out'/p for p in descriptor['recovery']['reports']]+
                     [pathlib.Path(jobs.path(jid,'log')),receipt_path],name='failed-recovery-originals')
        else:
            assert descriptor['phase']=='closed' and descriptor['recovery']['status']=='reported_complete',descriptor
            assert descriptor['power']['stage']=='restored'
            assert current['status']=='clear' and current['bridge_held'] is False,current
            assert not pathlib.Path(info['path']).exists()
            keep('charge-after',{'path':info['path'],'present':False,'binding_bytes_preserved':True})
            for original in json.loads((control/'failed-recovery-originals.json').read_text()):
                assert pathlib.Path(original['source']).read_bytes()==pathlib.Path(original['copy']).read_bytes(),original
            first=jobs.status(previous['id'])
            assert first['exit']==2 and first['outcome']=='failed' and first['recovery']['status']=='not_reported',first
            code,original=request('POST',session_url+'/recover',{},previous['request_id'])
            assert code==200 and original['id']==previous['id'],(code,original)
            closed_plan=keep('closed-recovery-plan',get(session_url+'/recovery'))
            assert closed_plan['supported'] and not closed_plan['eligible'] and closed_plan['reason'],closed_plan
            assert closed_plan['source_job_id']==plan['jid'] and closed_plan['target_job_id']==jid
            assert [item['job_id'] for item in closed_plan['attempts']]==[previous['id'],jid]
            assert all(item['queue_state']=='blocked' for item in closed_plan['attempts'])
            # Scoped PF success and a fresh clear lease are evidence for explicit
            # review, not automatic release. Preserve every attempt's failure.
            for release_id in (jid,previous['id'],plan['jid']):
                code,released=request('POST','/jobs/'+release_id+'/queue-release',
                    {'note':'Reviewed actual fixture receipts, original process completion, and fresh clear synthetic lease; not a real-device claim'})
                assert code==200 and released['released'] is True,(code,released)
                assert released['job']['queue']['state']=='done'
                releases.append(release_id)
            keep('explicit-release-order',releases)
            assert all(jobs.dispatch.queue.get(r['job_id'])['state']=='done' for r in rows)
            assert jobs.status(previous['id'])['exit']==2 and jobs.status(plan['jid'])['exit']==2
        answer({'job':done,'recovery_id':jid,'descriptor':descriptor,'lease':current,'receipts':receipts,
                'source_queue_state':jobs.dispatch.queue.get(plan['jid'])['state'],
                'pre_review_queue_states':{r['job_id']:r['state'] for r in rows},
                'release_order':releases,'job_count':len(rows),'output':ownership(out),
                'receipt':ownership(receipt_path),'originals_unchanged':True})
    else: raise AssertionError('unknown fixture helper operation')
finally:
    server.shutdown(); server.server_close(); thread.join(timeout=2)
    # The helper process exits after any dispatcher work. Do not close SafeRoot
    # underneath a dispatcher thread if a failure interrupted this helper.
'''


class RecoveryFixture(SharedFixture):
    def __init__(self, artifact_dir, phonefarm, devlock):
        super().__init__(artifact_dir, phonefarm, devlock)
        self.report['scope'] = 'one actual contained PF battery session, failed and successful typed process_group recoveries across UIDs, explicit leaf-first releases; synthetic Android only'

    def helper(self, operation, *, job_identity=False):
        uid,gid = (self.uid,self.gid) if job_identity else (self.node_uid,self.node_gid)
        process=subprocess.Popen(['/usr/bin/python3','-I',str(self.scratch/'node-helper.py'),
                                  operation,str(self.scratch/'node-plan.json')],
            user=uid,group=gid,extra_groups=[] if job_identity else [self.gid],cwd='/',
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PF_NODE_TOKEN':'fixture-secret-must-not-reach-job'},
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.helper_handles.append(process); self.known_pids.add(process.pid)
        try: stdout,stderr=process.communicate(timeout=90 if operation in ('source','recover-fail','recover') else 45 if operation=='cleanup' else 12)
        except subprocess.TimeoutExpired:
            process.kill(); stdout,stderr=process.communicate(timeout=3)
            (self.artifacts/(operation+'.stdout')).write_text(stdout)
            (self.artifacts/(operation+'.stderr')).write_text(stderr)
            raise
        suffix='-after' if operation=='job-read' and (self.artifacts/'job-read.stdout').exists() else ''
        (self.artifacts/(operation+suffix+'.stdout')).write_text(stdout)
        (self.artifacts/(operation+suffix+'.stderr')).write_text(stderr)
        assert process.returncode==0,(operation,process.returncode,stderr)
        value=json.loads(stdout); assert value['helper_uid']==uid
        return value

    def device(self):
        device=super().device()
        # Only the synthetic Android backing crosses identities. The real old
        # PF output is left private after worker handback, throughout recovery.
        os.chown(device,0,self.gid); device.chmod(0o2770)
        for name in ('state.json','initial-state.json'):
            path=device/name; state=json.loads(path.read_text())
            state['files'][CHARGE_NODE]='0\n'
            # Same sampling roots as gray-control-sampling-host-cli: presence
            # is proved by ancestor listings, not just the created leaf entry.
            state['files']['/sys/kernel/tracing/tracing_on']='1\n'
            state['dirs']=sorted(set(state['dirs'])|{str(p) for p in Path(CHARGE_NODE).parents}
                                |{'/sys','/sys/kernel','/sys/kernel/tracing','/sys/kernel/tracing/instances'})
            state['initially_discharging']=False
            save(path,state); os.chown(path,0,self.gid); path.chmod(0o660 if name=='state.json' else 0o640)
        os.chown(device/'synthetic.png',0,self.gid); (device/'synthetic.png').chmod(0o640)
        self.transport_log.touch(mode=0o660); os.chown(self.transport_log,0,self.gid); self.transport_log.chmod(0o660)
        (device/'restore-mismatch').write_text('known successful write with mismatching readback\n')
        (device/'restore-mismatch').chmod(0o444)
        return device

    def run(self):
        if sys.platform!='linux' or os.geteuid()!=0:
            raise RuntimeError('isolated root Linux integration required; no skip')
        assert self.phonefarm.is_file() and self.devlock.is_file() and Path('/bin/zsh').is_file()
        mount=Path('/sys/fs/cgroup')
        assert any(line.split()[4]==str(mount) and line.split(' - ')[1].split()[0]=='cgroup2'
                   for line in Path('/proc/self/mountinfo').read_text().splitlines() if ' - ' in line)
        self.cgroup_root=mount/('phonefarm-pf-recovery-'+self.nonce)
        self.cgroup_root.mkdir(mode=0o755); self.cgroup_created=True
        assert events(self.cgroup_root)['populated']=='0' and (self.cgroup_root/'cgroup.kill').is_file()
        for name in (self.node_account,self.username):
            try: pwd.getpwnam(name)
            except KeyError: pass
            else: raise RuntimeError('fixture account already exists')
            self.command(['/usr/sbin/useradd','--system','--user-group','--no-create-home','--shell','/usr/sbin/nologin',name])
            if name==self.node_account: self.node_created=True
            else: self.account_created=True
        node,job=pwd.getpwnam(self.node_account),pwd.getpwnam(self.username)
        self.node_uid,self.node_gid,self.uid,self.gid=node.pw_uid,node.pw_gid,job.pw_uid,job.pw_gid
        self.command(['/usr/sbin/usermod','--append','--groups',self.username,self.node_account])
        self.report['identity']={'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,'job_gid':self.gid}
        self.scratch=Path(tempfile.mkdtemp(prefix='phonefarm-pf-recovery-',dir='/tmp')); self.scratch.chmod(0o755)
        node_root=self.scratch/'node'; node_root.mkdir(mode=0o755); os.chown(node_root,self.node_uid,self.node_gid)
        jobs_root=node_root/'_jobs'; jobs_root.mkdir(mode=0o755); os.chown(jobs_root,self.node_uid,self.node_gid)
        control=self.scratch/'control'; control.mkdir(mode=0o700); os.chown(control,self.node_uid,self.node_gid)
        roots={name:self.scratch/name for name in ('state','locks','device-leases')}
        for path in roots.values():
            path.mkdir(mode=0o770); os.chown(path,0,self.gid); path.chmod(0o2770)
        self.install_root=Path(tempfile.mkdtemp(prefix='phonefarm-recovery-install-',dir='/')); self.install_root.chmod(0o755)
        node_source=self.install_root/'node-source'; node_source.mkdir(mode=0o755)
        for source in sorted(NODE.glob('*.py')):
            target=node_source/source.name; shutil.copyfile(source,target); target.chmod(0o644)
        tools=self.install_root/'bin'; tools.mkdir(mode=0o755)
        phonefarm=tools/'phonefarm'; devlock=tools/'devlock'
        shutil.copyfile(self.phonefarm,phonefarm); phonefarm.chmod(0o755)
        shutil.copyfile(self.devlock,devlock); devlock.chmod(0o755)
        device=self.device(); fake=self.install_root/'fake-adb'
        fixture_env={'QA_GRAY_CAPTURE_DEVICE':str(device),'QA_GRAY_CAPTURE_LOG':str(self.transport_log),
                     'QA_GRAY_CAPTURE_SERIAL':'PFRECOVERY-FIXTURE'}
        fake.write_text('#!/usr/bin/python3\nimport os,sys\n'
            'if sys.argv[1:]==["--version"]:\n print("synthetic adb fixture"); sys.exit(0)\n'
            +'os.environ.update('+repr(fixture_env)+')\n'+compose(FAKE_ADB))
        fake.chmod(0o755)
        prefix=self.install_root/'worker'
        self.command(['/usr/bin/python3',str(node_source/'install_worker.py'),'--prefix',str(prefix),
            '--jobs-root',str(jobs_root),'--cgroup-root',str(self.cgroup_root),
            '--node-uid',str(self.node_uid),'--node-gid',str(self.node_gid),'--job-uid',str(self.uid),'--job-gid',str(self.gid),
            '--phonefarm-state-dir',str(roots['state']),'--devlock-dir',str(roots['locks']),
            '--phonefarm-lease-dir',str(roots['device-leases']),'--adb-bin',str(fake),'--devlock-bin',str(devlock)])
        installed=json.loads(self.report['commands'][-1]['stdout']); save(self.artifacts/'installation.json',installed)
        config=(prefix/'worker.json').stat()
        assert config.st_uid==0 and config.st_gid==self.node_gid and stat.S_IMODE(config.st_mode)==0o640
        self.sudoers_path=Path('/etc/sudoers.d')/('phonefarm-integration-'+self.nonce)
        fd=os.open(self.sudoers_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o440); self.sudoers_created=True
        with os.fdopen(fd,'w') as stream: stream.write(installed['sudoers'].rstrip()+'\n')
        self.command(['/usr/sbin/visudo','-cf',str(self.sudoers_path)])
        (self.scratch/'node-helper.py').write_text(NODE_HELPER); (self.scratch/'node-helper.py').chmod(0o644)
        plan={'prefix':str(prefix),'entry':installed['entry'],'jobs_root':str(jobs_root),'node_root':str(node_root),
            'node_source':str(node_source),'nonce':self.nonce,'node_uid':self.node_uid,'node_gid':self.node_gid,
            'job_uid':self.uid,'job_gid':self.gid,'phonefarm':str(phonefarm),'devlock':str(devlock),
            'control':str(control),'package':PKG,'serial':fixture_env['QA_GRAY_CAPTURE_SERIAL'],
            'fake_adb':str(fake),'state_root':str(roots['state']),'lock_root':str(roots['locks']),
            'device_lease_root':str(roots['device-leases'])}
        save(self.scratch/'node-plan.json',plan)
        prepared=self.helper('prepare'); self.jid=prepared['jid']; self.job=jobs_root/self.jid
        plan['jid']=self.jid; save(self.scratch/'node-plan.json',plan)
        self.store=SafeRoot(jobs_root)
        self.report['checks']['genuine_source_reservation']=prepared
        launched=self.helper('launch'); self.known_pids.add(launched['worker_handle_pid'])
        assert not Path('/proc/'+str(launched['launcher_pid'])).exists()
        self.report['checks']['failed_source_handed_back']=self.helper('source')
        assert events(self.cgroup_root)['populated']=='0' and not [p for p in self.cgroup_root.iterdir() if p.is_dir()]
        self.report['checks']['sealed_before_recovery']=self.helper('job-read',job_identity=True)
        calls=[json.loads(line) for line in self.transport_log.read_text().splitlines()]
        mismatch=next(i for i,c in enumerate(calls) if c['kind']=='known-charge-readback-mismatch')
        assert not any(c['kind'] in ('sampling-close','sampling-trace-remove') for c in calls[mismatch:])
        before_recovery=len(calls)
        self.report['checks']['failed_typed_recovery']=self.helper('recover-fail')
        calls=[json.loads(line) for line in self.transport_log.read_text().splitlines()]
        before_success=len(calls)
        failed_recovery=calls[before_recovery:before_success]
        assert any(c['kind']=='known-charge-readback-mismatch' for c in failed_recovery)
        assert not any(c['kind'] in ('sampling-close','sampling-trace-remove') for c in failed_recovery)
        # Only after the real R1 failure remove the fake Android readback fault,
        # never a PF record or a queue state, and recover the same source via R1.
        (device/'restore-mismatch').unlink()
        self.report['checks']['typed_recovery']=self.helper('recover')
        self.report['checks']['sealed_after_recovery']=self.helper('job-read',job_identity=True)
        calls=[json.loads(line) for line in self.transport_log.read_text().splitlines()]
        recovery=calls[before_recovery:]
        success=calls[before_success:]
        assert len([c for c in calls if c['kind']=='control-sample'])==1
        assert len([c for c in calls if c['kind']=='launch'])==1
        assert [c['value'] for c in calls if c['kind']=='charge-control-write']==['1','0','0','0']
        assert not any(c['kind'] in ('control-sample','launch') for c in recovery)
        groups=next(i for i,c in enumerate(success) if c['kind']=='sampling-groups-verify')
        charge=next(i for i,c in enumerate(success) if c['kind']=='charge-control-write')
        closed=next(i for i,c in enumerate(success) if c['kind']=='sampling-close')
        assert groups<charge<closed
        first_id=self.report['checks']['failed_typed_recovery']['recovery_id']
        second_id=self.report['checks']['typed_recovery']['recovery_id']
        assert all(c['job_id']==first_id for c in failed_recovery)
        assert all(c['job_id']==second_id for c in success)
        assert all(c['uid']==self.uid for c in calls[:before_recovery])
        assert all(c['uid']==self.node_uid for c in recovery)
        assert all(c['shared_gid']==str(self.gid) for c in calls)
        assert all(c['recovery_out']==str(self.job/'out') for c in recovery)
        assert all(c['gate_ok'] and c.get('pending_ok',True) and c.get('parent_pending_ok',True) for c in calls)
        assert all('PF_NODE_TOKEN' not in c['environment_keys'] for c in calls)
        before=json.loads((device/'initial-state.json').read_text()); after=json.loads((device/'state.json').read_text())
        assert after['properties']==before['properties'] and after['files'][CHARGE_NODE]=='0\n'
        assert all(after['files'].get(path)==value for path,value in before['files'].items())
        assert after['processes']==['init','org.example.other']
        assert len(after['sampling_rounds'])==1 and after['sampling_closed']
        self.report['checks']['transport_scope']={'transports':len(calls),'source_transports':before_recovery,
            'recovery_transports':len(recovery),'failed_recovery_transports':len(failed_recovery),
            'successful_recovery_transports':len(success),'samples':1,'charge_writes':['1','0','0','0'],
            'source_reservation':'explicitly_released',
            'release_order':[second_id,first_id,self.jid],
            'device_state':'synthetic only','game_state_restored':False}
        self.report['passed']=True

    def cleanup(self):
        # Cancel only workers in this newly created root, through real recorded
        # worker ownership. No saved PID/group is blindly signalled.
        if self.store and hasattr(self,'jid'):
            try: self.report['cleanup_request']=self.helper('cleanup')
            except Exception as error:
                self.report['cleanup_request_error']=repr(error); self.report['passed']=False
            for path in self.job.parent.glob('*/meta.json'):
                try:
                    meta=json.loads(path.read_text())
                    for pid in (meta.get('pid'),meta.get('execution',{}).get('child_pid')):
                        if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
                except (OSError,ValueError): pass
        super().cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir',type=Path,required=True)
    parser.add_argument('--phonefarm',type=Path,required=True)
    parser.add_argument('--devlock-bin',type=Path,required=True)
    args=parser.parse_args()
    fixture=RecoveryFixture(args.artifact_dir.absolute(),args.phonefarm.resolve(strict=True),args.devlock_bin.resolve(strict=True))
    def interrupted(signum,frame): raise TimeoutError('integration interrupted by signal '+str(signum))
    for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,interrupted)
    signal.alarm(240)
    try: fixture.run()
    except BaseException as error:
        fixture.report['failure']=repr(error)
        (fixture.artifacts/'failure.txt').write_text(traceback.format_exc())
    finally:
        signal.alarm(0); fixture.cleanup(); save(fixture.artifacts/'result.json',fixture.report)
    print(json.dumps(fixture.report,indent=2))
    return 0 if fixture.report['passed'] else 1


if __name__=='__main__': sys.exit(main())
