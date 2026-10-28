"""Bounded existing GOL model-stage wire; no TensorFlow/device/network imports."""
import hashlib,json,math,os,re,stat,time
from pathlib import Path
JSON_LIMIT=1024*1024
OUTPUT_LIMIT=4*1024*1024
RAW_LIMIT=384*1024*1024
STAGES={'design','train','quantize','deploy','evaluate'}
ROLES={'design':'architecture','train':'weights','quantize':'quantized_model','deploy':'deployment_receipt','evaluate':'evaluation_report'}
WALL={'design':30000,'train':180000,'quantize':120000,'deploy':30000,'evaluate':30000}
OUTPUT_CONTRACT={
 'design':{'architecture.json':('architecture','game_opt_loop/local_sr_architecture@1'),'design-evidence.json':('metadata','game_opt_loop/local_design_evidence@1')},
 'train':{'trained.weights.h5':('weights','keras_weights_hdf5'),'train-evidence.json':('metadata','game_opt_loop/local_training_evidence@1')},
 'quantize':{'model-fp32.tflite':('quantized_model','tflite_flatbuffer_fp32'),'model-fp16.tflite':('quantized_model','tflite_flatbuffer_float16_weights'),'quantize-evidence.json':('metadata','game_opt_loop/local_quantization_evidence@1')},
 'deploy':{'deployment.json':('deployment_receipt','explicit_unimplemented_android_receipt')},
 'evaluate':{'evaluation.json':('evaluation_report','explicit_unimplemented_android_receipt')}
}
INPUT_CONTRACT={'dataset':('data','game_opt_loop/paired_training_dataset@1'),**{name:value for stage in OUTPUT_CONTRACT.values() for name,value in stage.items()}}
class Rejected(ValueError):pass
def require(c,m):
 if not c:raise Rejected(m)
def hash_bytes(b):return hashlib.sha256(b).hexdigest()
def object_unique(pairs):
 d={}
 for k,v in pairs:require(k not in d,'duplicate JSON key');d[k]=v
 return d
def parse(b):
 def finite_float(text):
  value=float(text);require(math.isfinite(value),'nonfinite JSON');return value
 return json.loads(b,object_pairs_hook=object_unique,parse_float=finite_float,parse_constant=lambda _:(_ for _ in ()).throw(Rejected('nonfinite JSON')))
def encode(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(v):return hash_bytes(encode(v))
def hash_ok(s):return isinstance(s,str) and re.fullmatch('[0-9a-f]{64}',s) is not None
def integer(v,lo,hi,n):require(type(v) is int and lo<=v<=hi,'invalid '+n)
def keys(d,expected,n):require(isinstance(d,dict) and set(d)==set(expected),'unknown/missing '+n+' fields')
def absolute(p):
 p=Path(p);require(p.is_absolute(),'absolute path required')
 require(all(x not in ('.','..','private-auth') for x in p.parts),'forbidden path component')
 return p
def directory(p):
 p=absolute(p);fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  for part in p.parts[1:]:
   nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
  return fd
 except BaseException:os.close(fd);raise
def read(p,limit=JSON_LIMIT):
 p=absolute(p);d=directory(p.parent)
 try:fd=os.open(p.name,os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=d)
 finally:os.close(d)
 with os.fdopen(fd,'rb') as f:
  a=os.fstat(f.fileno());require(stat.S_ISREG(a.st_mode) and a.st_size<=limit,'nonregular/oversize input');b=f.read(limit+1);z=os.fstat(f.fileno())
 require(len(b)<=limit and a.st_dev==z.st_dev and a.st_ino==z.st_ino and a.st_size==z.st_size==len(b) and a.st_mtime_ns==z.st_mtime_ns,'input changed while snapshotting')
 return b
def snapshot(ref,limit=JSON_LIMIT):
 require(hash_ok(ref['sha256']),'missing input SHA');b=read(ref['path'],limit);require(hash_bytes(b)==ref['sha256'],'input SHA mismatch');return b
def write_new(p,b):
 p=absolute(p);require(len(b)<=OUTPUT_LIMIT,'output cap');d=directory(p.parent)
 try:
  fd=os.open(p.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=d)
  with os.fdopen(fd,'wb') as f:f.write(b);f.flush();os.fsync(f.fileno())
  os.fsync(d)
 finally:os.close(d)
def atomic_receipt(p,value):
 p=absolute(p);d=directory(p.parent);tmp=p.name+'.publishing'
 try:
  # Final receipt must not already exist; generic pipeline manages retries.
  try:os.stat(p.name,dir_fd=d,follow_symlinks=False)
  except FileNotFoundError:pass
  else:raise Rejected('receipt already exists')
  fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=d)
  with os.fdopen(fd,'wb') as f:f.write(encode(value)+b'\n');f.flush();os.fsync(f.fileno())
  os.link(tmp,p.name,src_dir_fd=d,dst_dir_fd=d,follow_symlinks=False);os.unlink(tmp,dir_fd=d);os.fsync(d)
 finally:os.close(d)
class Session:
 def __init__(self,environ=None):
  e=os.environ if environ is None else environ
  self.start=time.monotonic();self.path=absolute(e['GOL_PIPELINE_REQUEST']);self.receipt=absolute(e['GOL_PIPELINE_RECEIPT']);self.dir=absolute(e['GOL_PIPELINE_STAGE_DIR']);self.request=parse(read(self.path))
  r=self.request;keys(r,['schema','pipeline_id','plan_sha256','stage','model','inputs','drivers','outputs','budget'],'request')
  require(r['schema']=='game_opt_loop/model_stage_request@1' and r['stage'] in STAGES and hash_ok(r['plan_sha256']),'request schema/stage/plan')
  require(self.path.parent==self.dir and self.receipt.parent==self.dir and self.path.name=='request.json' and self.receipt.name=='receipt.json','fixed stage wire paths')
  self.stage=r['stage'];b=r['budget'];keys(b,['wall_ms','output_bytes','steps','tokens','device_ms'],'budget')
  integer(b['wall_ms'],1,WALL[self.stage],'wall');integer(b['output_bytes'],1,OUTPUT_LIMIT,'output');integer(b['steps'],0,128,'steps');require(b['tokens']==0 and b['device_ms']==0,'API/device forbidden')
  require(isinstance(r['inputs'],list) and 1<=len(r['inputs'])<=128 and isinstance(r['drivers'],list) and r['drivers'],'bounded inputs/drivers required')
  self.inputs={};names=set()
  for a in r['inputs']:
   keys(a,['name','role','path','sha256','format','source'],'input');require(a['name'] not in names,'duplicate input name');names.add(a['name']);require(a['source'] in ('unknown','synthetic'),'driver does not authenticate real-game source')
   require(a['name'] in INPUT_CONTRACT and (a['role'],a['format'])==INPUT_CONTRACT[a['name']],'input role/format differs from fixed local stage contract')
   # Capture blobs are data-contract references, read by the bounded data adapter.
   self.inputs[a['name']]=(a,snapshot(a,OUTPUT_LIMIT if a['role']=='weights' else JSON_LIMIT))
  for a in r['drivers']:snapshot(a,512*1024*1024)
  self.cache=None;self.cache_owner=None;self.side_evidence=None;self.outputs={};self.records=[];source='synthetic' if any(a['source']=='synthetic' for a in r['inputs']) else 'unknown'
  require(isinstance(r['outputs'],list) and 1<=len(r['outputs'])<=8,'output set bound')
  for a in r['outputs']:
   keys(a,['name','role','path','format','source'],'output');require(re.fullmatch('[a-zA-Z0-9_.-]{1,128}',a['name']) and a['name'] not in ('.','..','request.json','receipt.json','stdout.log','stderr.log') and a['name'] not in self.outputs,'output name')
   require(absolute(a['path'])==self.dir/a['name'] and a['role'] in (ROLES[self.stage],'metadata') and a['source']==source,'output role/path/source differs')
   self.outputs[a['name']]=a
  require(set(self.outputs)==set(OUTPUT_CONTRACT[self.stage]) and all((a['role'],a['format'])==OUTPUT_CONTRACT[self.stage][name] for name,a in self.outputs.items()),'output role/format/set differs from fixed local stage contract');self.source=source;self.total=0
 def create_runtime_cache(self):
  require(self.cache is None,'runtime cache already registered')
  path=self.dir.parent/('runtime-cache-'+self.stage);d=directory(self.dir.parent)
  try:os.mkdir(path.name,0o700,dir_fd=d);os.fsync(d)
  finally:os.close(d)
  self.cache=path;self.cache_owner={'schema':'game_opt_loop/local_runtime_cache_owner@1','pipeline_id':self.request['pipeline_id'],'plan_sha256':self.request['plan_sha256'],'stage':self.stage,'request_sha256':digest(self.request),'path':str(path),'scope':'exclusive local runtime configuration, counted within frozen4MiB host output budget'}
  write_new(path/'owner.json',encode(self.cache_owner)+b'\n');self.check_cost();return path
 def check_cost(self):
  files=[];total=0;d=directory(self.dir)
  try:
   count=0
   with os.scandir(d) as entries:
    for e in entries:
     count+=1;require(count<=64,'flat stage entry cap');m=e.stat(follow_symlinks=False);require(stat.S_ISREG(m.st_mode),'flat stage must contain regular files only');total+=m.st_size
  finally:os.close(d)
  if self.cache is not None:
   d=directory(self.cache)
   try:
    seen=set()
    with os.scandir(d) as entries:
     for e in entries:
      require(e.name in ('owner.json','keras.json') and e.name not in seen,'unknown runtime cache file');seen.add(e.name);m=e.stat(follow_symlinks=False);require(stat.S_ISREG(m.st_mode) and m.st_size<=65536,'runtime cache must be bounded regular files only');b=read(self.cache/e.name,65536);total+=len(b);files.append({'file':e.name,'bytes':len(b),'sha256':hash_bytes(b)})
    require('owner.json' in seen and parse(read(self.cache/'owner.json'))==self.cache_owner,'runtime cache owner changed')
   finally:os.close(d)
  require(total<=self.request['budget']['output_bytes'],'stage plus runtime cache exceed frozen host output budget')
  self.side_evidence={'runtime_cache_path':str(self.cache) if self.cache else None,'runtime_cache_owner_sha256':next((x['sha256'] for x in files if x['file']=='owner.json'),None),'runtime_cache_files':files,'runtime_cache_file_count':len(files),'runtime_cache_bytes':sum(x['bytes'] for x in files),'stage_plus_runtime_cache_bytes':total,'host_output_cap_bytes':self.request['budget']['output_bytes'],'scope':'current regular file inventory/bytes; wall includes imports; not OS disk quota or dynamic dependency attestation'}
  return self.side_evidence
 def input(self,name,role=None):
  require(name in self.inputs,'required upstream/input absent: '+name);a,b=self.inputs[name];require(role is None or a['role']==role,'input role mismatch');return parse(b)
 def emit(self,name,data):
  require(name in self.outputs,'undeclared output');self.check_wall();self.check_cost();require(name not in [r['name'] for r in self.records],'duplicate output emission');self.total+=len(data);require(self.total<=self.request['budget']['output_bytes'],'combined output budget');write_new(self.outputs[name]['path'],data);self.records.append({'name':name,'sha256':hash_bytes(data)})
 def check_wall(self):
  require((time.monotonic()-self.start)*1000<self.request['budget']['wall_ms'],'driver wall deadline');self.check_cost()
 def finish(self,status,note,steps=0):
  self.check_wall();integer(steps,0,self.request['budget']['steps'],'actual steps');require(status in ('succeeded','failed'),'receipt status')
  require(status!='succeeded' or {x['name'] for x in self.records}==set(self.outputs),'missing declared output')
  for record in self.records:require(hash_bytes(read(self.outputs[record['name']]['path'],OUTPUT_LIMIT))==record['sha256'],'output changed before receipt')
  v={'schema':'game_opt_loop/model_stage_receipt@1','pipeline_id':self.request['pipeline_id'],'plan_sha256':self.request['plan_sha256'],'stage':self.stage,'request_sha256':digest(self.request),'status':status,'usage':{'steps':steps,'tokens':0,'device_ms':0},'outputs':self.records if status=='succeeded' else [],'note':note}
  self.check_wall();cost=self.check_cost();require(cost['stage_plus_runtime_cache_bytes']+len(encode(v))+1<=self.request['budget']['output_bytes'],'receipt plus stage/runtime cache exceed frozen host output budget')
  atomic_receipt(self.receipt,v);return v
