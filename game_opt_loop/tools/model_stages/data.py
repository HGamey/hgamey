"""Native 3/2 pairs or explicit synthetic input; frame split precedes patches."""
import math,random,types
from pathlib import Path
import protocol as P
VALIDATOR_SHA='71dc98567f08cef9375fd509ad9851226622f37545bfbd2e1ff2d442f0815fd0'
DATA_KEYS=['schema','source','kind','seed','steps','batch','learning_rate','train_frame_ids','heldout_frame_ids','capture','synthetic_frames','patches_per_frame']

def config(v):
 P.keys(v,DATA_KEYS,'dataset');P.require(v['schema']=='game_opt_loop/paired_training_dataset@1','dataset schema')
 P.require(v['source'] in ('synthetic','unknown'),'unattested dataset source');P.integer(v['seed'],0,2**31-1,'seed');P.integer(v['steps'],1,128,'steps');P.integer(v['batch'],1,2,'batch');P.integer(v['patches_per_frame'],1,8,'patch count')
 lr=v['learning_rate'];P.require(type(lr) in (float,int) and math.isfinite(lr) and 0<lr<=0.01,'learning rate')
 for k in ['train_frame_ids','heldout_frame_ids']:
  a=v[k];P.require(isinstance(a,list) and 1<=len(a)<=16 and all(type(i) is int and 0<=i<=100000 for i in a) and len(set(a))==len(a),'frame identities')
 P.require(set(v['train_frame_ids']).isdisjoint(v['heldout_frame_ids']),'train/heldout frame overlap')
 if v['kind']=='native_refbench':P.require(v['source']=='unknown' and v['synthetic_frames']==[] and isinstance(v['capture'],dict),'native declared purpose/source')
 elif v['kind']=='synthetic_rgb':P.require(v['source']=='synthetic' and v['capture'] is None,'synthetic purpose/source')
 else:raise P.Rejected('unsupported dataset kind; no fallback')
 return v

def native_contract():
 path=Path(__file__).parent/'refbench_native_pairs_v1.py';b=P.read(path.absolute());P.require(P.hash_bytes(b)==VALIDATOR_SHA,'pinned refbench consumer changed')
 m=types.ModuleType('fixed_native_contract');m.__file__=str(path);exec(compile(b,str(path),'exec'),m.__dict__);return m

def frames(v):
 """Return checked immutable RGB snapshots, with actual original identities."""
 config(v);out=[];total=0;provenance={}
 if v['kind']=='native_refbench':
  c=v['capture'];P.keys(c,['manifest','rules','candidate_spirv'],'native capture refs')
  for x in c.values():P.keys(x,['path','sha256'],'capture ref');P.snapshot(x)
  n=native_contract();m,mb,r,rb,candidate,blobs,completion,output=n.load_capture(c['manifest']['path'],c['rules']['path'],c['candidate_spirv']['path'])
  P.require(P.hash_bytes(mb)==c['manifest']['sha256'] and P.hash_bytes(rb)==c['rules']['sha256'] and P.hash_bytes(candidate)==c['candidate_spirv']['sha256'],'capture input changed')
  P.require(m['source']=='native_rendered_controlled_whitebox' and m['scene_content']=='procedural_alu' and m['count']>=4,'native source declaration/count; GPU authenticity remains unknown')
  provenance={'capture_manifest_sha256':P.hash_bytes(mb),'completion_sha256':P.hash_bytes(completion),'run_output_sha256':P.hash_bytes(output),'source_attestation':'not_checked','capture_mode':'quality_only','real_game_qualification':False,'declared_native_source':'native_rendered_controlled_whitebox'}
  for f in m['frames']:
   images={i['role']:i for i in f['images']};a,b=images['native_lr'],images['native_hr'];P.require((a['width'],a['height'])==(1280,720) and (b['width'],b['height'])==(1920,1080),'native source geometry must be3/2')
   lr=blobs[a['file']];hr=blobs[b['file']];total+=len(lr)+len(hr);P.require(total<=P.RAW_LIMIT,'dataset raw budget')
   P.require(all(x==255 for x in lr[3::4]) and all(x==255 for x in hr[3::4]),'unsupported raw alpha')
   # Remove alpha only; never downsample HR to manufacture LR.
   rgb=lambda x: bytes(z for i,z in enumerate(x) if i%4!=3)
   out.append({'frame_id':f['frame_index'],'host_frame_id':f['host_frame_id'],'timestamp_ns':f['timestamp_ns'],'phase_bits':f['phase_bits'],'lr':rgb(lr),'hr':rgb(hr),'lr_shape':[720,1280,3],'hr_shape':[1080,1920,3],'lr_original_sha256':a['sha256'],'hr_original_sha256':b['sha256'],'color':'linear_shader_UNORM8_to_RGB_float32_div255'})
 else:
  P.require(isinstance(v['synthetic_frames'],list) and 2<=len(v['synthetic_frames'])<=16,'synthetic frame count')
  for f in v['synthetic_frames']:
   P.keys(f,['frame_id','lr','hr','lr_shape','hr_shape','color'],'synthetic frame');P.integer(f['frame_id'],0,100000,'frame');P.require(f['color']=='linear_rgb8','synthetic color')
   a,b=f['lr_shape'],f['hr_shape'];P.require(len(a)==len(b)==3 and all(type(x) is int for x in a+b) and a[2]==b[2]==3 and 16<=a[0]<=64 and 16<=a[1]<=64 and a[0]%2==a[1]%2==0 and b[0]*2==a[0]*3 and b[1]*2==a[1]*3,'synthetic integer3/2 dimensions')
   lr=P.snapshot(f['lr'],P.RAW_LIMIT);hr=P.snapshot(f['hr'],P.RAW_LIMIT);P.require(len(lr)==math.prod(a) and len(hr)==math.prod(b),'raw shape/size mismatch');total+=len(lr)+len(hr);P.require(total<=P.RAW_LIMIT,'dataset raw cap')
   out.append(dict(f,lr=lr,hr=hr,lr_original_sha256=f['lr']['sha256'],hr_original_sha256=f['hr']['sha256'],host_frame_id='synthetic:'+str(f['frame_id']),timestamp_ns=None,phase_bits=None))
  provenance={'source':'synthetic','source_attestation':'not_checked','gpu_capture':False,'real_game_qualification':False}
 ids=[f['frame_id'] for f in out];P.require(len(set(ids))==len(ids),'duplicate source frame identity');P.require(set(ids)==set(v['train_frame_ids'])|set(v['heldout_frame_ids']),'all frames must have one frozen split')
 return out,provenance

def patch_grid(f,count,seed):
 h,w,_=f['lr_shape'];P.require(h%2==w%2==0,'even LR geometry')
 grid=[(y,x) for y in range(0,h-15,16) for x in range(0,w-15,16)];P.require(len(grid)>=count,'not enough nonoverlapping registered patches')
 rng=random.Random(seed+f['frame_id']);selected=sorted(rng.sample(grid,count))
 return [{'lr_origin':[y,x],'hr_origin':[y*3//2,x*3//2],'lr_shape':[16,16,3],'hr_shape':[24,24,3]} for y,x in selected]

def arrays(v,checked):
 import numpy as np
 result={s:{'lr':[],'hr':[],'identities':[]} for s in ['train','heldout']}
 for f in checked:
  split='train' if f['frame_id'] in v['train_frame_ids'] else 'heldout';a=np.frombuffer(f['lr'],dtype=np.uint8).reshape(f['lr_shape']);b=np.frombuffer(f['hr'],dtype=np.uint8).reshape(f['hr_shape'])
  for patch in patch_grid(f,v['patches_per_frame'],v['seed']):
   y,x=patch['lr_origin'];hy,hx=patch['hr_origin'];lr=a[y:y+16,x:x+16,:].astype(np.float32)/255.0;hr=b[hy:hy+24,hx:hx+24,:].astype(np.float32)/255.0
   P.require(lr.shape==(16,16,3) and hr.shape==(24,24,3) and np.isfinite(lr).all() and np.isfinite(hr).all(),'patch shape/nonfinite')
   result[split]['lr'].append(lr);result[split]['hr'].append(hr);result[split]['identities'].append(dict(patch,frame_id=f['frame_id'],host_frame_id=f['host_frame_id'],lr_raw_sha256=f['lr_original_sha256'],hr_raw_sha256=f['hr_original_sha256']))
 for value in result.values():
  value['lr']=np.stack(value['lr']);value['hr']=np.stack(value['hr'])
 return result
