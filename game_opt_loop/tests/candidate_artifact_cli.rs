use game_opt_loop::engine::{Candidate, CandidateOrigin};
use game_opt_loop::{tracks, TrackMode};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = if cfg!(target_os = "macos") {
            "/private/tmp"
        } else {
            "/tmp"
        };
        let path = Path::new(root).join(format!(
            "gol-compilation-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    fn input(&self, mode: TrackMode, source: Option<String>) -> PathBuf {
        let source = source.unwrap_or_else(|| tracks::embedded_template(mode).into());
        let candidate = Candidate::new(
            "candidate-1",
            2,
            mode.id(),
            Some("baseline".into()),
            tracks::spec(mode).budget_ms,
            source,
            CandidateOrigin::LocalMutation,
            Some("fixture source; no game evidence".into()),
        );
        let path = self.path("input.json");
        fs::write(&path, serde_json::to_vec_pretty(&candidate).unwrap()).unwrap();
        path
    }
    fn compile(&self, input: &Path, directory: &str) -> Output {
        cli(&[
            "candidate-artifact",
            "compile",
            "--candidate",
            input.to_str().unwrap(),
            "--out",
            self.path(directory).to_str().unwrap(),
        ])
    }
    fn success(&self) -> (PathBuf, Value) {
        let output = self.compile(&self.input(TrackMode::Sr, None), "artifact");
        assert_success(&output);
        (self.path("artifact"), parse(&output))
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(args)
        .env("PATH", "")
        .output()
        .unwrap()
}
fn verify(dir: &Path, identity: &str) -> Output {
    cli(&[
        "candidate-artifact",
        "verify",
        "--artifact",
        dir.to_str().unwrap(),
        "--expected-identity",
        identity,
    ])
}
fn parse(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap()
}
fn assert_success(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn read_json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}
fn write_json(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec_pretty(value).unwrap()).unwrap();
}
fn sha(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn identity(bytes: &[u8]) -> Value {
    json!({"sha256":sha(bytes),"bytes":bytes.len()})
}
// Maliciously rehash a complete ledger, so tests exercise replay rather than only SHA mismatch.
fn rehash(dir: &Path, changed: &str) -> String {
    let mut descriptor = read_json(&dir.join("descriptor.json"));
    let data = fs::read(dir.join(changed)).unwrap();
    if changed != "descriptor.json" {
        descriptor["files"][changed] = identity(&data);
    }
    write_json(&dir.join("descriptor.json"), &descriptor);
    let mut manifest = read_json(&dir.join("manifest.json"));
    manifest["files"][changed] = identity(&data);
    let descriptor_bytes = fs::read(dir.join("descriptor.json")).unwrap();
    manifest["files"]["descriptor.json"] = identity(&descriptor_bytes);
    manifest["frozen_identity"] = json!(sha(&descriptor_bytes));
    write_json(&dir.join("manifest.json"), &manifest);
    sha(&descriptor_bytes)
}

#[test]
fn actual_sr_compiler_replays_and_exports_adapter_binding() {
    let f = Fixture::new();
    let (dir, compilation) = f.success();
    assert_eq!(compilation["state"], "host_compiled");
    let output = verify(&dir, compilation["frozen_identity"].as_str().unwrap());
    assert_success(&output);
    let report = parse(&output);
    assert_eq!(report["state"], "host_compiled_replay_match");
    assert!(report["proof_flags"]
        .as_object()
        .unwrap()
        .values()
        .all(|flag| flag == false));
    assert_eq!(
        report["adapter_input"]["source_sha256"],
        sha(&fs::read(dir.join("original.comp")).unwrap())
    );
    let spv = fs::read(dir.join("original.spv")).unwrap();
    assert_eq!(&spv[..4], &[3, 2, 35, 7]);
    assert_eq!(report["adapter_input"]["original_spirv_sha256"], sha(&spv));
    let descriptor = read_json(&dir.join("descriptor.json"));
    assert_eq!(
        descriptor["compiler_identity"]["embedded_files"]["Cargo.lock"]["sha256"],
        sha(include_bytes!("../Cargo.lock"))
    );
}

#[test]
fn actual_framegen_compiles_without_claiming_sr_adapter_or_gpu() {
    let f = Fixture::new();
    let output = f.compile(&f.input(TrackMode::FrameGen, None), "framegen");
    assert_success(&output);
    let report = parse(&output);
    assert_eq!(report["track"], "frame_gen");
    assert_success(&verify(
        &f.path("framegen"),
        report["frozen_identity"].as_str().unwrap(),
    ));
    assert_eq!(report["proof_flags"]["deployable"], false);
}

#[test]
fn unsafe_candidate_is_persisted_but_cannot_export_or_verify_as_success() {
    let f = Fixture::new();
    let source = tracks::embedded_template(TrackMode::Sr).replace(
        "// [LLM 变异插槽: 高频重构与边缘自适应锐化]",
        "while (true) { color += vec4(0.01); }",
    );
    let output = f.compile(&f.input(TrackMode::Sr, Some(source)), "rejected");
    assert!(!output.status.success());
    let report = parse(&output);
    assert_eq!(report["state"], "gate_rejected");
    assert_eq!(report["adapter_input"], Value::Null);
    assert!(!f.path("rejected/original.spv").exists());
    let replay = verify(
        &f.path("rejected"),
        report["frozen_identity"].as_str().unwrap(),
    );
    assert!(!replay.status.success());
    assert_eq!(parse(&replay)["state"], "gate_rejected_replay_match");
}

#[test]
fn strict_candidate_rejects_missing_unknown_duplicate_and_wrong_fields() {
    for mutation in [
        "missing",
        "unknown",
        "track",
        "origin",
        "fingerprint",
        "budget",
        "id",
    ] {
        let f = Fixture::new();
        let input = f.input(TrackMode::Sr, None);
        let mut value = read_json(&input);
        match mutation {
            "missing" => {
                value.as_object_mut().unwrap().remove("parent_id");
            }
            "unknown" => value["gate_passed"] = json!(true),
            "track" => value["track"] = json!("unknown"),
            "origin" => value["origin"] = json!("verified_real_game"),
            "fingerprint" => value["fingerprint"] = json!("0000000000000000"),
            "budget" => value["budget_ms"] = json!(0),
            _ => value["id"] = json!(" "),
        }
        write_json(&input, &value);
        assert!(!f.compile(&input, "invalid").status.success(), "{mutation}");
        assert!(!f.path("invalid").exists());
    }
    let f = Fixture::new();
    let input = f.input(TrackMode::Sr, None);
    let bytes = fs::read_to_string(&input)
        .unwrap()
        .replacen('{', "{\"id\":\"duplicated\",", 1);
    fs::write(&input, bytes).unwrap();
    assert!(!f.compile(&input, "duplicate").status.success());
}

#[test]
fn missing_or_modified_artifacts_fail_closed() {
    let f = Fixture::new();
    let (dir, report) = f.success();
    for name in [
        "candidate.json",
        "original.comp",
        "original.spv",
        "rules.json",
        "gate.json",
        "descriptor.json",
    ] {
        let path = dir.join(name);
        let bytes = fs::read(&path).unwrap();
        fs::write(&path, b"modified").unwrap();
        assert!(
            !verify(&dir, report["frozen_identity"].as_str().unwrap())
                .status
                .success(),
            "{name}"
        );
        fs::write(&path, &bytes).unwrap();
        fs::remove_file(&path).unwrap();
        assert!(
            !verify(&dir, report["frozen_identity"].as_str().unwrap())
                .status
                .success(),
            "missing {name}"
        );
        fs::write(path, bytes).unwrap();
    }
}

#[test]
fn rehashed_bytecode_cannot_bypass_actual_recompilation() {
    let f = Fixture::new();
    let (dir, original) = f.success();
    let mut spv = fs::read(dir.join("original.spv")).unwrap();
    let end = spv.len() - 1;
    spv[end] ^= 1;
    fs::write(dir.join("original.spv"), spv).unwrap();
    let forged = rehash(&dir, "original.spv");
    assert!(!verify(&dir, original["frozen_identity"].as_str().unwrap())
        .status
        .success());
    // Even deliberately trusting the replacement identity does not skip the gate/compiler.
    let replay = verify(&dir, &forged);
    assert!(!replay.status.success());
    assert!(String::from_utf8_lossy(&replay.stderr).contains("replay mismatch"));
}

#[test]
fn rehashed_rules_gate_and_binary_claims_are_not_accepted() {
    for name in ["rules.json", "gate.json", "descriptor.json"] {
        let f = Fixture::new();
        let (dir, _) = f.success();
        let mut value = read_json(&dir.join(name));
        match name {
            "rules.json" => value["limits"]["max_loop_trip"] = json!(999999),
            "gate.json" => value["features"]["image_loads"] = json!(9999),
            _ => value["compiler_identity"]["executable_sha256"] = json!("0".repeat(64)),
        }
        write_json(&dir.join(name), &value);
        let forged = rehash(&dir, name);
        assert!(!verify(&dir, &forged).status.success(), "{name}");
    }
}

#[test]
fn replacing_all_frozen_candidate_identity_requires_new_external_approval() {
    let f = Fixture::new();
    let (dir, original) = f.success();
    let mut candidate = read_json(&dir.join("candidate.json"));
    candidate["id"] = json!("substituted-candidate");
    write_json(&dir.join("candidate.json"), &candidate);
    let mut descriptor = read_json(&dir.join("descriptor.json"));
    descriptor["candidate_id"] = candidate["id"].clone();
    write_json(&dir.join("descriptor.json"), &descriptor);
    let forged = rehash(&dir, "candidate.json");
    assert!(!verify(&dir, original["frozen_identity"].as_str().unwrap())
        .status
        .success());
    assert_success(&verify(&dir, &forged));
    assert_ne!(forged, original["frozen_identity"]);
}

#[test]
fn symlink_inputs_ancestors_outputs_and_artifacts_are_rejected() {
    let f = Fixture::new();
    let input = f.input(TrackMode::Sr, None);
    symlink(&input, f.path("linked-input")).unwrap();
    assert!(!f
        .compile(&f.path("linked-input"), "input-symlink")
        .status
        .success());
    symlink(&f.0, f.path("linked-parent")).unwrap();
    assert!(!f
        .compile(&f.path("linked-parent/input.json"), "parent-symlink")
        .status
        .success());
    assert!(!f.compile(&input, "linked-parent/output").status.success());
    symlink(&f.0, f.path("output-link")).unwrap();
    assert!(!f.compile(&input, "output-link").status.success());
    let (dir, report) = f.success();
    let source = dir.join("original.comp");
    fs::rename(&source, f.path("saved-source")).unwrap();
    symlink(f.path("saved-source"), &source).unwrap();
    assert!(!verify(&dir, report["frozen_identity"].as_str().unwrap())
        .status
        .success());
}

#[test]
fn overwrite_extra_files_unbounded_inputs_and_unknown_proofs_fail() {
    let f = Fixture::new();
    let (dir, report) = f.success();
    let old = fs::read(dir.join("manifest.json")).unwrap();
    assert!(!f
        .compile(&f.path("input.json"), "artifact")
        .status
        .success());
    assert_eq!(old, fs::read(dir.join("manifest.json")).unwrap());
    fs::write(dir.join("unexpected"), b"extra").unwrap();
    assert!(!verify(&dir, report["frozen_identity"].as_str().unwrap())
        .status
        .success());
    fs::remove_file(dir.join("unexpected")).unwrap();
    let mut manifest = read_json(&dir.join("manifest.json"));
    manifest["proof_flags"]["deployable"] = json!(true);
    write_json(&dir.join("manifest.json"), &manifest);
    assert!(!verify(&dir, report["frozen_identity"].as_str().unwrap())
        .status
        .success());
    fs::write(f.path("huge"), vec![b' '; 1024 * 1024 + 1]).unwrap();
    assert!(!f.compile(&f.path("huge"), "too-big").status.success());
    assert!(!f.path("too-big").exists());
}
