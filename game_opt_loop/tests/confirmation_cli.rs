use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
static ID: AtomicU64 = AtomicU64::new(0);
fn dir() -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "gol_confirmation_cli_{}_{}",
        std::process::id(),
        ID.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}
fn run(args: &[&str], root: &std::path::Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .current_dir(root)
        .args(args)
        .output()
        .unwrap()
}
#[test]
fn real_cli_freezes_then_refuses_overwrite_and_records_blocked_synthetic_replay() {
    let root = dir();
    std::fs::write(
        root.join("plan.json"),
        include_str!("../examples/confirmation_plan.json"),
    )
    .unwrap();
    let r = run(
        &[
            "confirm",
            "freeze",
            "--plan",
            "plan.json",
            "--out",
            "frozen.json",
        ],
        &root,
    );
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    let original = std::fs::read(root.join("frozen.json")).unwrap();
    assert!(!run(
        &[
            "confirm",
            "freeze",
            "--plan",
            "plan.json",
            "--out",
            "frozen.json"
        ],
        &root
    )
    .status
    .success());
    assert_eq!(std::fs::read(root.join("frozen.json")).unwrap(), original);
    let frozen: Value = serde_json::from_slice(&original).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&r.stdout).unwrap(), frozen);
    let samples = json!({"frozen_plan_sha256":frozen["sha256"],"candidate_artifact_sha256":frozen["plan"]["candidate_artifact_sha256"],"conditions_sha256":frozen["plan"]["conditions_sha256"],"source_kind":"synthetic_fixture","pairs":[]});
    std::fs::write(
        root.join("samples.json"),
        serde_json::to_vec(&samples).unwrap(),
    )
    .unwrap();
    let judged = run(
        &[
            "confirm",
            "judge",
            "--frozen",
            "frozen.json",
            "--samples",
            "samples.json",
            "--out",
            "report.json",
        ],
        &root,
    );
    assert!(!judged.status.success());
    let report: Value =
        serde_json::from_slice(&std::fs::read(root.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["verdict"], "BLOCKED");
    assert_eq!(report["promotion_allowed"], false);
    assert_eq!(report["real_game_optimization_proven"], false);
    assert!(report["issues"].as_array().unwrap().len() >= 2);
    assert_eq!(
        serde_json::from_slice::<Value>(&judged.stdout).unwrap(),
        report
    );
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn real_cli_rejects_posthoc_pairing_and_unsafe_alpha_before_registration() {
    for mutate in [0, 1, 2] {
        let root = dir();
        let mut plan: Value =
            serde_json::from_str(include_str!("../examples/confirmation_plan.json")).unwrap();
        match mutate {
            0 => plan["family_alpha"] = json!(1.0),
            1 => plan["schedule"][1]["a_run_id"] = plan["schedule"][0]["a_run_id"].clone(),
            _ => plan["paired_t_assumptions_acknowledged"] = json!(false),
        }
        std::fs::write(root.join("plan.json"), serde_json::to_vec(&plan).unwrap()).unwrap();
        assert!(!run(
            &[
                "confirm",
                "freeze",
                "--plan",
                "plan.json",
                "--out",
                "frozen.json"
            ],
            &root
        )
        .status
        .success());
        assert!(!root.join("frozen.json").exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}
