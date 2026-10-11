use game_opt_loop::engine::{Candidate, CandidateOrigin, EvalReport, ParetoArchive};
use game_opt_loop::evidence::{qualification_state, QualificationState};
use game_opt_loop::optimize::{eval, report};
use serde_json::{json, Value};

fn reply() -> Value {
    json!({
        "candidate_id":"c1", "status":"PASS", "source":"phonefarm",
        "metrics":{"operator_latency_ms":0.4,"fps_p95_ms":16.7,"power_watt":null,"psnr_db":40.0},
        "verdict":{"is_pareto_improvement":true,"p_value":0.001},
        "qualification":{"stage":"conclusion","eligible":true,"status":"ready", "checks":[{"id":"restore","state":"pass"}]},
        "artifacts":{"quality":{"hud_ghost_db":22.0,"frames":["f1","f2"]}},
        "metric_provenance":{"fps_p95_ms":{"source":"gpu_submit_proxy","display_verified":false}},
        "sequence_quality":{"metrics_verdict":"PASS","real_game_qualification":false},
        "future_receiver_evidence":{"nested":[null, false, {"file_sha256":"abc"}]}
    })
}

#[test]
fn process_reply_and_operator_archive_preserve_receiver_evidence() {
    let raw = reply();
    let r = game_opt_loop::harness::parse_eval_report(&format!("progress\n{}", raw)).unwrap();
    let c = Candidate::new(
        "c1",
        1,
        "sr",
        None,
        1.5,
        "void main() {}".into(),
        CandidateOrigin::LocalMutation,
        None,
    );
    let mut archive = ParetoArchive::new("sr", 1.5, 38.0);
    assert!(archive.insert(&c, r, "2026-10-11T00:00:00Z".into()));
    let path = std::env::temp_dir().join(format!("gol_evidence_{}.json", std::process::id()));
    archive.save(&path).unwrap();
    let saved = ParetoArchive::load(&path).unwrap();
    std::fs::remove_file(path).unwrap();
    let entry = &saved.entries[0];
    assert_eq!(entry.qualification_state, QualificationState::Qualified);
    let stored = serde_json::to_value(&entry.report).unwrap();
    for name in [
        "qualification",
        "artifacts",
        "metric_provenance",
        "sequence_quality",
        "future_receiver_evidence",
    ] {
        assert_eq!(stored[name], raw[name], "lost {name}");
    }
}

#[test]
fn optimize_entry_keeps_evidence_and_rejects_unqualified_fake_pass() {
    let mut raw = reply();
    raw["version"] = json!(2);
    raw["kind"] = json!("gray");
    raw["qualification"]["eligible"] = json!(false);
    let r: eval::Report = serde_json::from_value(raw.clone()).unwrap();
    let entry = report::Entry::from_report(
        "c1",
        eval::Kind::Gray,
        1,
        "experiment",
        report::Change::Gray {
            rewrites: vec!["loadop_dont_care".into()],
        },
        &r,
    );
    assert!(!entry.kept);
    assert!(!entry.measurement_valid);
    assert_eq!(entry.qualification_state, QualificationState::Ineligible);
    let saved = serde_json::to_value(entry).unwrap();
    assert_eq!(saved["measurement_evidence"]["artifacts"], raw["artifacts"]);
    assert_eq!(
        saved["measurement_evidence"]["qualification"],
        raw["qualification"]
    );
}

#[test]
fn malformed_or_preflight_qualification_cannot_promote_in_either_entry() {
    for qualification in [
        Value::Null,
        json!({}),
        json!({"stage":"conclusion","eligible":"true"}),
        json!({"stage":"probe","eligible":true}),
        json!({"stage":"experiment","eligible":true}),
        json!({"stage":"conclusion","eligible":false}),
    ] {
        let mut raw = reply();
        raw["qualification"] = qualification;
        let operator: EvalReport = serde_json::from_value(raw.clone()).unwrap();
        assert!(!operator.is_admissible(1.5, 38.0));
        let optimize: eval::Report = serde_json::from_value(raw).unwrap();
        assert!(!optimize.kept());
    }
}

#[test]
fn offline_qualification_replays_cannot_promote_even_with_a_fake_pass() {
    for marker in [json!(true), Value::Null, json!("true"), json!({})] {
        let mut raw = reply();
        raw["qualification_only"] = marker;
        let operator: EvalReport = serde_json::from_value(raw.clone()).unwrap();
        assert_eq!(
            qualification_state(&operator.extensions),
            QualificationState::Ineligible
        );
        assert!(!operator.is_admissible(1.5, 38.0));
        let optimize: eval::Report = serde_json::from_value(raw).unwrap();
        assert!(!optimize.kept());
    }
    let mut measured = reply();
    measured["qualification_only"] = json!(false);
    let operator: EvalReport = serde_json::from_value(measured).unwrap();
    assert!(operator.is_admissible(1.5, 38.0));
}

#[test]
fn qualification_does_not_bypass_original_budget_quality_or_statistics() {
    let mut raw = reply();
    raw["metrics"]["operator_latency_ms"] = json!(5.0);
    let r: EvalReport = serde_json::from_value(raw.clone()).unwrap();
    assert!(!r.is_admissible(1.5, 38.0));
    raw["metrics"]["operator_latency_ms"] = json!(0.4);
    raw["metrics"]["psnr_db"] = json!(20.0);
    let r: EvalReport = serde_json::from_value(raw.clone()).unwrap();
    assert!(!r.is_admissible(1.5, 38.0));
    raw["metrics"]["psnr_db"] = json!(40.0);
    raw["verdict"]["p_value"] = json!(0.7);
    let r: EvalReport = serde_json::from_value(raw).unwrap();
    assert!(!r.is_admissible(1.5, 38.0));
}

#[test]
fn old_reports_remain_readable_but_are_explicitly_unqualified() {
    let mut raw = reply();
    raw.as_object_mut().unwrap().remove("qualification");
    let r: EvalReport = serde_json::from_value(raw.clone()).unwrap();
    assert_eq!(
        qualification_state(&r.extensions),
        QualificationState::LegacyUnqualified
    );
    assert!(r.is_admissible(1.5, 38.0));
    let opt: eval::Report = serde_json::from_value(raw).unwrap();
    let entry = report::Entry::from_report(
        "c1",
        eval::Kind::Shader,
        1,
        "legacy",
        report::Change::Shader {
            track: "sr".into(),
            shader_path: "c.comp".into(),
        },
        &opt,
    );
    assert_eq!(
        entry.qualification_state,
        QualificationState::LegacyUnqualified
    );
}

#[test]
fn abort_reply_is_valid_history_and_never_a_front_candidate() {
    let mut raw = reply();
    raw["status"] = json!("ABORT");
    let r = game_opt_loop::harness::parse_eval_report(&raw.to_string()).unwrap();
    assert!(!r.is_admissible(1.5, 38.0));
}
