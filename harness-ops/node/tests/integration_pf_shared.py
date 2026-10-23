#!/usr/bin/env python3
"""One isolated Linux PF owner across job/node UIDs; Android transport is fake.

Uses the actual installed worker, phonefarm lease/controller and node readers.
Run explicitly as root in an isolated CI VM, never during normal test discovery.
No public contained capability is enabled and no existing device state is read.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import zlib

from integration_job_privileged import PrivilegedFixture
from integration_job_cgroup import NODE, events, save, until
from artifact_io import SafeRoot
from job_runtime import runner_alive


# Reused from the frozen gray-control host transport, without its host runner.
# It interprets only the established fixture commands; it never runs a shell or
# delegates to adb. The fixture binds its own paths into an immutable wrapper.
FAKE_ADB = r'''#!/usr/bin/env python3
"""Gray control extension of gray-capture-fake-adb.py; never invokes a real transport."""
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import sys
import time

argv=sys.argv[1:]
serial=argv[1] if len(argv)>=2 and argv[0]=='-s' else None
args=argv[2:]
log=Path(os.environ['QA_GRAY_CAPTURE_LOG'])
device=Path(os.environ['QA_GRAY_CAPTURE_DEVICE'])
entry={'event':'transport','argv':argv,'serial':serial,'pid':os.getpid(),'pgid':os.getpgrp(),
       'simulation':True,'gate_ok':False,'kind':'unhandled','environment_keys':sorted(os.environ)}
markers=[]
for name in ('devlock.d','phonefarm-lease.d'):
    path=Path(os.environ['DEVLOCK_DIR'])/name/'phonefarm-lease.json'
    if path.is_file():
        marker=json.loads(path.read_text())
        if marker.get('serial')==serial:
            markers.append((path,marker))
if serial==os.environ['QA_GRAY_CAPTURE_SERIAL'] and len(markers)==1:
    path,marker=markers[0]
    entry.update(marker_path=str(path),marker=marker)
    entry['gate_ok']=(marker.get('child_pgid')==os.getpgrp()
                      and marker.get('child_pid')==os.getpid()==os.getpgrp()
                      and marker.get('command_pending') is False)


def finish(code,output='',error='',binary=None):
    entry.update(exit=code,stdout=output,stderr=error)
    if binary is not None:
        entry['binary_bytes']=len(binary)
    with log.open('a') as stream:
        stream.write(json.dumps(entry,ensure_ascii=False)+'\n')
    if binary is not None:
        sys.stdout.buffer.write(binary)
    else:
        sys.stdout.write(output)
    sys.stderr.write(error)
    raise SystemExit(code)


if not entry['gate_ok']:
    finish(99,error='transport outside direct-command lease identity\n')
context=entry['marker'].get('recovery_context')
session=json.loads((Path(context)/'session.json').read_text()) if context else None
entry['session']=session
state_path=device/'state.json'
state=json.loads(state_path.read_text())
initial=json.loads((device/'initial-state.json').read_text())
pkg=state['target_pkg']
marker_path='/storage/emulated/0/Android/data/'+pkg+'/files/knobs_layer_out.json'
audit_path='/storage/emulated/0/Android/data/'+pkg+'/files/knobs_frameaudit.json'


def save():
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n')


def pending():
    entry['pending_ok']=(session is not None and session.get('write_pending') is not None
                         and entry['marker'].get('recovery_required') is True)
    if not entry['pending_ok']:
        finish(99,error='write preceded durable pending/hold\n')


def is_running():
    return pkg in state['processes']


def launch():
    pending()
    if not is_running():
        state['processes'].append(pkg)
    state['ever_started']=True
    state['marker_frames']=10
    state['files'][marker_path]=json.dumps({'layer_loaded':True,'pkg':pkg,'readonly_stats':{'frames':10,'render_pass_begins':300}})
    save()


if args==['exec-out','screencap','-p']:
    entry['kind']='screenshot'
    data=(device/'synthetic.png').read_bytes()
    if os.environ.get('QA_GRAY_CAPTURE_BAD_PNG')=='1':
        data=b'synthetic invalid screenshot, safe read failure'
    finish(0,binary=data)
if len(args)==3 and args[0]=='push':
    entry['kind']='push-so'
    pending()
    parent=str(PurePosixPath(args[2]).parent)
    if state.get('dir_owners',{}).get(parent)!='shell':
        finish(1,error='synthetic adb push permission denied: staging not owned by shell\n')
    state['files'][args[2]]=Path(args[1]).read_bytes().decode('latin1')
    save()
    finish(0,'synthetic bytes pushed\n')
if len(args)!=2 or args[0]!='shell':
    finish(99,error='unexpected transport shape\n')
shell=args[1]
words=shlex.split(shell)
as_root=False
if words[:2]==['su','-c'] and len(words)==3:
    as_root=True
    shell=words[2]
    words=shlex.split(shell)
entry.update(shell=shell,words=words,as_root=as_root)
merge_stderr='2>&1' in words
words=[word for word in words if word!='2>&1']


def fail(message):
    finish(1,output=message+'\n' if merge_stderr else '',error='' if merge_stderr else message+'\n')


def operands():
    return [word for word in words[1:] if not word.startswith('-')]


if words==['cat','/proc/sys/kernel/random/boot_id']:
    entry['kind']='boot'
    finish(0,'11111111-2222-3333-4444-555555555555\n')
elif words[:1]==['cat']:
    entry['kind']='read-file'
    path=operands()[-1]
    entry['path']=path
    if path==marker_path and is_running() and state.get('ever_started'):
        state['marker_frames']+=10
        state['files'][path]=json.dumps({'layer_loaded':True,'pkg':pkg,'readonly_stats':{'frames':state['marker_frames'],'render_pass_begins':state['marker_frames']*30}})
        save()
    if path not in state['files']:
        fail('cat: '+path+': No such file (synthetic)')
    finish(0,state['files'][path])
elif words[:1]==['ls']:
    directory=operands()[-1].rstrip('/') or '/'
    entry.update(kind='list-directory',path=directory)
    if '-Zd' in words:
        if directory not in state['files'] and directory not in state['dirs']:
            fail('ls: path does not exist')
        finish(0,'u:object_r:apk_data_file:s0 '+directory+'\n')
    if directory not in state['dirs']:
        fail('ls: '+directory+': No such directory (synthetic)')
    names={PurePosixPath(path).name for path in [*state['files'],*state['dirs']]
           if str(PurePosixPath(path).parent)==directory and path!=directory}
    finish(0,''.join(name+'\n' for name in sorted(names)))
elif words==['dumpsys','input']:
    entry['kind']='rotation-read'
    finish(0,'Input Reader State:\n  SurfaceOrientation: 1\n')
elif words[:2]==['input','swipe']:
    entry['kind']='swipe'
    pending()
    values=[int(value) for value in words[2:]]
    if len(values)!=5 or not 1<=values[-1]<=10000:
        fail('invalid bounded swipe')
    state.setdefault('control_inputs',[]).append({'kind':'swipe','values':values})
    save()
    time.sleep(values[-1]/1000)
    entry['natural_complete']=True
    finish(0)
elif words[:2]==['dumpsys','package'] and len(words)==3:
    entry['kind']='package-read'
    if words[2]!=pkg:
        fail('unexpected target package')
    finish(0,'Packages:\n  legacyNativeLibraryDir='+str(PurePosixPath(state['target_dir']).parent)+'\n')
elif words[:1]==['getprop'] and len(words)==2:
    entry.update(kind='property-read',property=words[1])
    prop=words[1]
    value=state['properties'].get(prop,'')
    if os.environ.get('QA_GRAY_CAPTURE_RESTORE_MISMATCH')=='1' and state.get('restored_'+prop):
        value='999'
        entry['kind']='property-restore-mismatch'
    if os.environ.get('QA_GRAY_CAPTURE_META_FAILURE')=='1' and state.get('restored_'+prop):
        # Fault only local final metadata creation, after recovery writes completed.
        parent=Path(context).resolve()
        if not parent.is_relative_to(device.parent.resolve()):
            finish(99,error='metadata fault escaped isolated fixture\n')
        (parent/'capture_meta.json').mkdir(exist_ok=True)
    finish(0,value+'\n')
elif words[:1]==['setprop'] and len(words)==3:
    entry.update(kind='property-write',property=words[1],value=words[2])
    pending()
    prop,value=words[1:]
    state['properties'][prop]=value
    if (state.get('ever_started') and not is_running() and prop=='debug.knobs.npuprobe'
            and value==initial['properties'][prop]):
        state['restored_'+prop]=True
    if prop=='debug.knobs.frameaudit_go' and value!='0' and is_running() and state.get('ever_started'):
        state['files'][audit_path]=json.dumps({'knob':'gray_frameaudit','version':1,'go':int(value),
                                              'images':[],'frames':[],'swapchain':{'w':32,'h':18,'fmt':37}})
    save()
    if (os.environ.get('QA_GRAY_CAPTURE_UNKNOWN_WRITE')=='1' and prop=='debug.knobs.loadop'
            and value!=initial['properties'][prop]):
        entry['kind']='unknown-property-write'
        fail('synthetic transport returned nonzero after applying property')
    finish(0)
elif words[:2]==['am','force-stop'] and len(words)==3:
    entry['kind']='stop-target'
    pending()
    state['processes']=[name for name in state['processes'] if name!=words[2] and not name.startswith(words[2]+':')]
    save()
    finish(0)
elif words==['ps','-A','-o','NAME']:
    entry['kind']='process-read'
    finish(0,'NAME\n'+''.join(name+'\n' for name in state['processes']))
elif words[:3]==['cmd','package','resolve-activity']:
    entry['kind']='activity-read'
    finish(0,pkg+'/.MainActivity\n')
elif words[:2]==['am','start'] or words[:1]==['monkey']:
    entry['kind']='launch'
    launch()
    finish(0,'Starting: Intent synthetic\nStatus: ok\n')
elif words[:2]==['input','tap']:
    entry['kind']='tap'
    pending()
    state.setdefault('control_inputs',[]).append({'kind':'tap','values':[int(v) for v in words[2:]]})
    save()
    finish(0)
elif words[:1]==['mkdir']:
    entry['kind']='mkdir'
    pending()
    for path in operands():
        if path not in state['dirs']:
            state['dirs'].append(path)
        state.setdefault('dir_owners',{})[path]='root' if as_root else 'shell'
    save()
    finish(0)
elif words[:1] in (['cp'],['mv']):
    entry['kind']='copy-file' if words[0]=='cp' else 'move-file'
    pending()
    source,target=operands()
    if source not in state['files']:
        fail('missing synthetic source '+source)
    state['files'][target]=state['files'][source]
    if words[0]=='mv':
        del state['files'][source]
    save()
    finish(0)
elif words[:1] in (['chmod'],['chown'],['chcon']):
    entry['kind']='file-metadata-write'
    pending()
    finish(0)
elif words[:1]==['stat']:
    entry['kind']='file-stat'
    finish(0,'755 1000 1000\n')
elif words[:1]==['rm']:
    entry['kind']='remove-file'
    pending()
    for path in operands():
        state['files'].pop(path,None)
    save()
    finish(0)
elif words[:1]==['rmdir']:
    entry['kind']='rmdir'
    pending()
    for directory in operands():
        if any(path.startswith(directory+'/') for path in [*state['files'],*state['dirs']]):
            fail('directory not empty')
        if directory in state['dirs']:
            state['dirs'].remove(directory)
        state.get('dir_owners',{}).pop(directory,None)
    save()
    finish(0)
elif words[:1] in (['printf'],['echo']) and '>' in words:
    entry['kind']='write-state'
    pending()
    arrow=words.index('>')
    text=words[2:arrow] if words[0]=='printf' else words[1:arrow]
    state['files'][words[arrow+1]]=' '.join(text)+'\n'
    save()
    finish(0)
finish(99,error='unexpected synthetic command: '+repr(words)+'\n')
'''

PKG = 'com.example.fixture'
NATIVE = '/data/app/~~synthetic/com.example.fixture/lib/arm64'


def png_bytes():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    raw = b''.join(b'\0' + bytes((x * 37 + y * 53 + c * 101) % 256
                                for x in range(32) for c in range(3)) for y in range(18))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 32, 18, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


NODE_HELPER = r'''
import errno,json,os,pathlib,sys,time
operation, plan_path = sys.argv[1:]
plan = json.loads(pathlib.Path(plan_path).read_text())
job = pathlib.Path(plan['jobs_root']) / plan['jid']
def answer(value):
    print(json.dumps({'helper_pid':os.getpid(),'helper_uid':os.getuid(),**value}),flush=True)
if operation == 'job-read':
    assert os.getuid() == plan['job_uid']
    denied=[]
    for path in (job/'meta.json',job/'out/session-control.json'):
        try: path.read_bytes()
        except OSError as error:
            assert error.errno in (errno.EACCES,errno.EPERM),repr(error)
            denied.append(str(path))
        else: raise AssertionError('job identity can read sealed evidence')
    answer({'denied':denied})
    sys.exit(0)
assert os.getuid() == plan['node_uid'] and plan['job_gid'] in os.getgroups()
os.umask(0o077)
# Runtime and shim come from the administrator installation. Jobs/readers are
# the repository implementation, not a facade replacing session behavior.
sys.path[:0] = [plan['prefix'],plan['node_source']]
from node_service import Jobs
from node_session import SessionError
from node_lease import job_lease_receipts
from worker_config import node_environment
import job_runtime as runtime
env = node_environment(plan['prefix'],jobs_root=plan['jobs_root'])
assert env['PHONEFARM_SHARED_GID'] == str(plan['job_gid'])
assert env['PHONEFARM_STATE_DIR'] == plan['state_root']
assert env['DEVLOCK_DIR'] == plan['lock_root']
assert env['PHONEFARM_LEASE_DIR'] == plan['device_lease_root']
assert env['ADB_BIN'] == plan['fake_adb'] and env['DEVLOCK_BIN'] == plan['devlock']
assert 'PF_NODE_TOKEN' not in env and not any(k.startswith('PF_JOB_') for k in env)
jobs = Jobs(plan['node_root'],{'phonefarm':(plan['phonefarm'],plan['node_root'])},worker_prefix=plan['prefix'])
assert jobs.dir == plan['jobs_root']
store = jobs._read_root
def keep(name,value):
    path=pathlib.Path(plan['control'])/(name+'.json')
    path.write_text(json.dumps(value,indent=2)+'\n')
    return value
def bounded(check,seconds=40):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=check()
        if result: return result
        time.sleep(.1)
    raise AssertionError('bounded PF shared integration wait expired')
def status():
    return jobs.session(plan['jid'])
def ready():
    meta=jobs._read_meta(plan['jid'])
    if meta.get('state')=='finished':
        raise AssertionError('PF exited before control became ready: '+json.dumps(meta))
    try: value=status()
    except FileNotFoundError: return None
    except SessionError as error:
        if error.status == 404: return None
        raise
    if value.get('phase')=='recovery_required': raise AssertionError(value)
    return value if value.get('phase')=='ready' and value.get('live_owner') else None
def message(descriptor,rid,action):
    return {'schema':'phonefarm/gray-control-action@1','request_id':rid,
            'session_id':descriptor['session_id'],'actor_id':descriptor['controller']['actor_id'],
            'control_epoch':descriptor['controller']['epoch'],'action':action,'params':{}}
def completed(request):
    keep(request['request_id']+'-published',jobs.session(plan['jid'],action=request))
    def receipt():
        value=jobs.session(plan['jid'],request_id=request['request_id'])
        return value if value.get('status') in ('completed','failed','unknown','not_executed') else None
    value=keep(request['request_id'],bounded(receipt))
    assert value['status']=='completed',value
    return value
def lease():
    value=jobs.leases.snapshot(refresh=True,force=True)
    assert value['storage']['devlock_dir']==plan['lock_root'],value
    assert value['storage']['device_lease_dir']==plan['device_lease_root'],value
    assert value['storage']['readiness']=='ready',value
    return value
try:
    if operation == 'prepare':
        (job/'in').mkdir(parents=True,mode=0o700)
        job.chmod(0o700)
        (job/'out').mkdir(mode=0o700)
        (job/'in/synthetic.so').write_bytes(b'explicit synthetic SO fixture')
        (job/'in/synthetic.so').chmod(0o600)
        assert not list((job/'out').iterdir()),'contained output must begin empty'
        store.write_json(plan['jid']+'/meta.json',{'id':plan['jid'],'bin':'phonefarm','pid':None,
            'state':'running','phase':'starting','execution_policy':'contained',
            'execution_policy_source':'explicit','execution':{'kind':'supervised-v1'}})
        (job/'runner.lock').touch(mode=0o600)
        answer({'prepared':True,'before':lease(),'pf_environment':env})
    elif operation == 'launch':
        command=[plan['phonefarm'],'gray-layer','session','--mode','frameaudit',
            '--serial',plan['serial'],'--package',plan['package'],'--out',str(job/'out'),
            '--so',str(job/'in/synthetic.so'),'--actor-id','agent','--max-seconds','90',
            '--power-policy','observe','--evidence-origin','synthetic','--lock-wait','0s','--json']
        process=runtime.launch(store,plan['jid'],str(job),command,str(job/'out'),
            dict(os.environ,PF_NODE_TOKEN='fixture-secret-must-not-reach-job'),
            execution_policy='contained',privileged_entry=plan['entry'])
        receipt={'launcher_pid':os.getpid(),'worker_handle_pid':process.pid,'argv':command}
        keep('launch',receipt); answer(receipt)
        store.close(); os._exit(0)
    elif operation == 'control':
        descriptor=keep('ready',bounded(ready))
        current=keep('active-lease',lease())
        assert current['status'] in ('held','blocked') and current['bridge_held'] is True,current
        receipts=keep('active-receipts',job_lease_receipts(store,plan['jid']))
        assert receipts['status']=='recorded' and len(receipts['entries'])==1,receipts
        active=receipts['entries'][0]
        assert active['state']=='active' and active['serial']==plan['serial'],active
        assert active['recovery']['status']=='required',active
        meta=jobs._read_meta(plan['jid'])
        assert runtime.runner_alive(store,plan['jid'])
        assert meta['execution']['process_management']['completion_scope']=='owned_cgroup',meta
        session=descriptor['session_path']
        # These records are produced by the real PF owner under the job UID.
        base=store.read_json(plan['jid']+'/out/'+session+'/session.json')
        parent=store.read_json(plan['jid']+'/out/'+session+'/eval-session.json')
        assert base['serial']==plan['serial'] and parent['schema'].startswith('phonefarm/gray-control-resources@')
        capture=completed(message(descriptor,'shared-capture','capture'))
        same=keep('shared-capture-query',jobs.session(plan['jid'],request_id='shared-capture'))
        assert same==capture,'same-ID read changed its receipt'
        image=capture['result']['image']
        png=store.read_bytes(plan['jid']+'/out/'+image['path'],1<<20)
        assert png.startswith(b'\x89PNG\r\n\x1a\n') and image['width']==32 and image['height']==18,image
        published=job/'out/gray-control/requests/shared-capture.json'
        assert published.stat().st_uid==plan['node_uid'],'publisher did not run as the node identity'
        png_stat=(job/'out'/image['path']).stat()
        assert png_stat.st_uid==plan['job_uid'] and png_stat.st_gid==plan['job_gid']
        assert png_stat.st_mode & 0o777==0o640,'PF raw PNG was not explicitly shared'
        keep('shared-image',{'image':image,'length':len(png),'owner':png_stat.st_uid,
                             'group':png_stat.st_gid,'mode':png_stat.st_mode & 0o777})
        descriptor=bounded(ready)
        stopped=completed(message(descriptor,'shared-stop','stop'))
        answer({'capture':capture,'same_id':same,'stop':stopped,'live_receipts':receipts,
                'live_lease':current,'meta':meta,'image':image})
    elif operation == 'final':
        descriptor=keep('closed',status())
        assert descriptor['phase']=='closed' and not descriptor['live_owner'],descriptor
        assert descriptor['recovery']['status']=='reported_complete',descriptor
        original=json.loads((pathlib.Path(plan['control'])/'shared-capture.json').read_text())
        historical=keep('shared-capture-after-close',jobs.session(plan['jid'],request_id='shared-capture'))
        assert historical==original,'terminal same-ID query changed or replayed the action'
        receipts=keep('released-receipts',job_lease_receipts(store,plan['jid']))
        assert receipts['status']=='recorded' and len(receipts['entries'])==1,receipts
        assert receipts['entries'][0]['state']=='released',receipts
        assert receipts['entries'][0]['recovery']['status']=='reported_complete',receipts
        current=keep('final-lease',lease())
        assert current['status']=='clear' and current['bridge_held'] is False,current
        png=store.read_bytes(plan['jid']+'/out/'+descriptor['latest_image']['path'],1<<20)
        assert png.startswith(b'\x89PNG\r\n\x1a\n')
        metadata=jobs._read_meta(plan['jid'])
        assert metadata['state']=='finished' and metadata['exit']==0 and not runtime.runner_alive(store,plan['jid'])
        assert metadata['execution']['owned_cgroup']['status']=='absent',metadata
        answer({'descriptor':descriptor,'receipts':receipts,'lease':current,'meta':metadata,
                'historical_capture':historical,'png_length':len(png)})
    else: raise AssertionError('unknown node helper operation')
finally:
    store.close()
'''


class SharedFixture(PrivilegedFixture):
    def __init__(self, artifact_dir, phonefarm, devlock):
        super().__init__(artifact_dir)
        self.phonefarm, self.devlock = phonefarm, devlock
        self.report['scope'] = 'actual PF and node readers across two UIDs through fixed worker entry; synthetic Android transport only'

    def helper(self, operation, *, job_identity=False):
        uid, gid = (self.uid,self.gid) if job_identity else (self.node_uid,self.node_gid)
        proc = subprocess.Popen(['/usr/bin/python3','-I',str(self.scratch/'node-helper.py'),
                                 operation,str(self.scratch/'node-plan.json')],
            user=uid,group=gid,extra_groups=[] if job_identity else [self.gid],cwd='/',
            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PF_NODE_TOKEN':'fixture-secret-must-not-reach-job'},
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.helper_handles.append(proc); self.known_pids.add(proc.pid)
        try: stdout,stderr=proc.communicate(timeout=65 if operation=='control' else 10)
        except subprocess.TimeoutExpired:
            proc.kill(); stdout,stderr=proc.communicate(timeout=3)
            (self.artifacts/(operation+'.stdout')).write_text(stdout)
            (self.artifacts/(operation+'.stderr')).write_text(stderr)
            raise
        (self.artifacts/(operation+'.stdout')).write_text(stdout)
        (self.artifacts/(operation+'.stderr')).write_text(stderr)
        assert proc.returncode==0,(operation,proc.returncode,stderr)
        value=json.loads(stdout)
        assert value['helper_uid']==uid
        return value

    def device(self):
        device=self.scratch/'device'; device.mkdir(mode=0o700)
        os.chown(device,self.uid,self.gid)
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
        for name in ('state.json','initial-state.json'):
            save(device/name,state); os.chown(device/name,self.uid,self.gid); (device/name).chmod(0o600)
        (device/'synthetic.png').write_bytes(png_bytes())
        os.chown(device/'synthetic.png',self.uid,self.gid); (device/'synthetic.png').chmod(0o600)
        self.transport_log=device/'transport.jsonl'
        return device

    def run(self):
        if sys.platform!='linux' or os.geteuid()!=0:
            raise RuntimeError('isolated root Linux integration required; no skip')
        assert self.phonefarm.is_file() and self.devlock.is_file() and Path('/bin/zsh').is_file()
        mount=Path('/sys/fs/cgroup')
        assert any(line.split()[4]==str(mount) and line.split(' - ')[1].split()[0]=='cgroup2'
                   for line in Path('/proc/self/mountinfo').read_text().splitlines() if ' - ' in line)
        self.cgroup_root=mount/('phonefarm-pf-shared-'+self.nonce)
        self.cgroup_root.mkdir(mode=0o755); self.cgroup_created=True
        assert events(self.cgroup_root)['populated']=='0' and (self.cgroup_root/'cgroup.kill').is_file()
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
        self.command(['/usr/sbin/usermod','--append','--groups',self.username,self.node_account])
        self.report['identity']={'node_uid':self.node_uid,'node_gid':self.node_gid,'job_uid':self.uid,'job_gid':self.gid}
        self.scratch=Path(tempfile.mkdtemp(prefix='phonefarm-pf-shared-',dir='/tmp')); self.scratch.chmod(0o755)
        node_root=self.scratch/'node'; node_root.mkdir(mode=0o755); os.chown(node_root,self.node_uid,self.node_gid)
        jobs_root=node_root/'_jobs'; jobs_root.mkdir(mode=0o755); os.chown(jobs_root,self.node_uid,self.node_gid)
        control=self.scratch/'control'; control.mkdir(mode=0o700); os.chown(control,self.node_uid,self.node_gid)
        roots={name:self.scratch/name for name in ('state','locks','device-leases')}
        for path in roots.values():
            path.mkdir(mode=0o770); os.chown(path,0,self.gid); path.chmod(0o2770)
        self.install_root=Path(tempfile.mkdtemp(prefix='phonefarm-shared-install-',dir='/')); self.install_root.chmod(0o755)
        device=self.device()
        fake=self.install_root/'fake-adb'
        fixture_env={'QA_GRAY_CAPTURE_DEVICE':str(device),'QA_GRAY_CAPTURE_LOG':str(self.transport_log),
                     'QA_GRAY_CAPTURE_SERIAL':'PFSHARED-FIXTURE'}
        fake.write_text('#!/usr/bin/python3\nimport os,sys\n'
                        'if sys.argv[1:]==["--version"]:\n print("synthetic adb fixture"); sys.exit(0)\n'
                        +'os.environ.update('+repr(fixture_env)+')\n'+FAKE_ADB)
        fake.chmod(0o755)
        prefix=self.install_root/'worker'
        self.command(['/usr/bin/python3',str(NODE/'install_worker.py'),'--prefix',str(prefix),
            '--jobs-root',str(jobs_root),'--cgroup-root',str(self.cgroup_root),
            '--node-uid',str(self.node_uid),'--node-gid',str(self.node_gid),'--job-uid',str(self.uid),'--job-gid',str(self.gid),
            '--phonefarm-state-dir',str(roots['state']),'--devlock-dir',str(roots['locks']),
            '--phonefarm-lease-dir',str(roots['device-leases']),'--adb-bin',str(fake),'--devlock-bin',str(self.devlock)])
        installed=json.loads(self.report['commands'][-1]['stdout']); save(self.artifacts/'installation.json',installed)
        config=(prefix/'worker.json').stat()
        assert config.st_uid==0 and config.st_gid==self.node_gid and stat.S_IMODE(config.st_mode)==0o640
        self.sudoers_path=Path('/etc/sudoers.d')/('phonefarm-integration-'+self.nonce)
        fd=os.open(self.sudoers_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o440); self.sudoers_created=True
        with os.fdopen(fd,'w') as stream: stream.write(installed['sudoers'].rstrip()+'\n')
        self.command(['/usr/sbin/visudo','-cf',str(self.sudoers_path)])
        self.jid=self.nonce[:12]; self.job=jobs_root/self.jid; self.store=SafeRoot(jobs_root)
        (self.scratch/'node-helper.py').write_text(NODE_HELPER); (self.scratch/'node-helper.py').chmod(0o644)
        plan={'prefix':str(prefix),'entry':installed['entry'],'jobs_root':str(jobs_root),'node_root':str(node_root),
            'node_source':str(NODE),'jid':self.jid,'node_uid':self.node_uid,'node_gid':self.node_gid,
            'job_uid':self.uid,'job_gid':self.gid,'phonefarm':str(self.phonefarm),'devlock':str(self.devlock),
            'control':str(control),'package':PKG,'serial':fixture_env['QA_GRAY_CAPTURE_SERIAL'],
            'fake_adb':str(fake),'state_root':str(roots['state']),'lock_root':str(roots['locks']),
            'device_lease_root':str(roots['device-leases'])}
        save(self.scratch/'node-plan.json',plan)
        prepared=self.helper('prepare')
        assert prepared['before']['status']=='clear' and not list((self.job/'out').iterdir()),prepared
        self.report['checks']['same_fixed_lease_environment_before_launch']=prepared
        launched=self.helper('launch'); self.known_pids.add(launched['worker_handle_pid'])
        assert not Path('/proc/'+str(launched['launcher_pid'])).exists()
        self.report['checks']['actual_pf_control']=self.helper('control')
        def finished():
            meta=self.store.read_json(self.jid+'/meta.json')
            for pid in (meta.get('pid'),meta.get('execution',{}).get('child_pid')):
                if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
            return meta if meta.get('state')=='finished' and not runner_alive(self.store,self.jid) else None
        done=until(finished,seconds=15)
        assert done['outcome']=='succeeded' and done['exit']==0,done
        self.report['checks']['closed_node_reads']=self.helper('final')
        self.report['checks']['sealed_job_identity_denied']=self.helper('job-read',job_identity=True)
        assert len(self.report['checks']['sealed_job_identity_denied']['denied'])==2
        assert self.job.stat().st_uid==self.node_uid and stat.S_IMODE(self.job.stat().st_mode)==0o700
        image=self.report['checks']['actual_pf_control']['image']
        raw=self.job/'out'/image['path']
        assert raw.read_bytes()==png_bytes() and raw.stat().st_uid==self.node_uid
        assert stat.S_IMODE(raw.stat().st_mode)==0o600
        before=json.loads((device/'initial-state.json').read_text()); after=json.loads((device/'state.json').read_text())
        assert all(after[key]==before[key] for key in ('properties','files','legacy_settings'))
        assert sorted(after['dirs'])==before['dirs'] and after['processes']==['init','org.example.other']
        calls=[json.loads(line) for line in self.transport_log.read_text().splitlines()]
        assert calls and all(c['gate_ok'] and c.get('pending_ok',True) and c['kind']!='unhandled' for c in calls)
        assert sum(c['kind']=='screenshot' for c in calls)==1,'same-ID query repeated capture'
        assert all('PF_NODE_TOKEN' not in c['environment_keys'] for c in calls)
        self.known_pids.update(c['pid'] for c in calls)
        self.known_pids.update(c['marker']['pid'] for c in calls if isinstance(c.get('marker',{}).get('pid'),int))
        self.report['checks']['gated_transport_and_originals']={'transports':len(calls),'screenshots':1,
            'original_png_bytes':len(raw.read_bytes()),'fake_state_restored':True,'game_state_restored':False}
        assert events(self.cgroup_root)['populated']=='0' and not [p for p in self.cgroup_root.iterdir() if p.is_dir()]
        self.report['passed']=True

    def cleanup(self):
        # Retain all observed worker/transport identities even when an earlier
        # permission assertion failed; no signal is sent to a saved PID.
        if self.store and hasattr(self,'jid'):
            try:
                meta=self.store.read_json(self.jid+'/meta.json')
                for pid in (meta.get('pid'),meta.get('execution',{}).get('child_pid')):
                    if isinstance(pid,int) and pid>1: self.known_pids.add(pid)
            except (OSError,ValueError): pass
        if hasattr(self,'transport_log') and self.transport_log.exists():
            for line in self.transport_log.read_text().splitlines():
                try:
                    call=json.loads(line)
                    self.known_pids.add(call['pid'])
                    if isinstance(call.get('marker',{}).get('pid'),int): self.known_pids.add(call['marker']['pid'])
                except (ValueError,KeyError): pass
        super().cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir',type=Path,required=True)
    parser.add_argument('--phonefarm',type=Path,required=True)
    parser.add_argument('--devlock-bin',type=Path,required=True)
    args=parser.parse_args()
    fixture=SharedFixture(args.artifact_dir.absolute(),args.phonefarm.resolve(strict=True),args.devlock_bin.resolve(strict=True))
    def interrupted(signum,frame): raise TimeoutError(f'integration interrupted by signal {signum}')
    for signum in (signal.SIGALRM,signal.SIGTERM,signal.SIGINT): signal.signal(signum,interrupted)
    signal.alarm(120)
    try: fixture.run()
    except BaseException as error:
        fixture.report['failure']=repr(error)
        (fixture.artifacts/'failure.txt').write_text(traceback.format_exc())
    finally:
        signal.alarm(0); fixture.cleanup(); save(fixture.artifacts/'result.json',fixture.report)
    print(json.dumps(fixture.report,indent=2))
    return 0 if fixture.report['passed'] else 1


if __name__=='__main__': sys.exit(main())
