//! Actual CLI wiring with synthetic host receivers only; no ADB/API/SDK calls.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::os::unix::fs::PermissionsExt;
use std::{
    path::PathBuf,
    process::{Command, Output},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
const RECEIVER: &str = r##"#!/usr/bin/env python3
import sys,json,pathlib,hashlib,os,shutil,time,datetime
args=sys.argv[1:]
def val(k):return args[args.index(k)+1]
def read(p):return json.loads(pathlib.Path(p).read_bytes())
def write(p,v):pathlib.Path(p).write_text(json.dumps(v))
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def raw(d,n):
 b=('#phonefarm_render_witness v2\nsynthetic_host_only '+n+'\n#end\n').encode();(d/n).write_bytes(b);return sha(d/n)
def ref(d,n):return {'path':n,'sha256':sha(d/n)}
RAW=[('trace','trace.txt'),('env','sampler.log'),('workload_exit','workload_exit.txt'),('sampler_exit','sampler_exit.txt'),('capture_exit','capture_exit.txt'),('restore_exit','restore_exit.txt'),('root_id','root_id.txt'),('boot_id','boot_id.txt'),('probe','probe.txt'),('snapshot_before','snapshot_before.txt'),('snapshot_at_run','snapshot_at_run.txt'),('snapshot_after_run','snapshot_after_run.txt'),('snapshot_restored','snapshot_restored.txt'),('thermal_admission','pre-thermal.raw.tsv'),('thermal_window','thermal_sampler.log'),('thermal_sampler_exit','thermal_sampler_exit.txt'),('render_before','render-before.raw.tsv'),('render_after','render-after.raw.tsv'),('render_final','render-final.raw.tsv')]
def raw_run(d,run,phase,role,pair):
 refs={}
 for key,suffix in RAW:
  n='probe.txt' if key=='probe' else run+'-'+suffix
  raw(d,n);refs[key]=ref(d,n)
 start=datetime.datetime.now(datetime.timezone.utc);finish=start+datetime.timedelta(seconds=6)
 write(d/(run+'-run.json'),{'schema':'phonefarm/run_one_bundle@3','evidence_origin':'recorded_device','phase':phase,'run_id':run,'pair_id':pair,'sample_role':role,'device_serial':'synthetic-device','started_at':start.isoformat(),'finished_at':finish.isoformat(),'conditions':ref(d,'conditions.json'),'artifact':ref(d,'baseline.json' if role=='baseline' else 'candidate.json'),'raw':refs})
 return read(d/(run+'-run.json'))
root=pathlib.Path(os.environ['SYSTEM_CASE_FIXTURE_ROOT']);mode=os.environ.get('SYSTEM_CASE_FIXTURE_MODE','normal')
with (root/'invocations').open('a') as f:f.write(json.dumps(args)+'\n')
if mode=='hang':time.sleep(60)
if mode=='closed_child':
 pid=os.fork()
 if pid==0:
  os.close(1);os.close(2);time.sleep(60);(root/'late').write_text('bad');os._exit(0)
 print('{}');sys.exit(0)
PROFILE='kgsl_queued_origin_spf_v1'
if args[1]=='prepare':
 r=read(val('--request'));d=pathlib.Path(val('--out'));d.mkdir()
 (d/'candidate.json').write_bytes(pathlib.Path(r['candidate']).read_bytes());write(d/'baseline.json',{'kind':'sysparam','payload':{'params':{},'plan':[],'deny_keywords':[]}})
 for n,out in [('source.c','render_reader.source.c'),('reader.elf','render_reader.elf'),('build.json','render_reader.build.json'),('compiler.stdout','render_reader.compiler.stdout'),('compiler.stderr','render_reader.compiler.stderr')]:shutil.copyfile(pathlib.Path(r['render_reader_artifact'])/n,d/out)
 if mode=='native_receiver_changed':(d/'render_reader.elf').write_bytes(b'bad')
 raw(d,'render-initial.raw.tsv');write(d/'thermal-evidence.json',{'synthetic_fixture':True})
 tc={'profile_id':'loaded_steady_v1','receiver_policy_version':2,'profile_sha256':'8'*64,'reference_sha256':'9'*64,'thermal_contract_sha256':sha(d/'thermal-evidence.json')}
 policy={'version':2,'goal':'render_cadence','required_metrics':['frame_p95','fps_mean'],'temperature_profile_id':'loaded_steady_v1','temperature_profile_sha256':tc['profile_sha256'],'thermal_reference_sha256':tc['reference_sha256'],'temp_cap_c':None,'cool_c':None}
 sampling={'render_attribution_profile_id':PROFILE,'render_reader_build_sha256':sha(d/'render_reader.build.json'),'render_reader_elf_sha256':sha(d/'render_reader.elf')}
 write(d/'sampling-contract.json',sampling)
 cond={'schema':'phonefarm/confirmation_conditions@3','device_serial':'synthetic-device','device_boot_id':'host-only-boot','render_comm':r['render_comm'],'policy':policy,'thermal_contract':ref(d,'thermal-evidence.json'),'sampling_contract_sha256':sha(d/'sampling-contract.json'),'render_attribution':{'profile_id':PROFILE,'package':r['package'],'initial_witness':ref(d,'render-initial.raw.tsv')}}
 if mode=='legacy_receiver':cond['schema']='phonefarm/confirmation_conditions@2';cond.pop('render_attribution')
 write(d/'conditions.json',cond)
 th={'tid':42,'start_ticks':100,'comm':r['render_comm']}
 scope={'profile_id':PROFILE,'package':r['package'],'boot_id':cond['device_boot_id'],'pid':40,'process_start_ticks':90,'uid':10001,'package_metadata_sha256':'a'*64,'kernel_version_sha256':'b'*64,'render_thread':th,'threads':{'42':th},'device':'kgsl-3d0','event_formats_sha256':{k:format(i+1,'x')*64 for i,k in enumerate(['adreno_cmdbatch_queued','adreno_cmdbatch_submitted','kgsl_context_create','kgsl_context_detach','kgsl_context_destroy','sched_process_fork','sched_process_exit'])},'fork_parent_key':'pid','clock_capabilities_sha256':'c'*64,'cpus':[0,1]}
 rows=[];expl=[]
 for i in range(3):
  run=raw_run(d,'admission_'+str(i),'admission','baseline',None);hs=[run['raw'][k]['sha256'] for k in ['trace','render_before','render_after','render_final']];expl+=hs
  rows.append(dict(zip(['trace_sha256','before_sha256','after_sha256','final_sha256'],hs),submits_per_group=2,active_contexts=[11,12],caller_tids=[42],executor_counts={'100':150},selected_packets=150,boundary_submits=0,excluded_queued_classes=0))
 render={'schema':'phonefarm/frozen_render_attribution@1','scope':scope,'submits_per_group':2,'active_contexts':[11,12],'caller_tids':[42],'baselines':rows}
 write(d/'render-contract.json',render)
 write(d/'admission.json',{'schema':'phonefarm/confirmation_baseline_admission@2','conditions':ref(d,'conditions.json'),'baseline_artifact':ref(d,'baseline.json'),'runs':[ref(d,'admission_'+str(i)+'-run.json') for i in range(3)],'render_contract':ref(d,'render-contract.json')})
 if mode=='baseline_raw_changed':(d/'admission_2-render-final.raw.tsv').write_bytes(b'changed')
 if mode=='baseline_raw_missing':(d/'admission_2-render-final.raw.tsv').unlink()
 if mode=='baseline_wrong_role':
  n='admission_2-run.json';v=read(d/n);v['sample_role']='candidate';write(d/n,v);a=read(d/'admission.json');a['runs'][2]=ref(d,n);write(d/'admission.json',a)
 if mode=='baseline_ref_escape':
  a=read(d/'admission.json');a['runs'][2]['path']='../admission_2-run.json';write(d/'admission.json',a)
 rules=[{'name':'frame_p95_ms','direction':'lower','scale':'percent','minimum_improvement':1.,'maximum_regression':1.},{'name':'fps_mean','direction':'higher','scale':'percent','minimum_improvement':None,'maximum_regression':1.}]
 p={k:sha(d/n) for k,n in [('candidate_artifact_sha256','candidate.json'),('baseline_artifact_sha256','baseline.json'),('conditions_sha256','conditions.json'),('baseline_admission_sha256','admission.json')]}
 p.update(exploration_run_ids=['admission_0','admission_1','admission_2'],exploration_artifact_sha256=expl,pairs=len(r['schedule']),schedule=r['schedule'],family_alpha=.05,paired_t_assumptions_acknowledged=True,metrics=rules,temperature_contract=tc)
 write(d/'plan.json',p)
 print(json.dumps({'status':'PREPARED','plan':str(d/'plan.json'),'eligible':False,'evidence_origin':'synthetic' if mode=='synthetic' else 'live_device','real_game_optimization_proven':False,'promotion_allowed':False}))
else:
 d=pathlib.Path(val('--session'));f=read(val('--frozen'));p=f['plan'];render=read(d/'render-contract.json');pairs=[];write(d/'registered.json',f)
 for pair in p['schedule']:
  entry=dict(pair);entry.pop('baseline_first')
  for role in ['a','b']:
   run=entry[role+'_run_id'];record=dict(render['baselines'][0])
   run_manifest=raw_run(d,run,'confirmation','baseline' if role=='a' else 'candidate',pair['pair_id']);record.update({k:run_manifest['raw'][key]['sha256'] for k,key in [('trace_sha256','trace'),('before_sha256','render_before'),('after_sha256','render_after'),('final_sha256','render_final')]})
   diag={'profile_id':PROFILE,'origin_scope':'original target-TGID queued commands uniquely joined to measurement submitted packets; all executors; per-capture opaque context generation','scope':render['scope'],'record':record,'display_verified':False,'context_process_ownership_verified':False,'installed_kernel_source_verified':False,'continuous_foreground_verified':False,'quality_verified':False}
   gates={'target_command_origin_verified':True,'frozen_grouping_verified':True}
   binding={'run_id':run,'started_at':run_manifest['started_at'],'finished_at':run_manifest['finished_at'],'artifact_sha256':run_manifest['artifact']['sha256'],'conditions_sha256':p['conditions_sha256'],'frozen_plan_sha256':f['sha256'],'render_contract_sha256':sha(d/'render-contract.json'),'raw_artifacts':{binding_key:run_manifest['raw'][key]['sha256'] for key,binding_key in [('trace','trace_sha256'),('env','env_sha256'),('thermal_admission','thermal_admission_sha256'),('thermal_window','thermal_window_sha256'),('thermal_sampler_exit','thermal_sampler_exit_sha256'),('render_before','render_before_sha256'),('render_after','render_after_sha256'),('render_final','render_final_sha256')]}}

   if role=='a' and run==p['schedule'][0]['a_run_id']:
    if mode=='missing_origin':gates.pop('target_command_origin_verified')
   v={'schema':'phonefarm/confirmation_observation@2','source':'phonefarm','status':'VALID_OBSERVATION','qualification':{'stage':'experiment','eligible':False,'evidence_origin':'recorded_device'},'sample_qualification':{'gates':gates},'target_game_attribution':diag,'confirmation_binding':binding,'real_game_optimization_proven':False,'promotion_allowed':False,'host_fixture_only':True}
   write(d/(run+'-bundle.json'),{'schema':'phonefarm/confirmation_bundle@1','run':ref(d,run+'-run.json'),'baseline_admission':ref(d,'admission.json'),'frozen_plan':ref(d,'registered.json')});name=run+'-report.json';write(d/name,v);entry[role+'_report']=name;entry[role+'_report_sha256']=sha(d/name)
  pairs.append(entry)
 write(d/'samples.json',{'frozen_plan_sha256':f['sha256'],'candidate_artifact_sha256':p['candidate_artifact_sha256'],'conditions_sha256':p['conditions_sha256'],'source_kind':'synthetic_fixture','pairs':pairs})
 print(json.dumps({'status':'COLLECTED_OBSERVATIONS','samples':str(d/'samples.json'),'eligible':False,'evidence_origin':'live_device','real_game_optimization_proven':False,'promotion_allowed':False}))
"##;
struct F {
    root: PathBuf,
}
impl F {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "blackbox-case-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let f = Self {
            root: root.canonicalize().unwrap(),
        };
        f.write("probe.txt",b"bus.DDR.boost_freq.writable=yes\nbus.DDR.boost_freq.effect=live\nbus.DDR.boost_freq.cur=100\nbus.DDR.avail_freqs=100 300\n");
        f.write("receiver", RECEIVER.as_bytes());
        std::fs::set_permissions(
            f.root.join("receiver"),
            std::fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        f.json("workload.json",&json!({"schema":"phonefarm/deterministic_touch_workload@1","actions":[{"type":"sleep","seconds":6}]}));
        f.json("profile.json",&json!({"schema":"game_opt_loop/system_case_profile@1","case_id":"fixture","transport":{"kind":"adb","program":f.root.join("receiver"),"target":"controlled-host-fixture"},"package":"io.github.hgamey.refbench","render_comm":"RefbenchDrv","carrier":{"scene":"bw_pingpong","intensity":8,"frames":100000,"apk_sha256":"a".repeat(64),"source_commit":"b".repeat(40)},"probe":f.root.join("probe.txt"),"workload":f.root.join("workload.json"),"control_id":"bus.DDR.boost_freq","candidate_value":"300","capture_seconds":2,"lead_seconds":1,"pairs":5,"paired_t_assumptions_acknowledged":true,"budgets":{"prepare_seconds":30,"collect_seconds":30,"local_seconds":30,"whole_seconds":180,"stream_bytes":65536}}));
        f
    }
    fn write(&self, n: &str, b: &[u8]) {
        std::fs::write(self.root.join(n), b).unwrap()
    }
    fn json(&self, n: &str, v: &Value) {
        self.write(n, &serde_json::to_vec(v).unwrap())
    }
    fn read(&self, n: &str) -> Value {
        serde_json::from_slice(&std::fs::read(self.root.join(n)).unwrap()).unwrap()
    }
    fn cmd(&self, args: &[&str], mode: &str) -> Output {
        let out = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
            .current_dir(&self.root)
            .args(args)
            .env("SYSTEM_CASE_FIXTURE_ROOT", &self.root)
            .env("SYSTEM_CASE_FIXTURE_MODE", mode)
            .output()
            .unwrap();
        let receipt = NEXT.fetch_add(1, Ordering::Relaxed);
        self.write(&format!("cli-{receipt}.stdout"), &out.stdout);
        self.write(&format!("cli-{receipt}.stderr"), &out.stderr);
        out
    }
    fn plan(&self) {
        let out = self.cmd(
            &["case", "plan", "--profile", "profile.json", "--out", "case"],
            "normal",
        );
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    fn run(&self, mode: &str) -> Output {
        self.cmd(
            &[
                "case",
                "run",
                "--case",
                "case",
                "--phonefarm-bin",
                "receiver",
                "--execute",
            ],
            mode,
        )
    }
    fn count(&self) -> usize {
        std::fs::read_to_string(self.root.join("invocations"))
            .map(|s| s.lines().count())
            .unwrap_or(0)
    }
}
impl Drop for F {
    fn drop(&mut self) {
        if std::env::var_os("SYSTEM_CASE_KEEP_FIXTURES").is_none() {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }
}

impl F {
    fn blackbox() -> Self {
        let f = Self::new();
        std::fs::create_dir(f.root.join("native")).unwrap();
        let source = include_bytes!("../tools/contracts/phonefarm-render-reader-v1.c");
        f.write("native/source.c", source);
        let mut elf = vec![0u8; 64];
        elf[..6].copy_from_slice(b"\x7fELF\x02\x01");
        elf[16] = 3;
        elf[18] = 183;
        f.write("native/reader.elf", &elf);
        f.write("native/compiler.stdout", b"");
        f.write("native/compiler.stderr", b"");
        let hash = |b: &[u8]| format!("{:x}", Sha256::digest(b));
        f.json("native/build.json",&json!({"schema":"phonefarm/render_reader_build@1","status":"BUILT","target":"aarch64-linux-android30","ndk_revision":"28.2.13676358","flags":["-std=c11","-O2","-Wall","-Wextra","-Werror","-fPIE","-pie","-Wl,-z,relro,-z,now"],"source_sha256":hash(source),"elf_sha256":hash(&elf),"compiler":{"path":"/controlled-host/compiler","sha256":"d".repeat(64),"clang_path":"/controlled-host/clang","clang_sha256":"e".repeat(64),"package_sha256":"f".repeat(64)},"execution":{"exit":0,"stdout_sha256":hash(b""),"stderr_sha256":hash(b""),"process_group_absent":true},"source_authentication":"local_build_receipt_not_independent_authentication","device_run":false,"installed_kernel_source_verified":false,"game_proof":false}));
        let mut v = f.read("profile.json");
        v["schema"] = json!("game_opt_loop/system_case_profile@2");
        v["package"] = json!("com.example.blackbox");
        v["render_comm"] = json!("GenericRender");
        v["carrier"] = json!({"kind":"game_replay","application_version":"1.0.0","scene_id":"fixed-camp","reset_semantics":"caller_replay_no_exact_scene_reset"});
        v["source_budget_calls"] = json!(0);
        v["temperature_profile_id"] = json!("loaded_steady_v1");
        v["goal"] = json!("render_cadence");
        v["render_attribution_profile_id"] = json!("kgsl_queued_origin_spf_v1");
        v["render_reader_artifact"] = json!(f.root.join("native"));
        f.json("profile.json", &v);
        f
    }
    fn inspect(&self) -> Output {
        self.cmd(&["case", "inspect", "--case", "case"], "normal")
    }
    fn rehash_recorded(&self, names: &[&str]) {
        let mut result = self.read("case/case-result.json");
        let artifacts = result["artifacts"].as_object_mut().unwrap();
        for name in names {
            match std::fs::read(self.root.join("case").join(name)) {
                Ok(bytes) => {
                    artifacts.insert(
                        (*name).to_owned(),
                        json!({"sha256":format!("{:x}",Sha256::digest(&bytes)),"bytes":bytes.len()}),
                    );
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    artifacts.remove(*name);
                }
                Err(e) => panic!("rehash {name}: {e}"),
            }
        }
        self.json("case/case-result.json", &result);
    }
    fn mutate_first_report(&self, change: impl FnOnce(&mut Value)) {
        const REPORT: &str = "case/receiver/fixture_a00-report.json";
        let mut report = self.read(REPORT);
        change(&mut report);
        self.json(REPORT, &report);
        let report_sha256 = format!(
            "{:x}",
            Sha256::digest(&std::fs::read(self.root.join(REPORT)).unwrap())
        );
        let mut samples = self.read("case/receiver/samples.json");
        samples["pairs"][0]["a_report_sha256"] = json!(report_sha256);
        self.json("case/receiver/samples.json", &samples);
        self.rehash_recorded(&["receiver/fixture_a00-report.json", "receiver/samples.json"]);
    }
    fn inspect_rejects_and_restores(
        &self,
        mode: &str,
        expected_guard: &str,
        files: &[&str],
        change: impl FnOnce(&F),
    ) {
        let saved = files
            .iter()
            .map(|name| (*name, std::fs::read(self.root.join(name)).unwrap()))
            .collect::<Vec<_>>();
        let calls = self.count();
        change(self);
        let rejected = self.inspect();
        for (name, bytes) in saved {
            self.write(name, &bytes);
        }
        let restored = self.inspect();
        let stderr = String::from_utf8_lossy(&rejected.stderr);
        assert!(
            !rejected.status.success(),
            "{mode}: inspect accepted tampering"
        );
        assert!(
            stderr.contains(expected_guard),
            "{mode}: expected inner guard {expected_guard:?}, got: {stderr}"
        );
        assert!(
            !stderr.contains("final artifact hashes differ"),
            "{mode}: stopped at outer artifact hash instead of target guard: {stderr}"
        );
        assert!(
            restored.status.success(),
            "{mode}: restore did not recover archive: {}",
            String::from_utf8_lossy(&restored.stderr)
        );
        assert_eq!(self.count(), calls, "{mode}: inspect executed receiver");
        let result = self.read("case/case-result.json");
        assert_eq!(result["status"], "CASE_RECORDED", "{mode}: {result}");
        assert_eq!(result["promotion_allowed"], false, "{mode}: {result}");
    }
}
#[test]
fn v2_complete_chain_supports_inspection_and_rehashed_guard_regressions() {
    let f = F::blackbox();
    f.plan();
    let req = f.read("case/request.json");
    assert_eq!(req["package"], "com.example.blackbox");
    assert_eq!(req["render_comm"], "GenericRender");
    assert_eq!(
        req["render_attribution_profile_id"],
        "kgsl_queued_origin_spf_v1"
    );
    assert_eq!(
        req["render_reader_artifact"],
        json!(f.root.join("case/native-reader"))
    );
    let m = f.read("case/manifest.json");
    assert_eq!(m["schema"], "game_opt_loop/system_case@2");
    assert_eq!(m["files"].as_object().unwrap().len(), 11);
    assert_eq!(m["promotion_allowed"], false);
    assert_eq!(f.count(), 0);
    f.write("native/reader.elf", b"caller later changed");
    assert!(f.inspect().status.success());
    let compiler_stderr = std::fs::read(f.root.join("case/native-reader/compiler.stderr")).unwrap();
    f.write("case/native-reader/compiler.stderr", b"tampered");
    assert!(!f.inspect().status.success());
    f.write("case/native-reader/compiler.stderr", &compiler_stderr);
    assert!(f.inspect().status.success());

    // The complete chain is executed once; every following guard regression reuses its archive.
    let out = f.run("normal");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let result: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(result["status"], "CASE_RECORDED", "{result}");
    assert_eq!(result["verdict"], "BLOCKED");
    assert_eq!(result["promotion_allowed"], false);
    assert_eq!(f.count(), 2);
    assert!(f.inspect().status.success());
    assert!(!f.run("normal").status.success());
    assert_eq!(f.count(), 2);
    assert!(f.inspect().status.success());

    const REPORT_FILES: &[&str] = &[
        "case/receiver/fixture_a00-report.json",
        "case/receiver/samples.json",
        "case/case-result.json",
    ];
    const QUALIFICATION_GUARD: &str = "v2 queued origin/grouping qualification missing or changed";
    const ATTRIBUTION_GUARD: &str =
        "v2 scope/SPF/caller/context changed or unsupported attestation";
    for (mode, expected_guard) in [
        ("spf_changed", ATTRIBUTION_GUARD),
        ("context_changed", ATTRIBUTION_GUARD),
        ("caller_changed", ATTRIBUTION_GUARD),
        ("epoch_changed", ATTRIBUTION_GUARD),
        ("claim_display", ATTRIBUTION_GUARD),
        ("binding_changed", QUALIFICATION_GUARD),
    ] {
        f.inspect_rejects_and_restores(mode, expected_guard, REPORT_FILES, |f| {
            f.mutate_first_report(|report| match mode {
                "spf_changed" => {
                    report["target_game_attribution"]["record"]["submits_per_group"] = json!(1)
                }
                "context_changed" => {
                    report["target_game_attribution"]["record"]["active_contexts"] = json!([13])
                }
                "caller_changed" => {
                    report["target_game_attribution"]["record"]["caller_tids"] = json!([43])
                }
                "epoch_changed" => {
                    report["target_game_attribution"]["scope"]["process_start_ticks"] = json!(91)
                }
                "claim_display" => {
                    report["target_game_attribution"]["display_verified"] = json!(true)
                }
                "binding_changed" => {
                    report["confirmation_binding"]["render_contract_sha256"] = json!("0".repeat(64))
                }
                _ => unreachable!(),
            });
        });
    }

    const WITNESS: &str = "case/receiver/fixture_a00-render-before.raw.tsv";
    f.inspect_rejects_and_restores(
        "witness_changed",
        "current raw/run bytes changed: fixture_a00-render-before.raw.tsv",
        &[WITNESS, "case/case-result.json"],
        |f| {
            f.write(WITNESS, b"changed");
            f.rehash_recorded(&["receiver/fixture_a00-render-before.raw.tsv"]);
        },
    );

    const RUN: &str = "case/receiver/fixture_a00-run.json";
    const BUNDLE: &str = "case/receiver/fixture_a00-bundle.json";
    f.inspect_rejects_and_restores(
        "exploration_reused",
        "origin witness missing/changed/reused after baseline freeze",
        &[
            WITNESS,
            RUN,
            BUNDLE,
            REPORT_FILES[0],
            REPORT_FILES[1],
            "case/case-result.json",
        ],
        |f| {
            let reused = std::fs::read(
                f.root
                    .join("case/receiver/admission_0-render-before.raw.tsv"),
            )
            .unwrap();
            f.write(WITNESS, &reused);
            let hash = format!("{:x}", Sha256::digest(&reused));
            let mut run = f.read(RUN);
            run["raw"]["render_before"]["sha256"] = json!(hash.clone());
            f.json(RUN, &run);
            let run_sha256 = format!(
                "{:x}",
                Sha256::digest(&std::fs::read(f.root.join(RUN)).unwrap())
            );
            let mut bundle = f.read(BUNDLE);
            bundle["run"]["sha256"] = json!(run_sha256);
            f.json(BUNDLE, &bundle);
            f.mutate_first_report(|report| {
                report["target_game_attribution"]["record"]["before_sha256"] = json!(hash.clone());
                report["confirmation_binding"]["raw_artifacts"]["render_before_sha256"] =
                    json!(hash);
            });
            f.rehash_recorded(&[
                "receiver/fixture_a00-render-before.raw.tsv",
                "receiver/fixture_a00-run.json",
                "receiver/fixture_a00-bundle.json",
            ]);
        },
    );

    const ENV: &str = "case/receiver/fixture_a00-sampler.log";
    f.inspect_rejects_and_restores(
        "env_changed",
        "current raw/run bytes changed: fixture_a00-sampler.log",
        &[ENV, "case/case-result.json"],
        |f| {
            f.write(ENV, b"changed");
            f.rehash_recorded(&["receiver/fixture_a00-sampler.log"]);
        },
    );

    const THERMAL: &str = "case/receiver/fixture_a00-thermal_sampler.log";
    f.inspect_rejects_and_restores(
        "thermal_changed",
        "No such file or directory",
        &[THERMAL, "case/case-result.json"],
        |f| {
            std::fs::remove_file(f.root.join(THERMAL)).unwrap();
            f.rehash_recorded(&["receiver/fixture_a00-thermal_sampler.log"]);
        },
    );
}
#[test]
fn v2_profiles_refuse_missing_context_unknown_preset_or_model_budget() {
    for (key, value) in [
        ("package", json!("not-a.package/path")),
        ("render_comm", json!("comm-too-long-123456")),
        ("goal", json!("render_efficiency")),
        ("temperature_profile_id", json!("capability_absolute_v1")),
        ("render_attribution_profile_id", json!("comm_only")),
        ("source_budget_calls", json!(1)),
    ] {
        let f = F::blackbox();
        let mut p = f.read("profile.json");
        p[key] = value;
        f.json("profile.json", &p);
        assert!(!f
            .cmd(
                &["case", "plan", "--profile", "profile.json", "--out", "case"],
                "normal"
            )
            .status
            .success());
        assert!(!f.root.join("case").exists());
        assert_eq!(f.count(), 0);
    }
}
#[test]
fn v2_artifact_inventory_source_architecture_and_logs_are_checked() {
    for mode in ["extra", "missing", "source", "elf", "logs", "symlink"] {
        let f = F::blackbox();
        match mode {
            "extra" => f.write("native/unregistered", b"x"),
            "missing" => std::fs::remove_file(f.root.join("native/compiler.stderr")).unwrap(),
            "source" => f.write("native/source.c", b"changed"),
            "elf" => f.write("native/reader.elf", b"not ELF"),
            "logs" => f.write("native/compiler.stdout", b"changed"),
            "symlink" => {
                std::fs::remove_file(f.root.join("native/reader.elf")).unwrap();
                std::os::unix::fs::symlink(
                    f.root.join("receiver"),
                    f.root.join("native/reader.elf"),
                )
                .unwrap();
            }
            _ => unreachable!(),
        }
        assert!(!f
            .cmd(
                &["case", "plan", "--profile", "profile.json", "--out", "case"],
                "normal"
            )
            .status
            .success());
        assert_eq!(f.count(), 0);
    }
}
#[test]
fn v2_missing_origin_run_error_stops_before_judge() {
    let f = F::blackbox();
    f.plan();
    let out = f.run("missing_origin");
    assert!(
        !out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let result: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(result["status"], "STOPPED", "{result}");
    assert!(
        result["error"]
            .as_str()
            .unwrap()
            .contains("v2 queued origin/grouping qualification missing or changed"),
        "{result}"
    );
    assert!(!f.root.join("case/03-judge.intent.json").exists());
    assert_eq!(result["promotion_allowed"], false);
    assert_eq!(f.count(), 2);
}
#[test]
fn v2_synthetic_legacy_or_mismatched_native_prepare_never_freezes() {
    for mode in ["synthetic", "legacy_receiver", "native_receiver_changed"] {
        let f = F::blackbox();
        f.plan();
        let out = f.run(mode);
        assert!(!out.status.success());
        let result: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(result["status"], "STOPPED", "{mode}: {result}");
        assert!(!f.root.join("case/01-freeze.intent.json").exists());
        assert_eq!(f.count(), 1);
    }
}
#[test]
fn v1_module_and_existing_source_fingerprint_are_byte_unchanged() {
    let current = include_bytes!("../src/system_case.rs");
    assert_eq!(
        format!("{:x}", Sha256::digest(current)),
        "92581f5ef553d5bd3f1da9ff368574dd27c3eeacbfb52a52b8b0bdfdcccf22e7"
    );
}

#[test]
fn v2_host_timeout_or_closed_pipe_descendant_retains_unknown_without_retry() {
    for mode in ["hang", "closed_child"] {
        let f = F::blackbox();
        let mut p = f.read("profile.json");
        p["budgets"]["prepare_seconds"] = json!(5);
        f.json("profile.json", &p);
        f.plan();
        let out = f.run(mode);
        assert!(!out.status.success());
        let result: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(result["status"], "STOPPED");
        let stage = f.read("case/00-prepare.result.json");
        assert_eq!(stage["state"], "UNKNOWN");
        assert!(!f.root.join("case/01-freeze.intent.json").exists());
        assert_eq!(f.count(), 1);
        assert!(f.inspect().status.success());
        assert!(!f.run("normal").status.success());
        assert_eq!(f.count(), 1);
        assert!(!f.root.join("late").exists());
    }
}

#[test]
fn v2_baseline_run_refs_and_current_twelve_raw_files_are_mandatory() {
    for mode in [
        "baseline_raw_changed",
        "baseline_raw_missing",
        "baseline_wrong_role",
        "baseline_ref_escape",
    ] {
        let f = F::blackbox();
        f.plan();
        let out = f.run(mode);
        assert!(!out.status.success());
        let v: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(v["status"], "STOPPED", "{mode}: {v}");
        assert!(!f.root.join("case/01-freeze.intent.json").exists());
        assert_eq!(f.count(), 1);
    }
}
#[test]
fn v2_archive_and_inspect_include_the_third_baseline_final_witness() {
    let f = F::blackbox();
    f.plan();
    assert!(f.run("normal").status.success());
    let v = f.read("case/case-result.json");
    assert!(v["artifacts"]
        .get("receiver/admission_2-run.json")
        .is_some());
    assert!(v["artifacts"]
        .get("receiver/admission_2-trace.txt")
        .is_some());
    assert!(v["artifacts"]
        .get("receiver/admission_2-render-final.raw.tsv")
        .is_some());
    std::fs::remove_file(
        f.root
            .join("case/receiver/admission_2-render-final.raw.tsv"),
    )
    .unwrap();
    assert!(!f.inspect().status.success());
}
