//! Launch the actual CLI against a native fake phonefarm, not just a mocked
//! method. Logs count only real request files; capability probes are read-only.
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;

static SEQUENCE: AtomicU64 = AtomicU64::new(0);
fn root() -> PathBuf {
    let base = std::env::var_os("GOL_TEST_ARTIFACT_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let path = base.join(format!(
        "campaign_{}_{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&path).unwrap();
    path
}
fn fake_binary() -> &'static Path {
    static BIN: OnceLock<PathBuf> = OnceLock::new();
    BIN.get_or_init(|| {
        let path = root().join("native_fake_phonefarm");
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/support/fake_phonefarm.rs");
        let status = Command::new("rustc")
            .args(["--edition=2021"])
            .arg(source)
            .arg("-o")
            .arg(&path)
            .status()
            .unwrap();
        assert!(status.success());
        path
    })
}

struct Fixture {
    root: PathBuf,
    cwd: PathBuf,
    out: PathBuf,
    log: PathBuf,
    phonefarm: PathBuf,
    receiver_evidence: Option<&'static str>,
    force_pass: bool,
}
impl Fixture {
    fn new() -> Self {
        let root = root();
        let cwd = root.join("gol");
        let out = root.join("out");
        let pf = root.join("phonefarm");
        std::fs::create_dir_all(&cwd).unwrap();
        std::fs::create_dir_all(pf.join("knobs/gray/build")).unwrap();
        std::fs::write(
            pf.join("knobs/gray/build/build_layer.sh"),
            "fixture, never executed",
        )
        .unwrap();
        std::fs::write(pf.join("knobs/gray/workload_spin_touch_v1.json"), "{}").unwrap();
        std::fs::write(root.join("probe.txt"), "gpu.num_pwrlevels=5\ngpu.min_pwrlevel.writable=yes\ngpu.min_pwrlevel.effect=live\ngpu.min_pwrlevel.cur=4\ngpu.max_pwrlevel.writable=yes\ngpu.max_pwrlevel.effect=live\ngpu.max_pwrlevel.cur=0\nbus.DDR.boost_freq.writable=yes\nbus.DDR.boost_freq.effect=live\nbus.DDR.boost_freq.cur=547000\nbus.DDR.avail_freqs=547000 1555000 5333000\n").unwrap();
        let repo = Path::new(env!("CARGO_MANIFEST_DIR"));
        #[cfg(unix)]
        std::os::unix::fs::symlink(repo.join("templates"), cwd.join("templates")).unwrap();
        let phonefarm = pf.join("phonefarm");
        std::fs::copy(fake_binary(), &phonefarm).unwrap();
        Self {
            log: root.join("calls.txt"),
            root,
            cwd,
            out,
            phonefarm,
            receiver_evidence: None,
            force_pass: false,
        }
    }
    fn command(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_game_opt_loop"));
        cmd.current_dir(&self.cwd)
            .env("FAKE_PHONEFARM_LOG", &self.log)
            .args(args);
        if let Some(stage) = self.receiver_evidence {
            cmd.env("FAKE_QUALIFICATION", stage);
        }
        if self.force_pass {
            cmd.env("FAKE_PASS", "1");
        }
        cmd
    }
    fn call(&self, args: &[&str]) -> Output {
        self.command(args).output().unwrap()
    }
    fn journal(&self) -> Value {
        serde_json::from_slice(&std::fs::read(self.out.join("campaign.json")).unwrap()).unwrap()
    }
    fn calls(&self) -> Vec<String> {
        std::fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .map(str::to_string)
            .collect()
    }
    fn evolution(&self, resume: bool) -> Output {
        let mut args = vec![
            "--track",
            "sr",
            "--generations",
            "2",
            "--children",
            "4",
            "--no-llm",
            "--no-evidence",
            "--campaign-mode",
            "expert",
            "--device",
            "TEST0001",
            "--out",
            self.out.to_str().unwrap(),
            "--phonefarm",
            self.phonefarm.to_str().unwrap(),
        ];
        if resume {
            args.push("--resume");
        }
        self.call(&args)
    }
    fn optimize(&self, mode: &str, resume: bool) -> Output {
        let probe = self.root.join("probe.txt");
        let mut args = vec![
            "optimize",
            "--no-devlock",
            "--game",
            "原神",
            "--device",
            "TEST0001",
            "--budget",
            "2h",
            "--no-llm",
            "--campaign-mode",
            mode,
            "--out",
            self.out.to_str().unwrap(),
            "--phonefarm",
            self.phonefarm.to_str().unwrap(),
            "--sysparam-probe",
            probe.to_str().unwrap(),
            // 灰档旋钮来自 frame-audit 判定「确定没人读」的写回 (一刀切的 LoadOp 改写已撤回)。
            "--frame-audit",
            concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/frame_audit/genshin_7.1_analysis.json"),
        ];
        if resume {
            args.push("--resume");
        }
        self.call(&args)
    }
    fn decide(&self, id: &str, action: &str, reason: &str) {
        success(self.call(&[
            "campaign",
            "decide",
            "--out",
            self.out.to_str().unwrap(),
            "--candidate",
            id,
            "--action",
            action,
            "--reviewer",
            "test engineer",
            "--reason",
            reason,
        ]));
    }
    fn decide_pending(&self, action: &str) {
        success(self.call(&[
            "campaign",
            "decide",
            "--out",
            self.out.to_str().unwrap(),
            "--all-pending",
            "--action",
            action,
            "--reviewer",
            "test engineer",
            "--reason",
            "fixture expert direction decision",
        ]));
    }
}
fn success(output: Output) {
    assert!(
        output.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn shader_expert_reviews_each_generation_and_resume_never_repeats_measurement() {
    let f = Fixture::new();
    success(f.evolution(false));
    assert!(f.calls().is_empty());
    assert_eq!(f.journal()["entries"]["gen0_baseline"]["state"], "pending");
    f.decide("gen0_baseline", "accept", "establish baseline");
    success(f.evolution(true));
    assert_eq!(f.calls(), ["gen0_baseline"]);
    let staged: Vec<String> = f.journal()["entries"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(_, e)| e["state"] == "pending")
        .map(|(id, _)| id.clone())
        .collect();
    assert!(staged.len() >= 2, "must have a genuine generated batch");
    f.decide(&staged[0], "accept", "expected useful direction");
    f.decide(&staged[1], "defer", "need more evidence");
    for id in staged.iter().skip(2) {
        f.decide(id, "reject", "expensive low-return direction");
    }
    success(f.evolution(true));
    assert_eq!(f.calls().len(), 2);
    assert_eq!(f.calls()[1], staged[0]);
    success(f.evolution(true));
    assert_eq!(f.calls().len(), 2);
    f.decide(&staged[1], "reject", "risk remains too high");
    success(f.evolution(true));
    let j = f.journal();
    assert_eq!(
        j["entries"][&staged[1]]["decisions"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert!(j["entries"]
        .as_object()
        .unwrap()
        .values()
        .any(|e| e["generation"] == 2 && e["state"] == "pending"));
    f.decide_pending("accept");
    success(f.evolution(true));
    let done = f.calls();
    success(f.evolution(true));
    assert_eq!(f.calls(), done);
    let first_generation: Value =
        serde_json::from_slice(&std::fs::read(f.out.join("gen_1.json")).unwrap()).unwrap();
    assert_eq!(
        first_generation["best_psnr_so_far"], 46.0,
        "resume must not leak generation 2 improvements into generation 1"
    );
    let journal = f.journal();
    let measured_fingerprints: std::collections::HashSet<_> = done
        .iter()
        .map(|id| {
            journal["entries"][id]["candidate"]["fingerprint"]
                .as_str()
                .unwrap()
        })
        .collect();
    assert_eq!(
        measured_fingerprints.len(),
        done.len(),
        "duplicate siblings must not consume device time"
    );
    let archive: Value =
        serde_json::from_slice(&std::fs::read(f.out.join("archive.json")).unwrap()).unwrap();
    assert!(archive["entries"]
        .as_array()
        .unwrap()
        .iter()
        .all(|e| e["expert_decisions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|d| d["expert"] == true)));
}

#[test]
fn optimize_expert_covers_sysparam_and_gray_and_restores_frozen_plan() {
    let f = Fixture::new();
    success(f.optimize("expert", false));
    assert!(f.calls().is_empty());
    let j = f.journal();
    assert!(j["entries"]
        .as_object()
        .unwrap()
        .values()
        .any(|e| e["layer"] == "sysparam"));
    assert!(j["entries"]
        .as_object()
        .unwrap()
        .values()
        .any(|e| e["layer"] == "gray"));
    // frame-audit 给出多个灰档旋钮时一代有多个灰档候选, 逐个拒绝。
    let gray: Vec<String> = j["entries"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(_, e)| e["layer"] == "gray")
        .map(|(id, _)| id.clone())
        .collect();
    for id in &gray {
        f.decide(id, "reject", "quality route not ready");
    }
    f.decide_pending("accept");
    success(f.optimize("expert", true));
    assert!(!f.calls().iter().any(|id| id.starts_with("gray")));
    let calls = f.calls();
    assert!(!calls.is_empty());
    // Further generations may await fresh decisions, never replay old reports.
    success(f.optimize("expert", true));
    assert_eq!(f.calls(), calls);
    let report: Value =
        serde_json::from_slice(&std::fs::read(f.out.join("report.json")).unwrap()).unwrap();
    assert_eq!(report["campaign"]["mode"], "expert");
    assert!(report["archive"]
        .as_array()
        .unwrap()
        .iter()
        .all(|e| e["campaign_binding_sha256"].as_str().is_some()));
}

#[test]
fn autonomous_campaign_has_no_fabricated_expert_and_no_repeat() {
    let f = Fixture::new();
    success(f.optimize("autonomous", false));
    let done = f.calls();
    assert!(!done.is_empty());
    success(f.optimize("autonomous", true));
    assert_eq!(f.calls(), done);
    for entry in f.journal()["entries"].as_object().unwrap().values() {
        assert!(entry["decisions"]
            .as_array()
            .unwrap()
            .iter()
            .all(|d| d["expert"] == false));
    }
}

#[test]
fn candidate_request_artifact_and_frozen_conditions_tampering_blocks_measurement() {
    for attack in [
        "candidate",
        "request",
        "artifact",
        "conditions",
        "batch",
        "evaluator",
    ] {
        let f = Fixture::new();
        success(f.evolution(false));
        f.decide_pending("accept");
        let mut j = f.journal();
        match attack {
            "candidate" => {
                j["entries"]["gen0_baseline"]["candidate"]["shader_source"] = json!("changed")
            }
            "request" => {
                j["entries"]["gen0_baseline"]["request"]["protocol"]["p_threshold"] = json!(0.9)
            }
            "artifact" => {
                let path = j["entries"]["gen0_baseline"]["artifacts"][0]["path"]
                    .as_str()
                    .unwrap();
                std::fs::write(path, "changed").unwrap();
            }
            "conditions" => j["frozen"]["cool_c"] = json!(99.0),
            "evaluator" => std::fs::write(&f.phonefarm, "changed receiver").unwrap(),
            "batch" => {
                success(f.evolution(true));
                j = f.journal();
                let snaps = j["snapshots"].as_object_mut().unwrap();
                let snap = snaps.values_mut().next().unwrap();
                snap["value"]["batch"][0][0]["notes"] = json!("changed");
            }
            _ => unreachable!(),
        }
        std::fs::write(
            f.out.join("campaign.json"),
            serde_json::to_vec_pretty(&j).unwrap(),
        )
        .unwrap();
        let before = f.calls();
        let result = f.evolution(true);
        assert!(!result.status.success(), "{attack} was accepted");
        assert_eq!(f.calls(), before);
    }
}

#[test]
fn changed_probe_or_mode_cannot_reuse_optimize_approvals() {
    let f = Fixture::new();
    success(f.optimize("expert", false));
    f.decide_pending("accept");
    assert!(!f.optimize("autonomous", true).status.success());
    assert!(f.calls().is_empty());
    std::fs::write(f.root.join("probe.txt"), "gpu.num_pwrlevels=999\n").unwrap();
    assert!(!f.optimize("expert", true).status.success());
    assert!(f.calls().is_empty());
}

fn assert_receiver_evidence(value: &Value, stage: &str) {
    let expected_stage = if stage == "qualification_only" {
        "conclusion"
    } else {
        stage
    };
    assert_eq!(value["qualification"]["stage"], expected_stage);
    assert_eq!(value["qualification"]["eligible"], true);
    assert_eq!(value["qualification_only"], stage == "qualification_only");
    assert_eq!(value["artifacts"]["fixture"], "synthetic: no real frame");
    assert_eq!(value["metric_provenance"]["power_watt"], "unavailable");
    assert_eq!(
        value["future_receiver_evidence"]["nested"],
        json!([null, false, {"sha256":"synthetic"}])
    );
}

#[test]
fn shader_campaign_preserves_receiver_extensions_and_qualification_on_replay() {
    for stage in ["conclusion", "experiment", "qualification_only"] {
        let mut f = Fixture::new();
        f.receiver_evidence = Some(stage);
        success(f.evolution(false));
        for _ in 0..3 {
            f.decide_pending("accept");
            success(f.evolution(true));
        }
        let calls = f.calls();
        success(f.evolution(true));
        assert_eq!(f.calls(), calls);
        let journal = f.journal();
        let archive: Value =
            serde_json::from_slice(&std::fs::read(f.out.join("archive.json")).unwrap()).unwrap();
        let qualified = stage == "conclusion";
        for entry in archive["entries"].as_array().unwrap() {
            let id = entry["id"].as_str().unwrap();
            assert_receiver_evidence(&entry["report"], stage);
            assert_eq!(entry["report"], journal["entries"][id]["result"]);
            assert_eq!(
                entry["qualification_state"],
                if qualified { "qualified" } else { "ineligible" }
            );
            assert_eq!(
                entry["campaign_binding_sha256"],
                journal["entries"][id]["binding_sha256"]
            );
            assert!(entry["expert_decisions"][0]["expert"].as_bool().unwrap());
        }
        let rebuilt =
            game_opt_loop::engine::ParetoArchive::load(&f.out.join("archive.json")).unwrap();
        assert_eq!(!rebuilt.front().is_empty(), qualified);
    }
}

#[test]
fn optimize_campaign_preserves_measurement_evidence_and_qualification_on_replay() {
    for stage in ["conclusion", "experiment", "qualification_only"] {
        let mut f = Fixture::new();
        f.receiver_evidence = Some(stage);
        f.force_pass = true;
        success(f.optimize("expert", false));
        for _ in 0..10 {
            let waiting = f.journal()["entries"]
                .as_object()
                .unwrap()
                .values()
                .any(|e| e["state"] == "pending" || e["state"] == "deferred");
            if !waiting {
                break;
            }
            f.decide_pending("accept");
            success(f.optimize("expert", true));
        }
        let calls = f.calls();
        assert!(!calls.is_empty());
        success(f.optimize("expert", true));
        assert_eq!(f.calls(), calls);
        let journal = f.journal();
        let report: Value =
            serde_json::from_slice(&std::fs::read(f.out.join("report.json")).unwrap()).unwrap();
        assert_eq!(report["campaign"]["waiting_for_review"], false);
        let qualified = stage == "conclusion";
        for entry in report["archive"].as_array().unwrap() {
            let id = entry["id"].as_str().unwrap();
            assert_receiver_evidence(&entry["measurement_evidence"], stage);
            assert_receiver_evidence(&journal["entries"][id]["result"], stage);
            assert_eq!(
                entry["qualification_state"],
                if qualified { "qualified" } else { "ineligible" }
            );
            assert_eq!(entry["kept"], qualified);
            assert_eq!(entry["measurement_valid"], qualified);
            assert_eq!(
                entry["campaign_binding_sha256"],
                journal["entries"][id]["binding_sha256"]
            );
            assert_eq!(entry["expert_decisions"][0]["expert"], true);
        }
        assert_eq!(!report["kept"].as_array().unwrap().is_empty(), qualified);
    }
}

#[test]
fn process_interruption_remains_in_flight_and_never_redispatches() {
    let f = Fixture::new();
    success(f.evolution(false));
    f.decide_pending("accept");
    let args = [
        "--track",
        "sr",
        "--generations",
        "2",
        "--children",
        "4",
        "--no-llm",
        "--no-evidence",
        "--campaign-mode",
        "expert",
        "--device",
        "TEST0001",
        "--out",
        f.out.to_str().unwrap(),
        "--phonefarm",
        f.phonefarm.to_str().unwrap(),
        "--resume",
    ];
    let mut child = f
        .command(&args)
        .env("FAKE_PAUSE", "1")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let until = std::time::Instant::now() + std::time::Duration::from_secs(10);
    while f.calls().is_empty() && std::time::Instant::now() < until {
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    assert_eq!(f.calls(), ["gen0_baseline"]);
    let locked = f.call(&["campaign", "status", "--out", f.out.to_str().unwrap()]);
    assert!(
        !locked.status.success(),
        "another process cannot inspect/mutate an active journal"
    );
    assert!(String::from_utf8_lossy(&locked.stderr).contains("另一进程"));
    child.kill().unwrap();
    child.wait().unwrap();
    assert_eq!(
        f.journal()["entries"]["gen0_baseline"]["state"],
        "in_flight"
    );
    assert!(!f.evolution(true).status.success());
    assert_eq!(f.calls(), ["gen0_baseline"]);
    let receipt = f.root.join("verified_receipt.json");
    let mut report = json!({"candidate_id":"wrong_id", "status":"PASS",
        "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":16.0,"psnr_db":45.0,"power_watt":null},
        "verdict":{"is_pareto_improvement":true,"p_value":0.001},"source":"phonefarm"});
    let reconcile = || {
        f.call(&[
            "campaign",
            "reconcile",
            "--out",
            f.out.to_str().unwrap(),
            "--candidate",
            "gen0_baseline",
            "--report",
            receipt.to_str().unwrap(),
            "--reviewer",
            "test engineer",
            "--reason",
            "verified synthetic receiver receipt",
        ])
    };
    std::fs::write(&receipt, serde_json::to_vec(&report).unwrap()).unwrap();
    assert!(!reconcile().status.success());
    assert_eq!(
        f.journal()["entries"]["gen0_baseline"]["state"],
        "in_flight"
    );
    report["candidate_id"] = json!("gen0_baseline");
    std::fs::write(&receipt, serde_json::to_vec(&report).unwrap()).unwrap();
    success(reconcile());
    success(f.evolution(true));
    assert_eq!(
        f.calls(),
        ["gen0_baseline"],
        "reconciled receipt must not be remeasured"
    );
    assert_eq!(
        f.journal()["entries"]["gen0_baseline"]["state"],
        "completed"
    );
    assert!(f.journal()["entries"]["gen0_baseline"]["error"]
        .as_str()
        .unwrap()
        .contains("test engineer"));
}
