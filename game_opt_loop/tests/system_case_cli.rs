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
import sys,json,pathlib,hashlib,os,time
a=sys.argv[1:]
def val(k):return a[a.index(k)+1]
def read(p):return json.loads(pathlib.Path(p).read_bytes())
def write(p,v):pathlib.Path(p).write_text(json.dumps(v))
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
root=pathlib.Path(os.environ['SYSTEM_CASE_FIXTURE_ROOT'])
mode=os.environ.get('SYSTEM_CASE_FIXTURE_MODE','normal')
with (root/'invocations').open('a') as f:f.write(json.dumps(a)+'\n')
if mode=='hang':time.sleep(60)
if mode=='spam':print('x'*100000,flush=True);time.sleep(60)
if mode=='closed_child':
 pid=os.fork()
 if pid==0:
  os.close(1);os.close(2);time.sleep(60);(root/'late').write_text('bad');os._exit(0)
 print('{}');sys.exit(0)
if a[1]=='prepare':
 r=read(val('--request'));d=pathlib.Path(val('--out'));d.mkdir()
 (d/'candidate.json').write_bytes(pathlib.Path(r['candidate']).read_bytes())
 write(d/'baseline.json',{'kind':'sysparam','payload':{'params':{},'plan':[],'deny_keywords':[]}})
 goal=r.get('goal','render_efficiency')
 if mode=='goal_policy':goal='render_efficiency' if goal=='render_cadence' else 'render_cadence'
 write(d/'conditions.json',{'synthetic_fixture':True,'policy':{'goal':goal,'required_metrics':['frame_p95','fps_mean']+(['power_w_mean'] if goal=='render_efficiency' else [])}});write(d/'admission.json',{'synthetic_fixture':True})
 rules=[{'name':'frame_p95_ms','direction':'lower','scale':'percent','minimum_improvement':1.,'maximum_regression':1.},{'name':'fps_mean','direction':'higher','scale':'percent','minimum_improvement':None,'maximum_regression':1.},{'name':'power_watt','direction':'lower','scale':'percent','minimum_improvement':None,'maximum_regression':1.}]
 if r.get('goal')=='render_cadence':rules=rules[:2]
 p={k:sha(d/n) for k,n in [('candidate_artifact_sha256','candidate.json'),('baseline_artifact_sha256','baseline.json'),('conditions_sha256','conditions.json'),('baseline_admission_sha256','admission.json')]}
 p.update(exploration_run_ids=['admission_0','admission_1','admission_2'],exploration_artifact_sha256=['1'*64],pairs=len(r['schedule']),schedule=r['schedule'],family_alpha=.05,paired_t_assumptions_acknowledged=True,metrics=rules)
 if mode=='policy':p['metrics'][0]['minimum_improvement']=0.
 write(d/'plan.json',p)
 print(json.dumps({'status':'PREPARED','session':str(d),'plan':str(d/'plan.json'),'eligible':False,'evidence_origin':'synthetic' if mode=='synthetic' else 'live_device','real_game_optimization_proven':False,'promotion_allowed':False}))
else:
 d=pathlib.Path(val('--session'));f=read(val('--frozen'));p=f['plan']
 write(d/'samples.json',{'frozen_plan_sha256':f['sha256'],'candidate_artifact_sha256':p['candidate_artifact_sha256'],'conditions_sha256':p['conditions_sha256'],'source_kind':'synthetic_fixture','pairs':[]})
 print(json.dumps({'status':'COLLECTED_OBSERVATIONS','samples':str(d/'samples.json'),'eligible':False,'evidence_origin':'live_device','real_game_optimization_proven':False,'promotion_allowed':False}))
"##;
struct F {
    root: PathBuf,
}
impl F {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "system-case-{}-{}",
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
#[test]
fn deterministic_plan_validation_and_create_only() {
    let f = F::new();
    f.plan();
    let candidate = f.read("case/candidate.json");
    assert_eq!(candidate["payload"]["params"]["bus.DDR.boost_freq"], "300");
    let req = f.read("case/request.json");
    assert_eq!(req["schedule"].as_array().unwrap().len(), 5);
    assert_eq!(req["schedule"][1]["baseline_first"], false);
    let out = f.cmd(
        &[
            "case",
            "run",
            "--case",
            "case",
            "--phonefarm-bin",
            "receiver",
        ],
        "normal",
    );
    assert!(out.status.success());
    assert_eq!(f.count(), 0);
    assert!(!f.root.join("case/run-start.json").exists());
    assert!(!f
        .cmd(
            &["case", "plan", "--profile", "profile.json", "--out", "case"],
            "normal"
        )
        .status
        .success());
    f.write("case/candidate.json", b"{}");
    assert!(!f
        .cmd(&["case", "inspect", "--case", "case"], "normal")
        .status
        .success());
    assert_eq!(f.count(), 0);
}
#[test]
fn temperature_preset_is_forwarded_without_numeric_policy_override() {
    let f = F::new();
    let mut p = f.read("profile.json");
    p["temperature_profile_id"] = json!("loaded_steady_v1");
    f.json("profile.json", &p);
    f.plan();
    let request = f.read("case/request.json");
    assert_eq!(request["temperature_profile_id"], "loaded_steady_v1");
    assert!(request.get("temp_cap_c").is_none());
    assert!(request.get("cool_c").is_none());
}
#[test]
fn complete_crossprocess_chain_records_negative_without_promotion() {
    let f = F::new();
    f.plan();
    f.write("workload.json", b"{}");
    f.write("probe.txt", b"bad");
    let out = f.run("normal");
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let r = f.read("case/case-result.json");
    assert_eq!(r["status"], "CASE_RECORDED");
    assert_eq!(r["verdict"], "BLOCKED");
    assert_eq!(r["promotion_allowed"], false);
    assert_eq!(f.count(), 2);
    let req = f.read("case/request.json");
    assert!(req["workload"]
        .as_str()
        .unwrap()
        .ends_with("case/workload.json"));
    let b = std::fs::read(f.root.join("case/00-prepare.stdout")).unwrap();
    assert_eq!(
        f.read("case/00-prepare.result.json")["stdout_sha256"],
        format!("{:x}", Sha256::digest(b))
    );
    assert!(f
        .cmd(&["case", "inspect", "--case", "case"], "normal")
        .status
        .success());
    assert!(!f.run("normal").status.success());
    assert_eq!(f.count(), 2);
    let mut r = f.read("case/case-result.json");
    r["verdict"] = json!("PASS");
    f.json("case/case-result.json", &r);
    assert!(!f
        .cmd(&["case", "inspect", "--case", "case"], "normal")
        .status
        .success());
}
#[test]
fn inspect_rejects_rehashed_internal_stdout_report_contradiction() {
    let f = F::new();
    f.plan();
    assert!(f.run("normal").status.success());
    let mut reply = f.read("case/report.json");
    reply["verdict"] = json!("PASS");
    f.json("case/03-judge.stdout", &reply);
    let raw = std::fs::read(f.root.join("case/03-judge.stdout")).unwrap();
    let mut result = f.read("case/03-judge.result.json");
    result["stdout_sha256"] = json!(format!("{:x}", Sha256::digest(&raw)));
    f.json("case/03-judge.result.json", &result);
    let mut final_result = f.read("case/case-result.json");
    for n in ["03-judge.stdout", "03-judge.result.json"] {
        let b = std::fs::read(f.root.join("case").join(n)).unwrap();
        final_result["artifacts"][n] =
            json!({"sha256":format!("{:x}",Sha256::digest(&b)),"bytes":b.len()});
    }
    f.json("case/case-result.json", &final_result);
    let out = f.cmd(&["case", "inspect", "--case", "case"], "normal");
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("judgment stdout/report/exit mismatch"));
}
#[test]
fn synthetic_receiver_and_modified_policy_stop_before_collect() {
    for mode in ["synthetic", "policy"] {
        let f = F::new();
        f.plan();
        assert!(!f.run(mode).status.success());
        assert_eq!(f.count(), 1);
        assert!(!f.root.join("case/01-freeze.intent.json").exists());
        assert_eq!(f.read("case/case-result.json")["promotion_allowed"], false);
    }
}
#[test]
fn unknown_workload_control_and_symlink_refused_offline() {
    for change in ["field", "value", "duration", "symlink"] {
        let f = F::new();
        let mut p = f.read("profile.json");
        match change{"field"=>p["thermal_cap"]=json!(95),"value"=>p["candidate_value"]=json!("999"),"duration"=>f.json("workload.json",&json!({"schema":"phonefarm/deterministic_touch_workload@1","actions":[{"type":"sleep","seconds":1}]})),_=>{std::os::unix::fs::symlink("probe.txt",f.root.join("alias")).unwrap();p["probe"]=json!(f.root.join("alias"));}}
        f.json("profile.json", &p);
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
fn timeout_spam_and_closed_pipe_descendant_are_unknown_and_not_retried() {
    for mode in ["hang", "spam", "closed_child"] {
        let f = F::new();
        let mut p = f.read("profile.json");
        p["budgets"]["prepare_seconds"] = json!(3);
        f.json("profile.json", &p);
        f.plan();
        let begin = std::time::Instant::now();
        assert!(!f.run(mode).status.success());
        assert!(begin.elapsed().as_secs() < 25);
        let o = f.read("case/00-prepare.result.json");
        assert_eq!(o["state"], "UNKNOWN");
        assert!(!f.root.join("late").exists());
        assert!(!f.root.join("case/01-freeze.intent.json").exists());
        assert!(!f.run("normal").status.success());
        assert!(f.count() <= 1);
        assert!(f
            .cmd(&["case", "inspect", "--case", "case"], "normal")
            .status
            .success());
    }
}
#[test]
fn durable_inflight_without_completion_inspects_unknown() {
    let f = F::new();
    f.plan();
    f.json("case/run-start.json", &json!({"state":"STARTED"}));
    f.json("case/00-prepare.intent.json", &json!({"state":"IN_FLIGHT"}));
    let out = f.cmd(&["case", "inspect", "--case", "case"], "normal");
    assert!(out.status.success());
    let r: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(r["execution_state"], "UNKNOWN");
    assert_eq!(r["stages"][0]["result"]["state"], "UNKNOWN");
    assert_eq!(r["resumption_allowed"], false);
    assert!(!f.run("normal").status.success());
    assert_eq!(f.count(), 0);
}
#[test]
fn explicit_cadence_goal_freezes_two_metrics_without_energy_claims() {
    let f = F::new();
    let mut profile = f.read("profile.json");
    profile["goal"] = json!("render_cadence");
    f.json("profile.json", &profile);
    f.plan();
    assert_eq!(f.read("case/request.json")["goal"], "render_cadence");
    let r = f.run("normal");
    assert_eq!(r.status.code(), Some(0)); // Recorded negative is a completed case, not statistical PASS.
    let v: Value = serde_json::from_slice(&r.stdout).unwrap();
    assert_eq!(v["status"], "CASE_RECORDED");
    assert_eq!(v["verdict"], "BLOCKED");
    assert_eq!(v["promotion_allowed"], false);
    assert_eq!(v["real_game_optimization_proven"], false);
    assert_eq!(
        f.read("case/frozen.json")["plan"]["metrics"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(f.count(), 2);
}
#[test]
fn unknown_goal_and_receiver_goal_mismatch_never_downgrade() {
    let f = F::new();
    let mut p = f.read("profile.json");
    p["goal"] = json!("display_smoothness");
    f.json("profile.json", &p);
    let r = f.cmd(
        &["case", "plan", "--profile", "profile.json", "--out", "case"],
        "normal",
    );
    assert!(!r.status.success());
    assert!(!f.root.join("case").exists());
    assert_eq!(f.count(), 0);
    let f = F::new();
    let mut p = f.read("profile.json");
    p["goal"] = json!("render_cadence");
    f.json("profile.json", &p);
    f.plan();
    let r = f.run("goal_policy");
    assert!(!r.status.success());
    assert!(String::from_utf8_lossy(&r.stdout).contains("receiver goal/required metrics"));
    assert_eq!(f.count(), 1);
    assert!(!f.root.join("case/01-freeze.intent.json").exists());
}
