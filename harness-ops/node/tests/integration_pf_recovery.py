#!/usr/bin/env python3
"""One same-UID battery session, failed typed recovery, then successful recovery.

PF, Node, queue and task process groups are real; Android responses are fake.
Everything runs in a new temporary directory without devices or root access.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import time
import uuid
import subprocess
import sys
import tempfile
import traceback

from pf_recovery_fake import compose, FAKE_ADB, PKG, NATIVE, png_bytes

NODE = Path(__file__).resolve().parents[1]

def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


CHARGE_NODE = '/sys/class/power_supply/battery/input_suspend'

NODE_HELPER = r'''
import errno,hashlib,http.client,io,json,os,pathlib,stat,subprocess,sys,tarfile,threading,time
from http.server import ThreadingHTTPServer
from urllib.parse import quote
operation, plan_path = sys.argv[1:]
plan=json.loads(pathlib.Path(plan_path).read_text())
control=pathlib.Path(plan['control'])
def answer(value):
    print(json.dumps({'helper_pid':os.getpid(),'helper_uid':os.getuid(),**value}),flush=True)
job=pathlib.Path(plan['jobs_root'])/plan['jid'] if plan.get('jid') else None
os.umask(0o077)
sys.path.insert(0,plan['node_source'])
from node_service import Jobs,make_handler
from node_lease import job_lease_receipts
from job_dispatch import file_lock
import job_runtime as runtime
env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PHONEFARM_STATE_DIR':plan['state_root'],
     'DEVLOCK_DIR':plan['lock_root'],'PHONEFARM_LEASE_DIR':plan['device_lease_root'],
     'ADB_BIN':plan['fake_adb'],'DEVLOCK_BIN':plan['devlock'],'PHONEFARM_BIN':plan['phonefarm']}
os.environ.update(env)
jobs=Jobs(plan['node_root'],{'phonefarm':(plan['phonefarm'],plan['node_root'])})
jobs.pf_env=env
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
    def register(revision, device):
        result=subprocess.run([plan['phonefarm'],'lease','devices-set','--request','-','--json'],
            input=json.dumps({'expected_revision':revision,'device':device}),text=True,capture_output=True,env=env,check=True)
        return json.loads(result.stdout)
    register(0,{'device_id':plan['serial'],'label':'A','aliases':[plan['serial']],'enabled':True})
    binding=json.loads(subprocess.check_output([plan['phonefarm'],'lease','resolve','--serial',plan['serial'],'--json'],env=env))
    assert binding['registry_revision']==1
    register(1,{'device_id':'unrelated-B','label':'B','aliases':['FAKE-B'],'enabled':True})
    argv=['gray-layer','session','--mode','frameaudit','--serial',plan['serial'],
          '--package',plan['package'],'--out','@out','--so','@in/synthetic.so',
          '--actor-id','agent','--max-seconds','90','--power-policy','battery',
          '--evidence-origin','synthetic','--lock-wait','0s','--json']
    spec={'bin':'phonefarm','argv':argv,'label':'isolated battery recovery source',
          'execution_policy':'process_group','device_access':'write','resources':['fixture-device:'+plan['serial']],
          'device_binding':binding}
    body=io.BytesIO()
    with tarfile.open(fileobj=body,mode='w:gz') as archive:
        payload=b'explicit synthetic SO fixture'
        member=tarfile.TarInfo('synthetic.so'); member.size=len(payload); member.mode=0o600
        archive.addfile(member,io.BytesIO(payload))
    data=body.getvalue()
    digest=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()+b'\0'+data).hexdigest()
    # Exercise actual reservation, preparation and claim before the launcher exits.
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
    jobs.dispatch._launch(jobs.dispatch.queue.get(plan['jid']))
    meta=bounded(lambda: jobs._read_meta(plan['jid']) if jobs._read_meta(plan['jid']).get('pid') else None)
    value={'launcher_pid':os.getpid(),'worker_handle_pid':meta['pid']}
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
        assert done['execution']['owned_process_group']['status']=='absent',done
        assert jobs.dispatch.queue.get(plan['jid'])['state']=='done'
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
        raw=record.read_bytes(); binding=pathlib.Path(parent['charging']['binding']['record_evidence'])
        assert raw==binding.read_bytes()
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
        assert current['status']=='clear',current
        assert ownership(job/'out')['mode']==0o700 and ownership(session/'session.json')['mode']==0o600
        answer({'sample':sample,'stop':stopped,'meta':done,'descriptor':descriptor,'receipts':receipts,
                'lease':current,'preserved_count':len(preserved),'global_record':ownership(record)})
    elif operation in ('recover-fail','recover'):
        failing=operation=='recover-fail'
        name='failed-recovery' if failing else 'recovery'
        previous=None if failing else json.loads((control/'failed-recovery-id.json').read_text())
        target=plan['jid'] if failing else previous['id']
        endpoint='/jobs/'+target+'/session/recover'
        before=keep('before-'+name+'-lease',lease()); assert before['status']=='clear',before
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
        assert done['queue']['state']=='done' and jobs.dispatch.queue.get(jid)['state']=='done',done
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
        assert jobs._read_meta(plan['jid'])['exit']==2 and jobs.dispatch.queue.get(plan['jid'])['state']=='done'
        receipts=keep(name+'-receipts',job_lease_receipts(store,jid))
        assert receipts['status']=='recorded' and len(receipts['entries'])==1,receipts
        entry=receipts['entries'][0]
        old=json.loads((control/('source-required-receipts.json' if failing else 'failed-recovery-receipts.json')).read_text())['entries'][0]
        assert entry['job_id']==jid and entry['lease_id']!=old['lease_id'],entry
        assert entry['state']=='released',entry
        assert entry['recovery']['status']==('required' if failing else 'reported_complete'),entry
        assert entry['serial']==plan['serial'] and entry['recovery']['context']==old['recovery']['context']
        # Recovery ancestry is the saved source context and Node job chain.
        # No previous lock owner survives after its last fd closes.
        assert entry['recovery_of'] is None,entry
        out=pathlib.Path(jobs.path(jid,'out'))
        receipt_path=out/entry['artifact_path']
        downloaded=get('/jobs/'+jid+'/out?path='+quote(entry['artifact_path'],safe=''))
        assert downloaded==json.loads(receipt_path.read_text())
        current=keep(name+'-lease',lease())
        assert ownership(job)['mode']==0o700 and ownership(job/'out')['mode']==0o700
        assert ownership(job/'out'/saved['session_path']/'session.json')['mode']==0o600
        code,repeated=request('POST',endpoint,{},request_id)
        assert code==200 and repeated['id']==jid,(code,repeated)
        rows=jobs.dispatch.queue.rows(('preparing','queued','claimed','blocked','done'))
        assert len(rows)==(2 if failing else 3) and all(r['state']=='done' for r in rows),rows
        releases=[]
        if failing:
            assert descriptor['phase']=='recovery_required' and descriptor['recovery']['status']=='required',descriptor
            assert descriptor['power']['stage']=='restoring' and current['status']=='clear'
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
            assert all(item['queue_state']=='done' for item in closed_plan['attempts'])
            # Known host completion already returned every reservation. Device
            # recovery does not rewrite failed exits or require a manual release.
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


class RecoveryFixture:
    def __init__(self, artifact_dir, phonefarm, devlock):
        self.artifacts=artifact_dir
        artifact_dir.mkdir(parents=True,exist_ok=False)
        self.phonefarm,self.devlock=phonefarm,devlock
        self.nonce=uuid.uuid4().hex[:12]
        self.scratch=Path(tempfile.mkdtemp(prefix='phonefarm-pf-recovery-'))
        self.helper_handles=[]
        self.report={'passed':False,'checks':{},'scope':'actual same-UID PF/Node process groups, failed and successful recovery of one source; synthetic Android only'}

    def helper(self, operation):
        process=subprocess.Popen(['/usr/bin/python3','-I',str(self.scratch/'node-helper.py'),
                                  operation,str(self.scratch/'node-plan.json')],
            cwd=str(self.scratch),
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.helper_handles.append(process)
        try: stdout,stderr=process.communicate(timeout=90 if operation in ('source','recover-fail','recover') else 45 if operation=='cleanup' else 12)
        except subprocess.TimeoutExpired:
            process.kill(); stdout,stderr=process.communicate(timeout=3)
            (self.artifacts/(operation+'.stdout')).write_text(stdout)
            (self.artifacts/(operation+'.stderr')).write_text(stderr)
            raise
        (self.artifacts/(operation+'.stdout')).write_text(stdout)
        (self.artifacts/(operation+'.stderr')).write_text(stderr)
        assert process.returncode==0,(operation,process.returncode,stderr)
        value=json.loads(stdout)
        return value

    def device(self):
        device=self.scratch/'device'; device.mkdir(mode=0o700)
        props={'debug.vulkan.layers':''}
        for n,name in enumerate(('loadop','passdump','copyprobe','upop','frameaudit','frameaudit_ts',
                                 'frameaudit_go','storeop','storeop_live','storeop_on'),2):
            props['debug.knobs.'+name]=str(n)
        props.update({'debug.knobs.storeop':'deadbeef:1','debug.knobs.npuprobe':'3'})
        marker='/storage/emulated/0/Android/data/'+PKG+'/files/knobs_layer_out.json'
        audit='/data/local/tmp/knobs_frameaudit.'+PKG+'.json'
        files={marker:'{"historical_marker":"preserve"}\n',audit:'{"historical_audit":"preserve"}\n',
               NATIVE+'/libgame.so':'unrelated native library',
               '/data/local/tmp/libVkLayer_refknobs.so':'shared staging file, unknown owner'}
        dirs={'/','/data/local/tmp',NATIVE,'/data/data/'+PKG,
              '/storage/emulated/0/Android/data/'+PKG+'/files'}
        for path in [*files,*dirs]: dirs.update(str(p) for p in Path(path).parents)
        state={'target_pkg':PKG,'target_dir':NATIVE,'properties':props,'files':files,'dirs':sorted(dirs),
               'processes':['init',PKG,PKG+':render','org.example.other'],
               'legacy_settings':{'gpu_debug_app':'untouched'},'marker_frames':0}
        state['files'][CHARGE_NODE]='0\n'
        state['dirs']=sorted(set(state['dirs']) | {'/sys','/sys/kernel','/sys/kernel/tracing','/sys/kernel/tracing/instances'})
        state['dirs']=sorted(set(state['dirs']) | {str(p) for path in state['files'] for p in Path(path).parents})
        state['initially_discharging']=False
        for name in ('state.json','initial-state.json'):
            save(device/name,state)
        (device/'synthetic.png').write_bytes(png_bytes())
        self.transport_log=device/'transport.jsonl'
        self.transport_log.touch()
        (device/'restore-mismatch').write_text('known successful write with mismatching readback\n')
        return device


    def run(self):
        assert self.phonefarm.is_file() and self.devlock.is_file() and Path('/bin/zsh').is_file()
        node_root=self.scratch/'node'; node_root.mkdir()
        jobs_root=node_root/'_jobs'; jobs_root.mkdir()
        control=self.scratch/'control'; control.mkdir()
        roots={name:self.scratch/name for name in ('state','locks','device-leases')}
        device=self.device(); fake=self.scratch/'fake-adb'
        fixture_env={'QA_GRAY_CAPTURE_DEVICE':str(device),'QA_GRAY_CAPTURE_LOG':str(self.transport_log),
                     'QA_GRAY_CAPTURE_SERIAL':'PFRECOVERY-FIXTURE'}
        fake.write_text('#!/usr/bin/python3\nimport os,sys\n'
            'if sys.argv[1:]==["--version"]:\n print("synthetic adb fixture"); sys.exit(0)\n'
            +'os.environ.update('+repr(fixture_env)+')\n'+compose(FAKE_ADB))
        fake.chmod(0o700)
        (self.scratch/'node-helper.py').write_text(NODE_HELPER)
        plan={'jobs_root':str(jobs_root),'node_root':str(node_root),'node_source':str(NODE),
            'nonce':self.nonce,'phonefarm':str(self.phonefarm),'devlock':str(self.devlock),
            'control':str(control),'package':PKG,'serial':fixture_env['QA_GRAY_CAPTURE_SERIAL'],
            'fake_adb':str(fake),'state_root':str(roots['state']),'lock_root':str(roots['locks']),
            'device_lease_root':str(roots['device-leases'])}
        save(self.scratch/'node-plan.json',plan)
        prepared=self.helper('prepare'); self.jid=prepared['jid']; self.job=jobs_root/self.jid
        plan['jid']=self.jid; save(self.scratch/'node-plan.json',plan)
        self.report['checks']['genuine_source_reservation']=prepared
        launched=self.helper('launch')
        assert not Path('/proc/'+str(launched['launcher_pid'])).exists()
        self.report['checks']['failed_source_handed_back']=self.helper('source')
        calls=[json.loads(line) for line in self.transport_log.read_text().splitlines()]
        mismatch=next(i for i,c in enumerate(calls) if c['kind']=='known-charge-readback-mismatch')
        assert not any(c['kind'] in ('sampling-close','sampling-trace-remove') for c in calls[mismatch:])
        before_recovery=len(calls)
        source_binding=self.report['checks']['failed_source_handed_back']['descriptor']['device_binding']
        assert source_binding['registry_revision']==1 and source_binding['serial']==plan['serial']
        # Remove the old registry archive and change today's alias mapping. The
        # recovery must still use the source serial and binding, not this name.
        registry=roots['locks']/'device-registry.json'
        current=json.loads(registry.read_text())
        current['revision']+=1
        next(d for d in current['devices'] if d['device_id']==plan['serial'])['aliases']=['NEW-UNUSED-ALIAS']
        save(registry,current)
        history=roots['locks']/'device-registry-history'
        if history.exists(): shutil.rmtree(history)
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
        for name in ('failed_typed_recovery','typed_recovery'):
            assert self.report['checks'][name]['descriptor']['device_binding']==source_binding
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
            'source_reservation':'released_on_known_completion',
            'recovery_order':[first_id,second_id],
            'device_state':'synthetic only','game_state_restored':False}
        self.report['passed']=True

    def cleanup(self):
        if hasattr(self,'jid'):
            try: self.report['cleanup_request']=self.helper('cleanup')
            except Exception as error:
                self.report['cleanup_request_error']=repr(error); self.report['passed']=False
        for process in self.helper_handles:
            if process.poll() is None:
                process.kill(); process.wait(timeout=3)
        shutil.copytree(self.scratch,self.artifacts/'raw',symlinks=True)
        if not self.report.get('cleanup_request_error'):
            shutil.rmtree(self.scratch)



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
