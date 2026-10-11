//! 面板的数据模型 —— 后端唯一对外形状。
//!
//! 一条贯穿全模块的规矩: **读不到就是读不到**。凡是可能缺的字段一律
//! `Option`, 前端把 `None` 渲染成「未知/未测」。不允许用 0 / "" / false
//! 之类的"看着正常的默认值"去填空 —— 面板是拿来给人下判断的,
//! 一个填出来的 0 比一个空格危险得多。

use serde::Serialize;

/// 一轮的判定。三色分档就是给这个枚举上色。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RoundVerdict {
    /// 判据齐全且全部通过。
    Qualified,
    /// 判据齐全, 但有未通过的 (例如改善方向对但未达统计显著)。
    Eliminated,
    /// 判据不齐 / 没跑完 / 读不出来 —— **无法下判定**, 不是"判负"。
    Invalid,
    /// 只是一份证据, 不是闭环的一轮 (机制验证、探针、恢复核验)。
    /// 不计进合格/淘汰/无效。
    Evidence,
}

impl RoundVerdict {
    pub fn label(self) -> &'static str {
        match self {
            RoundVerdict::Qualified => "合格",
            RoundVerdict::Eliminated => "淘汰",
            RoundVerdict::Invalid => "无效",
            RoundVerdict::Evidence => "仅证据",
        }
    }
}

/// 一个大字指标。`value` 为 `None` 时前端显示「未测」。
#[derive(Debug, Clone, Serialize)]
pub struct Stat {
    pub label: String,
    pub value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    /// 前端配色: `good` / `bad` / `warn` / `plain`。
    pub tone: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hint: Option<String>,
}

impl Stat {
    pub fn plain(label: impl Into<String>, value: Option<String>, unit: Option<&str>) -> Self {
        Stat {
            label: label.into(),
            value,
            unit: unit.map(str::to_string),
            tone: "plain",
            hint: None,
        }
    }

    pub fn toned(
        label: impl Into<String>,
        value: Option<String>,
        unit: Option<&str>,
        tone: &'static str,
    ) -> Self {
        Stat {
            label: label.into(),
            value,
            unit: unit.map(str::to_string),
            tone,
            hint: None,
        }
    }

    pub fn with_hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }
}

/// 一条测试条件。`value` 为 `None` → 前端「未知/未测」。
#[derive(Debug, Clone, Serialize)]
pub struct Condition {
    pub label: String,
    pub value: Option<String>,
    /// 这个取值是从哪个文件读出来的, 供演示时追溯。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from: Option<String>,
}

impl Condition {
    pub fn new(label: impl Into<String>, value: Option<String>, from: Option<&str>) -> Self {
        Condition {
            label: label.into(),
            value,
            from: from.map(str::to_string),
        }
    }
}

/// 一条判据 (层②③ 的 criterion_*)。
#[derive(Debug, Clone, Serialize)]
pub struct Criterion {
    pub key: String,
    pub name: String,
    /// `None` = 这一条没评 (报告里 criteria_evaluated < criteria_total)。
    pub pass: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// 层① 的一个候选算子。
#[derive(Debug, Clone, Serialize)]
pub struct CandidateRow {
    pub id: String,
    pub origin: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
    /// `evaluated` / `duplicate_skipped` / `gate_rejected` / …, 原样带出。
    pub stage: String,
    pub verdict: RoundVerdict,
    pub verdict_text: String,
    pub latency_ms: Option<f64>,
    pub frame_p95_ms: Option<f64>,
    pub power_watt: Option<f64>,
    pub psnr_db: Option<f64>,
    pub p_value: Option<f64>,
    /// 是否落在档案当前的支配前沿上 (`None` = 档案读不到)。
    pub on_front: Option<bool>,
    /// 功耗为什么没参与支配排序; `None` = 参与了或档案没记。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub power_excluded_reason: Option<String>,
    /// Full receiver report plus its declaration-level qualification. A view grants no proof.
    pub evidence: Option<CandidateEvidence>,
    /// 静态预检未通过时的违规码。
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub violations: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CandidateEvidence {
    pub qualification_state: crate::evidence::QualificationState,
    pub qualification_label: &'static str,
    pub metric_source: &'static str,
    pub evidence_origin: Option<String>,
    pub reported_status: crate::engine::EvalStatus,
    pub receiver_report: serde_json::Value,
    pub view_grants_game_proof: bool,
    pub view_allows_promotion: bool,
}

/// 层②③ 的两臂逐轮数值 —— 前端的对比图直接吃这个。
#[derive(Debug, Clone, Serialize)]
pub struct AbArms {
    pub metric: String,
    pub a_label: String,
    pub b_label: String,
    pub a_runs: Vec<String>,
    pub b_runs: Vec<String>,
    pub a_values: Vec<f64>,
    pub b_values: Vec<f64>,
    pub unit: String,
    /// 可切换的其它指标, 键为指标名。
    pub others: Vec<AbMetric>,
}

#[derive(Debug, Clone, Serialize)]
pub struct AbMetric {
    pub metric: String,
    pub unit: String,
    pub a_values: Vec<f64>,
    pub b_values: Vec<f64>,
    pub diff_pct: Option<f64>,
    pub p_value: Option<f64>,
    pub significant: Option<bool>,
}

/// 层① 的 Pareto 散点 (耗时 vs 画质)。
#[derive(Debug, Clone, Serialize)]
pub struct ParetoChart {
    pub x_label: String,
    pub y_label: String,
    /// 画质真值的出处。跨出处的绝对 dB 不可比, 所以必须显示在图上。
    pub quality_reference: Option<String>,
    pub points: Vec<ParetoPoint>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ParetoPoint {
    pub id: String,
    pub generation: u32,
    pub x: f64,
    pub y: Option<f64>,
    pub on_front: bool,
    pub origin: String,
}

/// 一个可点开的证据文件。
#[derive(Debug, Clone, Serialize)]
pub struct EvidenceFile {
    pub label: String,
    /// 绝对路径 —— 前端拿它去 `/api/file` 取内容。
    pub path: String,
    /// 相对根目录的显示名。
    pub rel: String,
    /// `json` / `md` / `txt` / `png` / `other`
    pub kind: &'static str,
    pub size_bytes: Option<u64>,
}

/// 闭环的一轮 (层① 是一代演化, 层②③ 是一次 A/B)。
#[derive(Debug, Clone, Serialize)]
pub struct Round {
    pub id: String,
    pub layer: &'static str,
    pub title: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub when: Option<String>,
    pub verdict: RoundVerdict,
    pub verdict_text: String,
    /// 指标来源: 真机实测 / 静态估计 / 未知。这一条**绝不能**丢。
    pub metric_source: String,
    pub headline: Vec<Stat>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub candidates: Vec<CandidateRow>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub arms: Option<AbArms>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub criteria: Vec<Criterion>,
    pub conditions: Vec<Condition>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pareto: Option<ParetoChart>,
    pub evidence: Vec<EvidenceFile>,
    /// A/B receiver JSON is preserved for read-only inspection; declarations are not attestations.
    pub receiver_report: Option<serde_json::Value>,
    pub view_grants_game_proof: bool,
    pub view_allows_promotion: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub notes: Vec<String>,
}

/// 一个层级。
#[derive(Debug, Clone, Serialize)]
pub struct Layer {
    pub id: &'static str,
    pub index: u8,
    pub name: &'static str,
    pub subtitle: &'static str,
    pub rounds_total: usize,
    pub qualified: usize,
    pub eliminated: usize,
    pub invalid: usize,
    pub evidence_only: usize,
    pub rounds: Vec<Round>,
    /// 这一层读数据时出的岔子, 原样报出来。
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub problems: Vec<String>,
}

impl Layer {
    /// 按 rounds 里的判定把三色计数算出来。
    pub fn tally(mut self) -> Self {
        self.qualified = self.count(RoundVerdict::Qualified);
        self.eliminated = self.count(RoundVerdict::Eliminated);
        self.invalid = self.count(RoundVerdict::Invalid);
        self.evidence_only = self.count(RoundVerdict::Evidence);
        self.rounds_total = self.qualified + self.eliminated + self.invalid;
        self
    }

    fn count(&self, v: RoundVerdict) -> usize {
        self.rounds.iter().filter(|r| r.verdict == v).count()
    }
}

/// 一条提示。`level`: `retracted` / `warn` / `info`。
#[derive(Debug, Clone, Serialize)]
pub struct Notice {
    pub level: &'static str,
    pub title: String,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence: Option<EvidenceFile>,
}

/// 把路径包成可点开的证据文件。读不到大小就留空, 不编。
pub fn evidence_file(root: &std::path::Path, path: &std::path::Path, label: &str) -> EvidenceFile {
    let rel = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .to_string();
    let kind = match path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "json" => "json",
        "md" => "md",
        "txt" | "log" | "tsv" | "jsonl" => "txt",
        "png" | "jpg" | "jpeg" => "png",
        _ => "other",
    };
    EvidenceFile {
        label: label.to_string(),
        path: path.to_string_lossy().to_string(),
        rel,
        kind,
        size_bytes: std::fs::metadata(path).ok().map(|m| m.len()),
    }
}
