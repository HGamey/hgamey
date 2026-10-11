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
import sys,json,pathlib,hashlib,os,shutil,time
args=sys.argv[1:]
def val(k):return args[args.index(k)+1]
def read(p):return json.loads(pathlib.Path(p).read_bytes())
def write(p,v):pathlib.Path(p).write_text(json.dumps(v))
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def raw(d,n):
 b=('#phonefarm_render_witness v2\nsynthetic_host_only '+n+'\n#end\n').encode();(d/n).write_bytes(b);return sha(d/n)
def ref(d,n):return {'path':n,'sha256':sha(d/n)}
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
 cond={'schema':'phonefarm/confirmation_conditions@3','device_boot_id':'host-only-boot','render_comm':r['render_comm'],'policy':policy,'thermal_contract':ref(d,'thermal-evidence.json'),'sampling_contract_sha256':sha(d/'sampling-contract.json'),'render_attribution':{'profile_id':PROFILE,'package':r['package'],'initial_witness':ref(d,'render-initial.raw.tsv')}}
 if mode=='legacy_receiver':cond['schema']='phonefarm/confirmation_conditions@2';cond.pop('render_attribution')
 write(d/'conditions.json',cond)
 th={'tid':42,'start_ticks':100,'comm':r['render_comm']}
 scope={'profile_id':PROFILE,'package':r['package'],'boot_id':cond['device_boot_id'],'pid':40,'process_start_ticks':90,'uid':10001,'package_metadata_sha256':'a'*64,'kernel_version_sha256':'b'*64,'render_thread':th,'threads':{'42':th},'device':'kgsl-3d0','event_formats_sha256':{k:format(i+1,'x')*64 for i,k in enumerate(['adreno_cmdbatch_queued','adreno_cmdbatch_submitted','kgsl_context_create','kgsl_context_detach','kgsl_context_destroy','sched_process_fork','sched_process_exit'])},'fork_parent_key':'pid','clock_capabilities_sha256':'c'*64,'cpus':[0,1]}
 rows=[];expl=[]
 for i in range(3):
  hs=[raw(d,'admission_'+str(i)+'-'+n) for n in ['trace.txt','render-before.raw.tsv','render-after.raw.tsv','render-final.raw.tsv']];expl+=hs
  rows.append(dict(zip(['trace_sha256','before_sha256','after_sha256','final_sha256'],hs),submits_per_group=2,active_contexts=[11,12],caller_tids=[42],executor_counts={'100':150},selected_packets=150,boundary_submits=0,excluded_queued_classes=0))
 render={'schema':'phonefarm/frozen_render_attribution@1','scope':scope,'submits_per_group':2,'active_contexts':[11,12],'caller_tids':[42],'baselines':rows}
 write(d/'render-contract.json',render)
 write(d/'admission.json',{'schema':'phonefarm/confirmation_baseline_admission@2','conditions':ref(d,'conditions.json'),'baseline_artifact':ref(d,'baseline.json'),'runs':[{'synthetic_host_only':i} for i in range(3)],'render_contract':ref(d,'render-contract.json')})
 rules=[{'name':'frame_p95_ms','direction':'lower','scale':'percent','minimum_improvement':1.,'maximum_regression':1.},{'name':'fps_mean','direction':'higher','scale':'percent','minimum_improvement':None,'maximum_regression':1.}]
 p={k:sha(d/n) for k,n in [('candidate_artifact_sha256','candidate.json'),('baseline_artifact_sha256','baseline.json'),('conditions_sha256','conditions.json'),('baseline_admission_sha256','admission.json')]}
 p.update(exploration_run_ids=['admission_0','admission_1','admission_2'],exploration_artifact_sha256=expl,pairs=len(r['schedule']),schedule=r['schedule'],family_alpha=.05,paired_t_assumptions_acknowledged=True,metrics=rules,temperature_contract=tc)
 write(d/'plan.json',p)
 print(json.dumps({'status':'PREPARED','plan':str(d/'plan.json'),'eligible':False,'evidence_origin':'synthetic' if mode=='synthetic' else 'live_device','real_game_optimization_proven':False,'promotion_allowed':False}))
else:
 d=pathlib.Path(val('--session'));f=read(val('--frozen'));p=f['plan'];render=read(d/'render-contract.json');pairs=[]
 for pair in p['schedule']:
  entry=dict(pair);entry.pop('baseline_first')
  for role in ['a','b']:
   run=entry[role+'_run_id'];record=dict(render['baselines'][0])
   record.update({k:raw(d,run+'-'+n) for k,n in [('trace_sha256','trace.txt'),('before_sha256','render-before.raw.tsv'),('after_sha256','render-after.raw.tsv'),('final_sha256','render-final.raw.tsv')]})
   diag={'profile_id':PROFILE,'origin_scope':'original target-TGID queued commands uniquely joined to measurement submitted packets; all executors; per-capture opaque context generation','scope':render['scope'],'record':record,'display_verified':False,'context_process_ownership_verified':False,'installed_kernel_source_verified':False,'continuous_foreground_verified':False,'quality_verified':False}
   gates={'target_command_origin_verified':True,'frozen_grouping_verified':True}
   binding={'run_id':run,'conditions_sha256':p['conditions_sha256'],'frozen_plan_sha256':f['sha256'],'render_contract_sha256':sha(d/'render-contract.json'),'raw_artifacts':dict(trace_sha256=record['trace_sha256'],render_before_sha256=record['before_sha256'],render_after_sha256=record['after_sha256'],render_final_sha256=record['final_sha256'],**{k:hashlib.sha256((run+k).encode()).hexdigest() for k in ['env_sha256','thermal_admission_sha256','thermal_window_sha256','thermal_sampler_exit_sha256']})}
   if role=='a' and run==p['schedule'][0]['a_run_id']:
    if mode=='missing_origin':gates.pop('target_command_origin_verified')
    if mode=='spf_changed':record['submits_per_group']=1
    if mode=='context_changed':record['active_contexts']=[13]
    if mode=='caller_changed':record['caller_tids']=[43]
    if mode=='epoch_changed':diag['scope']=dict(diag['scope'],process_start_ticks=91)
    if mode=='claim_display':diag['display_verified']=True
    if mode=='binding_changed':binding['render_contract_sha256']='0'*64
    if mode=='witness_changed':(d/(run+'-render-before.raw.tsv')).write_bytes(b'changed')
    if mode=='exploration_reused':record['before_sha256']=render['baselines'][0]['before_sha256'];binding['raw_artifacts']['render_before_sha256']=record['before_sha256'];shutil.copyfile(d/'admission_0-render-before.raw.tsv',d/(run+'-render-before.raw.tsv'))
   v={'schema':'phonefarm/confirmation_observation@2','source':'phonefarm','status':'VALID_OBSERVATION','qualification':{'stage':'experiment','eligible':False,'evidence_origin':'recorded_device'},'sample_qualification':{'gates':gates},'target_game_attribution':diag,'confirmation_binding':binding,'real_game_optimization_proven':False,'promotion_allowed':False,'host_fixture_only':True}
   name=run+'-report.json';write(d/name,v);entry[role+'_report']=name;entry[role+'_report_sha256']=sha(d/name)
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
}
#[test]
fn v2_plans_generic_game_and_freezes_native_bytes_without_attestation() {
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
    f.write("case/native-reader/compiler.stderr", b"tampered");
    assert!(!f.inspect().status.success());
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
fn v2_actual_four_stages_preserve_synthetic_negative_and_single_use() {
    let f = F::blackbox();
    f.plan();
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
}
#[test]
fn v2_origin_grouping_epoch_witness_and_unsupported_claims_stop_before_judge() {
    for mode in [
        "missing_origin",
        "spf_changed",
        "context_changed",
        "caller_changed",
        "epoch_changed",
        "claim_display",
        "binding_changed",
        "witness_changed",
        "exploration_reused",
    ] {
        let f = F::blackbox();
        f.plan();
        let out = f.run(mode);
        assert!(
            !out.status.success(),
            "{mode}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        let result: Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(result["status"], "STOPPED", "{mode}: {result}");
        assert!(!f.root.join("case/03-judge.intent.json").exists());
        assert_eq!(result["promotion_allowed"], false);
    }
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
fn v2_inspect_rejects_rehashed_outer_witness_tampering() {
    let f = F::blackbox();
    f.plan();
    assert!(f.run("normal").status.success());
    let name = "receiver/fixture_a00-render-before.raw.tsv";
    f.write(&format!("case/{name}"), b"changed");
    let mut v = f.read("case/case-result.json");
    v["artifacts"][name] = json!({"sha256":format!("{:x}",Sha256::digest(b"changed")),"bytes":7});
    f.json("case/case-result.json", &v);
    assert!(!f.inspect().status.success());
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
        p["budgets"]["prepare_seconds"] = json!(1);
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
