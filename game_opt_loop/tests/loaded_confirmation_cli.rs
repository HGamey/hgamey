//! Actual CLI consumer checks with synthetic, manually declared receiver fixtures.
//! No raw PF reconstruction, acquisition, device, model or physical qualification is implied.
use chrono::{DateTime, Duration};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT: AtomicU64 = AtomicU64::new(0);
fn sha(b: &[u8]) -> String {
    format!("{:x}", Sha256::digest(b))
}
fn contract() -> Value {
    json!({"profile_id":"loaded_steady_v1","receiver_policy_version":2,
        "profile_sha256":"1".repeat(64),"reference_sha256":"2".repeat(64),
        "thermal_contract_sha256":"3".repeat(64)})
}
struct F {
    root: PathBuf,
    frozen: Value,
    samples: Value,
}
impl F {
    fn command(root: &Path, args: &[&str]) -> Output {
        let output = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
            .current_dir(root)
            .args(args)
            .output()
            .unwrap();
        if std::env::var_os("LOADED_CONFIRMATION_TEST_ARTIFACT_DIR").is_some() {
            let id = NEXT.fetch_add(1, Ordering::Relaxed);
            std::fs::write(root.join(format!("cli-{id}.stdout")), &output.stdout).unwrap();
            std::fs::write(root.join(format!("cli-{id}.stderr")), &output.stderr).unwrap();
            Self::json(
                root,
                &format!("cli-{id}.receipt.json"),
                &json!({
                    "argv":args,"exit":output.status.code(),
                    "stdout_sha256":sha(&output.stdout),"stderr_sha256":sha(&output.stderr),
                    "scope":"synthetic_declared_receiver_mechanism_only","device_calls":0,"model_api_calls":0
                }),
            );
        }
        output
    }
    fn json(root: &Path, name: &str, value: &Value) {
        std::fs::write(root.join(name), serde_json::to_vec(value).unwrap()).unwrap();
    }
    fn new(loaded: bool) -> Self {
        let base = std::env::var_os("LOADED_CONFIRMATION_TEST_ARTIFACT_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir);
        std::fs::create_dir_all(&base).unwrap();
        let root = base.join(format!(
            "gol-loaded-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let mut p: Value =
            serde_json::from_str(include_str!("../examples/confirmation_plan.json")).unwrap();
        if loaded {
            p["temperature_contract"] = contract();
        }
        Self::json(&root, "plan.json", &p);
        let r = Self::command(
            &root,
            &[
                "confirm",
                "freeze",
                "--plan",
                "plan.json",
                "--out",
                "frozen.json",
            ],
        );
        assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
        let f: Value = serde_json::from_slice(&r.stdout).unwrap();
        let registered =
            DateTime::parse_from_rfc3339(f["registered_at"].as_str().unwrap()).unwrap();
        let mut pairs = vec![];
        for (i, pair) in p["schedule"].as_array().unwrap().iter().enumerate() {
            let mut reports = vec![];
            for (field, role) in [("a_run_id", "baseline"), ("b_run_id", "candidate")] {
                let id = pair[field].as_str().unwrap();
                let first = (role == "baseline") == pair["baseline_first"].as_bool().unwrap();
                let sec = 1 + i * 4 + usize::from(!first) * 2;
                let mut v = json!({"schema":"phonefarm/confirmation_observation@1","source":"phonefarm","status":"VALID_OBSERVATION",
                    "qualification":{"stage":"experiment","eligible":false,"evidence_origin":"recorded_device"},
                    "sample_qualification":{"schema":"phonefarm/confirmation_sample_qualification@1","valid_observation":true,
                        "baseline_admission_sha256":p["baseline_admission_sha256"],"gates":{"root_verified":true,"control_verified":true,
                        "cold_verified":true,"trace_window_verified":true,"workload_succeeded":true,"restore_verified":true,"baseline_admission_verified":true}},
                    "measurement_window":{"schema":"phonefarm/measurement_window@1","device_serial":"synthetic-contract-device","boot_id":"synthetic-boot",
                        "capture_start_uptime_s":1000.1+sec as f64,"capture_end_uptime_s":1000.8+sec as f64,
                        "env_start_uptime_s":1000.0+sec as f64,"env_end_uptime_s":1000.9+sec as f64,
                        "env_duration_s":1.0,"env_interval_s":0.25,"clock_scope":"capture_metadata_and_env_proc_uptime"},
                    "metric_provenance":{"power_watt":{"source":"battery_sysfs","rail":"battery","window_verified":true,"discharging":true}},
                    "metrics":{"power_watt":if role=="baseline" {100.0} else {91.0+i as f64}},
                    "confirmation_binding":{"pair_id":pair["pair_id"],"run_id":id,"sample_role":role,"aggregation_level":"single_run",
                        "artifact_sha256":if role=="baseline" {&p["baseline_artifact_sha256"]} else {&p["candidate_artifact_sha256"]},
                        "conditions_sha256":p["conditions_sha256"],"frozen_plan_sha256":f["sha256"],
                        "started_at":(registered+Duration::seconds(sec as i64)).to_rfc3339(),
                        "finished_at":(registered+Duration::seconds(sec as i64+1)).to_rfc3339(),
                        "raw_artifacts":{"trace_sha256":sha(format!("{id}:trace").as_bytes()),"env_sha256":sha(format!("{id}:env").as_bytes())}}});
                if loaded {
                    v["schema"] = json!("phonefarm/confirmation_observation@2");
                    v["sample_qualification"]["schema"] =
                        json!("phonefarm/confirmation_sample_qualification@2");
                    v["sample_qualification"]["gates"]
                        .as_object_mut()
                        .unwrap()
                        .remove("cold_verified");
                    v["sample_qualification"]["gates"]["thermal_profile_verified"] = json!(true);
                    v["sample_qualification"]["gates"]["thermal_window_verified"] = json!(true);
                    v["qualification"]["version"] = json!(2);
                    v["qualification"]["policy"] = json!({"version":2,"goal":"render_efficiency","required_metrics":["frame_p95","fps_mean","power_w_mean"],
                        "required_pairs":5,"require_quality":false,"temp_cap_c":null,"cool_c":null,"baseline_min_runs":3,
                        "dispersion_max_pct":5.0,"monotonic_max_ratio":0.9,"minimum_pairs_for_conclusion":5,"alpha":0.05,
                        "temperature_profile_id":"loaded_steady_v1","temperature_profile_sha256":"1".repeat(64),"thermal_reference_sha256":"2".repeat(64)});
                    v["confirmation_binding"]["thermal_contract"] = contract();
                    v["confirmation_binding"]["raw_artifacts"]["thermal_admission_sha256"] =
                        json!(sha(format!("{id}:thermal-pre-arm").as_bytes()));
                    v["confirmation_binding"]["raw_artifacts"]["thermal_window_sha256"] =
                        json!(sha(format!("{id}:thermal-window").as_bytes()));
                    v["confirmation_binding"]["raw_artifacts"]["thermal_sampler_exit_sha256"] =
                        json!(sha(b"0\n"));
                }
                let n = format!("{id}.json");
                Self::json(&root, &n, &v);
                reports.push((
                    n,
                    sha(&std::fs::read(root.join(format!("{id}.json"))).unwrap()),
                ));
            }
            pairs.push(json!({"pair_id":pair["pair_id"],"a_run_id":pair["a_run_id"],"b_run_id":pair["b_run_id"],
                "a_report":reports[0].0,"b_report":reports[1].0,"a_report_sha256":reports[0].1,"b_report_sha256":reports[1].1}));
        }
        let samples = json!({"frozen_plan_sha256":f["sha256"],"candidate_artifact_sha256":p["candidate_artifact_sha256"],
            "conditions_sha256":p["conditions_sha256"],"source_kind":"recorded_receiver_reports","pairs":pairs});
        Self {
            root,
            frozen: f,
            samples,
        }
    }
    fn edit(&mut self, change: impl FnOnce(&mut Value)) {
        let p = self
            .root
            .join(self.samples["pairs"][0]["a_report"].as_str().unwrap());
        let mut v: Value = serde_json::from_slice(&std::fs::read(&p).unwrap()).unwrap();
        change(&mut v);
        let b = serde_json::to_vec(&v).unwrap();
        std::fs::write(p, &b).unwrap();
        self.samples["pairs"][0]["a_report_sha256"] = json!(sha(&b));
    }
    fn judge(&self) -> Value {
        Self::json(&self.root, "samples.json", &self.samples);
        let r = Self::command(
            &self.root,
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
        );
        let v: Value =
            serde_json::from_slice(&std::fs::read(self.root.join("report.json")).unwrap()).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&r.stdout).unwrap(), v);
        assert_eq!(
            r.status.code(),
            Some(if v["verdict"] == "PASS" { 0 } else { 1 })
        );
        assert_eq!(v["promotion_allowed"], false);
        assert_eq!(v["real_game_optimization_proven"], false);
        v
    }
}
impl Drop for F {
    fn drop(&mut self) {
        if std::env::var_os("LOADED_CONFIRMATION_TEST_ARTIFACT_DIR").is_none() {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }
}

#[test]
fn loaded_cli_checks_frozen_identities_and_keeps_repeated_exit_status_legal() {
    let f = F::new(true);
    assert_eq!(f.frozen["plan"]["temperature_contract"], contract());
    assert_eq!(f.judge()["verdict"], "PASS");
    let f = F::new(false);
    assert!(f.frozen["plan"].get("temperature_contract").is_none());
    assert_eq!(f.judge()["verdict"], "PASS");
}

#[test]
fn changed_or_incomplete_loaded_reports_block_even_after_outer_rehash() {
    for i in 0..17 {
        let mut f = F::new(true);
        f.edit(|v| match i {
            0 => {
                v["confirmation_binding"]["thermal_contract"]["profile_sha256"] =
                    json!("f".repeat(64))
            }
            1 => {
                v["confirmation_binding"]["thermal_contract"]["reference_sha256"] =
                    json!("f".repeat(64))
            }
            2 => {
                v["confirmation_binding"]["thermal_contract"]["thermal_contract_sha256"] =
                    json!("F".repeat(64))
            }
            3 => v["confirmation_binding"]["thermal_contract"]["caller_cap"] = json!(95),
            4 => v["sample_qualification"]["gates"]["thermal_window_verified"] = json!("true"),
            5 => {
                v["sample_qualification"]["gates"]
                    .as_object_mut()
                    .unwrap()
                    .remove("thermal_profile_verified");
                v["sample_qualification"]["gates"]["cold_verified"] = json!(true);
            }
            6 => v["schema"] = json!("phonefarm/confirmation_observation@1"),
            7 => {
                v["sample_qualification"]["schema"] =
                    json!("phonefarm/confirmation_sample_qualification@1")
            }
            8 => v["qualification"]["version"] = json!(1),
            9 => v["qualification"]["policy"]["version"] = json!(1),
            10 => {
                v["qualification"]["policy"]["temperature_profile_sha256"] = json!("f".repeat(64))
            }
            11 => v["qualification"]["policy"]["cool_c"] = json!(40),
            12 => v["qualification"]["evidence_origin"] = json!("synthetic_fixture"),
            13 => v["status"] = json!("CONTROL_RESTORED"),
            14 => v["qualification"]["eligible"] = json!(true),
            15 => v["qualification_only"] = json!(true),
            _ => {
                v["confirmation_binding"]["raw_artifacts"]
                    .as_object_mut()
                    .unwrap()
                    .remove("thermal_sampler_exit_sha256");
            }
        });
        assert_eq!(f.judge()["verdict"], "BLOCKED", "case {i}");
    }
    let mut f = F::new(false);
    f.edit(|v| v["confirmation_binding"]["thermal_contract"] = contract());
    assert_eq!(f.judge()["verdict"], "BLOCKED");
}

#[test]
fn thermal_windows_are_not_independent_when_reused_or_missing() {
    for i in 0..4 {
        let mut f = F::new(true);
        let b: Value = serde_json::from_slice(
            &std::fs::read(
                f.root
                    .join(f.samples["pairs"][0]["b_report"].as_str().unwrap()),
            )
            .unwrap(),
        )
        .unwrap();
        f.edit(|v| match i {
            0 => {
                v["confirmation_binding"]["raw_artifacts"]["thermal_window_sha256"] =
                    b["confirmation_binding"]["raw_artifacts"]["thermal_window_sha256"].clone()
            }
            1 => {
                v["confirmation_binding"]["raw_artifacts"]["thermal_admission_sha256"] =
                    json!("a".repeat(64))
            }
            2 => {
                v["confirmation_binding"]["raw_artifacts"]["thermal_window_sha256"] =
                    v["confirmation_binding"]["raw_artifacts"]["env_sha256"].clone()
            }
            _ => {
                v["confirmation_binding"]["raw_artifacts"]["thermal_admission_sha256"] = Value::Null
            }
        });
        assert_eq!(f.judge()["verdict"], "BLOCKED", "case {i}");
    }
}

#[test]
fn bad_plan_versions_unknown_fields_and_caller_caps_refuse_before_freeze() {
    for i in 0..5 {
        let f = F::new(false);
        let mut p: Value =
            serde_json::from_str(include_str!("../examples/confirmation_plan.json")).unwrap();
        p["temperature_contract"] = contract();
        match i {
            0 => p["temperature_contract"]["profile_id"] = json!("legacy_cold_v1"),
            1 => p["temperature_contract"]["receiver_policy_version"] = json!(1),
            2 => p["temperature_contract"]["reference_sha256"] = json!("bad"),
            3 => p["temperature_contract"]["operating_cap_c"] = json!(95),
            _ => p["temperature_contract"]["receiver_policy_version"] = json!("2"),
        }
        F::json(&f.root, "bad-plan.json", &p);
        let r = F::command(
            &f.root,
            &[
                "confirm",
                "freeze",
                "--plan",
                "bad-plan.json",
                "--out",
                "bad-frozen.json",
            ],
        );
        assert!(!r.status.success(), "case {i}");
        assert!(!f.root.join("bad-frozen.json").exists());
    }
}

fn legacy_policy() -> Value {
    json!({"version":1,"goal":"render_efficiency","required_metrics":["frame_p95","fps_mean","power_w_mean"],
        "required_pairs":5,"require_quality":false,"temp_cap_c":70.0,"cool_c":40.0,
        "baseline_min_runs":3,"dispersion_max_pct":5.0,"monotonic_max_ratio":0.9,
        "minimum_pairs_for_conclusion":5,"alpha":0.05})
}

#[test]
fn legacy_omitted_and_explicit_v1_contracts_remain_compatible() {
    let f = F::new(false);
    assert_eq!(f.judge()["verdict"], "PASS");
    let mut f = F::new(false);
    f.edit(|v| {
        v["qualification"]["version"] = json!(1);
        v["qualification"]["policy"] = legacy_policy();
        // Original admission reports have their own v1 policy; free-text/check
        // diagnostics must not be interpreted as a thermal contract field.
        v["baseline_admission_qualification"] = json!({"version":1,"policy":legacy_policy(),
            "checks":[{"id":"thermal_caps","state":"pass"}],
            "claim_scope":"historical cold policy thermal diagnostics only"});
    });
    assert_eq!(f.judge()["verdict"], "PASS");
}

#[test]
fn legacy_mixed_purpose_versions_and_thermal_fields_block_after_rehash() {
    let mutations = [
        ("/qualification/version", json!(2)),
        ("/qualification/version", json!("1")),
        ("/qualification/version", json!(null)),
        ("/qualification/version", json!(1.0)),
        ("/qualification/policy", json!(null)),
        ("/qualification/policy/version", json!(2)),
        ("/qualification/policy/version", json!("1")),
        ("/qualification/policy/version", json!(null)),
        (
            "/qualification/policy/temperature_profile_id",
            json!("capability_absolute_v1"),
        ),
        (
            "/qualification/policy/temperature_profile_id",
            json!("loaded_steady_v1"),
        ),
        ("/qualification/policy/temperature_profile_id", json!(null)),
        (
            "/qualification/policy/temperature_profile_sha256",
            json!("1".repeat(64)),
        ),
        (
            "/qualification/policy/thermal_reference_sha256",
            json!("2".repeat(64)),
        ),
        ("/qualification/purpose", json!("capability_absolute_v1")),
        ("/purpose", json!("configuration_readback_only")),
        (
            "/temperature_reference_purpose",
            json!("diagnostic_only_no_comparison"),
        ),
        ("/temperature_profile_id", json!("capability_absolute_v1")),
        ("/temperature", json!({"stable_reference_required":false})),
        ("/sample_qualification/purpose", json!("control_only")),
        (
            "/sample_qualification/gates/thermal_profile_verified",
            json!(true),
        ),
        (
            "/sample_qualification/gates/thermal_window_verified",
            json!(false),
        ),
        ("/confirmation_binding/thermal_contract", json!(null)),
        ("/confirmation_binding/temperature_contract", contract()),
        (
            "/confirmation_binding/profile_id",
            json!("capability_absolute_v1"),
        ),
        (
            "/confirmation_binding/raw_artifacts/thermal_admission_sha256",
            json!("1".repeat(64)),
        ),
        (
            "/confirmation_binding/raw_artifacts/thermal_window_sha256",
            json!("2".repeat(64)),
        ),
        (
            "/confirmation_binding/raw_artifacts/thermal_sampler_exit_sha256",
            json!(null),
        ),
        ("/baseline_admission_qualification/version", json!(2)),
        ("/baseline_admission_qualification/policy/version", json!(2)),
        (
            "/baseline_admission_qualification/policy/temperature_profile_id",
            json!("capability_absolute_v1"),
        ),
        (
            "/baseline_admission_qualification/policy/thermal_reference_sha256",
            json!(null),
        ),
    ];
    for (path, replacement) in mutations {
        let mut f = F::new(false);
        F::json(
            &f.root,
            "countercase.json",
            &json!({"path":path,"value":replacement,"outer_report_rehashed":true}),
        );
        f.edit(|v| {
            v["qualification"]["version"] = json!(1);
            v["qualification"]["policy"] = legacy_policy();
            v["baseline_admission_qualification"] = json!({"version":1,"policy":legacy_policy()});
            let mut keys = path.trim_start_matches('/').split('/').peekable();
            let mut cursor = v;
            while let Some(key) = keys.next() {
                if keys.peek().is_none() {
                    cursor[key] = replacement;
                    break;
                }
                cursor = &mut cursor[key];
            }
        });
        assert_eq!(f.judge()["verdict"], "BLOCKED", "mixed legacy field {path}");
    }
}
