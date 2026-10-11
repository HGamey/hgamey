//! Durable experiment journal shared by both execution paths. Review authorizes
//! a specific candidate/request, never a future generation or a statistical win.
use anyhow::{bail, Context, Result};
use clap::ValueEnum;
use fs2::FileExt;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Autonomous,
    Expert,
}

#[derive(Debug, Clone)]
pub struct Options {
    pub mode: Mode,
    pub resume: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Accept,
    Reject,
    Defer,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Decision {
    pub action: Action,
    pub reviewer: String,
    pub reason: String,
    pub binding_sha256: String,
    pub recorded_at: String,
    pub expert: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Assessment {
    pub hypothesis: String,
    /// Qualitative estimates; these are not claimed measured improvements.
    pub expected_benefit: String,
    pub risks: Vec<String>,
    pub estimated_device_seconds: Option<u64>,
    pub cost_basis: String,
    pub previous_evidence: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Artifact {
    pub path: PathBuf,
    pub sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Pending,
    Deferred,
    Rejected,
    Ready,
    InFlight,
    Completed,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub id: String,
    pub generation: u32,
    pub layer: String,
    pub candidate: Value,
    pub request: Value,
    pub artifacts: Vec<Artifact>,
    pub assessment: Assessment,
    pub binding_sha256: String,
    pub decisions: Vec<Decision>,
    pub state: State,
    pub result: Option<Value>,
    pub result_sha256: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Snapshot {
    value: Value,
    sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Journal {
    pub schema: String,
    pub mode: Mode,
    pub created_at: String,
    pub frozen: Value,
    pub frozen_sha256: String,
    pub entries: BTreeMap<String, Entry>,
    snapshots: BTreeMap<String, Snapshot>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Summary {
    pub mode: Mode,
    pub journal: String,
    pub counts: BTreeMap<String, usize>,
    pub waiting_for_review: bool,
    pub expert_reviewed: usize,
}

pub enum Disposition<T> {
    Pending,
    Deferred,
    Rejected,
    Completed(T),
    Failed(String),
}

pub struct Campaign {
    path: PathBuf,
    pub journal: Journal,
    _lock: File,
}

pub fn digest<T: Serialize>(value: &T) -> Result<String> {
    let canonical = serde_json::to_value(value)?;
    Ok(bytes_digest(&serde_json::to_vec(&canonical)?))
}
fn bytes_digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn artifacts(paths: &[PathBuf]) -> Result<Vec<Artifact>> {
    let mut files = Vec::new();
    fn collect(path: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
        if path.is_dir() {
            let mut children: Vec<_> =
                std::fs::read_dir(path)?.collect::<std::io::Result<Vec<_>>>()?;
            children.sort_by_key(|e| e.path());
            for child in children {
                collect(&child.path(), out)?;
            }
        } else {
            out.push(
                path.canonicalize()
                    .with_context(|| format!("输入工件不存在: {}", path.display()))?,
            );
        }
        Ok(())
    }
    for path in paths {
        collect(path, &mut files)?;
    }
    files.sort();
    files.dedup();
    files
        .into_iter()
        .map(|path| {
            let sha256 = bytes_digest(&std::fs::read(&path)?);
            Ok(Artifact { path, sha256 })
        })
        .collect()
}
fn binding(frozen: &str, entry: &Entry) -> Result<String> {
    digest(&(
        frozen,
        &entry.id,
        entry.generation,
        &entry.layer,
        &entry.candidate,
        &entry.request,
        &entry.artifacts,
        &entry.assessment,
    ))
}

impl Campaign {
    fn lock(dir: &Path) -> Result<File> {
        std::fs::create_dir_all(dir)?;
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(dir.join("campaign.lock"))?;
        file.try_lock_exclusive()
            .context("campaign 正被另一进程使用，不能并发评测或决定")?;
        Ok(file)
    }

    pub fn open(dir: &Path, options: &Options, frozen: Value) -> Result<Self> {
        let lock = Self::lock(dir)?;
        let path = dir.join("campaign.json");
        let frozen_sha256 = digest(&frozen)?;
        let journal = if options.resume {
            let journal: Journal = serde_json::from_slice(
                &std::fs::read(&path)
                    .context("--resume 找不到 campaign.json；不能猜测既有实验的条件")?,
            )?;
            if journal.mode != options.mode || journal.frozen_sha256 != frozen_sha256 {
                bail!("campaign 模式/候选生成配置/冻结条件改变；拒绝恢复，请使用新 --out");
            }
            journal
        } else {
            if path.exists() {
                bail!("campaign 已存在；保持相同参数并加 --resume，或使用新 --out");
            }
            Journal {
                schema: "game_opt_loop/campaign@1".into(),
                mode: options.mode,
                created_at: crate::engine::evolution::now(),
                frozen,
                frozen_sha256,
                entries: BTreeMap::new(),
                snapshots: BTreeMap::new(),
            }
        };
        let campaign = Self {
            path,
            journal,
            _lock: lock,
        };
        campaign.verify()?;
        if let Some(entry) = campaign
            .journal
            .entries
            .values()
            .find(|e| e.state == State::InFlight)
        {
            bail!(
                "{}: in_flight 评测回执未知，不会重复下发；核对证据后用 campaign reconcile",
                entry.id
            );
        }
        campaign.save()?;
        Ok(campaign)
    }

    pub fn inspect(dir: &Path) -> Result<Self> {
        let lock = Self::lock(dir)?;
        let path = dir.join("campaign.json");
        let journal = serde_json::from_slice(&std::fs::read(&path)?)?;
        let campaign = Self {
            path,
            journal,
            _lock: lock,
        };
        campaign.verify()?;
        Ok(campaign)
    }

    pub fn verify(&self) -> Result<()> {
        if self.journal.schema != "game_opt_loop/campaign@1"
            || digest(&self.journal.frozen)? != self.journal.frozen_sha256
        {
            bail!("campaign 冻结内容被篡改或版本不支持");
        }
        for (key, snapshot) in &self.journal.snapshots {
            if digest(&snapshot.value)? != snapshot.sha256 {
                bail!("campaign 候选批次/冻结快照 {key} 被篡改");
            }
        }
        for (id, entry) in &self.journal.entries {
            if id != &entry.id
                || binding(&self.journal.frozen_sha256, entry)? != entry.binding_sha256
            {
                bail!("{id}: 候选/请求/评估摘要改变，审批绑定失效");
            }
            for artifact in &entry.artifacts {
                if bytes_digest(
                    &std::fs::read(&artifact.path)
                        .with_context(|| format!("{id}: 工件丢失 {}", artifact.path.display()))?,
                ) != artifact.sha256
                {
                    bail!("{id}: 工件 {} 改变，审批绑定失效", artifact.path.display());
                }
            }
            for decision in &entry.decisions {
                if decision.binding_sha256 != entry.binding_sha256 {
                    bail!("{id}: 决定绑定了不同候选/协议");
                }
            }
            if let Some(result) = &entry.result {
                if entry.result_sha256.as_deref() != Some(digest(result)?.as_str()) {
                    bail!("{id}: 缓存评测报告被篡改");
                }
            }
        }
        Ok(())
    }

    pub fn snapshot<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>> {
        self.journal
            .snapshots
            .get(key)
            .map(|s| serde_json::from_value(s.value.clone()).map_err(Into::into))
            .transpose()
    }
    pub fn store_snapshot<T: Serialize>(&mut self, key: &str, value: &T) -> Result<()> {
        let value = serde_json::to_value(value)?;
        if let Some(old) = self.journal.snapshots.get(key) {
            if old.value != value {
                bail!("冻结快照 {key} 已存在，不能覆盖");
            }
            return Ok(());
        }
        self.journal.snapshots.insert(
            key.into(),
            Snapshot {
                sha256: digest(&value)?,
                value,
            },
        );
        self.save()
    }

    #[allow(clippy::too_many_arguments)]
    pub fn stage(
        &mut self,
        id: &str,
        generation: u32,
        layer: &str,
        candidate: Value,
        request: Value,
        paths: &[PathBuf],
        assessment: Assessment,
    ) -> Result<()> {
        // An existing entry is authoritative. Callers load its exact request and
        // batch on resume, rather than regenerating from a now larger archive.
        if let Some(old) = self.journal.entries.get(id) {
            if old.candidate != candidate || old.request != request {
                bail!("{id}: 恢复候选或冻结请求改变");
            }
            return Ok(());
        }
        let mut entry = Entry {
            id: id.into(),
            generation,
            layer: layer.into(),
            candidate,
            request,
            artifacts: artifacts(paths)?,
            assessment,
            binding_sha256: String::new(),
            decisions: vec![],
            state: State::Pending,
            result: None,
            result_sha256: None,
            error: None,
        };
        entry.binding_sha256 = binding(&self.journal.frozen_sha256, &entry)?;
        if self.journal.mode == Mode::Autonomous {
            entry.decisions.push(Decision {
                action: Action::Accept,
                reviewer: "autonomous policy".into(),
                reason: "自主模式：通过本地合法性门禁后允许评测，未经专家审查".into(),
                binding_sha256: entry.binding_sha256.clone(),
                recorded_at: crate::engine::evolution::now(),
                expert: false,
            });
            entry.state = State::Ready;
        }
        self.journal.entries.insert(id.into(), entry);
        self.save()
    }

    pub fn decide(
        &mut self,
        ids: &[String],
        action: Action,
        reviewer: &str,
        reason: &str,
    ) -> Result<()> {
        if self.journal.mode != Mode::Expert {
            bail!("autonomous campaign 不可伪造专家审批；新建 expert campaign");
        }
        if reviewer.trim().is_empty() || reason.trim().is_empty() {
            bail!("reviewer 与 reason 必须非空");
        }
        self.verify()?;
        for id in ids {
            let entry = self
                .journal
                .entries
                .get(id)
                .with_context(|| format!("不存在候选 {id}"))?;
            if matches!(
                entry.state,
                State::InFlight | State::Completed | State::Failed
            ) {
                bail!("{id} 已下发评测，不能事后改专家决定");
            }
        }
        for id in ids {
            let entry = self.journal.entries.get_mut(id).unwrap();
            entry.decisions.push(Decision {
                action,
                reviewer: reviewer.into(),
                reason: reason.into(),
                binding_sha256: entry.binding_sha256.clone(),
                recorded_at: crate::engine::evolution::now(),
                expert: true,
            });
            entry.state = match action {
                Action::Accept => State::Ready,
                Action::Reject => State::Rejected,
                Action::Defer => State::Deferred,
            };
        }
        self.save()
    }

    pub fn request<T: DeserializeOwned>(&self, id: &str) -> Result<T> {
        serde_json::from_value(
            self.journal
                .entries
                .get(id)
                .context("缺少待评测候选")?
                .request
                .clone(),
        )
        .map_err(Into::into)
    }

    pub fn execute<T: Serialize + DeserializeOwned>(
        &mut self,
        id: &str,
        f: impl FnOnce() -> Result<T>,
    ) -> Result<Disposition<T>> {
        self.verify()?;
        let entry = self.journal.entries.get_mut(id).context("缺少待评测候选")?;
        match entry.state {
            State::Pending => return Ok(Disposition::Pending),
            State::Deferred => return Ok(Disposition::Deferred),
            State::Rejected => return Ok(Disposition::Rejected),
            State::Completed => return Ok(Disposition::Completed(serde_json::from_value(entry.result.clone().context("completed 缺报告")?)?)),
            State::Failed => return Ok(Disposition::Failed(entry.error.clone().unwrap_or_default())),
            State::InFlight => bail!("{id}: 上次下发后未取得可靠回执 (in_flight)；不会重复评测。核对 phonefarm 证据后用 campaign reconcile"),
            State::Ready => {
                let accepted = entry.decisions.last().is_some_and(|d| d.action == Action::Accept &&
                    (self.journal.mode == Mode::Autonomous || d.expert));
                if !accepted { bail!("{id}: ready 没有与模式匹配的接受决定"); }
            }
        }
        entry.state = State::InFlight;
        self.save()?;
        let result = f();
        let entry = self.journal.entries.get_mut(id).unwrap();
        match result {
            Ok(report) => {
                let value = serde_json::to_value(&report)?;
                entry.result_sha256 = Some(digest(&value)?);
                entry.result = Some(value);
                entry.state = State::Completed;
                self.save()?;
                Ok(Disposition::Completed(report))
            }
            Err(error) => {
                let error = format!("{error:#}");
                entry.error = Some(error.clone());
                entry.state = State::Failed;
                self.save()?;
                Ok(Disposition::Failed(error))
            }
        }
    }

    /// Recover a receipt, never re-dispatch an uncertain operation.
    pub fn reconcile(
        &mut self,
        id: &str,
        report: Value,
        reviewer: &str,
        reason: &str,
    ) -> Result<()> {
        if reviewer.trim().is_empty() || reason.trim().is_empty() {
            bail!("核对人和理由必须非空");
        }
        if report["candidate_id"].as_str() != Some(id)
            || report["source"].as_str() != Some("phonefarm")
        {
            bail!("核对报告必须匹配 candidate_id 且来源为 phonefarm");
        }
        let entry = self.journal.entries.get_mut(id).context("候选不存在")?;
        if entry.state != State::InFlight {
            bail!("只允许核对 in_flight 候选");
        }
        if entry.layer == "shader" {
            let _: crate::engine::EvalReport =
                serde_json::from_value(report.clone()).context("shader 回执字段不满足报告契约")?;
        } else {
            let r: crate::optimize::eval::Report =
                serde_json::from_value(report.clone()).context("优化回执字段不满足报告契约")?;
            if r.kind.is_none_or(|k| k.id() != entry.layer) {
                bail!("核对回执的kind必须匹配已下发路线");
            }
        }
        entry.error = Some(format!("人工回执核对: {reviewer}: {reason}"));
        entry.result_sha256 = Some(digest(&report)?);
        entry.result = Some(report);
        entry.state = State::Completed;
        self.save()
    }

    pub fn summary(&self) -> Summary {
        let mut counts = BTreeMap::new();
        for entry in self.journal.entries.values() {
            let key = serde_json::to_value(entry.state)
                .unwrap()
                .as_str()
                .unwrap()
                .to_string();
            *counts.entry(key).or_insert(0) += 1;
        }
        Summary {
            mode: self.journal.mode,
            journal: self.path.display().to_string(),
            waiting_for_review: self
                .journal
                .entries
                .values()
                .any(|e| matches!(e.state, State::Pending | State::Deferred)),
            expert_reviewed: self
                .journal
                .entries
                .values()
                .filter(|e| e.decisions.iter().any(|d| d.expert))
                .count(),
            counts,
        }
    }

    fn save(&self) -> Result<()> {
        atomic_write(&self.path, &serde_json::to_vec_pretty(&self.journal)?)?;
        let mut md = format!(
            "# Experiment campaign\n\n模式：{:?}。{}\n\n冻结配置 SHA-256：`{}`\n\n",
            self.journal.mode,
            if self.journal.mode == Mode::Expert {
                "只有明确接受的候选可以评测。"
            } else {
                "自主策略运行，未经专家审查。"
            },
            self.journal.frozen_sha256
        );
        for entry in self.journal.entries.values() {
            md.push_str(&format!("## {} · {} · 第 {} 代 · {:?}\n\n假设：{}\n\n预期收益（估计）：{}\n\n风险：{}\n\n成本（估计）：{:?} 秒设备时间；{}\n\n审批绑定：`{}`\n\n", entry.id, entry.layer, entry.generation, entry.state,
                entry.assessment.hypothesis, entry.assessment.expected_benefit, entry.assessment.risks.join("；"), entry.assessment.estimated_device_seconds,
                entry.assessment.cost_basis, entry.binding_sha256));
            for d in &entry.decisions {
                md.push_str(&format!(
                    "- {:?} · {} · {} · {}\n",
                    d.action, d.reviewer, d.recorded_at, d.reason
                ));
            }
            md.push('\n');
        }
        atomic_write(&self.path.with_extension("md"), md.as_bytes())
    }
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, bytes)?;
    File::open(&tmp)?.sync_all()?;
    std::fs::rename(&tmp, path)?;
    // Persist the rename before an external evaluator can receive a request.
    // Directory handles are not portable to Windows; its process-restart
    // guarantee remains atomic replacement, without claiming power-loss safety.
    #[cfg(unix)]
    File::open(path.parent().context("账本缺少输出目录")?)?.sync_all()?;
    Ok(())
}
