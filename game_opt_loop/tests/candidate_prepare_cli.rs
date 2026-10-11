//! Actual GOL children + synthetic PF protocol fixtures. No GPU/device/model calls.
use game_opt_loop::engine::{Candidate, CandidateOrigin};
use game_opt_loop::{tracks, TrackMode};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};
static NEXT: AtomicU64 = AtomicU64::new(0);

struct Fixture(PathBuf);
impl Fixture {
    fn new(behavior: &str) -> Self {
        let base = if cfg!(target_os = "macos") {
            "/private/tmp"
        } else {
            "/tmp"
        };
        let path = Path::new(base).join(format!(
            "gol-prepare-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        let f = Self(path);
        fs::write(
            f.path("candidate.json"),
            include_bytes!("../tasks/candidate_compilation_20261011/sr-candidate.json"),
        )
        .unwrap();
        fs::create_dir(f.path("seed")).unwrap();
        for (name, bytes) in [
            ("descriptor.json", include_bytes!("../tasks/candidate_compilation_20261011/adapted/storage-01e67a5e92937da8b0369f19bbc6a38bea0be40e8c7f820af75883e60fc595a1/descriptor.json").as_slice()),
            ("manifest.json", include_bytes!("../tasks/candidate_compilation_20261011/adapted/storage-01e67a5e92937da8b0369f19bbc6a38bea0be40e8c7f820af75883e60fc595a1/manifest.json").as_slice()),
            ("adapted.spv", include_bytes!("../tasks/candidate_compilation_20261011/adapted/storage-01e67a5e92937da8b0369f19bbc6a38bea0be40e8c7f820af75883e60fc595a1/adapted.spv").as_slice()),
        ] { fs::write(f.path("seed").join(name), bytes).unwrap(); }
        let code = format!(
            "#!/usr/bin/python3\nROOT = {}\nBEHAVIOR = {}\n{}",
            serde_json::to_string(f.0.to_str().unwrap()).unwrap(),
            serde_json::to_string(behavior).unwrap(),
            FAKE
        );
        fs::write(f.path("fake-pf"), code).unwrap();
        f
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn command(&self, extra: &[&str]) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"));
        c.args(["candidate-prepare", "run", "--candidate"])
            .arg(self.path("candidate.json"))
            .arg("--phonefarm-bin")
            .arg(self.path("fake-pf"))
            .arg("--out")
            .arg(self.path("session"))
            .arg("--synthetic");
        if !extra.contains(&"--stage-wall-ms") {
            c.args(["--stage-wall-ms", "30000"]);
        }
        if !extra.contains(&"--whole-wall-ms") {
            c.args(["--whole-wall-ms", "180000"]);
        }
        c.args(extra);
        c
    }
    fn run(&self, extra: &[&str]) -> Output {
        self.command(extra).output().unwrap()
    }
    fn read(&self, name: &str) -> Value {
        serde_json::from_slice(&fs::read(self.path(name)).unwrap()).unwrap()
    }
    fn inspect(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
            .args(["candidate-prepare", "inspect", "--session"])
            .arg(self.path("session"))
            .output()
            .unwrap()
    }
    fn calls(&self) -> usize {
        fs::read_to_string(self.path("calls.jsonl"))
            .unwrap_or_default()
            .lines()
            .count()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn parse(o: &Output) -> Value {
    serde_json::from_slice(&o.stdout).unwrap_or_else(|e| {
        panic!(
            "{e}: stdout={} stderr={}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        )
    })
}
fn success(o: &Output) {
    assert!(
        o.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&o.stdout),
        String::from_utf8_lossy(&o.stderr)
    );
}
fn no_proof(v: &Value) {
    assert!(v["proof_flags"]
        .as_object()
        .unwrap()
        .values()
        .all(|x| x == false));
}

// Replays archived protocol metadata, not the actual PF IR transformer. Always --synthetic.
const FAKE: &str = r#"
import hashlib,json,os,pathlib,sys,time
r=pathlib.Path(ROOT); a=sys.argv[1:]
with (r/'calls.jsonl').open('a') as f:f.write(json.dumps(a)+'\n')
def sha(b):return hashlib.sha256(b).hexdigest()
def enc(v):return json.dumps(v,sort_keys=True,indent=2).encode()
def opt(k):return a[a.index(k)+1]
if BEHAVIOR=='timeout':time.sleep(30)
if BEHAVIOR=='output':sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(30)
if BEHAVIOR=='lingering':
 p=os.fork()
 if p==0:
  os.close(1);os.close(2);time.sleep(30);os._exit(0)
 sys.exit(0)
if BEHAVIOR=='invalid':print('{}');sys.exit(0)
if a[:2]==['gray-artifact','adapt-storage']:
 d=json.loads((r/'seed/descriptor.json').read_text());m=json.loads((r/'seed/manifest.json').read_text())
 files={'original.comp':pathlib.Path(opt('--source')).read_bytes(),'original.spv':pathlib.Path(opt('--spirv')).read_bytes(),'adapted.spv':(r/'seed/adapted.spv').read_bytes()}
 if BEHAVIOR=='forge_source':files['original.comp']+=b'\n// changed after compiler export\n'
 d['original_source_sha256']=sha(files['original.comp']);d['original_spirv_sha256']=sha(files['original.spv']);d['adapted_spirv_sha256']=sha(files['adapted.spv'])
 files['descriptor.json']=enc(d); id=sha(files['descriptor.json']);m['adaptation_id']=id;m['files']={k:sha(v) for k,v in files.items()};files['manifest.json']=enc(m)
 p=pathlib.Path(opt('--out'))/('storage-'+id);p.mkdir()
 for k,v in files.items():(p/k).write_bytes(v)
elif a[:2]==['gray-artifact','verify-adaptation']:
 p=pathlib.Path(opt('--artifact'));d=json.loads((p/'descriptor.json').read_text());id=sha((p/'descriptor.json').read_bytes())
 if BEHAVIOR=='verify_failure':print('verification failed',file=sys.stderr);sys.exit(9)
elif a[:2]==['gray-artifact','build']:
 print('missing tool '+opt('--ndk')+'/glslc: install an Android NDK explicitly',file=sys.stderr);sys.exit(1)
else:raise Exception('unexpected argv '+repr(a))
print(json.dumps({'adaptation_id':id,'descriptor':d,'deployable':False,'device_validation':'not_run','qualification':'not_run','semantic_equivalence':'not_run','source_spirv_association':'caller_supplied_unverified','state':'verified_offline','path':str(p)}))
"#;

#[test]
fn complete_offline_mechanism_chain_has_real_compiler_and_no_qualification() {
    let f = Fixture::new("ok");
    let o = f.run(&[]);
    success(&o);
    let v = parse(&o);
    no_proof(&v);
    assert_eq!(v["status"], "PREPARED_OFFLINE");
    assert_eq!(v["provenance"], "synthetic_mechanism_fixture");
    assert_eq!(
        v["adapter_binding"]["local_compiler_export_bytes_equal"],
        true
    );
    assert_eq!(f.calls(), 2);
    let j = f.read("session/journal.json");
    assert!(j["stages"].as_array().unwrap()[..4]
        .iter()
        .all(|s| s["state"] == "COMPLETED" && s["group_absent"] == true));
    assert_eq!(j["stages"][4]["state"], "NOT_RUN");
    success(&f.inspect());
    let again = f.run(&[]);
    assert!(!again.status.success());
    assert_eq!(f.calls(), 2);
}
#[test]
fn gate_failure_retains_negative_before_any_pf_call() {
    let f = Fixture::new("ok");
    let source = tracks::embedded_template(TrackMode::Sr).replace(
        "imageStore(u_OutputTexture, outPos, color);",
        "while (true) { color += vec4(0.01); } imageStore(u_OutputTexture, outPos, color);",
    );
    let c = Candidate::new(
        "unsafe",
        0,
        "sr",
        None,
        1.5,
        source,
        CandidateOrigin::LocalMutation,
        None,
    );
    fs::write(f.path("candidate.json"), serde_json::to_vec(&c).unwrap()).unwrap();
    let o = f.run(&[]);
    assert!(!o.status.success());
    no_proof(&parse(&o));
    assert_eq!(f.calls(), 0);
    let j = f.read("session/journal.json");
    assert_eq!(j["stages"][0]["state"], "FAILED");
    assert!(f.path("session/compiled/gate.json").exists());
    assert!(!f.path("session/compiled/original.spv").exists());
}
#[test]
fn forged_adapter_source_is_rejected_even_with_updated_internal_hashes() {
    let f = Fixture::new("forge_source");
    let o = f.run(&[]);
    assert!(!o.status.success());
    let v = parse(&o);
    assert!(v["error"]
        .as_str()
        .unwrap()
        .contains("does not equal GOL export"));
    assert_eq!(f.calls(), 1);
    assert!(!f.path("session/prepared.json").exists());
}
#[test]
fn malformed_or_failed_verification_never_completes_chain() {
    for b in ["invalid", "verify_failure"] {
        let f = Fixture::new(b);
        let o = f.run(&[]);
        assert!(!o.status.success());
        let v = parse(&o);
        no_proof(&v);
        assert_eq!(v["preparation_chain_complete"], false);
        assert!(!f.path("session/prepared.json").exists());
    }
}
#[test]
fn output_and_lingering_groups_are_unknown_and_never_retried() {
    for b in ["output", "lingering"] {
        let f = Fixture::new(b);
        let o = f.run(&["--stream-bytes", "4096"]);
        assert!(!o.status.success());
        let v = parse(&o);
        assert_eq!(v["status"], "UNKNOWN");
        no_proof(&v);
        let count = f.calls();
        assert_eq!(count, 1);
        let j = f.read("session/journal.json");
        assert!(j["stages"][2]["elapsed_ms"].as_u64().unwrap() < 5000);
        assert!(j["stages"][2]["error"]
            .as_str()
            .unwrap()
            .contains(if b == "output" {
                "byte cap"
            } else {
                "group remains"
            }));
        let inspect = f.inspect();
        assert!(!inspect.status.success());
        assert_eq!(parse(&inspect)["status"], "UNKNOWN");
        let rerun = f.run(&[]);
        assert!(!rerun.status.success());
        assert_eq!(f.calls(), count);
    }
}
#[test]
fn wall_and_whole_budget_exhaustion_do_not_repeat_unknown_execution() {
    for args in [
        vec!["--stage-wall-ms", "500", "--whole-wall-ms", "20000"],
        vec!["--stage-wall-ms", "5000", "--whole-wall-ms", "500"],
    ] {
        let f = Fixture::new("timeout");
        let begin = Instant::now();
        let o = f.run(&args);
        assert!(!o.status.success());
        assert!(begin.elapsed() < Duration::from_secs(10));
        let v = parse(&o);
        assert!(v["status"] == "FAILED" || v["status"] == "UNKNOWN");
        let count = f.calls();
        assert!(count <= 1);
        assert!(!f.run(&[]).status.success());
        assert_eq!(f.calls(), count);
    }
}
#[test]
fn sdk_requests_are_explicit_blocked_negative_or_not_run() {
    let missing = Fixture::new("ok");
    let ndk = missing.path("missing-ndk");
    let o = missing.run(&["--build", "--ndk", ndk.to_str().unwrap()]);
    assert!(!o.status.success());
    let v = parse(&o);
    assert_eq!(v["status"], "BLOCKED");
    assert_eq!(v["native_build_state"], "BLOCKED");
    assert_eq!(v["native_build_reason"], "MISSING_ANDROID_NDK");
    no_proof(&v);
    assert_eq!(missing.calls(), 3);
    assert!(!missing.inspect().status.success());
    // The durable negative probe remains readable if the final receipt checkpoint
    // is interrupted. It must not appear as COMPLETED with a nonzero exit code.
    let mut interrupted = missing.read("session/journal.json");
    interrupted["status"] = json!("RUNNING");
    interrupted["prepared_receipt"] = Value::Null;
    let bytes = serde_json::to_vec(&interrupted).unwrap();
    fs::write(missing.path("session/journal.json"), &bytes).unwrap();
    let inspected = missing.inspect();
    assert_eq!(parse(&inspected)["status"], "UNKNOWN");
    assert_eq!(
        parse(&inspected)["journal"]["stages"][4]["state"],
        "BLOCKED"
    );
    assert_eq!(
        fs::read(missing.path("session/journal.json")).unwrap(),
        bytes
    );
    assert_eq!(missing.calls(), 3);
    let present = Fixture::new("ok");
    fs::create_dir(present.path("sdk")).unwrap();
    let o = present.run(&["--build", "--ndk", present.path("sdk").to_str().unwrap()]);
    assert!(!o.status.success());
    let v = parse(&o);
    assert_eq!(v["native_build_state"], "NOT_RUN");
    assert!(v["native_build_reason"]
        .as_str()
        .unwrap()
        .starts_with("BLOCKED_NATIVE_INPUTS"));
    assert_eq!(present.calls(), 2);
    no_proof(&v);
}
#[test]
fn inspection_rejects_artifact_logs_flags_and_missing_state_receipts() {
    for target in ["artifact", "log", "flags", "final"] {
        let f = Fixture::new("ok");
        success(&f.run(&[]));
        match target {
            "artifact" => {
                let p = f.read("session/journal.json")["stages"][2]["result"]["path"]
                    .as_str()
                    .unwrap()
                    .to_owned();
                fs::write(Path::new(&p).join("adapted.spv"), b"changed").unwrap();
            }
            "log" => fs::write(f.path("session/adapt.stdout.log"), b"changed").unwrap(),
            "flags" => {
                let mut j = f.read("session/journal.json");
                j["proof_flags"]["deployable"] = json!(true);
                fs::write(
                    f.path("session/journal.json"),
                    serde_json::to_vec(&j).unwrap(),
                )
                .unwrap();
            }
            _ => {
                let mut j = f.read("session/journal.json");
                j["prepared_receipt"] = Value::Null;
                fs::write(
                    f.path("session/journal.json"),
                    serde_json::to_vec(&j).unwrap(),
                )
                .unwrap();
            }
        }
        assert!(!f.inspect().status.success());
        assert_eq!(f.calls(), 2);
    }
}
#[test]
fn rehashed_final_receipt_cannot_contradict_current_bindings_or_native_policy() {
    let f = Fixture::new("ok");
    success(&f.run(&[]));
    let original = f.read("session/prepared.json");
    let journal = f.read("session/journal.json");
    for target in ["compiler", "rules", "tool", "extra", "native"] {
        let mut v = original.clone();
        let mut j = journal.clone();
        match target {
            "compiler" => v["compiler_receipt"]["candidate_id"] = json!("unbound-candidate"),
            "rules" => v["rules"]["sha256"] = json!("0".repeat(64)),
            "tool" => v["phonefarm_executable"]["sha256"] = json!("0".repeat(64)),
            "extra" => v["unknown_source_attestation"] = json!(true),
            _ => j["stages"][4]["state"] = json!("PENDING"),
        }
        let bytes = serde_json::to_vec(&v).unwrap();
        j["prepared_receipt"] =
            json!({"sha256":format!("{:x}",Sha256::digest(&bytes)),"bytes":bytes.len()});
        fs::write(f.path("session/prepared.json"), bytes).unwrap();
        fs::write(
            f.path("session/journal.json"),
            serde_json::to_vec(&j).unwrap(),
        )
        .unwrap();
        assert!(
            !f.inspect().status.success(),
            "accepted contradiction: {target}"
        );
        assert_eq!(f.calls(), 2);
    }
}
#[test]
fn in_flight_inspection_is_unknown_read_only_and_cannot_run() {
    let f = Fixture::new("ok");
    success(&f.run(&[]));
    let mut j = f.read("session/journal.json");
    j["status"] = json!("RUNNING");
    j["prepared_receipt"] = Value::Null;
    j["stages"][3]["state"] = json!("IN_FLIGHT");
    let bytes = serde_json::to_vec(&j).unwrap();
    fs::write(f.path("session/journal.json"), &bytes).unwrap();
    let o = f.inspect();
    assert!(!o.status.success());
    assert_eq!(parse(&o)["status"], "UNKNOWN");
    assert_eq!(fs::read(f.path("session/journal.json")).unwrap(), bytes);
    assert_eq!(f.calls(), 2);
}

#[test]
fn actual_host_interruption_leaves_unknown_owned_child_and_no_replay() {
    struct Group(u32);
    impl Drop for Group {
        fn drop(&mut self) {
            unsafe {
                libc::kill(-(self.0 as i32), libc::SIGKILL);
            }
        }
    }
    let f = Fixture::new("timeout");
    let mut host = f
        .command(&["--stage-wall-ms", "20000", "--whole-wall-ms", "120000"])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let limit = Instant::now() + Duration::from_secs(50);
    let group = loop {
        if let Ok(bytes) = fs::read(f.path("session/journal.json")) {
            if let Ok(j) = serde_json::from_slice::<Value>(&bytes) {
                if j["stages"][2]["state"] == "IN_FLIGHT" && f.calls() == 1 {
                    break Group(j["stages"][2]["pid"].as_u64().unwrap() as u32);
                }
            }
        }
        assert!(
            host.try_wait().unwrap().is_none(),
            "host failed before PF child entered"
        );
        assert!(
            Instant::now() < limit,
            "PF child did not enter before test deadline"
        );
        std::thread::sleep(Duration::from_millis(20));
    };
    host.kill().unwrap();
    let until = Instant::now() + Duration::from_secs(2);
    while host.try_wait().unwrap().is_none() {
        assert!(Instant::now() < until);
        std::thread::sleep(Duration::from_millis(10));
    }
    let before = fs::read(f.path("session/journal.json")).unwrap();
    let inspected = f.inspect();
    assert!(!inspected.status.success());
    assert_eq!(parse(&inspected)["status"], "UNKNOWN");
    assert_eq!(fs::read(f.path("session/journal.json")).unwrap(), before);
    assert!(!f.run(&[]).status.success());
    assert_eq!(f.calls(), 1);
    drop(group); // Test-owned cleanup, never an implicit driver resume/reconciliation.
}
#[test]
fn symlink_ancestors_inputs_and_existing_outputs_are_rejected() {
    let f = Fixture::new("ok");
    fs::rename(f.path("candidate.json"), f.path("real.json")).unwrap();
    symlink(f.path("real.json"), f.path("candidate.json")).unwrap();
    assert!(!f.run(&[]).status.success());
    assert_eq!(f.calls(), 0);
    let f = Fixture::new("ok");
    symlink(&f.0, f.path("alias")).unwrap();
    let o = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["candidate-prepare", "run", "--candidate"])
        .arg(f.path("alias/candidate.json"))
        .arg("--phonefarm-bin")
        .arg(f.path("fake-pf"))
        .arg("--out")
        .arg(f.path("session"))
        .output()
        .unwrap();
    assert!(!o.status.success());
    assert_eq!(f.calls(), 0);
}

/// Explicit opt-in actual cross-repository CLI acceptance, separate from synthetic fixtures.
#[test]
fn actual_phonefarm_adapter_and_missing_sdk_when_explicitly_configured() {
    let Some(pf) = std::env::var_os("GOL_TEST_ACTUAL_PHONEFARM_BIN") else {
        return;
    };
    let f = Fixture::new("ok");
    let o = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["candidate-prepare", "run", "--candidate"])
        .arg(f.path("candidate.json"))
        .arg("--phonefarm-bin")
        .arg(&pf)
        .arg("--out")
        .arg(f.path("actual-session"))
        .args(["--build", "--ndk"])
        .arg(f.path("missing-actual-ndk"))
        .output()
        .unwrap();
    assert!(!o.status.success());
    let v = parse(&o);
    assert_eq!(v["status"], "BLOCKED");
    assert_eq!(v["offline_compilation_adapter_complete"], true);
    assert_eq!(v["preparation_chain_complete"], false);
    assert_eq!(v["native_build_reason"], "MISSING_ANDROID_NDK");
    assert_eq!(
        v["adapter_binding"]["local_compiler_export_bytes_equal"],
        true
    );
    no_proof(&v);
    assert_eq!(f.calls(), 0);
}
