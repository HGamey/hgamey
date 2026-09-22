//! 进化中枢: 候选算子的数据模型、防爆门禁、变异、评测与 Pareto 归档。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub mod analysis;
pub mod archive;
pub mod cost_model;
pub mod evolution;
pub mod generator;
pub mod mutate;
pub mod pareto;
pub mod validator;

pub use archive::{ArchiveEntry, ParetoArchive};
pub use validator::{GateReport, GateStage, Validator, Violation, ViolationCode};

/// 候选算子从哪来的。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateOrigin {
    /// 赛道基线模板。
    Baseline,
    /// 大模型变异产物。
    Llm,
    /// 本地确定性变异器产物 (LLM 不可用时的兜底, 也是 dry-run 的来源)。
    LocalMutation,
}

/// 一个待评测的算子。字段与 `contracts/candidate.schema.json` 对齐。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    pub id: String,
    pub generation: u32,
    pub track: String,
    pub parent_id: Option<String>,
    pub budget_ms: f32,
    pub shader_source: String,
    /// 规范化源码的 sha256 前 16 位, 用于跨代去重 (同构算子不重复上真机)。
    pub fingerprint: String,
    pub origin: CandidateOrigin,
    /// 一行人类可读的形态描述。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
}

impl Candidate {
    // 参数多是因为它就是契约字段的逐项映射; 拆成 builder 只会让调用点更啰嗦。
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: impl Into<String>,
        generation: u32,
        track: &str,
        parent_id: Option<String>,
        budget_ms: f32,
        shader_source: String,
        origin: CandidateOrigin,
        notes: Option<String>,
    ) -> Self {
        let fingerprint = fingerprint(&shader_source);
        Self {
            id: id.into(),
            generation,
            track: track.to_string(),
            parent_id,
            budget_ms,
            shader_source,
            fingerprint,
            origin,
            notes,
        }
    }
}

/// 源码指纹。先折叠空白与注释再哈希: 只改了排版或注释的算子是同一个算子,
/// 不值得再占一次真机 60s 回放。
pub fn fingerprint(source: &str) -> String {
    let normalized: String = source
        .lines()
        .map(|l| {
            let l = match l.find("//") {
                Some(i) => &l[..i],
                None => l,
            };
            l.split_whitespace().collect::<Vec<_>>().join(" ")
        })
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("\n");

    let digest = Sha256::digest(normalized.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// 真机 (或成本模型) 给出的多维指标。字段与 `contracts/eval_report.schema.json` 对齐。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Metrics {
    /// 算子自身的 GPU compute pass 耗时。硬预算就卡这个数。
    pub operator_latency_ms: f32,
    /// 整帧时间 p95 (ms)。
    ///
    /// `None` = **本次没有量到**, 不是 0。headless 算子标尺量的是算子自身的
    /// GPU 耗时, 帧时 p95 需要真实渲染上下文才有意义; 真机 2026-09-22 回的
    /// 第一份报告这一项就是 null, 而当时它是 `f32`, 上游直接解析失败。
    /// 契约必须容得下「未测量」这个状态。
    pub fps_p95_ms: Option<f32>,
    /// 整机功耗 (W)。
    pub power_watt: f32,
    /// 画质 (dB)。必须 >= 赛道基线。
    ///
    /// `None` = **本次没有量到**, 不是 0。refbench 载体只跑真实渲染场景,
    /// 画质由 headless 载体负责 (refbench 的边界不允许它自带测量逻辑)。
    ///
    /// 这是第二次踩同一个坑: `fps_p95_ms` 已经因为同样的原因改成 Option 了,
    /// 当时只改了那一个字段, 没有把"指标可能量不到"这件事推广到整个 Metrics。
    pub psnr_db: Option<f32>,
}

/// 指标从哪来 —— 这一条**绝不能**丢。
///
/// 成本模型产出的数字是**静态估计**, 不是测量; 它只能用于本地排序与冒烟,
/// 不能拿来下任何性能结论。真机数据才带 p 值。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MetricSource {
    /// phonefarm 真机物理采样 + 冻结规则 t 检验。
    Phonefarm,
    /// 本地静态成本模型的估计值 (dry-run 专用, 非测量)。
    StaticCostModel,
}

impl MetricSource {
    pub fn is_measured(&self) -> bool {
        matches!(self, MetricSource::Phonefarm)
    }
}

/// 评测结论。取值与 `contracts/eval_report.schema.json` 的 status 枚举一致。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EvalStatus {
    Pass,
    /// 超出耗时硬预算。
    Slow,
    /// PSNR 低于基线 —— 画质一票否决。
    PoorQuality,
    Crash,
    Reverted,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Verdict {
    pub is_pareto_improvement: bool,
    /// 两样本 t 检验的 p 值。成本模型不产出 p 值, 记 NaN 之外的哨兵 1.0。
    pub p_value: f64,
}

/// 一份完整评测报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalReport {
    pub candidate_id: String,
    pub status: EvalStatus,
    pub metrics: Metrics,
    pub verdict: Verdict,
    /// 数据来源。序列化后依然带着, 归档里一眼能分辨真机数与估计值。
    pub source: MetricSource,
}

impl EvalReport {
    /// 是否有资格进入 Pareto 前沿。
    ///
    /// 三条硬约束同时成立才算数:
    /// - 耗时在预算内 (§3 赛道硬约束)
    /// - PSNR >= 基线 (§2 红线 3: 画质是约束, 不是目标)
    /// - 真机数据还要显著 (p < 0.01; §2 红线 4: 防硬件噪声欺骗)
    pub fn is_admissible(&self, budget_ms: f32, quality_baseline_db: f32) -> bool {
        if self.status != EvalStatus::Pass {
            return false;
        }
        if self.metrics.operator_latency_ms > budget_ms {
            return false;
        }
        // 量不到画质时不否决 —— "没量" 和 "不合格" 是两回事。
        // 该载体的画质由另一条载体负责, 在这里当成不合格会把好算子全毙掉。
        if let Some(q) = self.metrics.psnr_db {
            if q < quality_baseline_db {
                return false;
            }
        }
        if self.source.is_measured() && self.verdict.p_value >= 0.01 {
            return false;
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_ignores_comments_and_whitespace() {
        let a = "#version 450\nvoid main() { int x = 1; }";
        let b = "#version 450\n\n  void  main()   {  int x = 1; }  // 换了注释\n";
        assert_eq!(fingerprint(a), fingerprint(b));
    }

    #[test]
    fn fingerprint_separates_real_changes() {
        let a = "#version 450\nvoid main() { int x = 1; }";
        let b = "#version 450\nvoid main() { int x = 2; }";
        assert_ne!(fingerprint(a), fingerprint(b));
    }

    fn report(status: EvalStatus, latency: f32, psnr: f32, p: f64, src: MetricSource) -> EvalReport {
        EvalReport {
            candidate_id: "t".into(),
            status,
            metrics: Metrics {
                operator_latency_ms: latency,
                fps_p95_ms: Some(16.0),
                power_watt: 4.8,
                psnr_db: Some(psnr),
            },
            verdict: Verdict {
                is_pareto_improvement: false,
                p_value: p,
            },
            source: src,
        }
    }

    #[test]
    fn admissibility_enforces_the_latency_budget() {
        let r = report(EvalStatus::Pass, 1.9, 38.0, 0.001, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
    }

    #[test]
    fn admissibility_enforces_the_quality_floor() {
        let r = report(EvalStatus::Pass, 0.9, 37.0, 0.001, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
    }

    /// 真机数据不显著 = 被硬件噪声忽悠, 不算数。
    #[test]
    fn admissibility_requires_significance_for_measured_data() {
        let r = report(EvalStatus::Pass, 0.9, 38.5, 0.20, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
        let ok = report(EvalStatus::Pass, 0.9, 38.5, 0.004, MetricSource::Phonefarm);
        assert!(ok.is_admissible(1.5, 37.286));
    }

    /// 成本模型的估计值没有 p 值可言, 不能拿显著性去卡它 (也永远不该被当成真机结论)。
    #[test]
    fn simulated_reports_are_not_judged_on_significance() {
        let r = report(EvalStatus::Pass, 0.9, 38.5, 1.0, MetricSource::StaticCostModel);
        assert!(r.is_admissible(1.5, 37.286));
        assert!(!r.source.is_measured());
    }

    #[test]
    fn non_pass_status_is_never_admissible() {
        for s in [EvalStatus::Slow, EvalStatus::PoorQuality, EvalStatus::Crash, EvalStatus::Reverted] {
            let r = report(s, 0.5, 40.0, 0.0001, MetricSource::Phonefarm);
            assert!(!r.is_admissible(1.5, 37.286), "{s:?} 不该被接纳");
        }
    }
}
