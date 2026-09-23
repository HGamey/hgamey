//! 读 phonefarm 的 A/B 验收报告 (层② 图形接口改写 / 层③ 系统参数)。
//!
//! 这两层的报告是同一种东西: 一份 `report.json`, 里面若干条 `criterion_*`,
//! 加一个 `summary.all_pass`。报告有两种排布:
//!
//! - **两臂式** (`loop_v1/runs/report.json`, `loop_v1/runs_graylayer/report.json`):
//!   顶层有 `baseline` / `knob_arm` / `comparison`。
//! - **探针式** (`loop_v1/runs_refbench/report.json`): 判据里自带 `comparison`,
//!   顶层没有 `baseline`。
//!
//! 所以这里不去硬编码字段路径, 而是**按形状找**: 先找顶层 `comparison`,
//! 找不到再进每条判据里找。报告加判据、改键名都不该让面板打不开。

use crate::viz::model::{AbArms, AbMetric, Criterion, RoundVerdict};
use crate::viz::stats::round_to;
use serde_json::Value;
use std::path::Path;

/// 一份 A/B 报告解析出来的东西。
#[derive(Debug, Clone)]
pub struct AbReport {
    pub goal: Option<String>,
    pub primary_metric: Option<String>,
    pub criteria: Vec<Criterion>,
    pub all_pass: Option<bool>,
    pub criteria_total: Option<u64>,
    pub criteria_evaluated: Option<u64>,
    pub criteria_passed: Option<u64>,
    pub baseline_title: Option<String>,
    pub knob_title: Option<String>,
    pub baseline_runs: Vec<String>,
    pub knob_runs: Vec<String>,
    /// 对照臂各指标的离散度 (指标名 → dispersion_pct), 载体可复现性直接用。
    pub baseline_dispersion: Vec<(String, f64)>,
    pub baseline_values: Vec<(String, Vec<f64>)>,
    pub comparisons: Vec<Comparison>,
    pub attribution_verdict: Option<String>,
    pub attribution_evidence: Option<String>,
}

/// 一个指标上的两臂对比 (口径与报告里的 `comparison.*` 逐字一致)。
#[derive(Debug, Clone)]
pub struct Comparison {
    pub metric: String,
    pub a_values: Vec<f64>,
    pub b_values: Vec<f64>,
    pub diff_pct: Option<f64>,
    pub p_value: Option<f64>,
    pub significant: Option<bool>,
    pub ci95: Option<(f64, f64)>,
    pub hodges_lehmann: Option<f64>,
}

impl AbReport {
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        let raw = std::fs::read_to_string(path)?;
        let v: Value = serde_json::from_str(&raw)?;
        Ok(Self::from_value(&v))
    }

    pub fn from_value(v: &Value) -> Self {
        let summary = v.get("summary");
        let baseline = v.get("baseline");
        let knob = v.get("knob_arm");

        AbReport {
            goal: str_at(v, "goal"),
            primary_metric: str_at(v, "primary_metric"),
            criteria: collect_criteria(v),
            all_pass: summary.and_then(|s| s.get("all_pass")).and_then(Value::as_bool),
            criteria_total: u64_at(summary, "criteria_total"),
            criteria_evaluated: u64_at(summary, "criteria_evaluated"),
            criteria_passed: u64_at(summary, "criteria_passed"),
            baseline_title: baseline.and_then(|b| str_at(b, "title")),
            knob_title: knob.and_then(|b| str_at(b, "title")),
            baseline_runs: string_list(baseline, "runs").unwrap_or_else(|| runs_from_criteria(v)),
            knob_runs: string_list(knob, "runs").unwrap_or_default(),
            baseline_dispersion: arm_dispersion(baseline),
            baseline_values: arm_values(baseline),
            comparisons: collect_comparisons(v),
            attribution_verdict: v
                .get("criterion_2_attribution")
                .and_then(|c| str_at(c, "verdict")),
            attribution_evidence: v
                .get("criterion_2_attribution")
                .and_then(|c| str_at(c, "evidence")),
        }
    }

    /// 判定。
    ///
    /// - 全部判据通过 → 合格
    /// - 判据齐全但有未通过 → 淘汰
    /// - 判据没评齐 (`criteria_evaluated < criteria_total`) 或压根没有 summary
    ///   → **无效**: 这是"结论下不出来", 不是"结论是负的"。两者不能混。
    pub fn verdict(&self) -> (RoundVerdict, String) {
        let Some(all_pass) = self.all_pass else {
            return (RoundVerdict::Invalid, "报告里没有汇总判定".to_string());
        };
        let evaluated_all = match (self.criteria_evaluated, self.criteria_total) {
            (Some(e), Some(t)) => e >= t,
            _ => true,
        };
        if all_pass {
            return (
                RoundVerdict::Qualified,
                format!(
                    "{}/{} 条判据全部通过",
                    self.criteria_passed.unwrap_or_default(),
                    self.criteria_total.unwrap_or_default()
                ),
            );
        }
        // 未通过的那条是哪条 —— 演示时这句话比一个红叉有用得多。
        let failed: Vec<&str> = self
            .criteria
            .iter()
            .filter(|c| c.pass == Some(false))
            .map(|c| c.name.as_str())
            .collect();
        if !evaluated_all && failed.is_empty() {
            return (
                RoundVerdict::Invalid,
                format!(
                    "只评了 {}/{} 条判据, 无法下判定",
                    self.criteria_evaluated.unwrap_or_default(),
                    self.criteria_total.unwrap_or_default()
                ),
            );
        }
        let mut text = if failed.is_empty() {
            "有判据未通过".to_string()
        } else {
            format!("未通过: {}", failed.join(" / "))
        };
        if !evaluated_all {
            text.push_str(&format!(
                " (另有 {} 条未评)",
                self.criteria_total.unwrap_or_default() - self.criteria_evaluated.unwrap_or_default()
            ));
        }
        (RoundVerdict::Eliminated, text)
    }

    /// 主指标的两臂对比。没有 `primary_metric` 时退回第一个 comparison。
    pub fn primary(&self) -> Option<&Comparison> {
        if let Some(name) = &self.primary_metric {
            if let Some(c) = self.comparisons.iter().find(|c| &c.metric == name) {
                return Some(c);
            }
        }
        self.comparisons.first()
    }

    /// 组装成前端的两臂图数据。
    pub fn arms(&self) -> Option<AbArms> {
        let primary = self.primary()?;
        let others = self
            .comparisons
            .iter()
            .map(|c| AbMetric {
                metric: c.metric.clone(),
                unit: unit_for(&c.metric).to_string(),
                a_values: c.a_values.clone(),
                b_values: c.b_values.clone(),
                diff_pct: c.diff_pct,
                p_value: c.p_value,
                significant: c.significant,
            })
            .collect();
        Some(AbArms {
            metric: primary.metric.clone(),
            a_label: self
                .baseline_title
                .clone()
                .map(|t| format!("对照臂 {t}"))
                .unwrap_or_else(|| "对照臂".to_string()),
            b_label: self
                .knob_title
                .clone()
                .map(|t| format!("处理臂 {t}"))
                .unwrap_or_else(|| "处理臂".to_string()),
            a_runs: self.baseline_runs.clone(),
            b_runs: self.knob_runs.clone(),
            a_values: primary.a_values.clone(),
            b_values: primary.b_values.clone(),
            unit: unit_for(&primary.metric).to_string(),
            others,
        })
    }
}

/// 指标单位。查不到就留空字符串, 不瞎猜。
pub fn unit_for(metric: &str) -> &'static str {
    match metric {
        "fps_mean" => "fps",
        "bw_median" | "bw_max" => "MB/s",
        m if m.starts_with("frame_") || m.starts_with("gpu_active") || m.starts_with("queue_") => {
            "ms"
        }
        _ => "",
    }
}

fn str_at(v: &Value, key: &str) -> Option<String> {
    v.get(key)?.as_str().map(str::to_string)
}

fn u64_at(v: Option<&Value>, key: &str) -> Option<u64> {
    v?.get(key)?.as_u64()
}

fn f64_at(v: &Value, key: &str) -> Option<f64> {
    v.get(key)?.as_f64()
}

fn string_list(v: Option<&Value>, key: &str) -> Option<Vec<String>> {
    let arr = v?.get(key)?.as_array()?;
    Some(
        arr.iter()
            .filter_map(|x| x.as_str().map(str::to_string))
            .collect(),
    )
}

fn f64_list(v: &Value, key: &str) -> Vec<f64> {
    v.get(key)
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_f64).collect())
        .unwrap_or_default()
}

/// 探针式报告顶层没有 `baseline.runs`, 轮次名字在 `criterion_*.runs` 里。
fn runs_from_criteria(v: &Value) -> Vec<String> {
    let Some(obj) = v.as_object() else {
        return Vec::new();
    };
    for (k, c) in obj {
        if !k.starts_with("criterion_") {
            continue;
        }
        if let Some(runs) = string_list(Some(c), "runs") {
            if !runs.is_empty() {
                return runs;
            }
        }
    }
    Vec::new()
}

fn arm_dispersion(arm: Option<&Value>) -> Vec<(String, f64)> {
    let Some(metrics) = arm.and_then(|a| a.get("metrics")).and_then(Value::as_object) else {
        return Vec::new();
    };
    metrics
        .iter()
        .filter_map(|(name, m)| Some((name.clone(), f64_at(m, "dispersion_pct")?)))
        .collect()
}

fn arm_values(arm: Option<&Value>) -> Vec<(String, Vec<f64>)> {
    let Some(metrics) = arm.and_then(|a| a.get("metrics")).and_then(Value::as_object) else {
        return Vec::new();
    };
    metrics
        .iter()
        .map(|(name, m)| (name.clone(), f64_list(m, "values")))
        .collect()
}

/// 把 `criterion_3_statistically_significant` 这种键名变成人看的名字。
fn criterion_name(key: &str) -> String {
    match key {
        "criterion_1_workload_repeatable" | "criterion_1_repeatable" => "负载可重复",
        "criterion_2_attribution" => "归因一致",
        "criterion_2_ceiling_real" => "天花板真实",
        "criterion_3_statistically_significant" => "统计显著",
        "criterion_3_knob_effective" => "旋钮真生效",
        "criterion_4_no_residue" | "criterion_5_no_residue" => "设备无残留",
        "criterion_4_attribution_falsifiable" => "归因可伪证",
        "criterion_5_offline_replay" | "criterion_6_offline_replay" => "离线重放一致",
        other => other,
    }
    .to_string()
}

fn collect_criteria(v: &Value) -> Vec<Criterion> {
    let Some(obj) = v.as_object() else {
        return Vec::new();
    };
    let mut out: Vec<Criterion> = obj
        .iter()
        .filter(|(k, _)| k.starts_with("criterion_"))
        .map(|(k, c)| Criterion {
            key: k.clone(),
            name: criterion_name(k),
            pass: c.get("pass").and_then(Value::as_bool),
            detail: criterion_detail(c),
        })
        .collect();
    out.sort_by(|a, b| a.key.cmp(&b.key));
    out
}

/// 一条判据的"为什么" —— 优先 note, 其次归因结论, 再不行就把关键数字拼出来。
fn criterion_detail(c: &Value) -> Option<String> {
    if let Some(note) = str_at(c, "note") {
        return Some(note);
    }
    if let Some(verdict) = str_at(c, "verdict") {
        return Some(verdict);
    }
    if let (Some(d), Some(p)) = (f64_at(c, "diff_pct"), f64_at(c, "perm_p_two_sided")) {
        return Some(format!("差异 {d:+.3}%, p = {p:.4}"));
    }
    if let Some(d) = f64_at(c, "dispersion_pct") {
        return Some(format!("离散度 {d:.3}%"));
    }
    if c.get("identical").and_then(Value::as_bool) == Some(true) {
        return Some("快照逐行一致".to_string());
    }
    None
}

fn comparison_from(metric_hint: Option<&str>, c: &Value) -> Option<Comparison> {
    let a_values = f64_list(c, "a_values");
    let b_values = f64_list(c, "b_values");
    if a_values.is_empty() && b_values.is_empty() {
        return None;
    }
    let ci = c.get("ci95").and_then(Value::as_array).and_then(|a| {
        if a.len() == 2 {
            Some((a[0].as_f64()?, a[1].as_f64()?))
        } else {
            None
        }
    });
    Some(Comparison {
        metric: str_at(c, "metric")
            .or_else(|| metric_hint.map(str::to_string))
            .unwrap_or_else(|| "未知指标".to_string()),
        a_values,
        b_values,
        diff_pct: f64_at(c, "diff_pct").map(|v| round_to(v, 3)),
        p_value: f64_at(c, "perm_p_two_sided"),
        significant: c.get("significant_at_0.05").and_then(Value::as_bool),
        ci95: ci,
        hodges_lehmann: f64_at(c, "hodges_lehmann"),
    })
}

fn collect_comparisons(v: &Value) -> Vec<Comparison> {
    let mut out = Vec::new();
    // 两臂式: 顶层 comparison 是「指标名 → 对比块」。
    if let Some(map) = v.get("comparison").and_then(Value::as_object) {
        for (name, c) in map {
            if let Some(cmp) = comparison_from(Some(name), c) {
                out.push(cmp);
            }
        }
    }
    // 探针式: 对比块藏在判据里。
    if let Some(obj) = v.as_object() {
        for (k, c) in obj {
            if !k.starts_with("criterion_") {
                continue;
            }
            if let Some(inner) = c.get("comparison") {
                if let Some(cmp) = comparison_from(None, inner) {
                    if !out.iter().any(|e| e.metric == cmp.metric) {
                        out.push(cmp);
                    }
                }
            }
        }
    }
    out.sort_by(|a, b| a.metric.cmp(&b.metric));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn two_arm_shape_is_parsed() {
        let v = json!({
            "goal": "GOAL v1",
            "primary_metric": "frame_p95",
            "baseline": {"title": "ctrl*", "n_runs": 2, "runs": ["ctrl1", "ctrl2"],
                "metrics": {"frame_p95": {"values": [20.0, 19.7], "dispersion_pct": 2.673}}},
            "knob_arm": {"title": "knob*", "runs": ["knob1", "knob2"]},
            "comparison": {"frame_p95": {"metric": "frame_p95",
                "a_values": [20.0, 19.7], "b_values": [19.8, 19.6],
                "diff_pct": -0.51, "perm_p_two_sided": 0.325397,
                "ci95": [-0.3055, 0.1336], "significant_at_0.05": false}},
            "criterion_3_statistically_significant": {"pass": false},
            "summary": {"criteria_total": 5, "criteria_evaluated": 4,
                "criteria_passed": 3, "all_pass": false}
        });
        let r = AbReport::from_value(&v);
        assert_eq!(r.primary_metric.as_deref(), Some("frame_p95"));
        assert_eq!(r.baseline_runs, vec!["ctrl1", "ctrl2"]);
        assert_eq!(r.baseline_dispersion, vec![("frame_p95".into(), 2.673)]);
        let arms = r.arms().expect("两臂式必须出得来对比图数据");
        assert_eq!(arms.unit, "ms");
        assert_eq!(arms.b_values, vec![19.8, 19.6]);
        let (verdict, text) = r.verdict();
        assert_eq!(verdict, RoundVerdict::Eliminated);
        assert!(text.contains("统计显著"), "应点名是哪条没过: {text}");
        assert!(text.contains("1 条未评"), "未评的判据要说出来: {text}");
    }

    #[test]
    fn probe_shape_finds_comparison_inside_criterion() {
        let v = json!({
            "goal": "refbench",
            "criterion_1_repeatable": {"pass": true, "runs": ["bw_ctrl1", "bw_ctrl2"],
                "dispersion_pct": 0.2658},
            "criterion_3_knob_effective": {"pass": true, "comparison": {
                "metric": "frame_p95", "a_values": [9.93, 9.94], "b_values": [9.63, 9.62],
                "diff_pct": -2.973, "perm_p_two_sided": 0.007937,
                "significant_at_0.05": true}},
            "summary": {"criteria_total": 6, "criteria_passed": 6, "all_pass": true}
        });
        let r = AbReport::from_value(&v);
        assert_eq!(r.baseline_runs, vec!["bw_ctrl1", "bw_ctrl2"], "轮次名要从判据里捞");
        let arms = r.arms().expect("探针式也要出得来对比图数据");
        assert_eq!(arms.metric, "frame_p95");
        assert_eq!(r.verdict().0, RoundVerdict::Qualified);
    }

    #[test]
    fn missing_summary_is_invalid_not_eliminated() {
        let v = json!({"goal": "半截报告"});
        let r = AbReport::from_value(&v);
        let (verdict, text) = r.verdict();
        assert_eq!(verdict, RoundVerdict::Invalid, "下不出结论 ≠ 结论是负的");
        assert!(text.contains("没有汇总判定"));
    }

    #[test]
    fn criterion_names_and_order_are_stable() {
        let v = json!({
            "criterion_5_no_residue": {"pass": true, "identical": true},
            "criterion_1_repeatable": {"pass": true, "dispersion_pct": 0.27},
        });
        let r = AbReport::from_value(&v);
        assert_eq!(r.criteria.len(), 2);
        assert_eq!(r.criteria[0].name, "负载可重复");
        assert_eq!(r.criteria[0].detail.as_deref(), Some("离散度 0.270%"));
        assert_eq!(r.criteria[1].name, "设备无残留");
    }

    #[test]
    fn unknown_criterion_key_keeps_its_own_name() {
        let v = json!({"criterion_9_brand_new": {"pass": true}});
        let r = AbReport::from_value(&v);
        assert_eq!(r.criteria[0].name, "criterion_9_brand_new");
    }

    #[test]
    fn unit_for_known_metrics() {
        assert_eq!(unit_for("frame_p95"), "ms");
        assert_eq!(unit_for("gpu_active_mean"), "ms");
        assert_eq!(unit_for("fps_mean"), "fps");
        assert_eq!(unit_for("说不清的指标"), "");
    }
}
