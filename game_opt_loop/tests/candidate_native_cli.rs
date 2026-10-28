//! Actual GOL/PF24 children with explicitly synthetic regular SDK fixtures.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    root: PathBuf,
    pf: PathBuf,
    layer: PathBuf,
}
fn hash(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
fn parse(o: &Output) -> Value {
    serde_json::from_slice(&o.stdout).unwrap_or_else(|e| {
        panic!(
            "{e}; stdout={} stderr={}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        )
    })
}
impl Fixture {
    fn new(behavior: &str) -> Self {
        let pf = std::env::var_os("GOL_TEST_SUPERVISED_PHONEFARM").expect(
            "configuration error: GOL_TEST_SUPERVISED_PHONEFARM must be set to an explicit PF24 binary when running this ignored acceptance test",
        );
        let layer = PathBuf::from(std::env::var_os("GOL_TEST_PF_LAYER_ROOT").expect(
            "configuration error: GOL_TEST_PF_LAYER_ROOT must be set to the actual PF layer root when running this ignored acceptance test",
        ));
        Self::new_for(behavior, PathBuf::from(pf), layer)
    }
    fn new_for(behavior: &str, pf: PathBuf, layer: PathBuf) -> Self {
        let base = if cfg!(target_os = "macos") {
            "/private/tmp"
        } else {
            "/tmp"
        };
        let root = Path::new(base).join(format!(
            "gol-native-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let actual_pf = root.join("caller-pf");
        fs::copy(pf, &actual_pf).unwrap();
        fs::set_permissions(&actual_pf, fs::Permissions::from_mode(0o700)).unwrap();
        let f = Self {
            root,
            pf: actual_pf,
            layer,
        };
        fs::write(
            f.p("candidate.json"),
            include_bytes!("../tasks/candidate_compilation_20261011/sr-candidate.json"),
        )
        .unwrap();
        fs::create_dir(f.p("sdk")).unwrap();
        fs::write(f.p("sdk/copy-fixture.spv"),include_bytes!("../tasks/candidate_compilation_20261011/adapted/storage-01e67a5e92937da8b0369f19bbc6a38bea0be40e8c7f820af75883e60fc595a1/adapted.spv")).unwrap();
        for (relative, kind) in [
            ("shader-tools/linux-x86_64/glslc", "shader"),
            (
                "toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android30-clang++",
                "native",
            ),
            ("toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-nm", "nm"),
            (
                "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf",
                "readelf",
            ),
        ] {
            let tool = f.p("sdk").join(relative);
            fs::create_dir_all(tool.parent().unwrap()).unwrap();
            let code = format!(
                "#!/usr/bin/python3\nROOT={}\nBEHAVIOR={}\nKIND={}\n{}",
                json!(f.root.to_str().unwrap()),
                json!(behavior),
                json!(kind),
                DRIVER
            );
            fs::write(&tool, code).unwrap();
            fs::set_permissions(&tool, fs::Permissions::from_mode(0o700)).unwrap();
        }
        f
    }
    fn p(&self, n: &str) -> PathBuf {
        self.root.join(n)
    }
    fn run(&self, extra: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
            .args(["candidate-prepare", "run", "--candidate"])
            .arg(self.p("candidate.json"))
            .arg("--phonefarm-bin")
            .arg(&self.pf)
            .arg("--out")
            .arg(self.p("session"))
            .args(["--synthetic", "--build", "--ndk"])
            .arg(self.p("sdk"))
            .arg("--native-layer-input-root")
            .arg(&self.layer)
            .args(extra)
            .output()
            .unwrap()
    }
    fn inspect(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
            .args(["candidate-prepare", "inspect", "--session"])
            .arg(self.p("session"))
            .output()
            .unwrap()
    }
    fn read(&self, n: &str) -> Value {
        serde_json::from_slice(&fs::read(self.p(n)).unwrap()).unwrap()
    }
}
fn writable(path: &Path) {
    if let Ok(m) = fs::symlink_metadata(path) {
        if m.is_dir() {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
            for e in fs::read_dir(path).unwrap() {
                writable(&e.unwrap().path());
            }
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        writable(&self.root);
        let _ = fs::remove_dir_all(&self.root);
    }
}
const DRIVER: &str = r#"
import pathlib,sys,os,time,json
r=pathlib.Path(ROOT)
with (r/'tool-groups.jsonl').open('a') as f:f.write(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'tool':str(pathlib.Path(__file__).resolve()),'kind':KIND,'argv':sys.argv[1:]})+'\n')
if KIND=='shader' and '--version' in sys.argv:
 if BEHAVIOR=='hang':time.sleep(60)
 if BEHAVIOR=='stderr':sys.stderr.write('x'*10000000);sys.stderr.flush();time.sleep(30)
 if BEHAVIOR=='pf_mutation':(r/'caller-pf').write_bytes(b'caller changed after private snapshot')
 if BEHAVIOR=='sdk_mutation':(r/'sdk/shader-tools/linux-x86_64/glslc').write_bytes(b'caller SDK changed after private snapshot')
 if BEHAVIOR=='descendant':
  p=os.fork()
  if p==0:
   os.close(1);os.close(2);time.sleep(60);(r/'late-writer').write_text('unsafe');os._exit(0)
if '--version' in sys.argv:print('controlled synthetic SDK fixture v1');sys.exit(0)
if KIND=='shader':
 p=pathlib.Path(__file__).resolve()
 while p.name!='ndk.snapshot':p=p.parent
 pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes((p/'copy-fixture.spv').read_bytes())
elif KIND=='native':
 d=bytearray(64);d[:6]=b'\x7fELF\x02\x01';d[16:20]=b'\x03\x00\xb7\x00';pathlib.Path(sys.argv[sys.argv.index('-o')+1]).write_bytes(d)
elif KIND=='nm':print('000 T vkNegotiateLoaderLayerInterfaceVersion\n000 T vkGetInstanceProcAddr\n000 T vkGetDeviceProcAddr\n000 T vkEnumerateInstanceLayerProperties\n000 T vkEnumerateInstanceExtensionProperties')
elif KIND=='readelf':print('0x1 (NEEDED) Shared library: [liblog.so]\n0x1 (NEEDED) Shared library: [libc.so]')
"#;
fn no_proof(v: &Value) {
    assert!(v["proof_flags"]
        .as_object()
        .unwrap()
        .values()
        .all(|x| x == false));
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn actual_supervised_fixture_binds_sdk_source_adapter_and_outer_groups() {
    let f = Fixture::new("ok");
    let o = f.run(&[]);
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let v = parse(&o);
    no_proof(&v);
    assert_eq!(v["status"], "PREPARED_OFFLINE_NATIVE");
    assert_eq!(v["preparation_chain_complete"], true);
    assert_eq!(
        v["native_binding"]["host_artifact_integrity_accepted"],
        true
    );
    assert_eq!(v["native_binding"]["android_codegen_validated"], false);
    assert_eq!(v["native_binding"]["real_ndk_validation"], "not_run");
    assert_eq!(v["provenance"], "synthetic_mechanism_fixture");
    let j = f.read("session/journal.json");
    assert_eq!(
        v["native_plan"]["layer_contract"],
        if f.layer.join("npuprobe.cpp").exists() {
            "npuprobe6"
        } else if f.layer.join("dispatch_ownership.h").exists() {
            "ownership5"
        } else {
            "legacy4"
        }
    );
    let pid = j["stages"][4]["pid"].as_u64().unwrap();
    let groups = fs::read_to_string(f.p("tool-groups.jsonl")).unwrap();
    assert_eq!(groups.lines().count(), 8);
    for line in groups.lines() {
        let t: Value = serde_json::from_str(line).unwrap();
        assert_eq!(t["pgid"], pid);
        assert!(t["tool"]
            .as_str()
            .unwrap()
            .starts_with(f.p("session/ndk.snapshot").to_str().unwrap()));
    }
    assert!(j["stages"]
        .as_array()
        .unwrap()
        .iter()
        .all(|s| s["state"] == "COMPLETED" && s["group_absent"] == true));
    assert!(f.inspect().status.success());
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn actual_native_descendant_closed_pipes_never_accepts_or_retries() {
    let f = Fixture::new("descendant");
    let o = f.run(&[]);
    assert!(!o.status.success());
    let j = f.read("session/journal.json");
    assert_eq!(j["status"], "UNKNOWN");
    assert_eq!(j["stages"][4]["state"], "UNKNOWN");
    assert_eq!(j["stages"][5]["state"], "PENDING");
    no_proof(&j);
    assert!(!f.inspect().status.success());
    assert!(!f.run(&[]).status.success());
    std::thread::sleep(std::time::Duration::from_millis(2200));
    assert!(!f.p("late-writer").exists());
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn actual_helper_hang_and_full_stderr_are_unknown_and_bounded() {
    for behavior in ["hang", "stderr"] {
        let f = Fixture::new(behavior);
        let begin = std::time::Instant::now();
        let o = f.run(&[
            "--native-tool-timeout-seconds",
            "10",
            "--stage-wall-ms",
            "60000",
            "--whole-wall-ms",
            "240000",
        ]);
        assert!(!o.status.success());
        assert!(begin.elapsed() < std::time::Duration::from_secs(245));
        let j = f.read("session/journal.json");
        assert_eq!(j["stages"][4]["state"], "UNKNOWN");
        assert!(j["stages"][4]["elapsed_ms"].as_u64().unwrap() < 62_000);
        assert!(fs::read_to_string(f.p("tool-groups.jsonl"))
            .unwrap()
            .contains("shader"));
        assert_eq!(j["status"], "UNKNOWN");
        no_proof(&j);
        assert!(!f.inspect().status.success());
    }
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn caller_pf_and_sdk_mutation_cannot_replace_private_execution() {
    for behavior in ["pf_mutation", "sdk_mutation"] {
        let f = Fixture::new(behavior);
        let original_path = if behavior == "pf_mutation" {
            f.pf.clone()
        } else {
            f.p("sdk/shader-tools/linux-x86_64/glslc")
        };
        let original = fs::read(original_path).unwrap();
        let o = f.run(&[]);
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        let v = parse(&o);
        no_proof(&v);
        let frozen = fs::read(if behavior == "pf_mutation" {
            f.p("session/phonefarm.bin")
        } else {
            f.p("session/ndk.snapshot/shader-tools/linux-x86_64/glslc")
        })
        .unwrap();
        assert_eq!(original, frozen);
        assert!(f.inspect().status.success());
    }
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn unsupported_sdk_symlink_and_wrong_layer_root_are_retained_negative() {
    let f = Fixture::new("ok");
    symlink("copy-fixture.spv", f.p("sdk/alias")).unwrap();
    let o = f.run(&[]);
    assert!(!o.status.success());
    let v = parse(&o);
    assert_eq!(v["status"], "BLOCKED");
    assert!(v["native_build_reason"]
        .as_str()
        .unwrap()
        .contains("symlink"));
    assert_eq!(v["native_build_state"], "NOT_RUN");
    assert!(!f.p("tool-groups.jsonl").exists());
    no_proof(&v);
    assert!(!f.inspect().status.success());
}
#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn inspect_rejects_rehashed_native_receipt_and_changed_sdk() {
    let f = Fixture::new("ok");
    assert!(f.run(&[]).status.success());
    let prepared = fs::read(f.p("session/prepared.json")).unwrap();
    let journal = fs::read(f.p("session/journal.json")).unwrap();
    for field in [
        "descriptor",
        "sdk_inventory_sha256",
        "outer_verify_group_absent",
        "android_codegen_validated",
    ] {
        let mut p: Value = serde_json::from_slice(&prepared).unwrap();
        p["native_binding"][field] = json!("forged");
        let bytes = serde_json::to_vec(&p).unwrap();
        let mut j: Value = serde_json::from_slice(&journal).unwrap();
        j["prepared_receipt"] = json!({"sha256":hash(&bytes),"bytes":bytes.len()});
        fs::write(f.p("session/prepared.json"), bytes).unwrap();
        fs::write(f.p("session/journal.json"), serde_json::to_vec(&j).unwrap()).unwrap();
        assert!(!f.inspect().status.success());
    }
    // Preserve actual stdout and its journal hashes while forging both nested receipt copies.
    for field in ["build_receipt", "pf_receipt"] {
        let mut p: Value = serde_json::from_slice(&prepared).unwrap();
        let mut j: Value = serde_json::from_slice(&journal).unwrap();
        if field == "build_receipt" {
            j["stages"][4]["result"][field]["reused"] = json!(true);
            j["stages"][5]["result"][field]["reused"] = json!(true);
            p["native_binding"][field]["reused"] = json!(true);
        } else {
            j["stages"][5]["result"][field]["forged"] = json!(true);
            p["native_binding"][field]["forged"] = json!(true);
        }
        let bytes = serde_json::to_vec(&p).unwrap();
        j["prepared_receipt"] = json!({"sha256":hash(&bytes),"bytes":bytes.len()});
        fs::write(f.p("session/prepared.json"), bytes).unwrap();
        fs::write(f.p("session/journal.json"), serde_json::to_vec(&j).unwrap()).unwrap();
        assert!(
            !f.inspect().status.success(),
            "stdout/nested receipt contradiction accepted"
        );
    }
    fs::write(f.p("session/prepared.json"), prepared).unwrap();
    fs::write(f.p("session/journal.json"), journal).unwrap();
    assert!(f.inspect().status.success());
    let p = f.p("session/ndk.snapshot/copy-fixture.spv");
    fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(p, b"modified").unwrap();
    assert!(!f.inspect().status.success());
}

#[test]
#[ignore = "requires GOL_TEST_SUPERVISED_PHONEFARM and GOL_TEST_PF_LAYER_ROOT"]
fn actual_pf_descriptor_refuses_wrong_frozen_layer_expectation() {
    let mut f = Fixture::new("ok");
    let wrong = f.p("declared-layer");
    fs::create_dir(&wrong).unwrap();
    for name in [
        "vk_layer_refknobs.cpp",
        "artifact_stats.h",
        "copyprobe_copy.comp",
        "VkLayer_refknobs.json",
    ] {
        fs::copy(f.layer.join(name), wrong.join(name)).unwrap();
    }
    if f.layer.join("dispatch_ownership.h").exists() {
        fs::copy(
            f.layer.join("dispatch_ownership.h"),
            wrong.join("dispatch_ownership.h"),
        )
        .unwrap();
    }
    let p = wrong.join("VkLayer_refknobs.json");
    let mut bytes = fs::read(&p).unwrap();
    bytes.extend_from_slice(b"\n ");
    fs::write(p, bytes).unwrap();
    f.layer = wrong;
    let o = f.run(&[]);
    assert!(!o.status.success());
    let j = f.read("session/journal.json");
    assert_eq!(j["stages"][4]["state"], "FAILED");
    assert!(j["stages"][4]["error"]
        .as_str()
        .unwrap()
        .contains("PF compiled source differs"));
    assert_eq!(j["stages"][5]["state"], "PENDING");
    no_proof(&j);
    assert!(!f.p("session/prepared.json").exists());
}

#[test]
#[ignore = "requires GOL_TEST_LEGACY_SUPERVISED_PHONEFARM and GOL_TEST_LEGACY_PF_LAYER_ROOT"]
fn actual_legacy_four_input_builder_remains_explicitly_compatible() {
    let pf = std::env::var_os("GOL_TEST_LEGACY_SUPERVISED_PHONEFARM").expect(
        "configuration error: GOL_TEST_LEGACY_SUPERVISED_PHONEFARM must be set to an explicit legacy4 PF binary when running this ignored acceptance test",
    );
    let layer = std::env::var_os("GOL_TEST_LEGACY_PF_LAYER_ROOT").expect(
        "configuration error: GOL_TEST_LEGACY_PF_LAYER_ROOT must be set to the legacy4 PF layer root when running this ignored acceptance test",
    );
    let f = Fixture::new_for("ok", pf.into(), layer.into());
    let o = f.run(&[]);
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let v = parse(&o);
    assert_eq!(v["native_plan"]["layer_contract"], "legacy4");
    no_proof(&v);
    assert!(f.inspect().status.success());
}
