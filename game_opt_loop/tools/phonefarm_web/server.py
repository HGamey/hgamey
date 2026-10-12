#!/usr/bin/env python3
"""Read-only HTTP over an immutable synchronized catalog. No subprocess/device API."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, urlsplit
import catalog

CATALOG_CAP=24*1024*1024
ID=re.compile(r'(?:task|run|artifact)_[0-9a-f]{24}')


def bounded(root,relative,cap):
    parts=relative.split('/')
    if any(not p or p.startswith('.') or p=='..' for p in parts):raise ValueError('invalid release file')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        child=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(child,'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('nonregular release file')
            raw=f.read(cap+1)
            if len(raw)>cap:raise ValueError('release file cap')
            return raw
    finally:os.close(fd)


class Store:
    def __init__(self,root):self.root=Path(root)

    def load(self):
        # current may be atomically switched by deployment; pin one immutable release.
        pinned=self.root.resolve(strict=True)
        raw=bounded(pinned,'release-manifest.json',4*1024*1024)
        manifest=catalog.strict_json(raw)
        if manifest.get('schema')!='phonefarm_web/release_manifest@1' or manifest.get('complete') is not True:
            raise ValueError('incomplete snapshot')
        files=manifest.get('files')
        if not isinstance(files,list) or len(files)>12000:raise ValueError('invalid manifest')
        lookup={}
        for f in files:
            if not isinstance(f,dict) or set(f)!={'path','bytes','sha256'}:raise ValueError('invalid manifest file')
            path=f['path']
            if path!='catalog.json' and not re.fullmatch(r'artifacts/artifact_[0-9a-f]{24}\.json',path):
                raise ValueError('unsupported export artifact path')
            if path in lookup or type(f['bytes']) is not int or not 0<=f['bytes']<=CATALOG_CAP:
                raise ValueError('invalid manifest bytes/path')
            if not isinstance(f['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',f['sha256']):raise ValueError('invalid hash')
            lookup[path]=f
        raw=bounded(pinned,'catalog.json',CATALOG_CAP)
        f=lookup.get('catalog.json')
        if f is None or f['bytes']!=len(raw) or f['sha256']!=catalog.digest(raw):raise ValueError('catalog integrity failed')
        value=catalog.strict_json(raw)
        if value.get('schema')!='phonefarm_web/catalog@1' or value.get('snapshot')!=manifest.get('snapshot'):
            raise ValueError('catalog schema/snapshot mismatch')
        if value['snapshot'].get('live_device') is not False:raise ValueError('snapshot cannot claim live device')
        for key in ['tasks','runs','diagnostics']:
            if not isinstance(value.get(key),list):raise ValueError('catalog arrays required')
        if not isinstance(value.get('artifacts'),dict):raise ValueError('artifact index required')
        for kind,key in [('task','tasks'),('run','runs')]:
            ids=set()
            for row in value[key]:
                if not isinstance(row,dict) or not re.fullmatch(kind+r'_[0-9a-f]{24}',row.get('id','')) or row['id'] in ids:
                    raise ValueError('invalid/duplicate catalog ID')
                ids.add(row['id'])
        for ident,row in value['artifacts'].items():
            if not re.fullmatch(r'artifact_[0-9a-f]{24}',ident) or row.get('id')!=ident:raise ValueError('artifact ID invalid')
            if row.get('file')!='artifacts/'+ident+'.json' or row['file'] not in lookup:raise ValueError('artifact registration invalid')
            if row.get('sha256')!=lookup[row['file']]['sha256'] or row.get('bytes')!=lookup[row['file']]['bytes']:
                raise ValueError('artifact integrity metadata mismatch')
        return value,lookup,pinned

    def artifact(self,value,lookup,pinned,ident):
        row=value['artifacts'].get(ident)
        if row is None:raise KeyError('unknown artifact')
        raw=bounded(pinned,row['file'],CATALOG_CAP)
        f=lookup[row['file']]
        if len(raw)!=f['bytes'] or catalog.digest(raw)!=f['sha256']:raise ValueError('artifact bytes changed')
        return raw


def public_run(r):return {k:v for k,v in r.items() if k not in {'steps','artifacts','diagnostics'}}


def public_artifact(row):return {k:v for k,v in row.items() if k!='file'}


def page(query,items,snapshot):
    offset=query.get('offset',['0'])[0];limit=query.get('limit',['20'])[0]
    if not re.fullmatch(r'\d{1,9}',offset) or not re.fullmatch(r'\d{1,3}',limit):raise ValueError('invalid pagination')
    offset=int(offset);limit=int(limit)
    if not 1<=limit<=200:raise ValueError('page limit 1..200')
    return {'schema':'phonefarm_web/page@1','snapshot':snapshot,'items':items[offset:offset+limit],
            'total':len(items),'offset':offset,'limit':limit}


class Handler(BaseHTTPRequestHandler):
    server_version='phonefarm-readonly/1'

    def log_message(self,format,*args):pass  # URLs or arbitrary client text are not runtime evidence.

    def send(self,code,raw,content='application/json; charset=utf-8'):
        self.send_response(code);self.send_header('Content-Type',content)
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
        if self.command!='HEAD':self.wfile.write(raw)

    def error(self,code,reason):self.send(code,catalog.encode({'error':reason}))
    def do_POST(self):self.error(405,'read-only service')
    do_PUT=do_POST;do_DELETE=do_POST;do_PATCH=do_POST
    def do_HEAD(self):self.do_GET()

    def do_GET(self):
        try:
            if len(self.path)>2048:raise ValueError('URL cap')
            u=urlsplit(self.path);query=parse_qs(u.query,keep_blank_values=True)
            if any(len(v)!=1 for v in query.values()):raise ValueError('duplicate query parameter')
            if u.path in {'/','/index.html','/app.js','/app.css'}:
                if query:raise ValueError('static query unsupported')
                if self.server.assets is None:raise KeyError('assets unavailable')
                name='index.html' if u.path=='/' else u.path[1:]
                raw=bounded(self.server.assets,name,4*1024*1024)
                content={'html':'text/html; charset=utf-8','js':'application/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[name.rsplit('.',1)[-1]]
                self.send(200,raw,content);return
            if u.path=='/healthz':
                self.server.store.load();self.send(200,b'ok\n','text/plain');return
            value,lookup,pinned=self.server.store.load();snapshot=value['snapshot']
            tasks={t['id']:t for t in value['tasks']};runs={r['id']:r for r in value['runs']}
            parts=u.path.strip('/').split('/')
            if u.path=='/api/meta':
                if query:raise ValueError('meta query unsupported')
                body={'schema':'phonefarm_web/meta@1','snapshot':snapshot,
                      'counts':{'tasks':len(tasks),**{k:sum(r['kind']==k for r in runs.values()) for k in ['ui_run','confirmation','model_pipeline']}},
                      'diagnostics':value['diagnostics'],'device_live':False}
                body['counts']['ui_runs']=body['counts'].pop('ui_run')
            elif u.path=='/api/tasks':
                if set(query)-{'offset','limit'}:raise ValueError('unknown task query')
                body=page(query,value['tasks'],snapshot)
            elif u.path=='/api/runs' or len(parts)==4 and parts[:2]==['api','tasks'] and parts[3]=='runs':
                if set(query)-{'offset','limit','task','kind'}:raise ValueError('unknown run query')
                task=parts[2] if len(parts)==4 else query.get('task',[None])[0]
                if len(parts)==4 and 'task' in query:raise ValueError('ambiguous task query')
                if task is not None and task not in tasks:raise KeyError('unknown task')
                kind=query.get('kind',['all'])[0]
                if kind not in {'all','ui_run','confirmation','model_pipeline'}:raise ValueError('unknown run kind')
                rows=[public_run(r) for r in value['runs'] if (task is None or r['task_id']==task) and (kind=='all' or r['kind']==kind)]
                body=page(query,rows,snapshot)
            elif len(parts)>=3 and parts[:2]==['api','runs']:
                r=runs.get(parts[2])
                if r is None:raise KeyError('unknown run')
                if len(parts)==3:
                    if query:raise ValueError('run detail query unsupported')
                    body={'schema':'phonefarm_web/run_detail@1','snapshot':snapshot,'run':public_run(r),
                          'steps':r['steps'][:100],'step_total':len(r['steps']),
                          'artifacts':[public_artifact(value['artifacts'][a]) for a in r['artifacts']],
                          'diagnostics':r['diagnostics']}
                elif len(parts)==4 and parts[3]=='steps':
                    if set(query)-{'offset','limit'}:raise ValueError('unknown step query')
                    body=page(query,r['steps'],snapshot)
                elif len(parts)==5 and parts[3]=='steps' and re.fullmatch(r'\d{1,9}',parts[4]):
                    if query:raise ValueError('step detail query unsupported')
                    step=next((s for s in r['steps'] if s['index']==int(parts[4])),None)
                    if step is None:raise KeyError('unknown step')
                    body={'schema':'phonefarm_web/step@1','snapshot':snapshot,'step':step}
                else:raise KeyError('unknown route')
            elif len(parts)==3 and parts[:2]==['api','artifacts']:
                if query:raise ValueError('artifact query unsupported')
                raw=self.server.store.artifact(value,lookup,pinned,parts[2]);self.send(200,raw);return
            elif u.path=='/api/dashboard':
                if query:raise ValueError('dashboard query unsupported')
                body=value.get('dashboard')
                if body is None:raise KeyError('native dashboard snapshot unavailable')
            else:raise KeyError('unknown route')
            self.send(200,catalog.encode(body))
        except KeyError:self.error(404,'not found')
        except (ValueError,UnicodeError):self.error(400,'invalid query or snapshot')
        except FileNotFoundError:self.error(404,'not found')
        except OSError:self.error(503,'snapshot unavailable')


def make_server(snapshot,assets=None,host='127.0.0.1',port=8711):
    s=ThreadingHTTPServer((host,port),Handler);s.store=Store(snapshot)
    s.store.load()  # Startup must fail if synchronization is incomplete.
    s.assets=Path(assets) if assets else None
    return s


def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',required=True);p.add_argument('--assets')
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8711);a=p.parse_args()
    server=make_server(a.snapshot,a.assets,a.host,a.port)
    print(json.dumps({'host':a.host,'port':a.port,'source_mode':'synchronized_snapshot','device_live':False}),flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':main()
