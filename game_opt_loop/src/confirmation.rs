//! Independent confirmation of frozen, paired run summaries, never individual frames.
use anyhow::{bail, ensure, Context, Result};
use clap::Subcommand;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use statrs::distribution::{ContinuousCDF, StudentsT};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Subcommand, Debug)]
pub enum Action {
    /// Register a new immutable plan before collecting confirmation runs.
    Freeze {
        #[arg(long)]
        plan: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    /// Read exactly the frozen number of heldout pairs; never changes a Pareto archive.
    Judge {
        #[arg(long)]
        frozen: PathBuf,
        #[arg(long)]
        samples: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub baseline_artifact_sha256: String,
    pub candidate_artifact_sha256: String,
    pub conditions_sha256: String,
    pub baseline_admission_sha256: String,
    pub exploration_run_ids: Vec<String>,
    pub exploration_artifact_sha256: Vec<String>,
    pub pairs: usize,
    pub schedule: Vec<PlannedPair>,
    pub family_alpha: f64,
    /// A design assumption, not a normality test: independent run pairs and approximately
    /// normal paired differences. Frame samples within one run cannot be used as pairs.
    pub paired_t_assumptions_acknowledged: bool,
    pub metrics: Vec<MetricRule>,
    /// Receiver-frozen loaded thermal identities, never caller temperature limits.
    /// Omit None to preserve historical v1 plan fingerprints byte for byte.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub temperature_contract: Option<TemperatureContract>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TemperatureContract {
    pub profile_id: String,
    pub receiver_policy_version: u32,
    pub profile_sha256: String,
    pub reference_sha256: String,
    pub thermal_contract_sha256: String,
}
impl TemperatureContract {
    fn valid(&self) -> bool {
        self.profile_id == "loaded_steady_v1"
            && self.receiver_policy_version == 2
            && digest_ok(&self.profile_sha256)
            && digest_ok(&self.reference_sha256)
            && digest_ok(&self.thermal_contract_sha256)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlannedPair {
    pub pair_id: String,
    pub a_run_id: String,
    pub b_run_id: String,
    pub baseline_first: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MetricRule {
    pub name: String,
    pub direction: Direction,
    pub scale: Scale,
    /// At least one metric must have a strictly positive improvement target.
    pub minimum_improvement: Option<f64>,
    /// Maximum adverse difference in percent or the metric's native unit.
    pub maximum_regression: f64,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Lower,
    Higher,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Scale {
    Percent,
    Absolute,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Frozen {
    pub schema: String,
    pub registered_at: String,
    pub plan: Plan,
    pub sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Samples {
    pub frozen_plan_sha256: String,
    pub candidate_artifact_sha256: String,
    pub conditions_sha256: String,
    /// recorded_receiver_reports or synthetic_fixture. Never interpreted as capture attestation.
    pub source_kind: String,
    pub pairs: Vec<Pair>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pair {
    pub pair_id: String,
    pub a_run_id: String,
    pub b_run_id: String,
    pub a_report: PathBuf,
    pub b_report: PathBuf,
    /// Bind the exact bytes read, not only their filenames.
    pub a_report_sha256: String,
    pub b_report_sha256: String,
}

#[derive(Debug, Serialize)]
pub struct MetricResult {
    pub name: String,
    pub scale: Scale,
    pub pairs: usize,
    pub mean_adverse_delta: f64,
    pub standard_error: f64,
    pub upper_adverse_bound: f64,
    pub alpha_per_metric: f64,
    pub noninferiority_limit: f64,
    pub noninferiority_supported: bool,
    pub improvement_limit: Option<f64>,
    pub improvement_supported: Option<bool>,
}
#[derive(Debug, Serialize)]
pub struct Report {
    pub schema: &'static str,
    pub frozen_plan_sha256: String,
    pub samples_sha256: String,
    pub verdict: &'static str,
    pub issues: Vec<String>,
    pub metrics: Vec<MetricResult>,
    pub report_hashes: BTreeMap<String, String>,
    pub assumption: &'static str,
    pub source_attestation: &'static str,
    pub real_game_optimization_proven: bool,
    pub promotion_allowed: bool,
    pub claim_scope: &'static str,
}

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn digest_ok(v: &str) -> bool {
    v.len() == 64
        && v.bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn fingerprint(f: &Frozen) -> Result<String> {
    Ok(hash(&serde_json::to_vec(&(
        &f.schema,
        &f.registered_at,
        &f.plan,
    ))?))
}
fn validate(plan: &Plan) -> Result<()> {
    ensure!(
        plan.temperature_contract
            .as_ref()
            .is_none_or(TemperatureContract::valid),
        "unsupported loaded temperature contract/version/hash"
    );
    ensure!(
        digest_ok(&plan.baseline_artifact_sha256)
            && digest_ok(&plan.candidate_artifact_sha256)
            && digest_ok(&plan.conditions_sha256)
            && digest_ok(&plan.baseline_admission_sha256),
        "artifact/conditions must be lowercase SHA-256 digests"
    );
    ensure!(
        (5..=10000).contains(&plan.pairs),
        "freeze 5..10000 independent run pairs"
    );
    ensure!(
        plan.family_alpha.is_finite() && plan.family_alpha > 0.0 && plan.family_alpha <= 0.05,
        "family alpha must be in (0, .05]"
    );
    ensure!(
        plan.paired_t_assumptions_acknowledged,
        "acknowledge paired-t assumptions before registration"
    );
    let exploration: BTreeSet<_> = plan.exploration_run_ids.iter().collect();
    ensure!(
        !exploration.is_empty()
            && exploration.len() == plan.exploration_run_ids.len()
            && exploration.iter().all(|s| !s.trim().is_empty()),
        "enumerate unique nonempty exploration run IDs"
    );
    ensure!(
        plan.schedule.len() == plan.pairs,
        "freeze every pair and run ID before sampling"
    );
    ensure!(
        !plan.exploration_artifact_sha256.is_empty()
            && plan
                .exploration_artifact_sha256
                .iter()
                .all(|s| digest_ok(s)),
        "register exploration raw artifact hashes"
    );
    let mut ids = BTreeSet::new();
    let mut pair_ids = BTreeSet::new();
    for pair in &plan.schedule {
        ensure!(
            !pair.pair_id.trim().is_empty() && pair_ids.insert(&pair.pair_id),
            "unique frozen pair IDs required"
        );
        for id in [&pair.a_run_id, &pair.b_run_id] {
            ensure!(
                !id.trim().is_empty() && !exploration.contains(id) && ids.insert(id),
                "frozen confirmation IDs must be unique and disjoint from exploration"
            );
        }
    }
    let baseline_first = plan.schedule.iter().filter(|p| p.baseline_first).count();
    ensure!(
        baseline_first.abs_diff(plan.pairs - baseline_first) <= 1,
        "freeze counterbalanced AB/BA order"
    );
    ensure!(
        !plan.metrics.is_empty() && plan.metrics.len() <= 32,
        "freeze 1..32 metrics"
    );
    let mut names = BTreeSet::new();
    let mut primary = false;
    for r in &plan.metrics {
        ensure!(
            matches!(
                (r.name.as_str(), r.direction),
                (
                    "power_watt" | "fps_p95_ms" | "frame_p95_ms",
                    Direction::Lower
                ) | ("fps_mean", Direction::Higher)
            ),
            "unsupported metric or direction: {}",
            r.name
        );
        ensure!(
            !r.name.trim().is_empty() && names.insert(&r.name),
            "metric names must be nonempty and unique"
        );
        ensure!(
            r.maximum_regression.is_finite() && r.maximum_regression >= 0.0,
            "regression margin must be finite and nonnegative"
        );
        if let Some(m) = r.minimum_improvement {
            ensure!(
                m.is_finite() && m > 0.0,
                "improvement target must be finite and positive"
            );
            primary = true;
        }
    }
    ensure!(
        primary,
        "freeze at least one improvement target; absence of regression alone is not optimization"
    );
    Ok(())
}

fn exclusive_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .with_context(|| format!("refuse replacing {}", path.display()))?;
    file.write_all(&bytes)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

fn load_report(path: &Path, expected: &str) -> Result<Value> {
    ensure!(digest_ok(expected), "invalid report SHA-256");
    let bytes = std::fs::read(path).with_context(|| format!("read {}", path.display()))?;
    ensure!(
        hash(&bytes) == expected,
        "report changed: {}",
        path.display()
    );
    Ok(serde_json::from_slice(&bytes)?)
}

fn receiver_qualified(v: &Value, thermal: Option<&TemperatureContract>) -> bool {
    let replay = v
        .get("qualification_only")
        .is_some_and(|x| x.as_bool() != Some(false));
    let common = !replay
        && v["qualification"]["stage"] == "experiment"
        && v["qualification"]["eligible"] == false
        && v["status"] == "VALID_OBSERVATION"
        && v["source"] == "phonefarm"
        && matches!(
            v["qualification"]["evidence_origin"].as_str(),
            Some("live_device" | "recorded_device")
        )
        && v["sample_qualification"]["valid_observation"] == true
        && [
            "root_verified",
            "control_verified",
            "trace_window_verified",
            "workload_succeeded",
            "restore_verified",
            "baseline_admission_verified",
        ]
        .iter()
        .all(|gate| v["sample_qualification"]["gates"][*gate] == true);
    if !common {
        return false;
    }
    match thermal {
        None => {
            v["schema"] == "phonefarm/confirmation_observation@1"
                && v["sample_qualification"]["schema"]
                    == "phonefarm/confirmation_sample_qualification@1"
                && v["sample_qualification"]["gates"]["cold_verified"] == true
                && v["confirmation_binding"].get("thermal_contract").is_none()
        }
        Some(expected) => {
            v["schema"] == "phonefarm/confirmation_observation@2"
                && v["sample_qualification"]["schema"]
                    == "phonefarm/confirmation_sample_qualification@2"
                && v["sample_qualification"]["gates"]["thermal_profile_verified"] == true
                && v["sample_qualification"]["gates"]["thermal_window_verified"] == true
                && v["qualification"]["version"] == 2
                && v["qualification"]["policy"]["version"] == 2
                && v["qualification"]["policy"]["temperature_profile_id"] == expected.profile_id
                && v["qualification"]["policy"]["temperature_profile_sha256"]
                    == expected.profile_sha256
                && v["qualification"]["policy"]["thermal_reference_sha256"]
                    == expected.reference_sha256
                && v["qualification"]["policy"].get("temp_cap_c") == Some(&Value::Null)
                && v["qualification"]["policy"].get("cool_c") == Some(&Value::Null)
                && expected.valid()
                && serde_json::from_value::<TemperatureContract>(
                    v["confirmation_binding"]["thermal_contract"].clone(),
                )
                .is_ok_and(|actual| actual == *expected)
        }
    }
}

fn metric_provenance_valid(v: &Value, metric: &str) -> bool {
    let source = &v["metric_provenance"][metric];
    match metric {
        "power_watt" => {
            source["source"] == "battery_sysfs"
                && source["rail"] == "battery"
                && source["window_verified"] == true
                && source["discharging"] == true
        }
        "fps_p95_ms" | "frame_p95_ms" | "fps_mean" => {
            source["source"] == "raw_ftrace_kgsl"
                && source["confidence"] == "inferred"
                && source["semantics"] == "gpu_submit_proxy"
                && source["display_verified"] == false
        }
        _ => false,
    }
}

fn binding_valid(
    v: &Value,
    pair_id: &str,
    id: &str,
    role: &str,
    frozen: &Frozen,
    registered: chrono::DateTime<chrono::FixedOffset>,
) -> bool {
    let b = &v["confirmation_binding"];
    let artifact = if role == "baseline" {
        &frozen.plan.baseline_artifact_sha256
    } else {
        &frozen.plan.candidate_artifact_sha256
    };
    b["pair_id"] == pair_id
        && v["sample_qualification"]["baseline_admission_sha256"]
            == frozen.plan.baseline_admission_sha256
        && b["run_id"] == id
        && b["sample_role"] == role
        && b["aggregation_level"] == "single_run"
        && b["artifact_sha256"].as_str() == Some(artifact.as_str())
        && b["conditions_sha256"] == frozen.plan.conditions_sha256
        && b["frozen_plan_sha256"] == frozen.sha256
        && run_window(v).is_some_and(|(start, end)| start > registered && end > start)
}

fn run_window(
    v: &Value,
) -> Option<(
    chrono::DateTime<chrono::FixedOffset>,
    chrono::DateTime<chrono::FixedOffset>,
)> {
    let b = &v["confirmation_binding"];
    Some((
        chrono::DateTime::parse_from_rfc3339(b["started_at"].as_str()?).ok()?,
        chrono::DateTime::parse_from_rfc3339(b["finished_at"].as_str()?).ok()?,
    ))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MeasurementWindow {
    schema: String,
    device_serial: String,
    boot_id: String,
    capture_start_uptime_s: f64,
    capture_end_uptime_s: f64,
    env_start_uptime_s: f64,
    env_end_uptime_s: f64,
    env_duration_s: f64,
    env_interval_s: f64,
    clock_scope: String,
}

fn measurement_window(v: &Value) -> Option<MeasurementWindow> {
    let w: MeasurementWindow = serde_json::from_value(v["measurement_window"].clone()).ok()?;
    let (start, end) = run_window(v)?;
    let host_seconds = (end - start).num_milliseconds() as f64 / 1000.0;
    let valid = w.schema == "phonefarm/measurement_window@1"
        && w.clock_scope == "capture_metadata_and_env_proc_uptime"
        && !w.device_serial.trim().is_empty()
        && !w.boot_id.trim().is_empty()
        && [
            w.capture_start_uptime_s,
            w.capture_end_uptime_s,
            w.env_start_uptime_s,
            w.env_end_uptime_s,
            w.env_duration_s,
            w.env_interval_s,
        ]
        .iter()
        .all(|n| n.is_finite() && *n >= 0.0)
        && w.capture_start_uptime_s < w.capture_end_uptime_s
        && w.env_start_uptime_s < w.env_end_uptime_s
        && w.env_start_uptime_s <= w.capture_start_uptime_s
        && w.env_end_uptime_s >= w.capture_end_uptime_s
        && w.env_interval_s > 0.0
        && w.env_interval_s <= w.env_duration_s
        && w.env_duration_s <= host_seconds
        && w.env_end_uptime_s - w.env_start_uptime_s <= host_seconds;
    valid.then_some(w)
}

/// Positive differences always mean worse. One-sided simultaneous upper bounds use
/// Bonferroni alpha/m, so a nonsignificant difference cannot establish noninferiority.
fn summarize(rule: &MetricRule, a: &[f64], b: &[f64], alpha: f64) -> Result<MetricResult> {
    ensure!(
        a.len() == b.len() && a.len() >= 5,
        "need independent paired summaries"
    );
    let mut differences = Vec::with_capacity(a.len());
    for (&av, &bv) in a.iter().zip(b) {
        ensure!(
            av.is_finite() && bv.is_finite() && av > 0.0 && bv > 0.0,
            "metric must be finite and positive"
        );
        let delta = match rule.scale {
            Scale::Absolute => bv - av,
            Scale::Percent => {
                ensure!(av > 0.0, "percent delta needs positive baseline");
                100.0 * (bv - av) / av
            }
        };
        differences.push(match rule.direction {
            Direction::Lower => delta,
            Direction::Higher => -delta,
        });
    }
    let n = differences.len() as f64;
    let mean = differences.iter().sum::<f64>() / n;
    let variance = differences.iter().map(|d| (d - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let scale = differences.iter().fold(1.0_f64, |m, d| m.max(d.abs()));
    let numerical_floor = (64.0 * f64::EPSILON * scale).powi(2);
    // Zero variability in repeated device runs requires investigation, not infinite precision.
    ensure!(
        mean.is_finite() && variance.is_finite() && variance > numerical_floor,
        "zero/numerically degenerate/nonfinite paired variance cannot support confirmation"
    );
    let se = (variance / n).sqrt();
    let critical = StudentsT::new(0.0, 1.0, n - 1.0)?.inverse_cdf(1.0 - alpha);
    let upper = mean + critical * se;
    ensure!(upper.is_finite(), "nonfinite confidence bound");
    let improvement_limit = rule.minimum_improvement.map(|m| -m);
    Ok(MetricResult {
        name: rule.name.clone(),
        scale: rule.scale,
        pairs: a.len(),
        mean_adverse_delta: mean,
        standard_error: se,
        upper_adverse_bound: upper,
        alpha_per_metric: alpha,
        noninferiority_limit: rule.maximum_regression,
        noninferiority_supported: upper < rule.maximum_regression,
        improvement_limit,
        improvement_supported: improvement_limit.map(|limit| upper < limit),
    })
}

pub fn judge(
    frozen: &Frozen,
    samples: &Samples,
    base: &Path,
    samples_hash: String,
) -> Result<Report> {
    validate(&frozen.plan)?;
    ensure!(
        frozen.schema == "game_opt_loop/confirmation_plan@1"
            && fingerprint(frozen)? == frozen.sha256,
        "frozen plan changed"
    );
    let p = &frozen.plan;
    let registered = chrono::DateTime::parse_from_rfc3339(&frozen.registered_at)?;
    let mut out = Report {
        schema: "game_opt_loop/confirmation_report@1", frozen_plan_sha256: frozen.sha256.clone(),
        samples_sha256: samples_hash, verdict: "BLOCKED", issues: vec![], metrics: vec![], report_hashes: BTreeMap::new(),
        assumption: "independent run pairs; approximately normal paired adverse differences; paired t upper bounds, Bonferroni family correction",
        source_attestation: "receiver declarations and byte hashes checked; acquisition and registration chronology not attested",
        real_game_optimization_proven: false, promotion_allowed: false,
        claim_scope: "none",
    };
    if samples.frozen_plan_sha256 != frozen.sha256
        || samples.candidate_artifact_sha256 != p.candidate_artifact_sha256
        || samples.conditions_sha256 != p.conditions_sha256
    {
        out.issues
            .push("plan/artifact/conditions fingerprint mismatch".into());
    }
    if samples.source_kind != "recorded_receiver_reports" {
        out.issues
            .push("synthetic/unknown source cannot confirm device metrics".into());
    }
    if samples.pairs.len() != p.pairs {
        out.issues
            .push("exact frozen pair count required; no optional stopping".into());
    }
    let excluded: BTreeSet<_> = p.exploration_run_ids.iter().map(String::as_str).collect();
    let mut run_ids = BTreeSet::new();
    let mut pair_ids = BTreeSet::new();
    let mut bytes_seen = BTreeSet::new();
    let mut raw_seen: BTreeSet<String> = p.exploration_artifact_sha256.iter().cloned().collect();
    let mut arms = vec![];
    let mut previous_end = registered;
    let mut previous_measurement: Option<MeasurementWindow> = None;
    for (index, pair) in samples.pairs.iter().enumerate() {
        if p.schedule.get(index).is_none_or(|planned| {
            planned.pair_id != pair.pair_id
                || planned.a_run_id != pair.a_run_id
                || planned.b_run_id != pair.b_run_id
        }) {
            out.issues
                .push("pair schedule changed after registration".into());
        }
        if pair.pair_id.trim().is_empty() || !pair_ids.insert(&pair.pair_id) {
            out.issues.push("duplicate/empty pair ID".into());
        }
        let mut values = vec![];
        for (id, path, expected, role) in [
            (
                &pair.a_run_id,
                &pair.a_report,
                &pair.a_report_sha256,
                "baseline",
            ),
            (
                &pair.b_run_id,
                &pair.b_report,
                &pair.b_report_sha256,
                "candidate",
            ),
        ] {
            if id.trim().is_empty() || excluded.contains(id.as_str()) || !run_ids.insert(id) {
                out.issues
                    .push(format!("reused/exploration/empty run ID: {id}"));
            }
            if !bytes_seen.insert(expected) {
                out.issues.push(format!("reused report bytes: {id}"));
            }
            let path = if path.is_absolute() {
                path.clone()
            } else {
                base.join(path)
            };
            match load_report(&path, expected) {
                Ok(v) => {
                    if !receiver_qualified(&v, p.temperature_contract.as_ref()) {
                        out.issues
                            .push(format!("receiver not a valid single-run observation: {id}"));
                    }
                    if !binding_valid(&v, &pair.pair_id, id, role, frozen, registered) {
                        out.issues.push(format!(
                            "missing/mismatched single-run confirmation binding: {id}"
                        ));
                    }
                    for kind in ["trace_sha256", "env_sha256"] {
                        let digest = v["confirmation_binding"]["raw_artifacts"][kind]
                            .as_str()
                            .unwrap_or("");
                        if !digest_ok(digest) || !raw_seen.insert(digest.to_string()) {
                            out.issues.push(format!(
                                "missing/reused/exploration raw artifact {kind}: {id}"
                            ));
                        }
                    }
                    if p.temperature_contract.is_some() {
                        for kind in ["thermal_admission_sha256", "thermal_window_sha256"] {
                            let digest = v["confirmation_binding"]["raw_artifacts"][kind]
                                .as_str()
                                .unwrap_or("");
                            if !digest_ok(digest) || !raw_seen.insert(digest.to_string()) {
                                out.issues.push(format!(
                                    "missing/reused/exploration raw thermal artifact {kind}: {id}"
                                ));
                            }
                        }
                        // A bare exit status is normally byte-identical across successful arms.
                        // Its hash is required; uniqueness is not observation independence.
                        if !v["confirmation_binding"]["raw_artifacts"]
                            ["thermal_sampler_exit_sha256"]
                            .as_str()
                            .is_some_and(digest_ok)
                        {
                            out.issues
                                .push(format!("missing/invalid thermal sampler exit hash: {id}"));
                        }
                    }
                    values.push(v);
                    out.report_hashes.insert(id.clone(), expected.clone());
                }
                Err(e) => out.issues.push(format!("{id}: {e:#}")),
            }
        }
        if values.len() == 2 {
            if let Some(planned) = p.schedule.get(index) {
                let ordered = if planned.baseline_first {
                    [&values[0], &values[1]]
                } else {
                    [&values[1], &values[0]]
                };
                for v in ordered {
                    if let Some(w) = measurement_window(v) {
                        if previous_measurement.as_ref().is_some_and(|prior| {
                            prior.device_serial != w.device_serial
                                || prior.boot_id != w.boot_id
                                || w.env_start_uptime_s <= prior.env_end_uptime_s
                                || w.capture_start_uptime_s <= prior.capture_end_uptime_s
                        }) {
                            out.issues.push("overlapping/reversed device measurement windows or changed device/boot identity".into());
                        }
                        previous_measurement = Some(w);
                    } else {
                        out.issues
                            .push("missing/invalid whole measurement window declaration".into());
                    }
                }
            }
            if let (Some((a_start, a_end)), Some((b_start, b_end)), Some(planned)) = (
                run_window(&values[0]),
                run_window(&values[1]),
                p.schedule.get(index),
            ) {
                let (first_start, first_end, second_start, second_end) = if planned.baseline_first {
                    (a_start, a_end, b_start, b_end)
                } else {
                    (b_start, b_end, a_start, a_end)
                };
                if first_start <= previous_end || second_start <= first_end {
                    out.issues
                        .push("overlapping runs or frozen AB/BA order changed".into());
                }
                previous_end = second_end;
            }
            arms.push((values.remove(0), values.remove(0)));
        }
    }
    if !out.issues.is_empty() {
        return Ok(out);
    }
    for rule in &p.metrics {
        let mut a = vec![];
        let mut b = vec![];
        for (av, bv) in &arms {
            for (v, dst) in [(av, &mut a), (bv, &mut b)] {
                match v["metrics"][&rule.name].as_f64().filter(|n| n.is_finite()) {
                    Some(n) => dst.push(n),
                    None => out
                        .issues
                        .push(format!("missing required metric {}", rule.name)),
                }
                // A source description must survive each receiver reply. It is a declaration,
                // not sufficient acquisition attestation for a real-game optimization claim.
                if !metric_provenance_valid(v, &rule.name) {
                    out.issues
                        .push(format!("missing/unknown metric provenance {}", rule.name));
                }
            }
        }
        if a.len() == p.pairs && b.len() == p.pairs {
            match summarize(rule, &a, &b, p.family_alpha / p.metrics.len() as f64) {
                Ok(r) => out.metrics.push(r),
                Err(e) => out.issues.push(format!("{}: {e}", rule.name)),
            }
        }
    }
    if !out.issues.is_empty() {
        return Ok(out);
    }
    let supported = out
        .metrics
        .iter()
        .all(|r| r.noninferiority_supported && r.improvement_supported != Some(false));
    out.verdict = if supported { "PASS" } else { "INCONCLUSIVE" };
    out.claim_scope = "frozen metrics of independently labelled recorded run summaries, conditional on stated assumptions; no real-game/present/quality proof";
    Ok(out)
}

pub fn run(action: Action) -> Result<()> {
    match action {
        Action::Freeze { plan, out } => {
            let plan: Plan = serde_json::from_slice(&std::fs::read(plan)?)?;
            validate(&plan)?;
            let mut frozen = Frozen {
                schema: "game_opt_loop/confirmation_plan@1".into(),
                registered_at: chrono::Utc::now().to_rfc3339(),
                plan,
                sha256: String::new(),
            };
            frozen.sha256 = fingerprint(&frozen)?;
            exclusive_json(&out, &frozen)?;
            println!("{}", serde_json::to_string(&frozen)?);
        }
        Action::Judge {
            frozen,
            samples,
            out,
        } => {
            let frozen: Frozen = serde_json::from_slice(&std::fs::read(frozen)?)?;
            let bytes = std::fs::read(&samples)?;
            let parsed: Samples = serde_json::from_slice(&bytes)?;
            let report = judge(
                &frozen,
                &parsed,
                samples.parent().unwrap_or(Path::new(".")),
                hash(&bytes),
            )?;
            exclusive_json(&out, &report)?;
            println!("{}", serde_json::to_string(&report)?);
            if report.verdict != "PASS" {
                bail!("confirmation {} (report saved)", report.verdict);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn rule() -> MetricRule {
        MetricRule {
            name: "power_watt".into(),
            direction: Direction::Lower,
            scale: Scale::Percent,
            minimum_improvement: Some(1.0),
            maximum_regression: 3.0,
        }
    }
    #[test]
    fn one_sided_bound_matches_student_t_reference() {
        let mut r = rule();
        r.scale = Scale::Absolute;
        let a = [10.0; 5];
        let b = [11.0, 12.0, 13.0, 14.0, 15.0];
        let result = summarize(&r, &a, &b, 0.05).unwrap();
        // df=4, upper one-sided 95% critical t=2.131846786.
        assert!((result.upper_adverse_bound - (3.0 + 2.131846786 / 2.0_f64.sqrt())).abs() < 1e-7);
        assert!(!result.noninferiority_supported);
    }
    #[test]
    fn absence_of_significant_difference_does_not_prove_noninferiority() {
        let a = [100.0; 5];
        let b = [80.0, 90.0, 100.0, 110.0, 120.0];
        let result = summarize(&rule(), &a, &b, 0.05).unwrap();
        assert_eq!(result.mean_adverse_delta, 0.0);
        assert!(!result.noninferiority_supported);
        assert_eq!(result.improvement_supported, Some(false));
    }
    #[test]
    fn useful_improvement_and_bonferroni_direction() {
        let a = [100.0; 5];
        let b = [91.0, 92.0, 93.0, 94.0, 95.0];
        let one = summarize(&rule(), &a, &b, 0.05).unwrap();
        let many = summarize(&rule(), &a, &b, 0.025).unwrap();
        assert_eq!(many.improvement_supported, Some(true));
        assert!(many.upper_adverse_bound > one.upper_adverse_bound);
        let mut higher = rule();
        higher.direction = Direction::Higher;
        assert_eq!(
            summarize(&higher, &a, &b, 0.05)
                .unwrap()
                .improvement_supported,
            Some(false)
        );
    }
    #[test]
    fn reject_constant_bad_and_missing_paired_samples() {
        assert!(summarize(&rule(), &[100.0; 5], &[99.0; 5], 0.05).is_err());
        assert!(summarize(
            &rule(),
            &[0.1, 0.2, 0.3, 0.4, 0.5],
            &[0.09, 0.18, 0.27, 0.36, 0.45],
            0.05
        )
        .is_err());
        assert!(summarize(&rule(), &[0.0; 5], &[1.0, 2.0, 3.0, 4.0, 5.0], 0.05).is_err());
        assert!(summarize(&rule(), &[1.0; 4], &[1.0; 4], 0.05).is_err());
        assert!(summarize(
            &rule(),
            &[100.0; 5],
            &[-10.0, -11.0, -12.0, -13.0, -14.0],
            0.05
        )
        .is_err());
        assert!(summarize(&rule(), &[1.0; 5], &[1.0, 2.0, f64::NAN, 4.0, 5.0], 0.05).is_err());
    }
    #[test]
    fn pre_extension_frozen_plan_keeps_exact_historical_fingerprint() {
        // Frozen by the actual pre-extension CLI from the synthetic example plan.
        let legacy: Frozen = serde_json::from_str(include_str!(
            "../tests/fixtures/legacy_confirmation_frozen.json"
        ))
        .unwrap();
        assert!(legacy.plan.temperature_contract.is_none());
        assert_eq!(fingerprint(&legacy).unwrap(), legacy.sha256);
        assert_eq!(
            legacy.sha256,
            "5c7b606a4804b7a4dff1ec2135af32a63e15b6963e313b9dd7dc197238b700b0"
        );
        assert!(serde_json::to_value(&legacy.plan)
            .unwrap()
            .get("temperature_contract")
            .is_none());
    }
    #[test]
    fn offline_or_malformed_qualifications_do_not_confirm() {
        let mut v = serde_json::json!({"schema":"phonefarm/confirmation_observation@1","source":"phonefarm","status":"VALID_OBSERVATION","qualification":{"stage":"experiment","eligible":false,"evidence_origin":"recorded_device"},
            "sample_qualification":{"schema":"phonefarm/confirmation_sample_qualification@1","valid_observation":true,"gates":{"root_verified":true,"control_verified":true,"cold_verified":true,"trace_window_verified":true,"workload_succeeded":true,"restore_verified":true,"baseline_admission_verified":true}}});
        assert!(receiver_qualified(&v, None));
        v["qualification_only"] = Value::Bool(true);
        assert!(!receiver_qualified(&v, None));
        v["qualification_only"] = Value::Bool(false);
        v["qualification"]["stage"] = Value::String("conclusion".into());
        assert!(!receiver_qualified(&v, None));
    }
}

#[cfg(test)]
mod contract_tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicU64, Ordering};
    static ID: AtomicU64 = AtomicU64::new(0);
    fn fixture() -> (PathBuf, Frozen, Samples) {
        let root = std::env::temp_dir().join(format!(
            "gol_confirm_{}_{}",
            std::process::id(),
            ID.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&root).unwrap();
        let schedule: Vec<_> = (0..5)
            .map(|i| PlannedPair {
                pair_id: format!("p{i}"),
                a_run_id: format!("a{i}"),
                b_run_id: format!("b{i}"),
                baseline_first: i % 2 == 0,
            })
            .collect();
        let plan = Plan {
            baseline_artifact_sha256: "b".repeat(64),
            candidate_artifact_sha256: "c".repeat(64),
            conditions_sha256: "d".repeat(64),
            baseline_admission_sha256: "e".repeat(64),
            exploration_run_ids: vec!["search1".into()],
            exploration_artifact_sha256: vec!["a".repeat(64)],
            pairs: 5,
            schedule,
            family_alpha: 0.05,
            paired_t_assumptions_acknowledged: true,
            metrics: vec![MetricRule {
                name: "power_watt".into(),
                direction: Direction::Lower,
                scale: Scale::Percent,
                minimum_improvement: Some(1.0),
                maximum_regression: 3.0,
            }],
            temperature_contract: None,
        };
        let mut frozen = Frozen {
            schema: "game_opt_loop/confirmation_plan@1".into(),
            registered_at: "2026-10-11T00:00:00Z".into(),
            plan,
            sha256: String::new(),
        };
        frozen.sha256 = fingerprint(&frozen).unwrap();
        let mut pairs = vec![];
        for (i, p) in frozen.plan.schedule.iter().enumerate() {
            let mut hashes = vec![];
            for (id, role) in [(&p.a_run_id, "baseline"), (&p.b_run_id, "candidate")] {
                let first = (role == "baseline") == p.baseline_first;
                let second = i * 4 + 1 + usize::from(!first) * 2;
                let report = json!({"schema":"phonefarm/confirmation_observation@1","source":"phonefarm","status":"VALID_OBSERVATION","qualification":{"stage":"experiment","eligible":false,"evidence_origin":"recorded_device"},
                    "measurement_window":{"schema":"phonefarm/measurement_window@1","device_serial":"fixture-device","boot_id":"fixture-boot",
                        "capture_start_uptime_s":1000.0+second as f64+0.1,"capture_end_uptime_s":1000.0+second as f64+0.8,
                        "env_start_uptime_s":1000.0+second as f64,"env_end_uptime_s":1000.0+second as f64+0.9,
                        "env_duration_s":1.0,"env_interval_s":0.25,"clock_scope":"capture_metadata_and_env_proc_uptime"},
                    "sample_qualification":{"schema":"phonefarm/confirmation_sample_qualification@1","valid_observation":true,"baseline_admission_sha256":frozen.plan.baseline_admission_sha256,"gates":{"root_verified":true,"control_verified":true,"cold_verified":true,"trace_window_verified":true,"workload_succeeded":true,"restore_verified":true,"baseline_admission_verified":true}},
                    "metrics":{"power_watt":if role=="baseline" {100.0} else {91.0+i as f64}},
                    "metric_provenance":{"power_watt":{"source":"battery_sysfs","rail":"battery","window_verified":true,"discharging":true}},
                    "confirmation_binding":{"raw_artifacts":{"trace_sha256":hash(format!("{id}:trace").as_bytes()),"env_sha256":hash(format!("{id}:env").as_bytes())},"pair_id":p.pair_id,"run_id":id,"sample_role":role,"aggregation_level":"single_run","artifact_sha256":if role=="baseline" {&frozen.plan.baseline_artifact_sha256} else {&frozen.plan.candidate_artifact_sha256},"conditions_sha256":frozen.plan.conditions_sha256,"frozen_plan_sha256":frozen.sha256,
                    "started_at":format!("2026-10-11T00:00:{second:02}Z"),"finished_at":format!("2026-10-11T00:00:{:02}Z",second+1)}});
                let bytes = serde_json::to_vec(&report).unwrap();
                std::fs::write(root.join(format!("{id}.json")), &bytes).unwrap();
                hashes.push(hash(&bytes));
            }
            pairs.push(Pair {
                pair_id: p.pair_id.clone(),
                a_run_id: p.a_run_id.clone(),
                b_run_id: p.b_run_id.clone(),
                a_report: format!("{}.json", p.a_run_id).into(),
                b_report: format!("{}.json", p.b_run_id).into(),
                a_report_sha256: hashes.remove(0),
                b_report_sha256: hashes.remove(0),
            });
        }
        let samples = Samples {
            frozen_plan_sha256: frozen.sha256.clone(),
            candidate_artifact_sha256: frozen.plan.candidate_artifact_sha256.clone(),
            conditions_sha256: frozen.plan.conditions_sha256.clone(),
            source_kind: "recorded_receiver_reports".into(),
            pairs,
        };
        (root, frozen, samples)
    }
    fn check(root: &Path, f: &Frozen, s: &Samples) -> Report {
        judge(f, s, root, "e".repeat(64)).unwrap()
    }
    fn modify_first(root: &Path, s: &mut Samples, edit: impl FnOnce(&mut Value)) {
        let path = root.join(&s.pairs[0].a_report);
        let mut v: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        edit(&mut v);
        let bytes = serde_json::to_vec(&v).unwrap();
        std::fs::write(path, &bytes).unwrap();
        s.pairs[0].a_report_sha256 = hash(&bytes);
    }
    #[test]
    fn valid_declared_heldout_metrics_never_become_real_game_or_promotion_proof() {
        let (root, f, s) = fixture();
        let r = check(&root, &f, &s);
        assert_eq!(r.verdict, "PASS");
        assert!(!r.real_game_optimization_proven);
        assert!(!r.promotion_allowed);
        assert_eq!(r.report_hashes.len(), 10);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn mutated_rules_reused_runs_optional_stopping_and_changed_conditions_block() {
        let (root, mut f, mut s) = fixture();
        f.plan.metrics[0].maximum_regression = 100.0;
        assert!(judge(&f, &s, &root, String::new()).is_err());
        let (root2, f2, _) = fixture();
        s.conditions_sha256 = "f".repeat(64);
        assert_eq!(check(&root2, &f2, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_dir_all(root2).unwrap();
        let (root, f, mut s) = fixture();
        s.pairs.swap(0, 1);
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        s.pairs.pop();
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn forged_pair_or_role_binding_and_aggregated_report_block() {
        for (name, value) in [
            ("pair_id", json!("p99")),
            ("sample_role", json!("candidate")),
            ("aggregation_level", json!("multiple_runs")),
            ("artifact_sha256", json!("a".repeat(64))),
            ("conditions_sha256", json!("a".repeat(64))),
            ("started_at", json!("2026-10-10T00:00:00Z")),
        ] {
            let (root, f, mut s) = fixture();
            modify_first(&root, &mut s, |v| v["confirmation_binding"][name] = value);
            assert_eq!(check(&root, &f, &s).verdict, "BLOCKED", "{name}");
            std::fs::remove_dir_all(root).unwrap();
        }
    }
    #[test]
    fn unknown_sources_invalid_replay_and_missing_metrics_block() {
        for replacement in [
            json!({}),
            json!(false),
            json!("unknown"),
            json!({"rail":"battery","window_verified":false}),
        ] {
            let (root, f, mut s) = fixture();
            modify_first(&root, &mut s, |v| {
                v["metric_provenance"]["power_watt"] = replacement
            });
            assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
            std::fs::remove_dir_all(root).unwrap();
        }
        for marker in [json!(true), Value::Null, json!("false")] {
            let (root, f, mut s) = fixture();
            modify_first(&root, &mut s, |v| v["qualification_only"] = marker);
            assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
            std::fs::remove_dir_all(root).unwrap();
        }
        let (root, f, mut s) = fixture();
        modify_first(&root, &mut s, |v| v["metrics"]["power_watt"] = Value::Null);
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn source_kind_report_byte_tampering_and_frozen_id_overlap_block() {
        let (root, mut f, mut s) = fixture();
        s.source_kind = "synthetic_fixture".into();
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        s.source_kind = "recorded_receiver_reports".into();
        std::fs::write(root.join(&s.pairs[0].a_report), b"{}").unwrap();
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        f.plan.exploration_run_ids.push("a0".into());
        assert!(validate(&f.plan).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn raw_artifacts_cannot_be_reused_under_new_report_ids() {
        let (root, f, mut s) = fixture();
        modify_first(&root, &mut s, |v| {
            v["confirmation_binding"]["raw_artifacts"]["trace_sha256"] =
                serde_json::json!("a".repeat(64))
        });
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
        let (root, f, mut s) = fixture();
        let reused = hash(format!("{}:env", s.pairs[1].a_run_id).as_bytes());
        modify_first(&root, &mut s, |v| {
            v["confirmation_binding"]["raw_artifacts"]["env_sha256"] = serde_json::json!(reused)
        });
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn distinct_report_hashes_do_not_make_shared_or_unknown_device_windows_independent() {
        for (field, invalid) in [
            ("schema", json!("unknown")),
            ("clock_scope", json!("ftrace_event_clock")),
            ("device_serial", json!("other-device")),
            ("boot_id", json!("other-boot")),
            ("capture_end_uptime_s", Value::Null),
            ("env_start_uptime_s", json!(-1.0)),
            ("env_end_uptime_s", json!(1001.5)),
            ("env_duration_s", json!(220.0)),
            ("env_interval_s", json!(0.0)),
        ] {
            let (root, f, mut s) = fixture();
            modify_first(&root, &mut s, |v| v["measurement_window"][field] = invalid);
            let r = check(&root, &f, &s);
            assert_eq!(r.verdict, "BLOCKED", "{field}");
            assert!(
                r.issues.iter().any(|i| i.contains("measurement")),
                "{field}: {:?}",
                r.issues
            );
            std::fs::remove_dir_all(root).unwrap();
        }
        let (root, f, mut s) = fixture();
        let second: Value =
            serde_json::from_slice(&std::fs::read(root.join(&s.pairs[0].b_report)).unwrap())
                .unwrap();
        modify_first(&root, &mut s, |v| {
            v["measurement_window"] = second["measurement_window"].clone()
        });
        assert_ne!(s.pairs[0].a_report_sha256, s.pairs[0].b_report_sha256);
        let r = check(&root, &f, &s);
        assert_eq!(r.verdict, "BLOCKED");
        assert!(r
            .issues
            .iter()
            .any(|i| i.contains("overlapping/reversed device measurement")));
        std::fs::remove_dir_all(root).unwrap();
        let (root, f, mut s) = fixture();
        modify_first(&root, &mut s, |v| {
            v.as_object_mut().unwrap().remove("measurement_window");
        });
        assert_eq!(check(&root, &f, &s).verdict, "BLOCKED");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn aggregated_conclusions_and_incomplete_single_run_gates_block() {
        for gate in [
            "root_verified",
            "control_verified",
            "cold_verified",
            "trace_window_verified",
            "workload_succeeded",
            "restore_verified",
            "baseline_admission_verified",
        ] {
            for invalid in [json!(false), Value::Null, json!("true")] {
                let (root, f, mut s) = fixture();
                modify_first(&root, &mut s, |v| {
                    v["sample_qualification"]["gates"][gate] = invalid
                });
                assert_eq!(check(&root, &f, &s).verdict, "BLOCKED", "{gate}");
                std::fs::remove_dir_all(root).unwrap();
            }
        }
        for variant in 0..6 {
            let (root, f, mut s) = fixture();
            modify_first(&root, &mut s, |v| match variant {
                0 => {
                    v["qualification"]["stage"] = json!("conclusion");
                    v["qualification"]["eligible"] = json!(true);
                }
                1 => v["schema"] = json!("phonefarm/eval_report@2"),
                2 => v["status"] = json!("PASS"),
                3 => v["sample_qualification"]["baseline_admission_sha256"] = json!("f".repeat(64)),
                4 => v["sample_qualification"]["valid_observation"] = json!(false),
                _ => v["qualification"]["evidence_origin"] = json!("synthetic_fixture"),
            });
            assert_eq!(check(&root, &f, &s).verdict, "BLOCKED", "variant {variant}");
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn output_and_registered_rules_are_never_overwritten() {
        let (root, f, _) = fixture();
        let path = root.join("frozen.json");
        exclusive_json(&path, &f).unwrap();
        assert!(exclusive_json(&path, &f).is_err());
        let actual: Frozen = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        assert_eq!(fingerprint(&actual).unwrap(), f.sha256);
        std::fs::remove_dir_all(root).unwrap();
    }
}
