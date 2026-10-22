#!/usr/bin/env python3
"""Real local CPU SR stages; Android deploy/evaluate deliberately blocked."""
import os,sys,time,json,math,hashlib
from pathlib import Path
import protocol as P
import data as D
# Fixed registered local-CPU settings are set before importing native libraries.
os.environ['CUDA_VISIBLE_DEVICES']='-1'
os.environ['TF_NUM_INTEROP_THREADS']='1'
os.environ['TF_NUM_INTRAOP_THREADS']='2'
os.environ['OPENBLAS_NUM_THREADS']='2'
os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
ARCH='game_opt_loop/local_sr_architecture@1'

def architecture(config):
 return {'schema':ARCH,'seed':config['seed'],'input_shape':[1,16,16,3],'output_shape':[1,24,24,3],'scale_numerator':3,'scale_denominator':2,'color':'linear_RGB_float32_0_1','resize':'bilinear_half_pixel_centers','residual_layers':[{'kernel':3,'filters':8,'activation':'relu'},{'kernel':3,'filters':3,'activation':'linear'}],'params_expected':443,'params_limit':10000,'source':config['source'],'dataset_config_sha256':P.digest(config),'android_deployment':'BLOCKED_PENDING_ACTUAL_DEVICE_RUNTIME','real_game_qualification':False,'promotion_allowed':False}
def runtime_config(s):
 P.require(len(sys.argv)==2,'explicit bound runtime-config argv required');path=str(P.absolute(sys.argv[1]));found=[x for x in s.request['drivers'] if x['path']==path];P.require(len(found)==1,'runtime config not bound as driver artifact')
 c=P.parse(P.snapshot(found[0]));P.keys(c,['schema','tensorflow_version','python_executable','python_executable_sha256','site_packages','pyvenv_cfg','source','cpu_only','device_calls','model_api_calls'],'runtime config')
 P.require(c['schema']=='game_opt_loop/local_tf_runtime@1' and c['tensorflow_version']=='2.20.0' and c['cpu_only'] is True and c['device_calls']==c['model_api_calls']==0 and c['source']=='trusted_local_install_not_authenticated_dependency_closure','local CPU runtime scope')
 P.require(str(Path(sys.executable).resolve())==c['python_executable'] and P.hash_bytes(P.read(c['python_executable'],512*1024*1024))==c['python_executable_sha256'],'actual interpreter differs')
 P.snapshot(c['pyvenv_cfg']);d=P.directory(c['site_packages']);os.close(d);return c

def runtime(s):
 began=time.monotonic();c=runtime_config(s);sys.path.insert(0,c['site_packages']);os.environ['KERAS_HOME']=str(s.create_runtime_cache())
 import numpy as np
 import tensorflow as tf
 P.require(tf.__version__=='2.20.0','fixed TensorFlow version required')
 tf.config.set_visible_devices([], 'GPU');tf.config.threading.set_inter_op_parallelism_threads(1);tf.config.threading.set_intra_op_parallelism_threads(2)
 return tf,np,{'tensorflow_version':tf.__version__,'numpy_version':np.__version__,'tensorflow_import_seconds':time.monotonic()-began,'visible_devices':[d.name for d in tf.config.get_visible_devices()],'execution':'local_CPU_only_not_Android','model_api_calls':0,'device_calls':0}
def build(tf,a):
 P.require(a['schema']==ARCH and a['params_expected']==443 and a['input_shape']==[1,16,16,3] and a['output_shape']==[1,24,24,3],'fixed architecture required')
 tf.keras.utils.set_random_seed(a['seed'])
 x=tf.keras.Input(shape=(16,16,3),dtype='float32',name='lr');base=tf.keras.layers.Resizing(24,24,interpolation='bilinear',name='resize')(x)
 y=tf.keras.layers.Conv2D(8,3,padding='same',activation='relu',name='conv1')(base);y=tf.keras.layers.Conv2D(3,3,padding='same',name='conv2')(y)
 model=tf.keras.Model(x,tf.keras.layers.Add(name='residual')([base,y]));P.require(model.count_params()==443 and model.count_params()<=10000,'fixed parameter cap')
 return model

def dataset(session):
 c=session.input('dataset','data');D.config(c);P.require(c['source']==session.source,'source propagation');P.require(c['steps']<=session.request['budget']['steps'] or session.stage!='train','registered step cap')
 f,provenance=D.frames(c);session.check_wall();return c,f,provenance

def model_spec(session):
 m=session.request['model'];P.keys(m,['framework','runtime','abi','quantization'],'model');P.require(m['framework']=='tensorflow-2.20.0' and m['runtime']=='local_cpu_stages_only' and m['abi']=='NHWC_f32_16x16x3_to_24x24x3','fixed actual local runtime scope')
 q=m['quantization'];P.keys(q,['method','precision','calibration_data_sha256'],'quantization');P.require(q['method']=='post_training_float16_weights_and_fp32' and q['precision']=='fp32_and_fp16_weights' and q['calibration_data_sha256'] is None,'no integer calibration or unsupported precision claim')

def design(s):
 c,f,p=dataset(s);a=architecture(c)
 s.emit('architecture.json',P.encode(a)+b'\n');s.emit('design-evidence.json',P.encode({'schema':'game_opt_loop/local_design_evidence@1','source':s.source,'dataset_config_sha256':P.digest(c),'train_frame_ids':c['train_frame_ids'],'heldout_frame_ids':c['heldout_frame_ids'],'frame_identities':[{'frame_id':x['frame_id'],'lr_raw_sha256':x['lr_original_sha256'],'hr_raw_sha256':x['hr_original_sha256'],'patches':D.patch_grid(x,c['patches_per_frame'],c['seed'])} for x in f],'provenance':p,'tensorflow_imported':False,'training':'NOT_RUN','gpu':'NOT_RUN','real_game_qualification':False,'promotion_allowed':False})+b'\n')
 return s.finish('succeeded','Actual bounded architecture/data contract; no TensorFlow training, Android or game proof yet',0)

def train(s):
 c,f,p=dataset(s);a=s.input('architecture.json','architecture');P.require(a==architecture(c),'frozen architecture/data differs')
 tf,np,rt=runtime(s);s.check_wall();pairs=D.arrays(c,f);model=build(tf,a);initial=[x.numpy().copy() for x in model.trainable_variables]
 opt=tf.keras.optimizers.Adam(learning_rate=c['learning_rate']);rng=np.random.default_rng(c['seed']);curve=[];grad_norm=[]
 for step in range(c['steps']):
  s.check_wall();idx=rng.integers(0,len(pairs['train']['lr']),size=c['batch']);x=tf.constant(pairs['train']['lr'][idx]);y=tf.constant(pairs['train']['hr'][idx])
  with tf.GradientTape() as tape:
   pred=model(x,training=True);loss=tf.reduce_mean(tf.abs(pred-y))
  grads=tape.gradient(loss,model.trainable_variables);P.require(all(g is not None for g in grads),'missing actual gradient')
  v=float(loss.numpy());gn=float(tf.linalg.global_norm(grads).numpy());P.require(math.isfinite(v) and math.isfinite(gn) and all(np.isfinite(g.numpy()).all() for g in grads),'nonfinite loss/gradient')
  opt.apply_gradients(zip(grads,model.trainable_variables));curve.append(v);grad_norm.append(gn);print(json.dumps({'stage':'train','step':step+1,'loss_l1':v,'gradient_norm':gn}),flush=True)
 s.check_wall();final=[x.numpy().copy() for x in model.trainable_variables];delta=max(float(np.max(np.abs(b-a))) for a,b in zip(initial,final));P.require(delta>0 and math.isfinite(delta),'ZERO_WEIGHT_CHANGE: no actual update')
 h=lambda weights:hashlib.sha256(b''.join(np.asarray(x,dtype='<f4').tobytes() for x in weights)).hexdigest()
 tmp=s.dir/'captured.weights.h5';P.require(not tmp.exists(),'weights staging collision');model.save_weights(str(tmp));blob=P.read(tmp,P.OUTPUT_LIMIT);P.require(blob[:8]==b'\x89HDF\r\n\x1a\n','H5 signature')
 fresh=build(tf,a);fresh.load_weights(str(tmp));P.require(all(np.array_equal(x.numpy(),z) for x,z in zip(fresh.trainable_variables,final)),'actual H5 weight reload differs')
 val=model(tf.constant(pairs['heldout']['lr'][:1]),training=False).numpy();P.require(val.shape==(1,24,24,3) and np.isfinite(val).all(),'heldout local host shape/finite check')
 s.emit('trained.weights.h5',blob)
 ev={'schema':'game_opt_loop/local_training_evidence@1','source':s.source,'actual_gradient_tape':True,'actual_optimizer_steps':c['steps'],'loss_l1':curve,'gradient_norm':grad_norm,'initial_weights_sha256':h(initial),'final_weights_sha256':h(final),'maximum_absolute_weight_change':delta,'actual_h5_reload':True,'weights_sha256':P.hash_bytes(blob),'architecture_sha256':P.digest(a),'dataset_config_sha256':P.digest(c),'train_frame_ids':c['train_frame_ids'],'heldout_frame_ids':c['heldout_frame_ids'],'patch_identity_by_split':{k:v['identities'] for k,v in pairs.items()},'runtime':rt,'provenance':p,'heldout_host_shape':list(val.shape),'heldout_claim':'one finite host inference only; not formal quality/generalization/Android test','runtime_wall_seconds_including_import':time.monotonic()-s.start,'host_cost':s.check_cost(),'android_deploy':'BLOCKED','gpu':'NOT_RUN','real_game_qualification':False,'promotion_allowed':False}
 s.emit('train-evidence.json',P.encode(ev)+b'\n');return s.finish('succeeded','Actual CPU backprop, changed weights and H5 reload; finite synthetic/unknown-source data only, no Android/quality/gain proof',c['steps'])

def quantize(s):
 c,f,p=dataset(s);a=s.input('architecture.json','architecture');P.require(a==architecture(c),'architecture/data binding');e=s.input('train-evidence.json','metadata');P.require(e['source']==s.source and e['actual_gradient_tape'] is True and e['actual_optimizer_steps']==c['steps'] and e['maximum_absolute_weight_change']>0 and e['dataset_config_sha256']==P.digest(c),'actual training evidence missing')
 art,blob=s.inputs['trained.weights.h5'];P.require(art['role']=='weights' and P.hash_bytes(blob)==e['weights_sha256'] and blob[:8]==b'\x89HDF\r\n\x1a\n','bound actual H5 weights')
 tf,np,rt=runtime(s);s.check_wall();pairs=D.arrays(c,f);model=build(tf,a);tmp=s.dir/'input.weights.h5';P.write_new(tmp,blob);model.load_weights(str(tmp))
 loaded=[x.numpy() for x in model.trainable_variables];P.require(all(np.isfinite(x).all() for x in loaded),'NONFINITE_WEIGHTS')
 loaded_sha=hashlib.sha256(b''.join(np.asarray(x,dtype='<f4').tobytes() for x in loaded)).hexdigest();P.require(loaded_sha==e['final_weights_sha256'],'semantic H5 weights differ from recorded actual training')
 @tf.function(input_signature=[tf.TensorSpec([1,16,16,3],tf.float32,name='lr')])
 def inference(x):return {'sr':model(x,training=False)}
 from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2
 frozen=convert_variables_to_constants_v2(inference.get_concrete_function());records=[]
 for precision in ['fp32','fp16']:
  s.check_wall();converter=tf.lite.TFLiteConverter.from_concrete_functions([frozen],model);converter.target_spec.supported_ops=[tf.lite.OpsSet.TFLITE_BUILTINS]
  if precision=='fp16':converter.optimizations=[tf.lite.Optimize.DEFAULT];converter.target_spec.supported_types=[tf.float16]
  binary=converter.convert();P.require(len(binary)<=P.OUTPUT_LIMIT and binary[4:8]==b'TFL3','actual bounded TFLite format')
  interp=tf.lite.Interpreter(model_content=binary,num_threads=2);interp.allocate_tensors();inputs=interp.get_input_details();outputs=interp.get_output_details();P.require(len(inputs)==len(outputs)==1 and list(inputs[0]['shape'])==[1,16,16,3] and list(outputs[0]['shape'])==[1,24,24,3] and inputs[0]['dtype']==outputs[0]['dtype']==np.float32,'actual TFLite NHWC shape/dtype')
  sample=pairs['heldout']['lr'][:1];sample_identity=pairs['heldout']['identities'][0];interp.set_tensor(inputs[0]['index'],sample);interp.invoke();pred=interp.get_tensor(outputs[0]['index']);P.require(np.isfinite(pred).all() and pred.shape==(1,24,24,3),'actual host TFLite output nonfinite/shape')
  tensors=interp.get_tensor_details();half=sum(x['dtype']==np.float16 for x in tensors);P.require(precision!='fp16' or half>0,'float16 conversion contains no float16 tensors')
  operators=[x['op_name'] for x in interp._get_ops_details()];name='model-'+precision+'.tflite';s.emit(name,binary);records.append({'precision':precision,'sha256':P.hash_bytes(binary),'bytes':len(binary),'input_shape':[int(x) for x in inputs[0]['shape']],'output_shape':[int(x) for x in outputs[0]['shape']],'input_dtype':'float32','output_dtype':'float32','float16_tensor_count':half,'operators':operators,'host_cpu_invocation_finite':True,'android_delegate':'NOT_RUN','input_frame_id':sample_identity['frame_id'],'input_patch_identity':sample_identity,'input_sample_float32_sha256':P.hash_bytes(np.asarray(sample,dtype='<f4').tobytes())})
 s.emit('quantize-evidence.json',P.encode({'schema':'game_opt_loop/local_quantization_evidence@1','source':s.source,'actual_training_weights_sha256':P.hash_bytes(blob),'models':records,'runtime':rt,'runtime_wall_seconds_including_import':time.monotonic()-s.start,'host_cost':s.check_cost(),'data_provenance':p,'scope':'actual local conversion/parse/one CPU invocation; no Android/GPU/loss/power/quality/gain claim','fp16_scope':'Float16 stored weights may dequantize to float32 on CPU; no delegate or speed inference','android_deploy':'BLOCKED','android_evaluate':'BLOCKED','real_game_qualification':False,'promotion_allowed':False})+b'\n')
 return s.finish('succeeded','Actual fp32 and float16-weight TFLite conversion/parsing/finite host CPU invocation; Android and benefit not tested',0)

def main():
 s=None
 try:
  s=P.Session();model_spec(s);runtime_config(s)
  if s.stage in ('deploy','evaluate'):
   s.finish('failed','BLOCKED_PENDING_ACTUAL_DEVICE_RUNTIME: local host work is not Android deployment/evaluation',0);return 2
  {'design':design,'train':train,'quantize':quantize}[s.stage](s);return 0
 except BaseException as error:
  print('LOCAL_STAGE_REJECTED: '+str(error),file=sys.stderr,flush=True)
  if s is not None:
   try:s.finish('failed','LOCAL_STAGE_REJECTED: '+str(error),0)
   except BaseException as receipt_error:print('NEGATIVE_RECEIPT_UNAVAILABLE: '+str(receipt_error),file=sys.stderr,flush=True)
  return 2
if __name__=='__main__':sys.exit(main())
