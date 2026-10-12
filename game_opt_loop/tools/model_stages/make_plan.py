#!/usr/bin/env python3
"""Create-only explicit synthetic actual-training plan or native descriptor plan.
Does not execute drivers, import TensorFlow, use existing weights, or access devices.
"""
import argparse,math,sys
from pathlib import Path
import protocol as P
import data as D
DRIVER=Path(__file__).parent.absolute()

def synthetic_frames(out):
 frames=[]
 for index in range(4):
  files={}
  for role,size in [('lr',16),('hr',24)]:
   raw=bytearray()
   for y in range(size):
    for x in range(size):
     u=(x+.5)/size;v=(y+.5)/size
     vals=[.1+.7*u,.15+.65*v,.4+.2*math.sin(2*math.pi*(u+v)+index*.25)]
     raw.extend(round(255*a) for a in vals)
   path=out/f'synthetic-{index}-{role}.rgb';P.write_new(path,bytes(raw));files[role]={'path':str(path),'sha256':P.hash_bytes(raw)}
  frames.append({'frame_id':index,'lr':files['lr'],'hr':files['hr'],'lr_shape':[16,16,3],'hr_shape':[24,24,3],'color':'linear_rgb8'})
 return frames

def plan(out,python,dataset,runtime_config):
 config=D.config(dataset);P.write_new(out/'dataset.json',P.encode(config)+b'\n')
 stages=[]
 outputs={'design':[('architecture.json','architecture','game_opt_loop/local_sr_architecture@1'),('design-evidence.json','metadata','game_opt_loop/local_design_evidence@1')],'train':[('trained.weights.h5','weights','keras_weights_hdf5'),('train-evidence.json','metadata','game_opt_loop/local_training_evidence@1')],'quantize':[('model-fp32.tflite','quantized_model','tflite_flatbuffer_fp32'),('model-fp16.tflite','quantized_model','tflite_flatbuffer_float16_weights'),('quantize-evidence.json','metadata','game_opt_loop/local_quantization_evidence@1')],'deploy':[('deployment.json','deployment_receipt','explicit_unimplemented_android_receipt')],'evaluate':[('evaluation.json','evaluation_report','explicit_unimplemented_android_receipt')]}
 for name in ['design','train','quantize','deploy','evaluate']:
  stages.append({'stage':name,'argv':[str(python),str(DRIVER/'stage_driver.py'),str(runtime_config)],'cwd':str(DRIVER),'driver_artifacts':[str(DRIVER/f) for f in ['stage_driver.py','protocol.py','data.py','refbench_native_pairs_v1.py']]+[str(runtime_config)],'outputs':[{'name':n,'role':r,'format':f,'source':config['source']} for n,r,f in outputs[name]],'budget':{'wall_ms':P.WALL[name],'output_bytes':P.OUTPUT_LIMIT,'steps':config['steps'] if name=='train' else 0,'tokens':0,'device_ms':0}})
 return {'schema':'game_opt_loop/model_pipeline_plan@1','pipeline_id':out.name,'mode':'autonomous','candidate_description':'Actual CPU 3/2 SR backprop/H5/TFLite mechanism; '+config['source']+' data; Android deploy/evaluate intentionally BLOCKED','model':{'framework':'tensorflow-2.20.0','runtime':'local_cpu_stages_only','abi':'NHWC_f32_16x16x3_to_24x24x3','quantization':{'method':'post_training_float16_weights_and_fp32','precision':'fp32_and_fp16_weights','calibration_data_sha256':None}},'data':[{'name':'dataset','role':'data','path':str(out/'dataset.json'),'sha256':P.hash_bytes(P.read(out/'dataset.json')),'format':'game_opt_loop/paired_training_dataset@1','source':config['source']}],'stages':stages}
def main():
 a=argparse.ArgumentParser(description=__doc__);a.add_argument('--out',required=True);a.add_argument('--python',required=True);a.add_argument('--synthetic',action='store_true');a.add_argument('--native-dataset');args=a.parse_args()
 P.require(args.synthetic != (args.native_dataset is not None),'one explicit data mode required');out=P.absolute(args.out);out.mkdir(mode=0o700);requested_python=P.absolute(args.python);venv=requested_python.parent.parent;python=requested_python.resolve(strict=True)
 cfg_path=venv/'pyvenv.cfg';cfg=P.read(cfg_path);version=[x.split('=',1)[1].strip() for x in cfg.decode().splitlines() if x.startswith('version =')];P.require(len(version)==1 and version[0].startswith('3.12.'),'registered Python3.12 runtime')
 site=venv/'lib/python3.12/site-packages';d=P.directory(site);__import__('os').close(d)
 runtime_config=out/'runtime-config.json';P.write_new(runtime_config,P.encode({'schema':'game_opt_loop/local_tf_runtime@1','tensorflow_version':'2.20.0','python_executable':str(python),'python_executable_sha256':P.hash_bytes(P.read(python,512*1024*1024)),'site_packages':str(site),'pyvenv_cfg':{'path':str(cfg_path),'sha256':P.hash_bytes(cfg)},'source':'trusted_local_install_not_authenticated_dependency_closure','cpu_only':True,'device_calls':0,'model_api_calls':0})+b'\n')
 if args.synthetic:
  dataset={'schema':'game_opt_loop/paired_training_dataset@1','source':'synthetic','kind':'synthetic_rgb','seed':23,'steps':8,'batch':1,'learning_rate':.001,'train_frame_ids':[0,1,2],'heldout_frame_ids':[3],'capture':None,'synthetic_frames':synthetic_frames(out),'patches_per_frame':1}
 else:
  dataset=P.parse(P.read(P.absolute(args.native_dataset)));P.require(dataset['kind']=='native_refbench','native input cannot choose synthetic fallback')
 value=plan(out,python,dataset,runtime_config);P.write_new(out/'plan.json',P.encode(value)+b'\n');print(P.encode({'plan':str(out/'plan.json'),'sha256':P.hash_bytes(P.read(out/'plan.json')),'source':dataset['source'],'execution':'NOT_RUN','device_calls':0,'model_api_calls':0}).decode())
if __name__=='__main__':main()
