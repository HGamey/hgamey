//! Saved producer snapshots and explicit contract mutations. No producer/driver/device execution.
#![cfg(unix)]
use game_opt_loop::viz::cases::{collect, Collection};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
static ID: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    dir: PathBuf,
    gol: PathBuf,
    phone: PathBuf,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}
impl Fixture {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "gol-case-view-{}-{}",
            std::process::id(),
            ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(&dir).unwrap();
        let dir = dir.canonicalize().unwrap();
        let gol = dir.join("gol");
        let phone = dir.join("phonefarm");
        fs::create_dir_all(&gol).unwrap();
        fs::create_dir_all(&phone).unwrap();
        for root in [&gol, &phone] {
            for name in ["runs", "tasks"] {
                fs::create_dir_all(root.join(name)).unwrap();
            }
        }
        Self { dir, gol, phone }
    }
    fn copy() -> Self {
        let f = Self::new();
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/cases");
        copy(&source.join("gol"), &f.gol);
        copy(&source.join("phonefarm"), &f.phone);
        f
    }
    fn read(&self) -> Collection {
        collect(&self.gol, &self.phone)
    }
}
fn copy(a: &Path, b: &Path) {
    fs::create_dir_all(b).unwrap();
    for e in fs::read_dir(a).unwrap() {
        let e = e.unwrap();
        let target = b.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy(&e.path(), &target);
        } else {
            fs::copy(e.path(), target).unwrap();
        }
    }
}
fn write(p: &Path, v: &Value) {
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, serde_json::to_vec_pretty(v).unwrap()).unwrap();
}
fn read(p: &Path) -> Value {
    serde_json::from_slice(&fs::read(p).unwrap()).unwrap()
}
fn find<'a>(c: &'a Collection, s: &str) -> &'a game_opt_loop::viz::cases::Case {
    c.items.iter().find(|x| x.id.ends_with(s)).unwrap()
}

#[test]
fn actual_saved_confirmation_and_fake_journal_keep_exact_text_negative_results_and_scope() {
    let f = Fixture::copy();
    let c = f.read();
    assert_eq!(c.items.len(), 3);
    assert!(!c.partial);
    let pass = find(&c, "judge.json");
    assert_eq!(pass.declared_status.as_deref(), Some("PASS"));
    assert!(pass
        .source_declarations
        .iter()
        .any(|s| s == "context:synthetic (unverified)"));
    assert_eq!(
        pass.associations
            .iter()
            .filter(|a| a.role.starts_with("receiver "))
            .count(),
        10
    );
    let p = &c.documents[pass.primary_document];
    let raw = fs::read(f.phone.join("tasks/cross_cli/judge.json")).unwrap();
    assert_eq!(p.raw_text.as_ref().unwrap().as_bytes(), raw);
    assert_eq!(p.sha256, format!("{:x}", Sha256::digest(&raw)));
    let blocked = find(&c, "judge-tampered.json");
    assert_eq!(blocked.declared_status.as_deref(), Some("BLOCKED"));
    assert!(!blocked.issues.is_empty());
    let model = find(&c, "pipeline.json");
    assert_eq!(model.stages.len(), 5);
    assert_eq!(
        model.declared_status.as_deref(),
        Some("completed_protocol_declaration")
    );
    assert!(model
        .source_declarations
        .iter()
        .any(|s| s == "data:synthetic (unverified)"));
    assert!(model
        .issues
        .iter()
        .any(|s| s.contains("outside fixed roots")));
    for case in &c.items {
        assert!(!case.view_grants_game_proof && !case.view_allows_promotion);
    }
    assert!(!c.view_grants_game_proof && !c.view_allows_promotion);
}
#[test]
fn malicious_claims_unknown_fields_and_missing_qualification_cannot_grant_permissions() {
    let f = Fixture::copy();
    let p = f.phone.join("tasks/cross_cli/judge.json");
    let mut v = read(&p);
    v["real_game_optimization_proven"] = json!(true);
    v["promotion_allowed"] = json!(true);
    v["qualification"] = Value::Null;
    v["new_receiver_extension"] = json!({"restore":"UNKNOWN","proof":true});
    write(&p, &v);
    let c = f.read();
    let case = find(&c, "judge.json");
    assert!(!case.view_grants_game_proof && !case.view_allows_promotion);
    assert_eq!(case.qualification, Some(Value::Null));
    assert!(case.issues.iter().any(|x| x.contains("contradictory")));
    assert_eq!(
        c.documents[case.primary_document].json.as_ref().unwrap()["new_receiver_extension"],
        v["new_receiver_extension"]
    );
}
#[test]
fn receiver_tampering_keeps_complete_snapshot_but_marks_hash_mismatch() {
    let f = Fixture::copy();
    let p = f.phone.join("tasks/cross_cli/a1.report.json");
    let mut v = read(&p);
    v["future_field"] = json!({"negative":true,"restoration":"UNKNOWN"});
    write(&p, &v);
    let c = f.read();
    let case = find(&c, "judge.json");
    let a = case
        .associations
        .iter()
        .find(|x| x.role == "receiver a1")
        .unwrap();
    assert_eq!(a.integrity, "sha_mismatch");
    assert_eq!(
        c.documents[a.document].json.as_ref().unwrap()["future_field"],
        v["future_field"]
    );
    assert!(case.issues.iter().any(|s| s.contains("hash mismatch")));
}
#[test]
fn samples_hash_mismatch_does_not_follow_unbound_receiver_paths() {
    let f = Fixture::copy();
    let p = f.phone.join("tasks/cross_cli/samples.json");
    let mut v = read(&p);
    v["pairs"][0]["a_report"] = json!("../../outside.json");
    write(&p, &v);
    let c = f.read();
    let case = find(&c, "judge.json");
    assert!(!case
        .associations
        .iter()
        .any(|a| a.role.starts_with("receiver ")));
    assert!(case.issues.iter().any(|s| s.contains("hash mismatch")));
}
#[test]
fn model_recovery_unknown_negative_and_missing_values_remain_distinct() {
    let f = Fixture::copy();
    let p = f.gol.join("tasks/model_demo/pipeline.json");
    let mut v = read(&p);
    v["entries"][0]["state"] = json!("unknown");
    v["entries"][0]["error"] = json!("workers killed; external effects unknown");
    v["entries"][1]["state"] = json!("failed");
    v["entries"][2]["state"] = json!("deferred");
    v["entries"][3]["state"] = Value::Null;
    v["entries"][4]["evidence"]["reconciliation"] = json!({"reviewer":"engineer","reason":"manual review only","workers_stopped_attested":true});
    write(&p, &v);
    let c = f.read();
    let case = find(&c, "pipeline.json");
    assert_eq!(case.stages[0].state.as_deref(), Some("unknown"));
    assert_eq!(case.stages[1].state.as_deref(), Some("failed"));
    assert_eq!(case.stages[2].state.as_deref(), Some("deferred"));
    assert_eq!(case.stages[3].state, None);
    assert_eq!(
        case.stages[4].reconciliation,
        v["entries"][4]["evidence"].get("reconciliation").cloned()
    );
    assert_eq!(
        case.declared_status.as_deref(),
        Some("saved_stage_states_only")
    );
    assert!(!case.view_allows_promotion);
}
#[test]
fn duplicate_keys_and_truncated_primary_json_are_not_silently_normalized() {
    let f = Fixture::new();
    let dir = f.gol.join("tasks/bad");
    fs::create_dir_all(&dir).unwrap();
    let raw = r#"{"schema":"game_opt_loop/model_pipeline_journal@1","entries":[],"entries":[1]}"#;
    fs::write(dir.join("pipeline.json"), raw).unwrap();
    let c = f.read();
    assert_eq!(c.items.len(), 1);
    let d = &c.documents[c.items[0].primary_document];
    assert_eq!(d.raw_text.as_deref(), Some(raw));
    assert!(d.json.is_none());
    assert!(d.problem.as_deref().unwrap().contains("duplicate JSON key"));
    assert!(c.items[0].declared_status.is_none());
    fs::write(dir.join("pipeline.json"), "{\"schema\":").unwrap();
    let c = f.read();
    assert!(c.documents[0].json.is_none());
    assert!(!c.items[0].view_allows_promotion);
}
#[test]
fn absolute_inside_root_reference_is_relative_fd_read_but_escape_and_symlink_are_refused() {
    let f = Fixture::copy();
    let p = f.gol.join("tasks/model_demo/pipeline.json");
    let mut v = read(&p);
    let output = f.gol.join("tasks/model_demo/evaluation.json");
    let raw = b"{\"qualification\":null,\"restore\":\"UNKNOWN\",\"synthetic\":true}";
    fs::write(&output, raw).unwrap();
    v["entries"][4]["evidence"]["outputs"][0]["path"] = json!(output);
    v["entries"][4]["evidence"]["outputs"][0]["sha256"] =
        json!(format!("{:x}", Sha256::digest(raw)));
    write(&p, &v);
    let c = f.read();
    let case = find(&c, "pipeline.json");
    let a = case
        .associations
        .iter()
        .find(|a| a.role == "model output")
        .unwrap();
    assert_eq!(a.integrity, "sha_match_declaration_only");
    assert_eq!(
        c.documents[a.document]
            .raw_text
            .as_ref()
            .unwrap()
            .as_bytes(),
        raw
    );
    fs::remove_file(&output).unwrap();
    std::os::unix::fs::symlink(&p, &output).unwrap();
    let c = f.read();
    let case = find(&c, "pipeline.json");
    assert!(!case
        .associations
        .iter()
        .any(|a| a.role == "model output" && a.integrity == "sha_match_declaration_only"));
    v["entries"][4]["evidence"]["outputs"][0]["path"] = json!("../outside.json");
    write(&p, &v);
    let c = f.read();
    assert!(find(&c, "pipeline.json")
        .issues
        .iter()
        .any(|s| s.contains("parent traversal")));
    let external = f.dir.join("outside");
    fs::create_dir(&external).unwrap();
    write(&external.join("pipeline.json"), &v);
    std::os::unix::fs::symlink(external, f.gol.join("tasks/linked")).unwrap();
    assert_eq!(f.read().items.len(), 3);
}
#[test]
fn fifo_does_not_block_bounded_reader() {
    let f = Fixture::new();
    let dir = f.gol.join("tasks/fifo");
    fs::create_dir_all(&dir).unwrap();
    let p = dir.join("pipeline.json");
    let name = std::ffi::CString::new(p.to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    let start = Instant::now();
    let c = f.read();
    assert!(start.elapsed() < Duration::from_secs(2));
    assert!(c.items.is_empty());
    assert!(c.problems.iter().any(|s| s.contains("regular file")));
}
#[test]
fn file_and_case_limits_are_visible_and_never_imply_complete_inventory() {
    let f = Fixture::new();
    let dir = f.gol.join("tasks/limits");
    fs::create_dir_all(&dir).unwrap();
    fs::File::create(dir.join("huge.json"))
        .unwrap()
        .set_len(2 * 1024 * 1024 + 1)
        .unwrap();
    let c = f.read();
    assert!(c.partial);
    assert!(c.problems.iter().any(|s| s.contains("byte limit")));
    fs::remove_file(dir.join("huge.json")).unwrap();
    for i in 0..65 {
        write(
            &dir.join(format!("report-{i:02}.json")),
            &json!({"schema":"game_opt_loop/confirmation_report@1","verdict":"BLOCKED","issues":["synthetic"],"promotion_allowed":false,"real_game_optimization_proven":false}),
        );
    }
    let c = f.read();
    assert_eq!(c.items.len(), 64);
    assert!(c.partial);
    assert!(c.problems.iter().any(|s| s.contains("partial")));
    assert!(c
        .items
        .iter()
        .any(|x| x.issues.iter().any(|s| s.contains("duplicate primary"))));
}
#[test]
fn total_byte_and_depth_limits_are_visible() {
    let f = Fixture::new();
    let dir = f.gol.join("tasks/bytes");
    fs::create_dir_all(&dir).unwrap();
    let v = json!({"not_a_case":"x".repeat(2*1024*1024-1024)});
    for i in 0..10 {
        write(&dir.join(format!("data-{i}.json")), &v);
    }
    let c = f.read();
    assert!(c.partial);
    assert!(c.snapshot_bytes <= 16 * 1024 * 1024);
    fs::remove_dir_all(&dir).unwrap();
    let deep = f.gol.join("tasks/a/b/c/d/e/f/g");
    fs::create_dir_all(&deep).unwrap();
    write(
        &deep.join("pipeline.json"),
        &json!({"schema":"game_opt_loop/model_pipeline_journal@1"}),
    );
    let c = f.read();
    assert!(c.partial);
    assert!(c.items.is_empty());
    assert!(c.problems.iter().any(|s| s.contains("depth limit")));
}
#[test]
fn unavailable_directory_and_ancestor_symlink_are_explicit_partial_inventory() {
    let f = Fixture::copy();
    fs::remove_dir_all(f.phone.join("runs")).unwrap();
    let c = f.read();
    assert!(c.partial);
    assert!(c
        .problems
        .iter()
        .any(|s| s.contains("runs: directory unavailable")));
    let alias = f.dir.join("alias");
    std::os::unix::fs::symlink(&f.dir, &alias).unwrap();
    let c = collect(&alias.join("gol"), &f.phone);
    assert!(c.partial);
    assert!(c.problems.iter().any(|s| s.contains("root unavailable")));
    assert!(!c.items.iter().any(|s| s.kind == "model_pipeline"));
}
#[test]
fn actual_cli_http_no_fleet_exposes_snapshots_and_has_no_write_or_driver_route() {
    let f = Fixture::copy();
    let fifo = f.gol.join("tasks/fifo/pipeline.json");
    fs::create_dir_all(fifo.parent().unwrap()).unwrap();
    let fifo_name = std::ffi::CString::new(fifo.to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo_name.as_ptr(), 0o600) }, 0);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let child = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["serve", "--no-fleet", "--port", &port.to_string(), "--root"])
        .arg(&f.gol)
        .arg("--phonefarm-root")
        .arg(&f.phone)
        .arg("--lock-root")
        .arg(f.dir.join("locks"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    struct Server(Child);
    impl Drop for Server {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let _server = Server(child);
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(3))
        .build()
        .unwrap();
    let base = format!("http://127.0.0.1:{port}");
    let start = Instant::now();
    loop {
        if client.get(format!("{base}/healthz")).send().is_ok() {
            break;
        }
        assert!(start.elapsed() < Duration::from_secs(5));
        std::thread::sleep(Duration::from_millis(20));
    }
    let d: Value = client
        .get(format!("{base}/api/dashboard"))
        .send()
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(d["cases"]["items"].as_array().unwrap().len(), 3);
    assert_eq!(d["cases"]["view_allows_promotion"], false);
    assert!(d.get("fleet").is_none());
    assert!(d["cases"]["problems"]
        .as_array()
        .unwrap()
        .iter()
        .any(|p| p.as_str().unwrap().contains("regular file")));
    assert!(d["cases"]["documents"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v["raw_text"]
            .as_str()
            .is_some_and(|s| s.contains("VALID_OBSERVATION"))));
    assert_eq!(
        client
            .post(format!("{base}/api/dashboard"))
            .send()
            .unwrap()
            .status(),
        reqwest::StatusCode::METHOD_NOT_ALLOWED
    );
    assert_eq!(
        client
            .get(format!("{base}/model-pipeline/resume"))
            .send()
            .unwrap()
            .status(),
        reqwest::StatusCode::NOT_FOUND
    );
    let script = client
        .get(format!("{base}/app.js"))
        .send()
        .unwrap()
        .text()
        .unwrap();
    assert!(script.contains("view_grants_game_proof=false"));
    assert!(script.contains("原始 UTF-8 文本"));
}
