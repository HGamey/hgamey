#!/usr/bin/env python3
"""Deploy the actual read-only task service and atomically synchronized catalog."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess

DOMAIN = 'phonefarm.osaka.ananthe.party'
PUBLIC_CAP = 128 * 1024 * 1024


def sha(b): return hashlib.sha256(b).hexdigest()


def read(p, cap=PUBLIC_CAP):
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        m = os.fstat(f.fileno())
        if not stat.S_ISREG(m.st_mode) or m.st_size > cap:
            raise ValueError('unbounded or nonregular deployment file')
        b = f.read(cap+1)
        after = os.fstat(f.fileno())
        if len(b) != m.st_size or (m.st_size, m.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('deployment file changed while reading')
        return b


def run(argv, **kw):
    return subprocess.run(argv, check=True, timeout=90, capture_output=True, **kw)


REMOTE_INSTALL = r"""
import hashlib,json,os,pathlib,re,stat,subprocess,sys
cache=pathlib.Path(sys.argv[1]);code_name=sys.argv[2];data_name=sys.argv[3];install=sys.argv[4]=='install'
def sha(b):return hashlib.sha256(b).hexdigest()
def read(p,cap=134217728):
    f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(f,'rb') as stream:
        m=os.fstat(stream.fileno())
        if not stat.S_ISREG(m.st_mode) or m.st_size>cap:raise ValueError('bad regular file')
        b=stream.read(cap+1);a=os.fstat(stream.fileno())
        if len(b)!=m.st_size or (m.st_size,m.st_mtime_ns)!=(a.st_size,a.st_mtime_ns):raise ValueError('changed file')
        return b
def publish(src,dest,manifest_name,schema,code=False):
    mb=read(src/manifest_name);manifest=json.loads(mb)
    if manifest.get('schema')!=schema or manifest.get('complete') is not True:raise ValueError('bad manifest')
    files=manifest['files'];payload={};total=0
    for row in files:
        path=row['path']
        if code:
            allowed=path in {'src/viz/assets/index.html','src/viz/assets/app.js','src/viz/assets/app.css'} or re.fullmatch(r'tools/phonefarm_web/[A-Za-z0-9_\-]+\.(py|md)',path)
        else:
            allowed=path=='catalog.json' or re.fullmatch(r'artifacts/artifact_[a-f0-9]{24}\.json',path)
        if not allowed or path in payload:raise ValueError('unregistered deployment path')
        b=read(src/path);total+=len(b)
        if total>134217728 or len(b)!=row['bytes'] or sha(b)!=row['sha256']:raise ValueError('payload integrity')
        payload[path]=b
    if not code and 'catalog.json' not in payload:raise ValueError('missing catalog')
    if code and not {'tools/phonefarm_web/server.py','tools/phonefarm_web/catalog.py','src/viz/assets/app.js','src/viz/assets/index.html','src/viz/assets/app.css'}<=payload.keys():raise ValueError('incomplete application')
    payload[manifest_name]=mb
    dest.mkdir(parents=True,exist_ok=True)
    for path,b in payload.items():
        p=dest/path;p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists():
            if read(p)!=b:raise ValueError('existing immutable release differs')
        else:
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
            with os.fdopen(fd,'wb') as f:f.write(b)
        os.chmod(p,0o644)
    for p in [dest,*[p for p in dest.rglob('*') if p.is_dir()]]:os.chmod(p,0o755)
    return {'path':str(dest),'manifest_sha256':sha(mb),'files':len(payload)}
def switch(root,dest):
    root.mkdir(parents=True,exist_ok=True)
    current=root/'current';previous=os.readlink(current) if current.is_symlink() else None
    if current.exists() and previous is None:raise ValueError('unexpected current path')
    pending=root/('current-'+dest.name)
    if pending.exists() or pending.is_symlink():raise ValueError('pending path exists')
    os.symlink(str(dest),pending);os.replace(pending,current);return previous
data_root=pathlib.Path('/var/lib/phonefarm/web');data_dest=data_root/'snapshots'/data_name
data=publish(cache/'snapshot',data_dest,'release-manifest.json','phonefarm_web/release_manifest@1')
result={'data':data,'previous_data':switch(data_root,data_dest)}
if install:
    code_root=pathlib.Path('/opt/phonefarm/web');code_dest=code_root/'releases'/code_name
    code=publish(cache/'app',code_dest,'application-manifest.json','phonefarm_web/application_manifest@1',True)
    result.update(code=code,previous_code=switch(code_root,code_dest))
    unit='''[Unit]
Description=Phonefarm task history and experiment API (read only)
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/phonefarm/web/current/tools/phonefarm_web
ExecStart=/usr/bin/python3 /opt/phonefarm/web/current/tools/phonefarm_web/server.py --snapshot /var/lib/phonefarm/web/current --assets /opt/phonefarm/web/current/src/viz/assets --host 127.0.0.1 --port 8711
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
'''
    pathlib.Path('/etc/systemd/system/phonefarm-web.service').write_text(unit)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','phonefarm-web.service'],check=True)
    subprocess.run(['systemctl','restart','phonefarm-web.service'],check=True)
print(json.dumps(result))
"""


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--snapshot',required=True,type=Path)
    p.add_argument('--evidence',required=True,type=Path)
    p.add_argument('--install',action='store_true')
    a=p.parse_args()
    source=Path(__file__).resolve().parents[2]
    mb=read(a.snapshot/'release-manifest.json')
    manifest=json.loads(mb)
    if manifest.get('schema')!='phonefarm_web/release_manifest@1' or manifest.get('complete') is not True:
        raise ValueError('no complete catalog manifest')
    catalog=json.loads(read(a.snapshot/'catalog.json'))
    if manifest['snapshot']!=catalog['snapshot']:raise ValueError('catalog snapshot mismatch')
    actual={x.relative_to(a.snapshot).as_posix() for x in a.snapshot.rglob('*') if not x.is_dir()}
    expected={x['path'] for x in manifest['files']}|{'release-manifest.json'}
    if actual!=expected:raise ValueError('extra snapshot files')
    for row in manifest['files']:
        b=read(a.snapshot/row['path'])
        if len(b)!=row['bytes'] or sha(b)!=row['sha256']:raise ValueError('snapshot hash mismatch')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    data_name=stamp+'-'+sha(mb)[:12]
    evidence=a.evidence/data_name;evidence.mkdir(parents=True,exist_ok=False)
    app=evidence/'app';files=[]
    if a.install:
        candidates=[source/'src/viz/assets'/x for x in ['index.html','app.js','app.css']]
        candidates.extend(sorted((source/'tools/phonefarm_web').glob('*.py')))
        candidates.extend(sorted((source/'tools/phonefarm_web').glob('*.md')))
        for path in candidates:
            b=read(path);relative=path.relative_to(source).as_posix();dest=app/relative
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(b)
            files.append({'path':relative,'bytes':len(b),'sha256':sha(b)})
        body=json.dumps({'schema':'phonefarm_web/application_manifest@1','complete':True,'files':files},sort_keys=True,indent=2).encode()+b'\n'
        (app/'application-manifest.json').write_bytes(body);code_name=stamp+'-'+sha(body)[:12]
    else:code_name='existing'
    remote='/home/kamico/.cache/phonefarm-web/'+data_name
    run(['ssh','osakasb','mkdir -p -- '+shlex.quote(remote)])
    run(['rsync','-a','--',str(a.snapshot.absolute())+'/', 'osakasb:'+remote+'/snapshot/'])
    if a.install:run(['rsync','-a','--',str(app.absolute())+'/', 'osakasb:'+remote+'/app/'])
    result=run(['ssh','osakasb','sudo -n python3 - '+shlex.quote(remote)+' '+shlex.quote(code_name)+' '+shlex.quote(data_name)+' '+('install' if a.install else 'sync')],input=REMOTE_INSTALL.encode())
    installed=json.loads(result.stdout)
    receipt={'schema':'phonefarm_web/deployment_receipt@1','completed':True,'at':dt.datetime.now(dt.timezone.utc).isoformat(),
             'snapshot':catalog['snapshot'],'host':'osakasb','public_url':'https://'+DOMAIN,'application_installed':a.install,**installed}
    (evidence/'deployment-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'receipt':str(evidence/'deployment-receipt.json'),'snapshot':catalog['snapshot'],'data_release':installed['data']['path']}))


if __name__=='__main__':main()
