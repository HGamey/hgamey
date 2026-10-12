"""Explicit actual CPU runtime tests. Never run by default Rust tests.
Requires GOL_MODEL_TEST_PYTHON and GOL_MODEL_TEST_GOL, real local TensorFlow2.20.
Data/output source synthetic; actual math/format evidence, no Android/game gain.
"""
import copy,hashlib,json,os,shutil,subprocess,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools/model_stages'))
import protocol as P
PYTHON=os.environ.get('GOL_MODEL_TEST_PYTHON');GOL=os.environ.get('GOL_MODEL_TEST_GOL')
@unittest.skipUnless(PYTHON and GOL,'explicit actual runtime/tool paths absent: NOT_RUN')
class Runtime(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  base=Path(os.environ.get('GOL_MODEL_TEST_ARTIFACT_DIR',str(ROOT/'tasks/local_model_stages_20261012/runtime-tests'))).absolute();base.mkdir(mode=0o700,parents=True,exist_ok=False);cls.base=base.resolve();cls.n=0
  cls.good=cls.base/'actual-positive';cls.command('make-positive',[sys.executable,str(ROOT/'tools/model_stages/make_plan.py'),'--out',str(cls.good),'--python',PYTHON,'--synthetic'],0,10)
  out=cls.command('actual-pipeline',[GOL,'model-pipeline','start','--plan',str(cls.good/'plan.json'),'--out',str(cls.good/'pipeline')],1,360)
  journal=json.loads(out);assert [x['state'] for x in journal['entries']]==['completed','completed','completed','failed','pending'],journal['entries']
  cls.journal=journal;cls.quant_request=json.loads((cls.good/'pipeline/2_quantize/request.json').read_bytes())
 @classmethod
 def command(cls,label,args,expected,timeout):
  cls.n+=1;prefix=cls.base/f'{cls.n:02}-{label}';begin=time.monotonic();argv=list(map(str,args));started=time.time();p=subprocess.run(argv,capture_output=True,timeout=timeout,env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
  prefix.with_suffix('.stdout').write_bytes(p.stdout);prefix.with_suffix('.stderr').write_bytes(p.stderr);prefix.with_suffix('.command.json').write_text(json.dumps({'argv':argv,'exit_code':p.returncode,'started_unix_s':started,'finished_unix_s':time.time(),'wall_seconds':time.monotonic()-begin,'stdout_sha256':P.hash_bytes(p.stdout),'stderr_sha256':P.hash_bytes(p.stderr),'device_calls':0,'model_api_calls':0},indent=2)+'\n');assert p.returncode==expected,(label,p.returncode,p.stderr.decode());return p.stdout
 def direct(self,label,mutator,stage='quantize'):
  d=self.base/label;d.mkdir();request=copy.deepcopy(self.quant_request);request['stage']=stage;request['budget']['wall_ms']=120000 if stage=='quantize' else 30000
  mutator(request,d)
  for o in request['outputs']:o['path']=str(d/o['name'])
  (d/'request.json').write_bytes(P.encode(request));receipt=d/'receipt.json';env={'PATH':'/usr/bin:/bin','LC_ALL':'C','GOL_PIPELINE_REQUEST':str(d/'request.json'),'GOL_PIPELINE_RECEIPT':str(receipt),'GOL_PIPELINE_STAGE_DIR':str(d)}
  runtime_cfg=self.good/'runtime-config.json';program=json.loads((self.good/'plan.json').read_bytes())['stages'][0]['argv'][0];argv=[program,str(ROOT/'tools/model_stages/stage_driver.py'),str(runtime_cfg)];begin=time.monotonic();p=subprocess.run(argv,env=env,capture_output=True,timeout=130)
  (d/'stdout').write_bytes(p.stdout);(d/'stderr').write_bytes(p.stderr);(d/'command.json').write_text(json.dumps({'argv':argv,'exit_code':p.returncode,'elapsed_s':time.monotonic()-begin,'source':'synthetic','device_calls':0,'model_api_calls':0},indent=2)+'\n');self.assertEqual(p.returncode,2,p.stderr.decode());r=json.loads(receipt.read_bytes());self.assertEqual(r['status'],'failed');self.assertEqual(r['outputs'],[]);self.assertEqual(r['request_sha256'],P.digest(request));return r
 def test_actual_backprop_h5_tflite_and_no_android(self):
  d=self.good/'pipeline';t=json.loads((d/'1_train/train-evidence.json').read_bytes());q=json.loads((d/'2_quantize/quantize-evidence.json').read_bytes());self.assertTrue(t['actual_gradient_tape']);self.assertTrue(t['actual_h5_reload']);self.assertEqual(t['actual_optimizer_steps'],8);self.assertGreater(t['maximum_absolute_weight_change'],0);self.assertNotEqual(t['initial_weights_sha256'],t['final_weights_sha256']);self.assertEqual(t['source'],'synthetic');self.assertEqual(t['train_frame_ids'],[0,1,2]);self.assertEqual(t['heldout_frame_ids'],[3]);self.assertEqual(len(q['models']),2);self.assertGreater(q['models'][1]['float16_tensor_count'],0)
  for m in q['models']:self.assertTrue(m['host_cpu_invocation_finite']);self.assertEqual(m['input_shape'],[1,16,16,3]);self.assertEqual(m['output_shape'],[1,24,24,3]);self.assertEqual(m['android_delegate'],'NOT_RUN')
  self.assertFalse(self.journal['real_game_optimization_proven']);self.assertFalse(self.journal['promotion_allowed']);self.assertFalse(self.journal['model_semantics_verified']);self.assertEqual(self.journal['entries'][4]['state'],'pending')
 def test_resume_does_not_retrain_failed_deploy(self):
  d=self.good/'pipeline';before={str(p.relative_to(d)):P.hash_bytes(p.read_bytes()) for p in d.glob('*/*') if p.is_file()}
  self.command('resume-no-retrain',[GOL,'model-pipeline','resume','--out',str(d)],1,10)
  after={str(p.relative_to(d)):P.hash_bytes(p.read_bytes()) for p in d.glob('*/*') if p.is_file()};self.assertEqual(before,after)
 def test_tampered_actual_weights_rejected_before_export(self):
  def change(r,d):
   a=next(a for a in r['inputs'] if a['name']=='trained.weights.h5');b=bytearray(P.read(a['path']));b[-1]^=1;p=d/'tampered.weights.h5';p.write_bytes(b);a['path']=str(p);a['sha256']=P.hash_bytes(b)
  result=self.direct('tampered-h5',change);self.assertIn('bound actual H5',result['note'])
 def test_split_overlap_rejected_without_runtime(self):
  def change(r,d):
   a=next(a for a in r['inputs'] if a['name']=='dataset');v=P.parse(P.read(a['path']));v['heldout_frame_ids']=[2];p=d/'dataset.json';p.write_bytes(P.encode(v));a['path']=str(p);a['sha256']=P.hash_bytes(P.read(p))
  result=self.direct('overlap',change);self.assertIn('overlap',result['note'])
 def test_raw_change_rejected_without_downsample_fallback(self):
  def change(r,d):
   a=next(a for a in r['inputs'] if a['name']=='dataset');v=P.parse(P.read(a['path']));p=d/'changed.rgb';p.write_bytes(b'wrong');v['synthetic_frames'][0]['lr']['path']=str(p);q=d/'dataset.json';q.write_bytes(P.encode(v));a['path']=str(q);a['sha256']=P.hash_bytes(P.read(q))
  result=self.direct('changed-raw',change);self.assertIn('input SHA mismatch',result['note'])
 def test_nonfinite_actual_h5_rejected_after_reload(self):
  def change(r,d):
   a=next(a for a in r['inputs'] if a['name']=='trained.weights.h5');path=d/'nan.weights.h5';path.write_bytes(P.read(a['path']))
   cfg=json.loads((self.good/'runtime-config.json').read_bytes());program=cfg['python_executable']
   code="import sys;sys.path.insert(0,sys.argv[1]);import h5py;f=h5py.File(sys.argv[2],'r+');names=[];f.visititems(lambda n,o: names.append(n) if isinstance(o,h5py.Dataset) and o.dtype.kind=='f' else None);assert names;v=f[names[0]][:];v.flat[0]=float('nan');f[names[0]][:]=v;f.close()"
   self.command('write-real-h5-nan',[program,'-c',code,cfg['site_packages'],str(path)],0,30)
   a['path']=str(path);a['sha256']=P.hash_bytes(P.read(path));meta=next(x for x in r['inputs'] if x['name']=='train-evidence.json');v=P.parse(P.read(meta['path']));v['weights_sha256']=a['sha256'];m=d/'rehashed-evidence.json';m.write_bytes(P.encode(v));meta['path']=str(m);meta['sha256']=P.hash_bytes(P.read(m))
  result=self.direct('nonfinite-real-h5',change);self.assertIn('NONFINITE_WEIGHTS',result['note'])
 def test_wrong_three_halves_geometry_rejected(self):
  def change(r,d):
   a=next(a for a in r['inputs'] if a['name']=='dataset');v=P.parse(P.read(a['path']));v['synthetic_frames'][0]['hr_shape']=[32,32,3];p=d/'geometry.json';p.write_bytes(P.encode(v));a['path']=str(p);a['sha256']=P.hash_bytes(P.read(p))
  result=self.direct('wrong-ratio',change);self.assertIn('3/2 dimensions',result['note'])
 def test_explicit_evaluate_unsupported_even_with_real_host_models(self):
  def change(r,d):
   r['outputs']=[{'name':'evaluation.json','role':'evaluation_report','path':str(d/'evaluation.json'),'format':'explicit_unimplemented_android_receipt','source':'synthetic'}]
  result=self.direct('evaluate-blocked',change,'evaluate');self.assertIn('BLOCKED_PENDING_ACTUAL_DEVICE_RUNTIME',result['note'])
if __name__=='__main__':unittest.main(verbosity=2)
