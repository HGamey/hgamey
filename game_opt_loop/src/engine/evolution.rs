//! 多代自主演化闭环
//!
//! 一代的状态机 (与 phonefarm `SPEC_SR_LOOP` §5.1 同构):
//!
//! ```text
//!   选亲本 (Pareto 前沿 + 拥挤距离)
//!        ↓
//!   变异 (LLM 优先, 失败逐个降级到本地确定性变异器)
//!        ↓
//!   去重 (指纹, 同构算子不重复占真机名额)
//!        ↓
//!   防爆门禁 (语法 / 零拷贝契约 / 循环有界 / SPIR-V)   ← 打回的不上真机
//!        ↓
//!   评测 (真机 phonefarm  或  dry-run 静态成本模型)
//!        ↓
//!   归档 (Pareto 支配排序, 原子写)
//! ```
//!
//! 两条贯穿始终的规矩:
//! - **单个候选出错不中断整代**。LLM 空回包、某个算子编不过、某次真机崩了,
//!   都只让那一个候选作废。整代靠本地变异器兜底推进 —— 零人工介入的底线。
//! - **估计值与实测值永不混淆**。每条记录都带 `MetricSource`, 汇总先按来源分组。

use crate::engine::archive::ParetoArchive;
use crate::engine::cost_model::{self, DeviceModel, NX809J_1080P};
use crate::engine::generator::{EliteSummary, Generator, MutationContext, RejectionSummary};
use crate::engine::mutate::{Genome, Rng};
use crate::engine::validator::{GateReport, Validator, ViolationCode};
use crate::engine::{Candidate, CandidateOrigin, EvalReport, MetricSource};
use crate::harness::PhonefarmDriver;
use crate::tracks::{self, TrackSpec};
use crate::TrackMode;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// 演化的运行参数。
#[derive(Debug, Clone)]
pub struct EvolutionConfig {
    pub track: TrackMode,
    pub generations: u32,
    pub children: u32,
    pub budget_ms: f32,
    pub out_dir: PathBuf,
    /// true = 不连真机, 用静态成本模型估分。
    pub dry_run: bool,
    /// 随机种子。固定种子 → 同样的 dry-run 结果, 归档可复现。
    pub seed: u64,
    /// LLM 服务名。None = 按密钥可用性自动挑。
    pub prefer_provider: Option<String>,
    /// secrets.env 路径。
    pub secrets_path: PathBuf,
    /// 是否启用 LLM 变异通道。false = 只用本地确定性变异器。
    pub use_llm: bool,
    /// LLM 单次请求超时 (秒)。None = 用服务表里的默认值。
    pub llm_timeout_s: Option<u64>,
    /// 设备成本模型 (dry-run 用)。
    pub device: DeviceModel,
    /// 真机评测载体: refbench (真实渲染场景) 或 headless (孤立工装)。
    pub carrier: String,
    /// refbench 每臂帧数与场景负载强度。
    pub scene_frames: u32,
    pub scene_intensity: u32,
    /// 冷机门槛 (摄氏度)。见 CLI --cool-c 的说明。
    pub cool_c: f32,
    /// 画质真值: 真机截帧目录 (或单张 PNG)。None = 接收端用程序化图案。
    /// 见 `harness::EvalRequest::quality_reference`。
    pub quality_reference: Option<String>,
}

impl EvolutionConfig {
    pub fn spec(&self) -> &'static TrackSpec {
        tracks::spec(self.track)
    }
}

/// 一个候选走完整条流水线之后的结局。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateOutcome {
    pub id: String,
    pub origin: CandidateOrigin,
    pub fingerprint: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
    pub stage: OutcomeStage,
    /// 门禁结论 (被门禁打回时必定存在)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gate: Option<GateReport>,
    /// 评测结果 (上了机/过了成本模型才有)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub report: Option<EvalReport>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutcomeStage {
    /// 指纹与既有算子重复, 没必要再评一次。
    DuplicateSkipped,
    /// 被本地防爆门禁打回, 从未上真机。
    GateRejected,
    /// 评测过程本身出错 (真机掉线 / 回包不合契约)。
    EvalError,
    /// 走完全程。
    Evaluated,
}

/// 一代的完整记录, 落盘为 `gen_N.json`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GenerationRecord {
    pub generation: u32,
    pub track: String,
    pub started_at: String,
    /// 本代子代从哪来的统计。
    pub origin_counts: BTreeMap<String, usize>,
    /// 门禁拦截统计: 违规码 → 次数。
    pub gate_rejections: BTreeMap<String, usize>,
    pub outcomes: Vec<CandidateOutcome>,
    /// 本代评测数据的来源。
    pub metric_source: MetricSource,
    /// 归档中可行域内的历史最优 PSNR。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_psnr_so_far: Option<f32>,
    /// 本代最优 PSNR。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gen_best_psnr: Option<f32>,
    /// 归档中可行域内的历史最低延迟。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub best_latency_so_far: Option<f32>,
    pub front_size: usize,
    /// LLM 这一代的情况。
    pub llm: LlmStatus,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmStatus {
    pub attempted: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    pub produced: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// 整轮演化的汇总。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionSummary {
    pub track: String,
    pub generations: u32,
    pub children_per_generation: u32,
    pub seed: u64,
    pub dry_run: bool,
    pub metric_source: MetricSource,
    pub total_candidates: usize,
    pub gate_rejected: usize,
    pub duplicates_skipped: usize,
    pub eval_errors: usize,
    pub evaluated: usize,
    /// 门禁拦截率 = 打回数 / 送检数。
    pub gate_rejection_rate: f64,
    pub gate_rejections_by_code: BTreeMap<String, usize>,
    pub archive_size: usize,
    pub front_size: usize,
    /// 逐代的累计最优 PSNR 序列 (Gate 4 那条曲线)。
    pub best_psnr_series: Vec<Option<f32>>,
    pub gen_best_psnr_series: Vec<Option<f32>>,
    pub archive_path: String,
    pub generation_records: Vec<String>,
    /// 本轮画质真值是什么 —— 从接收端实际回报的来源汇总, 不是从配置里抄。
    ///
    /// 配置里写了 `--quality-reference` 不等于真的用上了: 路径写错、接收端是
    /// 旧版本、画质补测被关掉, 都会让最终的 dB 仍然来自程序化图案。
    /// 只认报告里回来的那个来源。
    pub quality_reference: QualityReferenceSummary,
}

/// 整轮演化里画质真值来源的汇总。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct QualityReferenceSummary {
    /// 配置里要求的来源 (CLI `--quality-reference`)。None = 没要求。
    pub requested: Option<String>,
    /// 接收端实际报回来的来源描述, 去重后按出现顺序。
    ///
    /// 正常情况下只有一项。多于一项说明同一轮里换过真值 ——
    /// 那批 dB 互相之间不可比, 必须看得见。
    pub observed: Vec<String>,
    /// 有多少份报告的画质真值是真实游戏帧。
    pub real_frame_reports: usize,
    /// 有多少份报告的画质真值是 runner 内置程序化图案。
    pub procedural_reports: usize,
    /// 有多少份报告报了本仓库不认识的 `kind`。
    ///
    /// 单独数而不是并进 procedural: `kind` 是接收端传过来的字符串,
    /// 把不认识的值当成程序化图案, 等于替接收端瞎猜, 还会误报告警。
    pub unknown_kind_reports: usize,
}

/// 从一批报告里汇总画质真值的来源。
///
/// 只数**接收端报回来的**来源, 不数配置里要求的: 路径写错、接收端是旧版本、
/// 画质补测被关掉, 都会让最终的 dB 仍然来自程序化图案, 而配置那一行照样写着
/// 一个漂亮的目录名。
///
/// 没报来源的报告 (成本模型估计值, 或旧版接收端) 两边都不计 —— 它既不是
/// 真实帧也不能断言是程序化图案。
pub fn summarise_quality_reference<'a>(
    requested: Option<String>,
    reports: impl Iterator<Item = &'a EvalReport>,
) -> QualityReferenceSummary {
    let mut s = QualityReferenceSummary {
        requested,
        ..Default::default()
    };
    for r in reports {
        let Some(q) = r.quality_reference.as_ref() else {
            continue;
        };
        if q.is_real_frames() {
            s.real_frame_reports += 1;
        } else if q.is_procedural() {
            s.procedural_reports += 1;
        } else {
            s.unknown_kind_reports += 1;
        }
        let d = q.describe();
        if !s.observed.contains(&d) {
            s.observed.push(d);
        }
    }
    s
}

/// 换了画质真值就不能接着用旧档案。
///
/// 档案是**追加**的, 跨轮复用; `objectives()` 与 `best_psnr()` 直接比 `psnr_db`,
/// 不看它是对着哪张参考图量的。一旦混进两种真值, 程序化图案那批 (42.8 dB)
/// 会永久支配真机截帧那批 (31.3 dB) —— 不是因为它们更清晰, 而是因为量它们的
/// 尺子不一样。前沿与亲本池就此被钉死在旧数据上, 而每一条记录单看都是对的。
///
/// 这跟 `MetricSource` 是同一类问题 (估计值不能和实测值同台比较), 处理也该同一档:
/// 当场拒绝, 让人换一个 `--out` 目录, 而不是给一个悄悄错掉的前沿。
pub fn check_archive_quality_reference(
    archive: &ParetoArchive,
    requested: Option<&str>,
) -> Result<()> {
    // 档案里已有记录用的是哪些真值 (没报来源的记录不参与判断: 成本模型估计值
    // 与旧版接收端都属于这一类, 它们没声称过自己对着什么量)。
    let mut seen: Vec<String> = Vec::new();
    for e in &archive.entries {
        if let Some(q) = e.report.quality_reference.as_ref() {
            let key = if q.is_real_frames() {
                q.source.clone().unwrap_or_else(|| "real_frames".into())
            } else {
                q.kind.clone()
            };
            if !seen.contains(&key) {
                seen.push(key);
            }
        }
    }
    if seen.is_empty() {
        return Ok(());
    }
    let want = requested.unwrap_or("procedural").to_string();
    if seen.len() == 1 && seen[0] == want {
        return Ok(());
    }
    anyhow::bail!(
        "里已有的记录是对着 {} 量的画质, 本次要用 {} —— 绝对 dB 不可跨参考图比较, \
         混在同一个 Pareto 前沿里, 旧记录会仅仅因为尺子不同就永久支配新记录。\n\
         换一个 --out 目录重新开始, 或把画质真值改回原来那个。",
        seen.join(" / "),
        want
    );
}

/// 演化引擎。
pub struct Evolution {
    config: EvolutionConfig,
    validator: Validator,
    archive: ParetoArchive,
    archive_path: PathBuf,
    rng: Rng,
    /// 候选 id → 基因组。只有本地变异产物有基因组 (LLM 产物只有源码),
    /// 所以这张表是「档案条目」到「可继续变异的亲本」的索引。
    genome_pool: BTreeMap<String, Genome>,
    driver: Option<PhonefarmDriver>,
    generator: Option<Generator>,
    llm_error: Option<String>,
}

impl Evolution {
    pub fn new(config: EvolutionConfig) -> Result<Self> {
        let spec = config.spec();
        let archive_path = config.out_dir.join("archive.json");
        std::fs::create_dir_all(&config.out_dir)
            .with_context(|| format!("创建输出目录失败: {}", config.out_dir.display()))?;

        let archive = ParetoArchive::load_or_new(
            &archive_path,
            spec.id,
            config.budget_ms,
            spec.quality_baseline_db,
        );
        check_archive_quality_reference(&archive, config.quality_reference.as_deref())
            .with_context(|| format!("档案 {}", archive_path.display()))?;

        // LLM 是尽力而为: 拿不到就记下原因, 整代照样用本地变异器推进。
        let (generator, llm_error) = if config.use_llm {
            let secrets = crate::engine::generator::load_secrets(&config.secrets_path);
            match Generator::from_secrets_with_timeout(
                &secrets,
                config.prefer_provider.as_deref(),
                config.llm_timeout_s,
            ) {
                Ok(g) => (Some(g), None),
                Err(e) => (None, Some(format!("{e:#}"))),
            }
        } else {
            (None, None)
        };

        let driver = if config.dry_run {
            None
        } else {
            Some(
                PhonefarmDriver::new()
                    .with_carrier(config.carrier.clone())
                    .with_scene(config.scene_frames, config.scene_intensity)
                    .with_cool_c(config.cool_c)
                    .with_quality_reference(config.quality_reference.clone()),
            )
        };

        Ok(Self {
            rng: Rng::new(config.seed),
            genome_pool: BTreeMap::new(),
            validator: Validator::new(),
            archive,
            archive_path,
            driver,
            generator,
            llm_error,
            config,
        })
    }

    pub fn archive(&self) -> &ParetoArchive {
        &self.archive
    }

    pub fn archive_path(&self) -> &Path {
        &self.archive_path
    }

    pub fn llm_available(&self) -> bool {
        self.generator.is_some()
    }

    pub fn llm_error(&self) -> Option<&str> {
        self.llm_error.as_deref()
    }

    /// 跑完全部代际。
    pub fn run(&mut self) -> Result<EvolutionSummary> {
        let spec = self.config.spec();
        let mut records = Vec::new();
        let mut record_paths = Vec::new();
        let mut best_series = Vec::new();
        let mut gen_best_series = Vec::new();
        let mut last_rejections: Vec<RejectionSummary> = Vec::new();

        // Gen 0 是基线: 先把模板本身评一次, 后面的代才有参照。
        self.seed_baseline()?;

        for gen in 1..=self.config.generations {
            let (record, rejections) = self.run_generation(gen, &last_rejections)?;
            last_rejections = rejections;

            let path = self.config.out_dir.join(format!("gen_{gen}.json"));
            std::fs::write(&path, serde_json::to_string_pretty(&record)?)
                .with_context(|| format!("写入 {} 失败", path.display()))?;
            record_paths.push(path.display().to_string());

            best_series.push(record.best_psnr_so_far);
            gen_best_series.push(record.gen_best_psnr);
            records.push(record);

            self.archive.save(&self.archive_path)?;
        }

        let metric_source = if self.config.dry_run {
            MetricSource::StaticCostModel
        } else {
            MetricSource::Phonefarm
        };

        let mut gate_rejections_by_code: BTreeMap<String, usize> = BTreeMap::new();
        let (mut gate_rejected, mut duplicates, mut errors, mut evaluated) = (0, 0, 0, 0);
        for r in &records {
            for (code, n) in &r.gate_rejections {
                *gate_rejections_by_code.entry(code.clone()).or_insert(0) += n;
            }
            for o in &r.outcomes {
                match o.stage {
                    OutcomeStage::GateRejected => gate_rejected += 1,
                    OutcomeStage::DuplicateSkipped => duplicates += 1,
                    OutcomeStage::EvalError => errors += 1,
                    OutcomeStage::Evaluated => evaluated += 1,
                }
            }
        }
        let total: usize = records.iter().map(|r| r.outcomes.len()).sum();
        // 拦截率只在「真正送检的」里面算: 指纹重复的根本没进门禁。
        let screened = total - duplicates;

        Ok(EvolutionSummary {
            track: spec.id.to_string(),
            generations: self.config.generations,
            children_per_generation: self.config.children,
            seed: self.config.seed,
            dry_run: self.config.dry_run,
            metric_source,
            total_candidates: total,
            gate_rejected,
            duplicates_skipped: duplicates,
            eval_errors: errors,
            evaluated,
            gate_rejection_rate: if screened == 0 {
                0.0
            } else {
                gate_rejected as f64 / screened as f64
            },
            gate_rejections_by_code,
            archive_size: self.archive.entries.len(),
            front_size: self.archive.front().len(),
            best_psnr_series: best_series,
            gen_best_psnr_series: gen_best_series,
            archive_path: self.archive_path.display().to_string(),
            generation_records: record_paths,
            // 只数**本轮**的报告, 不数整个档案: 档案是跨轮追加的, 上一轮用真机
            // 参考帧跑过的记录会让这一轮的 real_frame_reports 凭空变正,
            // 把"要了真实帧却一份都没用上"那条告警压掉 —— 那条告警正是这项汇总
            // 存在的理由。
            quality_reference: summarise_quality_reference(
                self.config.quality_reference.clone(),
                records
                    .iter()
                    .flat_map(|r| r.outcomes.iter())
                    .filter_map(|o| o.report.as_ref()),
            ),
        })
    }

    /// 把赛道基线模板作为 Gen 0 收进档案。
    fn seed_baseline(&mut self) -> Result<()> {
        if !self.archive.entries.is_empty() {
            return Ok(());
        }
        let spec = self.config.spec();
        let source = tracks::load_template(self.config.track)?;
        let candidate = Candidate::new(
            "gen0_baseline",
            0,
            spec.id,
            None,
            self.config.budget_ms,
            source,
            CandidateOrigin::Baseline,
            Some("赛道基线模板".to_string()),
        );
        let gate = self.validator.gate(&candidate.shader_source, spec);
        if !gate.passed {
            anyhow::bail!("赛道基线模板自身过不了门禁: {}", gate.feedback_line());
        }
        // 基线自己也要落盘: 它既是 gen0 的档案条目, 也是真机 A/B 的 A 臂。
        self.persist_candidate(&candidate, &gate)?;
        self.persist_baseline_spirv(&gate)?;

        if let Ok(report) = self.evaluate(&candidate, &gate, &Genome::baseline(self.config.track)) {
            self.archive.insert(&candidate, report, now());
        }
        self.archive.save(&self.archive_path)?;
        Ok(())
    }

    /// 跑一代。
    fn run_generation(
        &mut self,
        generation: u32,
        prev_rejections: &[RejectionSummary],
    ) -> Result<(GenerationRecord, Vec<RejectionSummary>)> {
        let spec = self.config.spec();
        let started_at = now();

        // ---- 变异 ----
        let (mut batch, llm) = self.breed(generation, prev_rejections);

        // ---- 逐个过流水线 ----
        let mut outcomes = Vec::new();
        let mut gate_rejections: BTreeMap<String, usize> = BTreeMap::new();
        let mut rejections = Vec::new();
        let mut gen_best_psnr: Option<f32> = None;

        for (candidate, genome) in batch.drain(..) {
            if self.archive.contains_fingerprint(&candidate.fingerprint) {
                outcomes.push(CandidateOutcome {
                    id: candidate.id.clone(),
                    origin: candidate.origin,
                    fingerprint: candidate.fingerprint.clone(),
                    parent_id: candidate.parent_id.clone(),
                    notes: candidate.notes.clone(),
                    stage: OutcomeStage::DuplicateSkipped,
                    gate: None,
                    report: None,
                    error: None,
                });
                continue;
            }

            let gate = self.validator.gate(&candidate.shader_source, spec);
            if !gate.passed {
                for code in gate.codes() {
                    *gate_rejections.entry(code.to_string()).or_insert(0) += 1;
                }
                rejections.push(RejectionSummary {
                    id: candidate.id.clone(),
                    reason: gate.feedback_line(),
                });
                outcomes.push(CandidateOutcome {
                    id: candidate.id.clone(),
                    origin: candidate.origin,
                    fingerprint: candidate.fingerprint.clone(),
                    parent_id: candidate.parent_id.clone(),
                    notes: candidate.notes.clone(),
                    stage: OutcomeStage::GateRejected,
                    gate: Some(gate),
                    report: None,
                    error: None,
                });
                continue;
            }

            // 门禁过了才写盘 —— 非法算子连落盘的资格都没有。
            self.persist_candidate(&candidate, &gate)?;

            match self.evaluate(&candidate, &gate, &genome) {
                Ok(report) => {
                    if report.is_admissible(spec.budget_ms, spec.quality_baseline_db) {
                        if let Some(psnr) = report.metrics.psnr_db {
                            gen_best_psnr = Some(gen_best_psnr.map_or(psnr, |b: f32| b.max(psnr)));
                        }
                    } else {
                        rejections.push(RejectionSummary {
                            id: candidate.id.clone(),
                            // 地板写实际用的那个: 有本场基线实测就是它,
                            // 没有才是赛道常数。两者能差十几 dB, 不写清楚
                            // 就看不懂为什么 31.3 dB 过了而 42.8 dB 没过。
                            reason: format!(
                                "{:?}: 耗时 {:.3} ms (预算 {:.2}), 画质 {:.3} dB (地板 {:.3})",
                                report.status,
                                report.metrics.operator_latency_ms,
                                spec.budget_ms,
                                report.metrics.psnr_db.unwrap_or(f32::NAN),
                                report.quality_floor_db(spec.quality_baseline_db),
                            ),
                        });
                    }
                    self.archive.insert(&candidate, report.clone(), now());
                    // 只有本地变异产物才有真基因组可以传给下一代;
                    // LLM 算子只有源码, 它的「遗传」走的是 prompt 里的优胜范例。
                    if candidate.origin == CandidateOrigin::LocalMutation {
                        self.genome_pool
                            .insert(candidate.id.clone(), genome.clone());
                    }
                    outcomes.push(CandidateOutcome {
                        id: candidate.id.clone(),
                        origin: candidate.origin,
                        fingerprint: candidate.fingerprint.clone(),
                        parent_id: candidate.parent_id.clone(),
                        notes: candidate.notes.clone(),
                        stage: OutcomeStage::Evaluated,
                        gate: Some(gate),
                        report: Some(report),
                        error: None,
                    });
                }
                Err(e) => {
                    // 单个候选评测失败不中断整代。
                    outcomes.push(CandidateOutcome {
                        id: candidate.id.clone(),
                        origin: candidate.origin,
                        fingerprint: candidate.fingerprint.clone(),
                        parent_id: candidate.parent_id.clone(),
                        notes: candidate.notes.clone(),
                        stage: OutcomeStage::EvalError,
                        gate: Some(gate),
                        report: None,
                        error: Some(e.to_string()),
                    });
                }
            }
        }

        let mut origin_counts: BTreeMap<String, usize> = BTreeMap::new();
        for o in &outcomes {
            let key = match o.origin {
                CandidateOrigin::Baseline => "baseline",
                CandidateOrigin::Llm => "llm",
                CandidateOrigin::LocalMutation => "local_mutation",
            };
            *origin_counts.entry(key.to_string()).or_insert(0) += 1;
        }

        let record = GenerationRecord {
            generation,
            track: spec.id.to_string(),
            started_at,
            origin_counts,
            gate_rejections,
            outcomes,
            metric_source: if self.config.dry_run {
                MetricSource::StaticCostModel
            } else {
                MetricSource::Phonefarm
            },
            best_psnr_so_far: self.archive.best_psnr(),
            gen_best_psnr,
            best_latency_so_far: self.archive.best_latency(),
            front_size: self.archive.front().len(),
            llm,
        };

        Ok((record, rejections))
    }

    /// 生成本代的子代。LLM 优先, 不足的用本地变异器补齐。
    fn breed(
        &mut self,
        generation: u32,
        prev_rejections: &[RejectionSummary],
    ) -> (Vec<(Candidate, Genome)>, LlmStatus) {
        let spec = self.config.spec();
        let want = self.config.children as usize;
        let mut out: Vec<(Candidate, Genome)> = Vec::new();

        let mut llm = LlmStatus {
            attempted: false,
            provider: self
                .generator
                .as_ref()
                .map(|g| g.provider_name().to_string()),
            produced: 0,
            error: self.llm_error.clone(),
        };

        if let Some(generator) = self.generator.as_ref() {
            llm.attempted = true;
            let elites: Vec<EliteSummary> = self
                .archive
                .select_parents(3)
                .into_iter()
                .map(|e| EliteSummary {
                    id: e.id.clone(),
                    notes: e.notes.clone(),
                    report: e.report.clone(),
                    shader_source: e.shader_source.clone(),
                })
                .collect();

            let template = tracks::load_template(self.config.track).unwrap_or_default();
            let ctx = MutationContext {
                track: spec,
                generation,
                children: self.config.children,
                device: self.config.device.name,
                elites,
                rejections: prev_rejections
                    .iter()
                    .map(|r| RejectionSummary {
                        id: r.id.clone(),
                        reason: r.reason.clone(),
                    })
                    .collect(),
                template: &template,
            };

            let mut counter = 0u32;
            let mut next_id = || {
                counter += 1;
                format!("gen{generation}_llm{counter}")
            };
            match generator.mutate(&ctx, &mut next_id) {
                Ok(cands) => {
                    llm.produced = cands.len();
                    // LLM 没有基因组; 用一个基线占位, 它不会进基因池。
                    for c in cands {
                        out.push((c, Genome::baseline(self.config.track)));
                    }
                }
                Err(e) => llm.error = Some(format!("{e:#}")),
            }
        }

        // 本地变异器补齐余量 —— 这是「整代照样推进」的兜底。
        let mut idx = 0u32;
        while out.len() < want {
            idx += 1;
            let parent = self.pick_genome();
            let child = parent.mutate(&mut self.rng);
            let source = child.to_glsl();
            let cand = Candidate::new(
                format!("gen{generation}_loc{idx}"),
                generation,
                spec.id,
                None,
                self.config.budget_ms,
                source,
                CandidateOrigin::LocalMutation,
                Some(child.describe()),
            );
            out.push((cand, child));
            // 极端情况下 (基因池退化) 防止无限循环。
            if idx > want as u32 * 8 {
                break;
            }
        }

        (out, llm)
    }

    /// 挑一个亲本。
    ///
    /// 亲本必须来自档案的 Pareto 前沿 (不够再往下一层取, 同层按拥挤距离排),
    /// 而不是「所有评测过的算子」里均匀抽。
    ///
    /// 这个区别不是细节: 均匀抽会反复抽到早就被判 POOR_QUALITY 的基线基因组,
    /// 于是整代又生成一遍同样的东西, 指纹去重把它们丢掉, 搜索原地打转 ——
    /// 实测 12 个候选里有 4 个是这么浪费掉的。
    fn pick_genome(&mut self) -> Genome {
        let parents: Vec<Genome> = self
            .archive
            .select_parents(8)
            .into_iter()
            .filter_map(|e| self.genome_pool.get(&e.id).cloned())
            .collect();

        if !parents.is_empty() {
            let i = (self.rng.next_u64() % parents.len() as u64) as usize;
            return parents[i].clone();
        }

        // 前沿里还没有本地变异产物 (开局第一代, 或前沿全是 LLM 算子):
        // 退回到池子里任意一个, 再不行才用赛道基线。
        if self.genome_pool.is_empty() {
            return Genome::baseline(self.config.track);
        }
        let i = (self.rng.next_u64() % self.genome_pool.len() as u64) as usize;
        self.genome_pool
            .values()
            .nth(i)
            .cloned()
            .unwrap_or_else(|| Genome::baseline(self.config.track))
    }

    /// 把通过门禁的算子源码与 SPIR-V 机器码一并落盘。
    ///
    /// `.spv` 必须真的写出来: `eval_request.spirv_path` 指的就是它, phonefarm
    /// 拿这个路径把字节码推上真机。只写 `.comp` 的话, 真机下发第一步就死在
    /// 「文件不存在」上 —— 而且本地门禁已经编译过一次了, 没有理由让真机再编一遍。
    fn persist_candidate(&self, candidate: &Candidate, gate: &GateReport) -> Result<PathBuf> {
        let path = self.shader_path(candidate);
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(&path, &candidate.shader_source)
            .with_context(|| format!("写入 {} 失败", path.display()))?;
        write_spirv(&path.with_extension("spv"), gate)?;
        Ok(path)
    }

    /// 把赛道基线模板的 SPIR-V 落到候选目录, 作为 A/B 对照的 A 臂。
    ///
    /// phonefarm 的 `gpu-op` 按「候选 spv 的同目录下的 baseline.spv」找 A 臂;
    /// 没有 A 臂就没有对照, 整条 A/B/A/B 与 t 检验都无从谈起。
    fn persist_baseline_spirv(&self, gate: &GateReport) -> Result<PathBuf> {
        let path = self.config.out_dir.join("candidates").join("baseline.spv");
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        write_spirv(&path, gate)?;
        Ok(path)
    }

    fn shader_path(&self, candidate: &Candidate) -> PathBuf {
        self.config
            .out_dir
            .join("candidates")
            .join(format!("{}.comp", candidate.id))
    }

    /// 评测一个候选。
    ///
    /// dry-run 走静态成本模型 (估计值), 否则下发 phonefarm (实测值)。
    /// 两条路径产出的报告都带明确的 `MetricSource`。
    fn evaluate(
        &self,
        candidate: &Candidate,
        gate: &GateReport,
        genome: &Genome,
    ) -> Result<EvalReport> {
        let spec = self.config.spec();

        if let Some(driver) = self.driver.as_ref() {
            let shader_path = self.shader_path(candidate);
            let spirv_path = shader_path.with_extension("spv");
            let request_path = shader_path.with_extension("request.json");
            // 在位冠军的实测耗时一并下发 —— 接收端拿它回答"比现任便宜吗",
            // 而不是在 refbench 上拿 `正数 < 0` 这个恒假的问题糊弄过去。
            let request = driver.build_request_against(
                candidate,
                spec,
                &shader_path,
                &spirv_path,
                self.archive.best_latency(),
            );
            return driver.evaluate(&request, &request_path);
        }

        // 成本模型需要知道锐化参数; 本地基因组能直接给, LLM 算子只能从静态特征推。
        let (sharpen, adaptive, clamped) = match genome {
            Genome::Sr(g) if candidate.origin == CandidateOrigin::LocalMutation => {
                (g.sharpen, g.adaptive, g.clamp_ringing)
            }
            _ => infer_quality_traits(&candidate.shader_source),
        };

        let (report, _cost) = cost_model::evaluate(
            &candidate.id,
            &gate.features,
            self.config.track,
            spec,
            &self.config.device,
            sharpen,
            adaptive,
            clamped,
        );
        Ok(report)
    }
}

/// 从源码里粗推画质相关特征 (只用于成本模型对 LLM 算子的估分)。
///
/// 这是一个**弱启发**: 它看的是源码里有没有出现锐化/夹紧的典型写法。
/// 真 PSNR 必须由真机截帧算, 这里只是给 dry-run 排序用。
fn infer_quality_traits(source: &str) -> (f32, bool, bool) {
    let lower = source.to_lowercase();
    let sharpen = if lower.contains("sharpen") || lower.contains("unsharp") {
        0.30
    } else {
        0.0
    };
    let adaptive = lower.contains("smoothstep") || lower.contains("contrast");
    let clamped = lower.contains("nmin") || lower.contains("nmax");
    (sharpen, adaptive, clamped)
}

/// SPIR-V words 按小端写成 `.spv`。
///
/// 字节序必须是小端: SPIR-V 规范允许两种, 但所有实际的 Android/Adreno 驱动
/// 都按小端读, 而 naga 产出的就是 host-order words (x86_64/aarch64 均为小端)。
/// 这里显式转换, 不依赖宿主机字节序。
fn write_spirv(path: &Path, gate: &GateReport) -> Result<()> {
    let words = gate
        .spirv
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("门禁未产出 SPIR-V, 无法落盘 {}", path.display()))?;
    let mut bytes = Vec::with_capacity(words.len() * 4);
    for w in words {
        bytes.extend_from_slice(&w.to_le_bytes());
    }
    std::fs::write(path, &bytes).with_context(|| format!("写入 {} 失败", path.display()))?;
    Ok(())
}

pub fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

/// 默认的设备成本模型。
pub fn default_device() -> DeviceModel {
    NX809J_1080P
}

/// 防爆门禁的红队语料。
///
/// 每一条都对应一类真实会把设备搞挂或把帧数据搬出显存的写法。
/// `--self-check` 跑这批语料, 产出的拦截率是 M0 验收的硬证据 ——
/// 一个只会放行的门禁和没有门禁是一回事。
pub struct RedTeamCase {
    pub name: &'static str,
    pub track: TrackMode,
    pub source: String,
    pub expect: ViolationCode,
}

pub fn red_team_corpus() -> Vec<RedTeamCase> {
    let hdr = "#version 450\nlayout(local_size_x = 16, local_size_y = 16) in;\n\
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;\n\
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;\n";
    let guard = "    ivec2 p = ivec2(gl_GlobalInvocationID.xy);\n\
    ivec2 o = imageSize(u_OutputTexture);\n\
    if (p.x >= o.x || p.y >= o.y) return;\n";
    let sr = |body: &str| format!("{hdr}void main() {{\n{guard}{body}\n}}\n");

    vec![
        RedTeamCase {
            name: "while_true",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    while (true) { c += vec4(0.01); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "for_ever",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    for (;;) { c += vec4(0.01); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "do_while_true",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    do { c += vec4(0.01); } while (true);\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "tautological_condition",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    while (2 > 1) { c += vec4(0.01); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "counter_moves_away_from_bound",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    for (int i = 0; i < 16; i--) { c += vec4(0.01); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "runtime_sized_loop",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    int n = imageSize(u_InputTexture).x;\n    for (int i = 0; i < n; i++) { c += imageLoad(u_InputTexture, ivec2(i, 0)); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::UnboundedLoop,
        },
        RedTeamCase {
            name: "loop_trip_explosion",
            track: TrackMode::Sr,
            source: sr("    vec4 c = vec4(0.0);\n    for (int i = 0; i < 100000; i++) { c += vec4(1e-6); }\n    imageStore(u_OutputTexture, p, c);"),
            expect: ViolationCode::LoopTripTooLarge,
        },
        RedTeamCase {
            name: "staging_buffer_breaks_zero_copy",
            track: TrackMode::Sr,
            source: format!(
                "{hdr}layout(binding = 5, std430) buffer Staging {{ vec4 px[]; }} staging;\nvoid main() {{\n{guard}    staging.px[0] = imageLoad(u_InputTexture, p);\n    imageStore(u_OutputTexture, p, staging.px[0]);\n}}\n"
            ),
            expect: ViolationCode::NonZeroCopyResource,
        },
        RedTeamCase {
            name: "undeclared_binding",
            track: TrackMode::Sr,
            source: format!(
                "{hdr}layout(binding = 9, rgba8) uniform readonly image2D u_Rogue;\nvoid main() {{\n{guard}    imageStore(u_OutputTexture, p, imageLoad(u_Rogue, p));\n}}\n"
            ),
            expect: ViolationCode::UnknownBinding,
        },
        RedTeamCase {
            name: "renamed_contract_slot",
            track: TrackMode::Sr,
            source: "#version 450\nlayout(local_size_x = 16, local_size_y = 16) in;\nlayout(binding = 0, rgba8) uniform readonly image2D u_Src;\nlayout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;\nvoid main() {\n    ivec2 p = ivec2(gl_GlobalInvocationID.xy);\n    ivec2 o = imageSize(u_OutputTexture);\n    if (p.x >= o.x || p.y >= o.y) return;\n    imageStore(u_OutputTexture, p, imageLoad(u_Src, p));\n}\n".to_string(),
            expect: ViolationCode::BindingNameMismatch,
        },
        RedTeamCase {
            name: "divergent_barrier",
            track: TrackMode::Sr,
            source: format!(
                "{hdr}shared vec4 tile[16][16];\nvoid main() {{\n{guard}    uvec2 l = gl_LocalInvocationID.xy;\n    tile[l.y][l.x] = imageLoad(u_InputTexture, p);\n    if (l.x > 3u) {{ barrier(); }}\n    imageStore(u_OutputTexture, p, tile[l.y][l.x]);\n}}\n"
            ),
            expect: ViolationCode::DivergentBarrier,
        },
        RedTeamCase {
            name: "unguarded_store",
            track: TrackMode::Sr,
            source: format!(
                "{hdr}void main() {{\n    ivec2 p = ivec2(gl_GlobalInvocationID.xy);\n    imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));\n}}\n"
            ),
            expect: ViolationCode::UnguardedImageStore,
        },
        RedTeamCase {
            name: "oversized_workgroup",
            track: TrackMode::Sr,
            source: "#version 450\nlayout(local_size_x = 64, local_size_y = 64) in;\nlayout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;\nlayout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;\nvoid main() {\n    ivec2 p = ivec2(gl_GlobalInvocationID.xy);\n    ivec2 o = imageSize(u_OutputTexture);\n    if (p.x >= o.x || p.y >= o.y) return;\n    imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));\n}\n".to_string(),
            expect: ViolationCode::WorkgroupTooLarge,
        },
        RedTeamCase {
            name: "syntax_error",
            track: TrackMode::Sr,
            source: sr("    imageStore(u_OutputTexture, p imageLoad(u_InputTexture, p));"),
            expect: ViolationCode::SyntaxError,
        },
        RedTeamCase {
            name: "missing_version",
            track: TrackMode::Sr,
            source: "layout(local_size_x = 16, local_size_y = 16) in;\nvoid main() {}\n".to_string(),
            expect: ViolationCode::MissingVersionDirective,
        },
        RedTeamCase {
            name: "sr_shader_on_frame_gen_track",
            track: TrackMode::FrameGen,
            source: tracks::embedded_template(TrackMode::Sr).to_string(),
            expect: ViolationCode::MissingRequiredBinding,
        },
    ]
}

/// 红队语料的运行结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelfCheckResult {
    pub total: usize,
    pub intercepted: usize,
    pub interception_rate: f64,
    /// 违规码命中预期的条数。
    pub exact_code_matches: usize,
    pub cases: Vec<SelfCheckCase>,
    /// 合规样本全部放行才算门禁没有过度杀伤。
    pub benign_total: usize,
    pub benign_passed: usize,
    pub max_gate_us: u128,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SelfCheckCase {
    pub name: String,
    pub track: String,
    pub expected: String,
    pub intercepted: bool,
    pub actual_codes: Vec<String>,
    pub gate_us: u128,
}

/// 跑红队语料 + 合规样本, 产出拦截率证据。
pub fn self_check() -> SelfCheckResult {
    let validator = Validator::new();
    let mut cases = Vec::new();
    let mut intercepted = 0usize;
    let mut exact = 0usize;
    let mut max_us = 0u128;

    for case in red_team_corpus() {
        let spec = tracks::spec(case.track);
        let report = validator.gate(&case.source, spec);
        let codes: Vec<String> = report.codes().iter().map(|c| c.to_string()).collect();
        let hit = !report.passed;
        if hit {
            intercepted += 1;
        }
        if codes.contains(&case.expect.to_string()) {
            exact += 1;
        }
        max_us = max_us.max(report.elapsed_us);
        cases.push(SelfCheckCase {
            name: case.name.to_string(),
            track: spec.id.to_string(),
            expected: case.expect.to_string(),
            intercepted: hit,
            actual_codes: codes,
            gate_us: report.elapsed_us,
        });
    }

    // 合规样本: 两条赛道的基线模板 + 一批本地变异产物。
    let mut benign_total = 0usize;
    let mut benign_passed = 0usize;
    for track in [TrackMode::Sr, TrackMode::FrameGen] {
        let spec = tracks::spec(track);
        let mut rng = Rng::new(0x5EED);
        let mut genome = Genome::baseline(track);
        for _ in 0..24 {
            let r = validator.gate(&genome.to_glsl(), spec);
            benign_total += 1;
            if r.passed {
                benign_passed += 1;
            }
            max_us = max_us.max(r.elapsed_us);
            genome = genome.mutate(&mut rng);
        }
    }

    let total = cases.len();
    SelfCheckResult {
        total,
        intercepted,
        interception_rate: if total == 0 {
            0.0
        } else {
            intercepted as f64 / total as f64
        },
        exact_code_matches: exact,
        cases,
        benign_total,
        benign_passed,
        max_gate_us: max_us,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 红队语料必须 100% 被拦下, 且命中预期的违规码。
    /// 只统计「拦下了」不够 —— 拦对原因才说明分析真的生效了。
    #[test]
    fn red_team_corpus_is_fully_intercepted_with_the_right_reason() {
        let r = self_check();
        assert!(r.total >= 15, "红队语料太少, 覆盖不足");
        assert_eq!(
            r.intercepted,
            r.total,
            "有 {} 条恶意算子漏网",
            r.total - r.intercepted
        );
        assert_eq!(
            r.exact_code_matches,
            r.total,
            "拦是拦下了, 但有 {} 条的违规码与预期不符",
            r.total - r.exact_code_matches
        );
        assert_eq!(r.interception_rate, 1.0);
    }

    /// 门禁不能靠「全杀」刷拦截率。
    #[test]
    fn the_gate_does_not_over_kill_benign_operators() {
        let r = self_check();
        assert!(r.benign_total >= 40);
        assert_eq!(
            r.benign_passed,
            r.benign_total,
            "{} 个合规算子被误杀",
            r.benign_total - r.benign_passed
        );
    }

    #[test]
    fn the_gate_stays_fast_across_the_whole_corpus() {
        let r = self_check();
        assert!(
            r.max_gate_us < 500_000,
            "最慢一次门禁 {} us, 称不上秒级打回",
            r.max_gate_us
        );
    }

    fn report_with(q: Option<crate::engine::QualityReference>) -> EvalReport {
        EvalReport {
            candidate_id: "c".into(),
            status: crate::engine::EvalStatus::Pass,
            metrics: crate::engine::Metrics {
                operator_latency_ms: 0.5,
                fps_p95_ms: Some(7.3),
                power_watt: Some(6.5),
                psnr_db: Some(31.3),
            },
            verdict: crate::engine::Verdict {
                is_pareto_improvement: false,
                p_value: 0.004,
            },
            source: MetricSource::Phonefarm,
            quality_reference: q,
            power_source: None,
        }
    }

    fn real_frames(n: u32) -> crate::engine::QualityReference {
        crate::engine::QualityReference {
            kind: "real_frames".into(),
            source: Some("tasks/genshin_refframes".into()),
            frames: n,
            frame_files: vec![],
            candidate_psnr_db: vec![],
            baseline_psnr_db: vec![],
        }
    }

    /// 换了画质真值必须当场拒绝接着用旧档案 —— 混在一个前沿里,
    /// 旧记录会仅仅因为尺子不同就永久支配新记录。
    #[test]
    fn a_changed_quality_reference_refuses_to_reuse_the_old_archive() {
        let cand = Candidate::new(
            "gen0_baseline",
            0,
            "sr",
            None,
            1.5,
            "#version 450\nvoid main() {}".into(),
            CandidateOrigin::Baseline,
            None,
        );
        let mut archive = ParetoArchive::new("sr", 1.5, 38.666);
        archive.insert(&cand, report_with(Some(procedural())), now());

        // 同一把尺子继续跑: 放行。
        assert!(check_archive_quality_reference(&archive, None).is_ok());

        // 换成真机参考帧: 拒绝, 并说清楚该怎么办。
        let err = check_archive_quality_reference(&archive, Some("tasks/genshin_refframes"))
            .unwrap_err()
            .to_string();
        assert!(err.contains("procedural"), "{err}");
        assert!(err.contains("--out"), "{err}");

        // 空档案 / 没报来源的记录都不拦: 它们没声称过自己对着什么量。
        let empty = ParetoArchive::new("sr", 1.5, 38.666);
        assert!(check_archive_quality_reference(&empty, Some("x")).is_ok());
        let mut legacy = ParetoArchive::new("sr", 1.5, 38.666);
        legacy.insert(&cand, report_with(None), now());
        assert!(check_archive_quality_reference(&legacy, Some("x")).is_ok());
    }

    fn procedural() -> crate::engine::QualityReference {
        crate::engine::QualityReference {
            kind: "procedural".into(),
            source: None,
            frames: 1,
            frame_files: vec![],
            candidate_psnr_db: vec![],
            baseline_psnr_db: vec![],
        }
    }

    /// 汇总只认**接收端报回来的**来源, 不认配置里要求的。
    ///
    /// 要了真实帧不等于用上了真实帧: 路径写错、接收端是旧版本、画质补测被关掉,
    /// 都会让 dB 仍然来自程序化图案, 而配置那一行照样写着漂亮的目录名。
    #[test]
    fn the_quality_reference_summary_counts_what_came_back_not_what_was_asked_for() {
        let asked = Some("tasks/genshin_refframes".to_string());
        let reports = [
            report_with(Some(real_frames(12))),
            report_with(Some(real_frames(12))),
            report_with(Some(procedural())),
            report_with(None), // 旧版接收端 / 成本模型: 两边都不计
        ];
        let s = summarise_quality_reference(asked.clone(), reports.iter());
        assert_eq!(s.requested, asked);
        assert_eq!(s.real_frame_reports, 2);
        assert_eq!(s.procedural_reports, 1);
        // 同一轮里换过真值必须看得见 —— 那批 dB 互相之间不可比。
        assert_eq!(s.observed.len(), 2, "{:?}", s.observed);
        assert!(s.observed[0].contains("真机参考帧 x12"), "{:?}", s.observed);

        // 要了真实帧却一份都没用上。
        let s = summarise_quality_reference(asked, [report_with(Some(procedural()))].iter());
        assert_eq!(s.real_frame_reports, 0);
        assert!(s.requested.is_some());
    }

    /// 不认识的 `kind` 单独数, 不并进 procedural —— 否则汇总会替接收端瞎猜,
    /// 还会对着一批真实帧误报"用的是程序化图案"。
    #[test]
    fn an_unrecognised_reference_kind_is_counted_separately() {
        let weird = crate::engine::QualityReference {
            kind: "real_frame".into(), // 少了个 s
            source: Some("tasks/genshin_refframes".into()),
            frames: 12,
            frame_files: vec![],
            candidate_psnr_db: vec![],
            baseline_psnr_db: vec![],
        };
        let s = summarise_quality_reference(None, [report_with(Some(weird))].iter());
        assert_eq!(s.unknown_kind_reports, 1);
        assert_eq!(s.procedural_reports, 0);
        assert_eq!(s.real_frame_reports, 0);
        assert!(s.observed[0].contains("未知真值类型"), "{:?}", s.observed);
    }

    #[test]
    fn quality_trait_inference_is_conservative() {
        let (s, a, c) = infer_quality_traits("#version 450\nvoid main() {}");
        assert_eq!(s, 0.0);
        assert!(!a && !c);

        let (s2, a2, _) =
            infer_quality_traits("float gain = SHARPEN * smoothstep(0.0, 0.2, contrast);");
        assert!(s2 > 0.0 && a2);
    }
}
