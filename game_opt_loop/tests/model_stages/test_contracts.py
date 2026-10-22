import copy,hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'tools/model_stages'))
import protocol as P
import data as D
class Contracts(unittest.TestCase):
 def test_duplicate_nonfinite_json(self):
  for b in [b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e999}',b'{"x":-1e999}']:
   with self.assertRaises(ValueError):P.parse(b)
 def test_frame_split_and_geometry_scope(self):
  v={'schema':'game_opt_loop/paired_training_dataset@1','source':'unknown','kind':'native_refbench','seed':1,'steps':8,'batch':1,'learning_rate':0.001,'train_frame_ids':[0,1,2],'heldout_frame_ids':[3],'capture':{},'synthetic_frames':[],'patches_per_frame':1}
  self.assertEqual(D.config(v),v)
  for k,x in [('heldout_frame_ids',[2]),('source','synthetic'),('steps',129),('batch',3),('learning_rate',float('inf'))]:
   bad=copy.deepcopy(v);bad[k]=x
   with self.assertRaises(ValueError):D.config(bad)
 def test_integer_same_footprint_grid(self):
  f={'lr_shape':[720,1280,3],'frame_id':3};a=D.patch_grid(f,4,23);self.assertEqual(a,D.patch_grid(f,4,23))
  for p in a:self.assertEqual([x*3//2 for x in p['lr_origin']],p['hr_origin']);self.assertTrue(all(x%2==0 for x in p['lr_origin']))
 def test_current_pinned_native_consumer_identity(self):
  n=D.native_contract();self.assertEqual(n.SOURCE_NATIVE,'native_rendered_controlled_whitebox')
  with self.assertRaisesRegex(ValueError,'nonfinite'):n.parse(b'{"x":1e999}')
  with tempfile.TemporaryDirectory() as td:
   d=Path(td).resolve();p=d/'image.rgba';p.write_bytes(b'bytes');alias=d/'alias';alias.symlink_to(d,target_is_directory=True)
   with self.assertRaises(OSError):n.regular_bytes(alias/'image.rgba',128)
 def test_same_fd_snapshot_and_symlink_rejection(self):
  with tempfile.TemporaryDirectory() as td:
   d=Path(td).resolve();p=d/'data';p.write_bytes(b'bytes');self.assertEqual(P.read(p),b'bytes');s=d/'alias';s.symlink_to(p)
   with self.assertRaises(OSError):P.read(s)
   with self.assertRaises(ValueError):P.snapshot({'path':str(p),'sha256':'0'*64})
   alias=d/'diralias';alias.symlink_to(d,target_is_directory=True)
   with self.assertRaises(OSError):P.read(alias/'data')
 def test_atomic_create_only_receipt_and_outputs(self):
  with tempfile.TemporaryDirectory() as td:
   d=Path(td).resolve();p=d/'receipt.json';P.atomic_receipt(p,{'status':'failed'});self.assertEqual(P.parse(P.read(p))['status'],'failed')
   with self.assertRaises(ValueError):P.atomic_receipt(p,{'status':'succeeded'})
   o=d/'weights';P.write_new(o,b'x')
   with self.assertRaises(FileExistsError):P.write_new(o,b'replacement')
 def test_exclusive_cache_owner_unknown_files_and_links(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();d=root/'1_train';d.mkdir();s=P.Session.__new__(P.Session);s.dir=d;s.stage='train';s.cache=None;s.cache_owner=None;s.request={'pipeline_id':'fixture','plan_sha256':'a'*64,'stage':'train','budget':{'output_bytes':4194304}};s.side_evidence=None
   cache=s.create_runtime_cache();self.assertEqual(s.check_cost()['runtime_cache_file_count'],1);self.assertEqual(s.check_cost()['runtime_cache_owner_sha256'],P.hash_bytes(P.read(cache/'owner.json')))
   (cache/'unexpected').write_bytes(b'x')
   with self.assertRaisesRegex(ValueError,'unknown runtime'):s.check_cost()
   (cache/'unexpected').unlink();(cache/'keras.json').symlink_to(cache/'owner.json')
   with self.assertRaisesRegex(ValueError,'regular'):s.check_cost()
   (cache/'keras.json').unlink();(cache/'owner.json').write_bytes(b'{}')
   with self.assertRaisesRegex(ValueError,'owner changed'):s.check_cost()
 def test_cache_is_included_in_same_frozen_output_budget(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td).resolve();d=root/'1_train';d.mkdir();s=P.Session.__new__(P.Session);s.dir=d;s.stage='train';s.cache=None;s.cache_owner=None;s.request={'pipeline_id':'fixture','plan_sha256':'a'*64,'stage':'train','budget':{'output_bytes':1024}};s.side_evidence=None
   cache=s.create_runtime_cache();(cache/'keras.json').write_bytes(b'x'*800)
   with self.assertRaisesRegex(ValueError,'runtime cache exceed'):s.check_cost()
 def test_explicit_missing_native_never_synthetic_fallback(self):
  v={'schema':'game_opt_loop/paired_training_dataset@1','source':'unknown','kind':'native_refbench','seed':1,'steps':8,'batch':1,'learning_rate':0.001,'train_frame_ids':[0,1,2],'heldout_frame_ids':[3],'capture':{'manifest':{'path':'/NO_SUCH_NATIVE_CAPTURE/manifest.json','sha256':'0'*64},'rules':{'path':'/NO_SUCH_NATIVE_CAPTURE/rules.json','sha256':'0'*64},'candidate_spirv':{'path':'/NO_SUCH_NATIVE_CAPTURE/candidate.spv','sha256':'0'*64}},'synthetic_frames':[],'patches_per_frame':1}
  with self.assertRaises(OSError):D.frames(v)
 def test_receipt_bytes_are_included_before_publication(self):
  with tempfile.TemporaryDirectory() as td:
   d=Path(td).resolve();s=P.Session.__new__(P.Session);s.dir=d;s.start=__import__('time').monotonic();s.stage='deploy';s.cache=None;s.cache_owner=None;s.request={'schema':'game_opt_loop/model_stage_request@1','pipeline_id':'fixture','plan_sha256':'a'*64,'stage':'deploy','budget':{'wall_ms':30000,'steps':0,'output_bytes':512}};s.side_evidence=None;s.records=[];s.outputs={};s.receipt=d/'receipt.json';(d/'kept-negative').write_bytes(b'x'*256)
   with self.assertRaisesRegex(ValueError,'receipt plus'):s.finish('failed','BLOCKED_PENDING_ACTUAL_DEVICE_RUNTIME')
   self.assertFalse(s.receipt.exists())
if __name__=='__main__':unittest.main(verbosity=2)
