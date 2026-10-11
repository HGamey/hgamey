//! Real CLI + real external Python driver. No models, tokens or devices.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    dir: PathBuf,
    out: PathBuf,
    plan: PathBuf,
    driver: PathBuf,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}
impl Fixture {
    fn new(mode: &str, behavior: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "gol-model-pipeline-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&dir).unwrap();
        let driver = dir.join("driver.py");
        std::fs::copy(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/model_pipeline/fake_driver.py"),
            &driver,
        )
        .unwrap();
        let data = dir.join("data.json");
        std::fs::write(&data, b"synthetic source, not real game frames").unwrap();
        let python = Command::new("python3")
            .args([
                "-c",
                "import pathlib,sys;print(pathlib.Path(sys.executable).resolve())",
            ])
            .output()
            .expect("python3 needed for real external fixture");
        assert!(python.status.success());
        let python = String::from_utf8(python.stdout).unwrap().trim().to_string();
        let plan = dir.join("plan.json");
        let roles = [
            "architecture",
            "weights",
            "quantized_model",
            "deployment_receipt",
            "evaluation_report",
        ];
        let stages: Vec<Value> = ["design", "train", "quantize", "deploy", "evaluate"].into_iter().enumerate().map(|(i, stage)| json!({
            "stage": stage, "argv": [python, driver.to_str().unwrap(), if stage == "design" {behavior} else {"ok"}, "$(must_remain_literal)"],
            "cwd": dir, "driver_artifacts": [driver],
            "outputs": [{"name": format!("{stage}.json"), "role": roles[i], "format": "synthetic/json", "source": "synthetic"}],
            "budget": {"wall_ms": 5000, "output_bytes": 1048576, "steps": 1, "tokens": 0, "device_ms": 0}
        })).collect();
        write(
            &plan,
            &json!({"schema": "game_opt_loop/model_pipeline_plan@1", "pipeline_id": "fixture",
            "mode": mode, "candidate_description": "Synthetic protocol test, no training",
            "model": {"framework": "synthetic", "runtime": "synthetic_no_device", "abi": "synthetic/json",
                "quantization": {"method": "synthetic_no_conversion", "precision": "synthetic", "calibration_data_sha256": null}},
            "data": [{
                "name": "data", "role": "data", "path": data, "sha256": format!("{:x}", Sha256::digest(std::fs::read(&data).unwrap())),
                "format": "synthetic/text", "source": "synthetic"}], "stages": stages}),
        );
        Self {
            out: dir.join("run"),
            dir,
            plan,
            driver,
        }
    }
    fn cli(&self, action: &str, extra: &[&str]) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"));
        command
            .args(["model-pipeline", action, "--out"])
            .arg(&self.out);
        if action == "start" {
            command.arg("--plan").arg(&self.plan);
        }
        command.args(extra).output().unwrap()
    }
    fn journal(&self) -> Value {
        read(&self.out.join("pipeline.json"))
    }
    fn calls(&self) -> usize {
        std::fs::read_to_string(self.out.join("fake_calls.jsonl"))
            .unwrap_or_default()
            .lines()
            .count()
    }
    fn modify_plan(&self, f: impl FnOnce(&mut Value)) {
        let mut value = read(&self.plan);
        f(&mut value);
        write(&self.plan, &value);
    }
    fn decide(&self, stage: &str, action: &str) -> Output {
        self.cli(
            "decide",
            &[
                "--stage",
                stage,
                "--action",
                action,
                "--reviewer",
                "engineer",
                "--reason",
                "synthetic protocol review",
            ],
        )
    }
}
fn read(path: &Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}
fn write(path: &Path, v: &Value) {
    std::fs::write(path, serde_json::to_vec_pretty(v).unwrap()).unwrap();
}
fn success(output: Output) {
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
fn wait_file(path: &Path) {
    let timer = Instant::now();
    while !path.exists() {
        assert!(
            timer.elapsed() < Duration::from_secs(5),
            "waiting for {}",
            path.display()
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn actual_five_stage_driver_and_resume_never_promote_synthetic() {
    let f = Fixture::new("autonomous", "ok");
    let output = f.cli("start", &[]);
    let stdout: Value = serde_json::from_slice(&output.stdout)
        .expect("CLI emits machine-readable JSON without banner");
    assert_eq!(
        stdout["completion_scope"],
        "driver_protocol_and_artifact_integrity_only"
    );
    success(output);
    assert_eq!(f.calls(), 5);
    let j = f.journal();
    assert_eq!(j["promotion_allowed"], false);
    assert_eq!(j["real_game_optimization_proven"], false);
    assert_eq!(j["model_semantics_verified"], false);
    assert_eq!(j["usage_verification"], "driver_declared_unverified");
    for (i, entry) in j["entries"].as_array().unwrap().iter().enumerate() {
        assert_eq!(entry["state"], "completed");
        assert_eq!(entry["evidence"]["outputs"][0]["source"], "synthetic");
        assert_eq!(entry["evidence"]["outputs"][0]["format"], "synthetic/json");
        assert_eq!(entry["request"]["inputs"].as_array().unwrap().len(), i + 1);
        if i > 0 {
            assert_eq!(
                entry["request"]["inputs"][i]["sha256"],
                j["entries"][i - 1]["evidence"]["outputs"][0]["sha256"]
            );
        }
    }
    assert!(std::fs::read_to_string(f.out.join("fake_calls.jsonl"))
        .unwrap()
        .contains("$(must_remain_literal)"));
    success(f.cli("resume", &[]));
    assert_eq!(f.calls(), 5);
}

#[test]
fn expert_every_stage_waits_and_acceptance_binds_actual_inputs() {
    let f = Fixture::new("expert", "ok");
    success(f.cli("start", &[]));
    assert_eq!(f.calls(), 0);
    assert!(!f.decide("train", "accept").status.success());
    success(f.decide("design", "defer"));
    success(f.cli("resume", &[]));
    assert_eq!(f.calls(), 0);
    success(f.decide("design", "accept"));
    success(f.cli("resume", &[]));
    assert_eq!(f.calls(), 1);
    assert_eq!(f.journal()["entries"][1]["state"], "pending");
    for stage in ["train", "quantize", "deploy", "evaluate"] {
        success(f.decide(stage, "accept"));
        success(f.cli("resume", &[]));
    }
    assert_eq!(f.calls(), 5);
    success(f.cli("resume", &[]));
    assert_eq!(f.calls(), 5);
    assert_eq!(
        f.journal()["entries"][0]["decisions"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn reject_is_persistent_and_never_invokes_driver() {
    let f = Fixture::new("expert", "ok");
    success(f.cli("start", &[]));
    assert!(!f.decide("design", "reject").status.success());
    assert!(!f.cli("resume", &[]).status.success());
    assert_eq!(f.calls(), 0);
    assert_eq!(f.journal()["entries"][0]["state"], "rejected");
}

#[test]
fn negative_and_protocol_failures_persist_without_retry() {
    for behavior in [
        "negative",
        "nonzero",
        "missing",
        "wrong_binding",
        "wrong_hash",
        "usage",
    ] {
        let f = Fixture::new("autonomous", behavior);
        assert!(!f.cli("start", &[]).status.success(), "{behavior}");
        assert_eq!(f.journal()["entries"][0]["state"], "failed", "{behavior}");
        assert!(!f.cli("resume", &[]).status.success());
        assert_eq!(f.calls(), 1, "{behavior}");
        assert!(f.journal()["entries"][0]["error"].is_string());
        if behavior == "negative" {
            assert_eq!(
                f.journal()["entries"][0]["evidence"]["receipt"]["status"],
                "failed"
            );
        }
    }
}

#[test]
fn byte_input_output_driver_receipt_tamper_blocks_before_any_new_side_effect() {
    for target in ["data", "driver", "output", "receipt", "request", "plan"] {
        let f = Fixture::new("autonomous", "ok");
        success(f.cli("start", &[]));
        match target {
            "data" => std::fs::write(f.dir.join("data.json"), "changed").unwrap(),
            "driver" => std::fs::write(&f.driver, "changed").unwrap(),
            "output" => std::fs::write(f.out.join("0_design/design.json"), "changed").unwrap(),
            "receipt" => std::fs::write(f.out.join("0_design/receipt.json"), "changed").unwrap(),
            "request" => std::fs::write(f.out.join("0_design/request.json"), "changed").unwrap(),
            "plan" => {
                let mut j = f.journal();
                j["frozen"]["plan"]["mode"] = json!("expert");
                write(&f.out.join("pipeline.json"), &j);
            }
            _ => unreachable!(),
        }
        assert!(!f.cli("resume", &[]).status.success(), "{target}");
        assert_eq!(f.calls(), 5, "{target}");
    }
}

#[test]
fn approved_input_changes_do_not_reapprove_or_run() {
    let f = Fixture::new("expert", "ok");
    success(f.cli("start", &[]));
    success(f.decide("design", "accept"));
    std::fs::write(f.dir.join("data.json"), "changed").unwrap();
    assert!(!f.cli("resume", &[]).status.success());
    assert_eq!(f.calls(), 0);
    assert_eq!(
        f.journal()["entries"][0]["decisions"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn missing_driver_and_data_persist_preflight_negative() {
    let f = Fixture::new("autonomous", "ok");
    std::fs::remove_file(&f.driver).unwrap();
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.journal()["entries"][0]["state"], "failed");
    assert_eq!(f.calls(), 0);
    let f = Fixture::new("autonomous", "ok");
    std::fs::remove_file(f.dir.join("data.json")).unwrap();
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.journal()["entries"][0]["state"], "failed");
    assert_eq!(f.calls(), 0);
}

#[test]
fn timeout_and_byte_caps_preserve_uncertainty_and_no_retry() {
    for behavior in ["sleep", "oversize"] {
        let f = Fixture::new("autonomous", behavior);
        f.modify_plan(|p| {
            p["stages"][0]["budget"]["wall_ms"] = json!(150);
            if behavior == "oversize" {
                p["stages"][0]["budget"]["output_bytes"] = json!(10000);
            }
        });
        assert!(!f.cli("start", &[]).status.success());
        assert_eq!(f.journal()["entries"][0]["state"], "unknown");
        assert!(!f.cli("resume", &[]).status.success());
        assert_eq!(f.calls(), 1);
    }
}

#[test]
fn synthetic_inputs_cannot_be_relabelled_real_and_default_source_is_unknown() {
    let f = Fixture::new("autonomous", "ok");
    f.modify_plan(|p| {
        p["stages"][0]["outputs"][0]["source"] = json!("real_data_declared");
    });
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.calls(), 0);
    assert_eq!(f.journal()["entries"][0]["state"], "failed");
    let f = Fixture::new("expert", "ok");
    f.modify_plan(|p| {
        p["data"][0].as_object_mut().unwrap().remove("source");
    });
    success(f.cli("start", &[]));
    assert_eq!(
        f.journal()["entries"][0]["request"]["inputs"][0]["source"],
        "unknown"
    );
    assert_eq!(f.journal()["real_game_optimization_proven"], false);
}

#[test]
fn lock_blocks_parallel_writer_and_killed_host_requires_explicit_reconcile() {
    let f = Fixture::new("autonomous", "receipt_then_wait");
    let mut child = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["model-pipeline", "start", "--plan"])
        .arg(&f.plan)
        .arg("--out")
        .arg(&f.out)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let stage = f.out.join("0_design");
    wait_file(&stage.join("ready.flag"));
    assert!(!f.cli("resume", &[]).status.success());
    child.kill().unwrap();
    child.wait().unwrap();
    let receipt = stage.join("receipt.json");
    // A receipt can exist while its driver continues working. Never run next stage.
    assert!(!f
        .cli(
            "reconcile",
            &[
                "--stage",
                "design",
                "--receipt",
                receipt.to_str().unwrap(),
                "--reviewer",
                "engineer",
                "--reason",
                "driver still alive",
                "--workers-stopped"
            ]
        )
        .status
        .success());
    wait_file(&stage.join("driver_finished.flag"));
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(f.journal()["entries"][0]["state"], "in_flight");
    assert!(!f.cli("resume", &[]).status.success());
    assert_eq!(f.calls(), 1);
    let mut bad = read(&receipt);
    bad["request_sha256"] = json!("0".repeat(64));
    let bad_path = f.dir.join("bad.json");
    write(&bad_path, &bad);
    assert!(!f
        .cli(
            "reconcile",
            &[
                "--stage",
                "design",
                "--receipt",
                bad_path.to_str().unwrap(),
                "--reviewer",
                "engineer",
                "--reason",
                "synthetic test",
                "--workers-stopped"
            ]
        )
        .status
        .success());
    success(f.cli(
        "reconcile",
        &[
            "--stage",
            "design",
            "--receipt",
            receipt.to_str().unwrap(),
            "--reviewer",
            "engineer",
            "--reason",
            "confirmed fake driver stopped; no external effects",
            "--workers-stopped",
        ],
    ));
    assert_eq!(
        f.journal()["entries"][0]["evidence"]["reconciliation"]["reviewer"],
        "engineer"
    );
    success(f.cli("resume", &[]));
    assert_eq!(f.calls(), 5);
}

#[test]
fn launcher_leaving_live_workers_is_unknown_and_does_not_advance() {
    let f = Fixture::new("autonomous", "background");
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.journal()["entries"][0]["state"], "unknown");
    assert_eq!(f.calls(), 1);
    assert!(!f.cli("resume", &[]).status.success());
    assert_eq!(f.calls(), 1);
}

#[test]
fn valid_receipt_does_not_override_observed_timeout() {
    let f = Fixture::new("autonomous", "receipt_then_wait");
    f.modify_plan(|p| {
        p["stages"][0]["budget"]["wall_ms"] = json!(150);
    });
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.journal()["entries"][0]["state"], "unknown");
    let receipt = f.out.join("0_design/receipt.json");
    assert!(receipt.exists());
    assert!(!f
        .cli(
            "reconcile",
            &[
                "--stage",
                "design",
                "--receipt",
                receipt.to_str().unwrap(),
                "--reviewer",
                "engineer",
                "--reason",
                "stopped after timeout",
                "--workers-stopped"
            ]
        )
        .status
        .success());
    assert_eq!(f.journal()["entries"][0]["state"], "unknown");
    assert_eq!(f.calls(), 1);
}

#[test]
fn later_stage_cannot_swap_approved_upstream_artifact() {
    let f = Fixture::new("expert", "ok");
    success(f.cli("start", &[]));
    success(f.decide("design", "accept"));
    success(f.cli("resume", &[]));
    success(f.decide("train", "accept"));
    std::fs::write(
        f.out.join("0_design/design.json"),
        "changed upstream artifact",
    )
    .unwrap();
    assert!(!f.cli("resume", &[]).status.success());
    assert_eq!(f.calls(), 1);
    assert_eq!(
        f.journal()["entries"][1]["decisions"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn driver_cannot_complete_using_mutated_inputs_and_role_is_frozen() {
    let f = Fixture::new("autonomous", "mutate_input");
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.journal()["entries"][0]["state"], "failed");
    assert_eq!(f.calls(), 1);
    let f = Fixture::new("autonomous", "ok");
    f.modify_plan(|p| {
        p["stages"][1]["outputs"][0]["role"] = json!("architecture");
    });
    assert!(!f.cli("start", &[]).status.success());
    assert_eq!(f.calls(), 0);
}

#[test]
fn corrupted_future_ready_without_request_cannot_bypass_expert_review() {
    for state in ["ready", "in_flight", "unknown", "completed"] {
        let f = Fixture::new("expert", "ok");
        success(f.cli("start", &[]));
        success(f.decide("design", "accept"));
        let mut journal = f.journal();
        journal["entries"][1]["state"] = json!(state);
        assert!(journal["entries"][1]["request"].is_null());
        assert!(journal["entries"][1]["decisions"]
            .as_array()
            .unwrap()
            .is_empty());
        write(&f.out.join("pipeline.json"), &journal);
        assert!(!f.cli("resume", &[]).status.success(), "{state}");
        assert_eq!(f.calls(), 0, "{state}");
    }
}
