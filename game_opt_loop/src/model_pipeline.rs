//! At-most-once external model artifact stages. Completion is protocol evidence,
//! not proof of training, device evaluation, trusted provenance or promotion.
use anyhow::{bail, ensure, Context, Result};
use clap::{Subcommand, ValueEnum};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const PLAN_SCHEMA: &str = "game_opt_loop/model_pipeline_plan@1";
const JOURNAL_SCHEMA: &str = "game_opt_loop/model_pipeline_journal@1";
const REQUEST_SCHEMA: &str = "game_opt_loop/model_stage_request@1";
const RECEIPT_SCHEMA: &str = "game_opt_loop/model_stage_receipt@1";

#[derive(Debug, Subcommand)]
pub enum PipelineCommand {
    Start {
        #[arg(long)]
        plan: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
    Resume {
        #[arg(long)]
        out: PathBuf,
    },
    Status {
        #[arg(long)]
        out: PathBuf,
    },
    Decide {
        #[arg(long)]
        out: PathBuf,
        #[arg(long, value_enum)]
        stage: Stage,
        #[arg(long, value_enum)]
        action: Action,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        reason: String,
    },
    Reconcile {
        #[arg(long)]
        out: PathBuf,
        #[arg(long, value_enum)]
        stage: Stage,
        #[arg(long)]
        receipt: PathBuf,
        #[arg(long)]
        reviewer: String,
        #[arg(long)]
        reason: String,
        /// 人工确认所有驱动、子进程和外部任务已停止；本机也检查记录的进程组。
        #[arg(long)]
        workers_stopped: bool,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Design,
    Train,
    Quantize,
    Deploy,
    Evaluate,
}
impl Stage {
    fn role(self) -> Role {
        match self {
            Self::Design => Role::Architecture,
            Self::Train => Role::Weights,
            Self::Quantize => Role::QuantizedModel,
            Self::Deploy => Role::DeploymentReceipt,
            Self::Evaluate => Role::EvaluationReport,
        }
    }
    fn all() -> [Self; 5] {
        [
            Self::Design,
            Self::Train,
            Self::Quantize,
            Self::Deploy,
            Self::Evaluate,
        ]
    }
    fn id(self) -> &'static str {
        match self {
            Self::Design => "design",
            Self::Train => "train",
            Self::Quantize => "quantize",
            Self::Deploy => "deploy",
            Self::Evaluate => "evaluate",
        }
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Autonomous,
    Expert,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Accept,
    Reject,
    Defer,
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Source {
    #[default]
    Unknown,
    Synthetic,
    RealDataDeclared,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Data,
    Driver,
    Architecture,
    Weights,
    QuantizedModel,
    DeploymentReceipt,
    EvaluationReport,
    Metadata,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Quantization {
    pub method: String,
    pub precision: String,
    pub calibration_data_sha256: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelSpec {
    pub framework: String,
    pub runtime: String,
    pub abi: String,
    pub quantization: Quantization,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub name: String,
    pub role: Role,
    pub path: PathBuf,
    pub sha256: String,
    pub format: String,
    #[serde(default)]
    pub source: Source,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Output {
    pub name: String,
    pub role: Role,
    pub format: String,
    #[serde(default)]
    pub source: Source,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Budget {
    pub wall_ms: u64,
    pub output_bytes: u64,
    pub steps: u64,
    pub tokens: u64,
    pub device_ms: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StagePlan {
    pub stage: Stage,
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub driver_artifacts: Vec<PathBuf>,
    pub outputs: Vec<Output>,
    pub budget: Budget,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub schema: String,
    pub pipeline_id: String,
    pub mode: Mode,
    pub candidate_description: String,
    pub model: ModelSpec,
    pub data: Vec<Artifact>,
    pub stages: Vec<StagePlan>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Frozen {
    pub plan: Plan,
    pub drivers: Vec<Vec<Artifact>>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RequestedOutput {
    pub name: String,
    pub role: Role,
    pub path: PathBuf,
    pub format: String,
    pub source: Source,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub schema: String,
    pub pipeline_id: String,
    pub plan_sha256: String,
    pub stage: Stage,
    pub model: ModelSpec,
    pub inputs: Vec<Artifact>,
    pub drivers: Vec<Artifact>,
    pub outputs: Vec<RequestedOutput>,
    pub budget: Budget,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Usage {
    pub steps: u64,
    pub tokens: u64,
    pub device_ms: u64,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReceiptStatus {
    Succeeded,
    Failed,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiptOutput {
    pub name: String,
    pub sha256: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub schema: String,
    pub pipeline_id: String,
    pub plan_sha256: String,
    pub stage: Stage,
    pub request_sha256: String,
    pub status: ReceiptStatus,
    pub usage: Usage,
    pub outputs: Vec<ReceiptOutput>,
    pub note: String,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Pending,
    Deferred,
    Ready,
    Rejected,
    InFlight,
    Unknown,
    Completed,
    Failed,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Decision {
    pub action: Action,
    pub reviewer: String,
    pub reason: String,
    pub request_sha256: String,
    pub recorded_at: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Reconciliation {
    pub reviewer: String,
    pub reason: String,
    pub recorded_at: String,
    pub workers_stopped_attested: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Evidence {
    pub receipt: Receipt,
    pub receipt_sha256: String,
    pub receipt_file: PathBuf,
    pub outputs: Vec<Artifact>,
    pub reconciliation: Option<Reconciliation>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub stage: Stage,
    pub state: State,
    pub request: Option<Request>,
    pub request_sha256: Option<String>,
    pub decisions: Vec<Decision>,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub host_wall_ms: Option<u64>,
    pub exit_code: Option<i32>,
    pub driver_pid: Option<u32>,
    pub stdout_sha256: Option<String>,
    pub stderr_sha256: Option<String>,
    pub evidence: Option<Evidence>,
    pub error: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Journal {
    pub schema: String,
    pub frozen: Frozen,
    pub plan_sha256: String,
    pub entries: Vec<Entry>,
    pub usage_verification: String,
    pub completion_scope: String,
    pub promotion_allowed: bool,
    pub real_game_optimization_proven: bool,
    pub model_semantics_verified: bool,
}
struct Pipeline {
    dir: PathBuf,
    journal: Journal,
    _lock: File,
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}
pub fn digest<T: Serialize>(v: &T) -> Result<String> {
    let bytes = serde_json::to_vec(&serde_json::to_value(v)?)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}
fn file_digest(path: &Path) -> Result<String> {
    ensure!(
        std::fs::symlink_metadata(path)?.file_type().is_file(),
        "regular file required: {}",
        path.display()
    );
    let mut hash = Sha256::new();
    let mut file = File::open(path)?;
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn atomic_write<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let tmp = path.with_extension("json.tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&tmp)
        .with_context(|| {
            format!(
                "temporary checkpoint exists; inspect it before removing: {}",
                tmp.display()
            )
        })?;
    file.write_all(&serde_json::to_vec_pretty(value)?)?;
    file.sync_all()?;
    std::fs::rename(&tmp, path)?;
    File::open(path.parent().context("missing checkpoint parent")?)?.sync_all()?;
    Ok(())
}
fn read_json<T: for<'a> Deserialize<'a>>(path: &Path) -> Result<T> {
    ensure!(
        std::fs::metadata(path)?.len() <= 4 * 1024 * 1024,
        "JSON too large"
    );
    Ok(serde_json::from_slice(&std::fs::read(path)?)?)
}
fn nonblank(text: &str) -> bool {
    !text.trim().is_empty() && text.len() <= 8192
}
fn name_ok(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.')
        && name != "."
        && name != ".."
}
fn hash_ok(hash: &str) -> bool {
    hash.len() == 64
        && hash
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn absolute(path: &Path, base: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        base.join(path)
    };
    ensure!(
        !path.components().any(|c| c == Component::ParentDir),
        "parent path forbidden"
    );
    Ok(path)
}
fn verify_artifact(artifact: &Artifact) -> Result<()> {
    ensure!(
        file_digest(&artifact.path)? == artifact.sha256,
        "artifact bytes changed: {}",
        artifact.path.display()
    );
    Ok(())
}
fn validate_plan(plan: &mut Plan, base: &Path) -> Result<()> {
    ensure!(
        plan.schema == PLAN_SCHEMA && name_ok(&plan.pipeline_id),
        "invalid plan schema/id"
    );
    ensure!(
        nonblank(&plan.candidate_description) && !plan.data.is_empty(),
        "candidate description and data provenance required"
    );
    ensure!([&plan.model.framework, &plan.model.runtime, &plan.model.abi,
        &plan.model.quantization.method, &plan.model.quantization.precision].into_iter().all(|v| nonblank(v)),
        "explicit framework/runtime/ABI/quantization declarations required; use unknown when unknown");
    if let Some(hash) = &plan.model.quantization.calibration_data_sha256 {
        ensure!(
            hash_ok(hash) && plan.data.iter().any(|a| a.sha256 == *hash),
            "calibration hash must reference frozen input data"
        );
    }
    ensure!(
        plan.stages.len() == 5 && plan.stages.iter().map(|s| s.stage).eq(Stage::all()),
        "fixed design/train/quantize/deploy/evaluate order required"
    );
    let mut names = BTreeSet::new();
    for data in &mut plan.data {
        ensure!(
            data.role == Role::Data
                && name_ok(&data.name)
                && names.insert(data.name.clone())
                && hash_ok(&data.sha256)
                && nonblank(&data.format),
            "invalid/duplicate data identity"
        );
        data.path = absolute(&data.path, base)?;
    }
    for stage in &mut plan.stages {
        ensure!(
            !stage.argv.is_empty()
                && stage.argv.len() <= 128
                && Path::new(&stage.argv[0]).is_absolute(),
            "explicit absolute executable argv required"
        );
        ensure!(
            stage
                .argv
                .iter()
                .all(|a| a.len() <= 8192 && !a.contains('\0')),
            "invalid argv"
        );
        stage.cwd = absolute(&stage.cwd, base)?;
        for path in &mut stage.driver_artifacts {
            *path = absolute(path, base)?;
        }
        ensure!(
            !stage.outputs.is_empty() && stage.outputs.len() <= 32,
            "outputs required"
        );
        names.clear();
        for output in &stage.outputs {
            ensure!(
                output.role == stage.stage.role() || output.role == Role::Metadata,
                "output role does not match stage"
            );
            ensure!(
                name_ok(&output.name)
                    && names.insert(output.name.clone())
                    && nonblank(&output.format),
                "invalid/duplicate output"
            );
            ensure!(
                !["request.json", "receipt.json", "stdout.log", "stderr.log"]
                    .contains(&output.name.as_str()),
                "reserved output name"
            );
        }
        ensure!(
            stage.outputs.iter().any(|o| o.role == stage.stage.role()),
            "stage main artifact role missing"
        );
        ensure!(
            stage.budget.wall_ms > 0
                && stage.budget.wall_ms <= 86_400_000
                && stage.budget.output_bytes > 0,
            "finite positive host budgets required"
        );
    }
    Ok(())
}

impl Pipeline {
    fn lock(dir: &Path) -> Result<(PathBuf, File)> {
        std::fs::create_dir_all(dir)?;
        let dir = dir.canonicalize()?;
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(dir.join("pipeline.lock"))?;
        lock.try_lock_exclusive()
            .context("pipeline is in use; only one writer/stage allowed")?;
        Ok((dir, lock))
    }
    fn start(plan_path: &Path, dir: &Path) -> Result<Self> {
        let (dir, lock) = Self::lock(dir)?;
        ensure!(
            !dir.join("pipeline.json").exists(),
            "pipeline exists; use resume or a new out directory"
        );
        let plan_path = plan_path.canonicalize()?;
        let mut plan: Plan = read_json(&plan_path)?;
        validate_plan(
            &mut plan,
            plan_path.parent().context("missing plan parent")?,
        )?;
        let mut drivers = vec![Vec::new(); 5];
        let mut initialization_error = None;
        for (i, stage) in plan.stages.iter().enumerate() {
            let captured = (|| -> Result<Vec<Artifact>> {
                ensure!(stage.cwd.is_dir(), "driver cwd missing");
                let mut paths = stage.driver_artifacts.clone();
                paths.push(PathBuf::from(&stage.argv[0]));
                paths.sort();
                paths.dedup();
                for arg in stage.argv.iter().skip(1) {
                    let path = Path::new(arg);
                    let path = if path.is_absolute() {
                        path.to_owned()
                    } else {
                        stage.cwd.join(path)
                    };
                    ensure!(
                        !path.is_file() || paths.contains(&path),
                        "file argv must be bound in driver_artifacts: {}",
                        path.display()
                    );
                }
                paths
                    .iter()
                    .enumerate()
                    .map(|(n, path)| {
                        Ok(Artifact {
                            name: format!("driver_{n}"),
                            role: Role::Driver,
                            path: path.clone(),
                            sha256: file_digest(path)?,
                            format: "opaque_driver_file".into(),
                            source: Source::Unknown,
                        })
                    })
                    .collect()
            })();
            match captured {
                Ok(captured) => drivers[i] = captured,
                Err(err) => {
                    initialization_error = Some(format!("{}: {err:#}", stage.stage.id()));
                    break;
                }
            }
        }
        let frozen = Frozen { plan, drivers };
        let plan_sha256 = digest(&frozen)?;
        let entries = Stage::all()
            .into_iter()
            .map(|stage| Entry {
                stage,
                state: State::Pending,
                request: None,
                request_sha256: None,
                decisions: Vec::new(),
                started_at: None,
                finished_at: None,
                host_wall_ms: None,
                exit_code: None,
                driver_pid: None,
                stdout_sha256: None,
                stderr_sha256: None,
                evidence: None,
                error: None,
            })
            .collect();
        let mut pipeline = Self {
            dir,
            _lock: lock,
            journal: Journal {
                schema: JOURNAL_SCHEMA.into(),
                frozen,
                plan_sha256,
                entries,
                usage_verification: "driver_declared_unverified".into(),
                completion_scope: "driver_protocol_and_artifact_integrity_only".into(),
                promotion_allowed: false,
                real_game_optimization_proven: false,
                model_semantics_verified: false,
            },
        };
        if let Some(error) = initialization_error {
            pipeline.fail(0, error);
        }
        pipeline.save()?;
        Ok(pipeline)
    }
    fn open(dir: &Path) -> Result<Self> {
        let (dir, lock) = Self::lock(dir)?;
        let journal = read_json(&dir.join("pipeline.json"))?;
        let pipeline = Self {
            dir,
            _lock: lock,
            journal,
        };
        pipeline.verify()?;
        Ok(pipeline)
    }
    fn save(&self) -> Result<()> {
        atomic_write(&self.dir.join("pipeline.json"), &self.journal)
    }
    fn stage_dir(&self, index: usize) -> PathBuf {
        self.dir.join(format!(
            "{index}_{}",
            self.journal.entries[index].stage.id()
        ))
    }
    fn fail(&mut self, index: usize, error: String) {
        let entry = &mut self.journal.entries[index];
        entry.state = State::Failed;
        entry.error = Some(error);
        entry.finished_at = Some(now());
    }
    fn inputs(&self, index: usize) -> Result<Vec<Artifact>> {
        let mut inputs = self.journal.frozen.plan.data.clone();
        for prior in 0..index {
            ensure!(
                self.journal.entries[prior].state == State::Completed,
                "previous stage must be completed"
            );
            inputs.extend(
                self.journal.entries[prior]
                    .evidence
                    .as_ref()
                    .context("missing previous evidence")?
                    .outputs
                    .clone(),
            );
        }
        for artifact in &inputs {
            verify_artifact(artifact)?;
        }
        Ok(inputs)
    }
    fn expected_request(&self, index: usize) -> Result<Request> {
        let spec = &self.journal.frozen.plan.stages[index];
        let inputs = self.inputs(index)?;
        for artifact in &self.journal.frozen.drivers[index] {
            verify_artifact(artifact)?;
        }
        ensure!(
            !self.journal.frozen.drivers[index].is_empty(),
            "driver integrity missing"
        );
        let synthetic = inputs.iter().any(|a| a.source == Source::Synthetic);
        ensure!(
            !synthetic || spec.outputs.iter().all(|o| o.source == Source::Synthetic),
            "synthetic inputs must stay synthetic"
        );
        Ok(Request {
            schema: REQUEST_SCHEMA.into(),
            pipeline_id: self.journal.frozen.plan.pipeline_id.clone(),
            plan_sha256: self.journal.plan_sha256.clone(),
            stage: spec.stage,
            model: self.journal.frozen.plan.model.clone(),
            inputs,
            drivers: self.journal.frozen.drivers[index].clone(),
            outputs: spec
                .outputs
                .iter()
                .map(|o| RequestedOutput {
                    name: o.name.clone(),
                    role: o.role,
                    path: self.stage_dir(index).join(&o.name),
                    format: o.format.clone(),
                    source: o.source,
                })
                .collect(),
            budget: spec.budget.clone(),
        })
    }
    fn verify(&self) -> Result<()> {
        ensure!(
            self.journal.schema == JOURNAL_SCHEMA
                && !self.journal.promotion_allowed
                && !self.journal.real_game_optimization_proven
                && !self.journal.model_semantics_verified,
            "journal claim/schema changed"
        );
        ensure!(
            self.journal.completion_scope == "driver_protocol_and_artifact_integrity_only"
                && self.journal.usage_verification == "driver_declared_unverified",
            "journal verification scope changed"
        );
        ensure!(
            digest(&self.journal.frozen)? == self.journal.plan_sha256,
            "frozen plan/driver hashes changed"
        );
        ensure!(
            self.journal.entries.len() == 5
                && self
                    .journal
                    .entries
                    .iter()
                    .map(|e| e.stage)
                    .eq(Stage::all()),
            "journal stage order changed"
        );
        for (i, entry) in self.journal.entries.iter().enumerate() {
            if matches!(
                entry.state,
                State::Ready | State::InFlight | State::Unknown | State::Completed
            ) {
                ensure!(
                    entry.request.is_some() && entry.request_sha256.is_some(),
                    "ready/active/completed stage missing request and digest"
                );
                if self.journal.frozen.plan.mode == Mode::Expert {
                    ensure!(
                        entry
                            .decisions
                            .last()
                            .is_some_and(|d| d.action == Action::Accept
                                && Some(&d.request_sha256) == entry.request_sha256.as_ref()),
                        "expert stage without bound acceptance"
                    );
                }
            }
            for decision in &entry.decisions {
                ensure!(
                    entry.request.is_some()
                        && Some(&decision.request_sha256) == entry.request_sha256.as_ref(),
                    "expert decision binding changed"
                );
            }
            if let Some(request) = &entry.request {
                ensure!(
                    Some(digest(request)?) == entry.request_sha256,
                    "request digest changed"
                );
                ensure!(
                    digest(&self.expected_request(i)?)? == digest(request)?,
                    "request inputs/plan changed"
                );
                let request_file: Request = read_json(&self.stage_dir(i).join("request.json"))?;
                ensure!(
                    digest(&request_file)? == digest(request)?,
                    "on-disk request changed"
                );
            }
            if let Some(evidence) = &entry.evidence {
                ensure!(
                    file_digest(&evidence.receipt_file)? == evidence.receipt_sha256,
                    "receipt bytes changed"
                );
                let receipt: Receipt = read_json(&evidence.receipt_file)?;
                ensure!(
                    digest(&receipt)? == digest(&evidence.receipt)?,
                    "stored receipt changed"
                );
                let outputs = self.validate_receipt(i, &receipt)?;
                ensure!(
                    digest(&outputs)? == digest(&evidence.outputs)?,
                    "output evidence changed"
                );
            }
            if entry.state == State::Completed {
                ensure!(
                    entry
                        .evidence
                        .as_ref()
                        .is_some_and(|e| e.receipt.status == ReceiptStatus::Succeeded),
                    "completed without successful evidence"
                );
            }
            for (file, hash) in [
                ("stdout.log", &entry.stdout_sha256),
                ("stderr.log", &entry.stderr_sha256),
            ] {
                if let Some(hash) = hash {
                    ensure!(
                        file_digest(&self.stage_dir(i).join(file))? == *hash,
                        "stage log changed"
                    );
                }
            }
        }
        Ok(())
    }
    fn prepare(&mut self, index: usize) -> Result<()> {
        let request = self.expected_request(index)?;
        if let Some(existing) = &self.journal.entries[index].request {
            ensure!(
                digest(existing)? == digest(&request)?,
                "prepared request changed"
            );
            return Ok(());
        }
        let dir = self.stage_dir(index);
        std::fs::create_dir(&dir).context(
            "stage directory exists without checkpoint; inspect before creating a new experiment",
        )?;
        atomic_write(&dir.join("request.json"), &request)?;
        let entry = &mut self.journal.entries[index];
        entry.request_sha256 = Some(digest(&request)?);
        entry.request = Some(request);
        self.save()
    }
    fn validate_receipt(&self, index: usize, receipt: &Receipt) -> Result<Vec<Artifact>> {
        let entry = &self.journal.entries[index];
        let request = entry.request.as_ref().context("request missing")?;
        ensure!(
            digest(&self.expected_request(index)?)? == digest(request)?,
            "input/driver bytes changed during stage; receipt cannot complete it"
        );
        ensure!(
            receipt.schema == RECEIPT_SCHEMA
                && receipt.pipeline_id == request.pipeline_id
                && receipt.plan_sha256 == request.plan_sha256
                && receipt.stage == request.stage
                && Some(&receipt.request_sha256) == entry.request_sha256.as_ref(),
            "receipt request binding mismatch"
        );
        ensure!(nonblank(&receipt.note), "receipt note required");
        ensure!(
            receipt.usage.steps <= request.budget.steps
                && receipt.usage.tokens <= request.budget.tokens
                && receipt.usage.device_ms <= request.budget.device_ms,
            "declared driver usage exceeds frozen budget (unverified measurement)"
        );
        if receipt.status == ReceiptStatus::Failed {
            ensure!(
                receipt.outputs.is_empty(),
                "failed receipt must not claim completed outputs"
            );
            return Ok(Vec::new());
        }
        ensure!(
            entry
                .host_wall_ms
                .is_none_or(|ms| ms < request.budget.wall_ms),
            "observed host wall budget overrun cannot be reconciled as success"
        );
        ensure!(
            receipt.outputs.len() == request.outputs.len(),
            "missing/extra receipt outputs"
        );
        let mut seen = BTreeSet::new();
        let mut artifacts = Vec::new();
        let mut bytes = 0_u64;
        for output in &request.outputs {
            let record = receipt
                .outputs
                .iter()
                .find(|r| r.name == output.name)
                .context("missing registered output")?;
            ensure!(
                seen.insert(record.name.clone()) && hash_ok(&record.sha256),
                "duplicate/invalid output SHA"
            );
            bytes = bytes
                .checked_add(std::fs::metadata(&output.path)?.len())
                .context("output size overflow")?;
            ensure!(
                bytes <= request.budget.output_bytes,
                "output bytes exceed frozen budget before hashing"
            );
            let sha256 = file_digest(&output.path)?;
            ensure!(
                sha256 == record.sha256,
                "output SHA mismatch: {}",
                output.name
            );
            artifacts.push(Artifact {
                name: output.name.clone(),
                role: output.role,
                path: output.path.clone(),
                sha256,
                format: output.format.clone(),
                source: output.source,
            });
        }
        Ok(artifacts)
    }
    fn capture_evidence(
        &mut self,
        index: usize,
        path: &Path,
        reconciliation: Option<Reconciliation>,
    ) -> Result<()> {
        let receipt: Receipt = read_json(path)?;
        let outputs = self.validate_receipt(index, &receipt)?;
        let dest = self.stage_dir(index).join(if reconciliation.is_some() {
            "reconciled_receipt.json"
        } else {
            "receipt.json"
        });
        if path != dest {
            if dest.exists() {
                let existing: Receipt = read_json(&dest)?;
                ensure!(
                    digest(&existing)? == digest(&receipt)?,
                    "different receipt already staged; inspect interrupted reconcile"
                );
            } else {
                atomic_write(&dest, &receipt)?;
            }
        }
        let receipt_sha256 = file_digest(&dest)?;
        let succeeded = receipt.status == ReceiptStatus::Succeeded;
        let entry = &mut self.journal.entries[index];
        entry.evidence = Some(Evidence {
            receipt,
            receipt_sha256,
            receipt_file: dest,
            outputs,
            reconciliation,
        });
        entry.finished_at = Some(now());
        entry.state = if succeeded {
            State::Completed
        } else {
            State::Failed
        };
        entry.error = if succeeded {
            None
        } else {
            Some("driver reported negative stage result".into())
        };
        Ok(())
    }
    fn disk_bytes(&self, index: usize) -> Result<u64> {
        let mut total = 0_u64;
        for item in std::fs::read_dir(self.stage_dir(index))? {
            let item = item?;
            let meta = match std::fs::symlink_metadata(item.path()) {
                Ok(meta) => meta,
                // A driver may atomically rename its receipt between readdir and stat.
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            ensure!(
                meta.file_type().is_file(),
                "stage must contain only regular files"
            );
            if item.file_name() != "request.json" {
                total = total
                    .checked_add(meta.len())
                    .context("stage byte count overflow")?;
            }
        }
        Ok(total)
    }
    fn execute(&mut self, index: usize) -> Result<()> {
        let spec = self.journal.frozen.plan.stages[index].clone();
        let dir = self.stage_dir(index);
        for output in &spec.outputs {
            ensure!(
                !dir.join(&output.name).exists(),
                "pre-existing stage output; refusing driver side effect"
            );
        }
        ensure!(
            !dir.join("receipt.json").exists(),
            "pre-existing stage receipt"
        );
        let stdout = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(dir.join("stdout.log"))?;
        let stderr = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(dir.join("stderr.log"))?;
        self.journal.entries[index].state = State::InFlight;
        self.journal.entries[index].started_at = Some(now());
        self.save()?; // Never spawn before durable in-flight.
        let timer = Instant::now();
        let mut command = Command::new(&spec.argv[0]);
        command
            .args(&spec.argv[1..])
            .current_dir(&spec.cwd)
            .env_clear()
            .env("GOL_PIPELINE_REQUEST", dir.join("request.json"))
            .env("GOL_PIPELINE_RECEIPT", dir.join("receipt.json"))
            .env("GOL_PIPELINE_STAGE_DIR", &dir)
            .stdin(Stdio::null())
            .stdout(stdout)
            .stderr(stderr);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                self.fail(index, format!("spawn failed: {error}"));
                self.save()?;
                return Ok(());
            }
        };
        self.journal.entries[index].driver_pid = Some(child.id());
        if let Err(error) = self.save() {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            #[cfg(not(unix))]
            {
                let _ = child.kill();
            }
            let _ = child.wait();
            return Err(error.context("could not checkpoint driver pid; process group terminated"));
        }
        let mut interruption = None;
        let status = loop {
            match self.disk_bytes(index) {
                Ok(bytes) if bytes > spec.budget.output_bytes => {
                    interruption = Some("host output/log bytes budget exceeded".to_string())
                }
                Err(error) => interruption = Some(format!("invalid stage files: {error:#}")),
                _ => {}
            }
            if timer.elapsed().as_millis() >= u128::from(spec.budget.wall_ms) {
                interruption = Some("host wall budget exceeded".into());
            }
            if interruption.is_some() {
                #[cfg(unix)]
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                #[cfg(not(unix))]
                {
                    child.kill()?;
                }
                break child.wait()?;
            }
            if let Some(status) = child.try_wait()? {
                // A wrapper exiting does not establish that its workers stopped.
                #[cfg(unix)]
                unsafe {
                    if libc::kill(-(child.id() as i32), 0) == 0 {
                        libc::kill(-(child.id() as i32), libc::SIGKILL);
                        interruption = Some("driver left a live process group after exit".into());
                    }
                }
                break status;
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        let entry = &mut self.journal.entries[index];
        entry.host_wall_ms = Some(timer.elapsed().as_millis().min(u128::from(u64::MAX)) as u64);
        entry.exit_code = status.code();
        entry.finished_at = Some(now());
        entry.stdout_sha256 = Some(file_digest(&dir.join("stdout.log"))?);
        entry.stderr_sha256 = Some(file_digest(&dir.join("stderr.log"))?);
        if self.disk_bytes(index)? > spec.budget.output_bytes {
            interruption = Some("host output/log bytes budget exceeded at completion".into());
        }
        let entry = &mut self.journal.entries[index];
        if let Some(reason) = interruption {
            entry.state = State::Unknown;
            entry.error = Some(format!("{reason}; process group killed, external effects unknown; explicit reconcile required"));
        } else if !status.success() {
            self.fail(index, format!("driver nonzero exit: {status}"));
        } else if let Err(error) = self.capture_evidence(index, &dir.join("receipt.json"), None) {
            self.fail(index, format!("driver protocol failed: {error:#}"));
        }
        self.save()
    }
    fn run(&mut self) -> Result<()> {
        self.verify()?;
        for i in 0..5 {
            match self.journal.entries[i].state {
                State::Completed => continue,
                State::Failed | State::Rejected => return Ok(()),
                State::InFlight | State::Unknown => bail!("{} has unknown in-flight side effects; reconcile explicitly; never automatic retry", self.journal.entries[i].stage.id()),
                _ => {}
            }
            if let Err(error) = self.prepare(i) {
                self.fail(i, format!("preflight failed: {error:#}"));
                self.save()?;
                return Ok(());
            }
            if self.journal.frozen.plan.mode == Mode::Expert
                && self.journal.entries[i].state != State::Ready
            {
                return Ok(());
            }
            if self.journal.frozen.plan.mode == Mode::Autonomous {
                self.journal.entries[i].state = State::Ready;
                self.save()?;
            }
            if let Err(error) = self.execute(i) {
                // If checkpoint already says in-flight, preserve uncertainty.
                if self.journal.entries[i].state == State::InFlight {
                    self.journal.entries[i].state = State::Unknown;
                    self.journal.entries[i].error = Some(format!(
                        "execution bookkeeping failed: {error:#}; reconcile required"
                    ));
                } else {
                    self.fail(i, format!("execution preflight failed: {error:#}"));
                }
                self.save()?;
                return Ok(());
            }
            if self.journal.entries[i].state != State::Completed {
                return Ok(());
            }
        }
        Ok(())
    }
    fn decide(
        &mut self,
        stage: Stage,
        action: Action,
        reviewer: String,
        reason: String,
    ) -> Result<()> {
        ensure!(
            self.journal.frozen.plan.mode == Mode::Expert,
            "decide only applies to expert mode"
        );
        ensure!(
            nonblank(&reviewer) && nonblank(&reason),
            "reviewer/reason required"
        );
        let i = Stage::all()
            .iter()
            .position(|s| *s == stage)
            .context("unknown stage")?;
        ensure!(
            matches!(
                self.journal.entries[i].state,
                State::Pending | State::Deferred
            ),
            "stage already decided or started"
        );
        ensure!(
            self.journal.entries[i].request.is_some(),
            "stage not currently proposed; resume after previous completion"
        );
        self.verify()?;
        let entry = &mut self.journal.entries[i];
        entry.decisions.push(Decision {
            action,
            reviewer,
            reason,
            request_sha256: entry
                .request_sha256
                .clone()
                .context("missing request SHA")?,
            recorded_at: now(),
        });
        entry.state = match action {
            Action::Accept => State::Ready,
            Action::Reject => State::Rejected,
            Action::Defer => State::Deferred,
        };
        self.save()
    }
    fn reconcile(
        &mut self,
        stage: Stage,
        receipt: &Path,
        reviewer: String,
        reason: String,
        workers_stopped: bool,
    ) -> Result<()> {
        ensure!(
            nonblank(&reviewer) && nonblank(&reason),
            "reviewer/reason required"
        );
        ensure!(
            workers_stopped,
            "reconcile requires --workers-stopped after operator checks all driver/external tasks"
        );
        let i = Stage::all()
            .iter()
            .position(|s| *s == stage)
            .context("unknown stage")?;
        ensure!(
            matches!(
                self.journal.entries[i].state,
                State::InFlight | State::Unknown
            ),
            "only uncertain stages can be reconciled"
        );
        self.verify()?;
        #[cfg(unix)]
        if let Some(pid) = self.journal.entries[i].driver_pid {
            let alive = unsafe { libc::kill(-(pid as i32), 0) };
            ensure!(alive != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH), "recorded driver process group still alive or cannot be checked; cannot reconcile while it runs");
        }
        self.capture_evidence(
            i,
            receipt,
            Some(Reconciliation {
                reviewer,
                reason,
                recorded_at: now(),
                workers_stopped_attested: true,
            }),
        )?;
        self.save()
    }
}

pub fn run(command: PipelineCommand) -> Result<()> {
    let pipeline = match command {
        PipelineCommand::Start { plan, out } => {
            let mut p = Pipeline::start(&plan, &out)?;
            p.run()?;
            p
        }
        PipelineCommand::Resume { out } => {
            let mut p = Pipeline::open(&out)?;
            p.run()?;
            p
        }
        PipelineCommand::Status { out } => Pipeline::open(&out)?,
        PipelineCommand::Decide {
            out,
            stage,
            action,
            reviewer,
            reason,
        } => {
            let mut p = Pipeline::open(&out)?;
            p.decide(stage, action, reviewer, reason)?;
            p
        }
        PipelineCommand::Reconcile {
            out,
            stage,
            receipt,
            reviewer,
            reason,
            workers_stopped,
        } => {
            let mut p = Pipeline::open(&out)?;
            p.reconcile(stage, &receipt, reviewer, reason, workers_stopped)?;
            p
        }
    };
    println!("{}", serde_json::to_string_pretty(&pipeline.journal)?);
    if pipeline.journal.entries.iter().any(|e| {
        matches!(
            e.state,
            State::Failed | State::Rejected | State::Unknown | State::InFlight
        )
    }) {
        bail!("pipeline blocked; inspect persisted negative/uncertain stage evidence");
    }
    Ok(())
}
