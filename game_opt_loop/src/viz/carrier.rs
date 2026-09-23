//! 载体可复现性: 同一个载体连着跑 N 轮, 帧时散得有多开。
//!
//! 这一页是整个面板里**最该先看**的一页。载体自己散 5% 的时候, 一个
//! "改善 0.5%" 的结论没有意义 —— 所以离散度必须和结论并排摆着。
//!
//! 数字来源分两种:
//! - 报告里已经算好的 (`loop_v1/*/report.json` 的 `dispersion_pct`) —— 直接取。
//! - 只有逐轮 `summary.json` 的 (例如 Megacity 基线) —— 自己按同一个口径算
//!   (见 [`crate::viz::stats::dispersion_pct`], 有测试把口径钉在报告上)。

use crate::viz::model::{evidence_file, EvidenceFile};
use crate::viz::runlog::RunSummary;
use crate::viz::stats::{dispersion_pct, median, round_to};
use serde::Serialize;
use std::path::{Path, PathBuf};

/// 一个载体在一个时间窗里的可复现性。
#[derive(Debug, Clone, Serialize)]
pub struct Carrier {
    pub carrier: String,
    /// 这一组是什么窗口 (深夜 / 跨黄昏 / 基线…) —— 同一个载体不同时段散得不一样。
    pub window: String,
    pub n_runs: usize,
    pub p95_dispersion_pct: Option<f64>,
    pub p50_dispersion_pct: Option<f64>,
    pub p95_median_ms: Option<f64>,
    pub fps_median: Option<f64>,
    /// 判据阈值 (报告里写明的才有)。`None` = 这一组没有冻结阈值, 只报数。
    pub gate_pct: Option<f64>,
    /// 阈值卡的是哪个指标。`None` = 没有阈值。
    ///
    /// 必须跟着阈值一起带: refbench 的 0.5% 卡的是**中位**帧时
    /// (`repeatability_metric: frame_p50`), 拿它去卡 p95 会把报告里明明
    /// 判过的一组显示成不合格 —— 面板与报告打架, 演示时没法解释。
    pub gate_metric: Option<&'static str>,
    /// `pass` / `fail` / `unknown`
    pub status: &'static str,
    pub note: Option<String>,
    pub runs: Vec<String>,
    /// 读这一组时跳过了哪些目录 —— 静悄悄少算几轮比算错更难发现。
    pub problems: Vec<String>,
    pub evidence: Vec<EvidenceFile>,
}

/// 一组要统计的运行目录。
pub struct Group {
    pub carrier: &'static str,
    pub window: &'static str,
    /// 这些运行目录的父目录 (相对 phonefarm 根)。
    pub parent: &'static str,
    /// 目录名前缀, 例如 `mc_base` 会吃到 `mc_base1` / `mc_base2` / …
    pub prefix: &'static str,
    /// 冻结判据里的离散度阈值; `None` = 这一组没有冻结阈值。
    pub gate_pct: Option<f64>,
    /// 阈值卡在哪个指标上 (`frame_p50` / `frame_p95`)。
    pub gate_metric: Option<&'static str>,
    pub note: Option<&'static str>,
}

/// 面板里要展示的那几组。
///
/// 顺序就是页面顺序: 白档载体在前 (可复现性最好, 是尺子),
/// 商业游戏在后 (散得最开, 是靶子)。
pub const GROUPS: &[Group] = &[
    Group {
        carrier: "refbench (白档)",
        window: "带宽场景 · 对照臂",
        parent: "loop_v1/runs_refbench",
        prefix: "bw_ctrl",
        gate_pct: Some(0.5),
        gate_metric: Some("frame_p50"),
        note: Some("自研载体, 冻结阈值按中位帧时判 0.5%"),
    },
    Group {
        carrier: "refbench (白档)",
        window: "片元场景",
        parent: "loop_v1/runs_refbench",
        prefix: "frag",
        gate_pct: Some(0.5),
        gate_metric: Some("frame_p50"),
        note: Some("同上, 阈值卡中位帧时"),
    },
    Group {
        carrier: "Megacity",
        window: "城市巡航 · 基线",
        parent: "loop_v1/runs",
        prefix: "mc_base",
        gate_pct: None,
        gate_metric: None,
        note: Some("尚无冻结判据, 只报离散度"),
    },
    Group {
        carrier: "原神",
        window: "世界场景 · 60fps",
        parent: "loop_v1/runs_graylayer/ab",
        prefix: "ctrl",
        gate_pct: Some(5.0),
        gate_metric: Some("frame_p95"),
        note: None,
    },
    Group {
        carrier: "原神",
        window: "巡航 · 深夜",
        parent: "loop_v1/runs",
        prefix: "night",
        gate_pct: Some(5.0),
        gate_metric: Some("frame_p95"),
        note: None,
    },
    Group {
        carrier: "原神",
        window: "巡航 · 跨黄昏",
        parent: "loop_v1/runs",
        prefix: "base",
        gate_pct: Some(5.0),
        gate_metric: Some("frame_p95"),
        note: Some("跨天色变化, 场景本身在变 —— 散得最开的一组"),
    },
];

/// 目录名 = 前缀 + 纯数字。
///
/// 必须卡住"后面只能是数字": `ctrl` 前缀否则会把 `ctrl_extra` 之类一起吃进来,
/// 而 `base` 前缀更危险 —— 它会吃掉 `base1` 也吃掉 `mc_base1` 之外的任何
/// `base*`, 混进不同载体的轮次会直接把离散度算错。
fn matches(name: &str, prefix: &str) -> bool {
    match name.strip_prefix(prefix) {
        Some(rest) => !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()),
        None => false,
    }
}

fn collect_dirs(parent: &Path, prefix: &str) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(parent) else {
        return Vec::new();
    };
    let mut dirs: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.is_dir()
                && p.file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| matches(n, prefix))
        })
        .collect();
    dirs.sort();
    dirs
}

/// 按 [`GROUPS`] 扫一遍 phonefarm, 组不存在就跳过 (不占位、不编数)。
pub fn collect(phonefarm_root: &Path) -> Vec<Carrier> {
    GROUPS
        .iter()
        .filter_map(|g| one(phonefarm_root, g))
        .collect()
}

fn one(phonefarm_root: &Path, group: &Group) -> Option<Carrier> {
    let parent = phonefarm_root.join(group.parent);
    let dirs = collect_dirs(&parent, group.prefix);
    if dirs.is_empty() {
        return None;
    }

    let mut runs = Vec::new();
    let mut p95 = Vec::new();
    let mut p50 = Vec::new();
    let mut fps = Vec::new();
    let mut evidence = Vec::new();
    let mut problems = Vec::new();
    for d in &dirs {
        let name = d.file_name()?.to_string_lossy().to_string();
        let Some(s) = RunSummary::load(d) else {
            // 目录在、汇总读不出来 —— 说出来, 不要让这一轮无声地从统计里消失。
            problems.push(format!("{name}/summary.json 读不出来, 这一轮没进统计"));
            continue;
        };
        runs.push(name);
        // 按指标名取 —— 与前端切换指标、与报告里的键名走同一套名字。
        for (name, bucket) in [
            ("frame_p95", &mut p95),
            ("frame_p50", &mut p50),
            ("fps_mean", &mut fps),
        ] {
            if let Some(v) = s.metric(name) {
                bucket.push(v);
            }
        }
        if evidence.len() < 3 {
            evidence.push(evidence_file(
                phonefarm_root,
                &d.join("summary.json"),
                &format!("{} 汇总", d.file_name()?.to_string_lossy()),
            ));
        }
    }
    if runs.is_empty() {
        return None;
    }

    let p95_disp = dispersion_pct(&p95).map(|v| round_to(v, 3));
    let p50_disp = dispersion_pct(&p50).map(|v| round_to(v, 3));
    // 判的是阈值指定的那个指标, 不是永远拿 p95 去判。
    let gated = match group.gate_metric {
        Some("frame_p50") => p50_disp,
        Some("frame_p95") => p95_disp,
        _ => None,
    };
    let status = match (gated, group.gate_pct) {
        (Some(d), Some(gate)) if d <= gate => "pass",
        (Some(_), Some(_)) => "fail",
        _ => "unknown",
    };
    if runs.len() != p95.len() {
        problems.push(format!(
            "{} 轮里有 {} 轮没有 frame_p95, 离散度只按剩下的算",
            runs.len(),
            runs.len() - p95.len()
        ));
    }

    Some(Carrier {
        carrier: group.carrier.to_string(),
        window: group.window.to_string(),
        n_runs: runs.len(),
        p95_dispersion_pct: p95_disp,
        p50_dispersion_pct: p50_disp,
        p95_median_ms: median(&p95).map(|v| round_to(v, 3)),
        fps_median: median(&fps).map(|v| round_to(v, 2)),
        gate_pct: group.gate_pct,
        gate_metric: group.gate_metric,
        status,
        note: group.note.map(str::to_string),
        runs,
        problems,
        evidence,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefix_match_requires_trailing_digits() {
        assert!(matches("mc_base1", "mc_base"));
        assert!(matches("ctrl5", "ctrl"));
        assert!(!matches("ctrl", "ctrl"), "光是前缀本身不算一轮");
        assert!(!matches("ctrl_extra", "ctrl"));
        assert!(!matches("knob1", "ctrl"));
    }

    /// `base` 与 `mc_base` 是两个不同载体, 绝不能互相吃进来。
    #[test]
    fn base_prefix_does_not_swallow_mc_base() {
        assert!(!matches("mc_base1", "base"));
        assert!(matches("base1", "base"));
    }

    /// 每一组阈值都必须写明卡的是哪个指标 —— 否则拿 p95 去套 p50 的阈值,
    /// 面板会把报告里明明判过的一组显示成不合格。
    #[test]
    fn every_gate_declares_which_metric_it_gates() {
        for g in GROUPS {
            assert_eq!(
                g.gate_pct.is_some(),
                g.gate_metric.is_some(),
                "{} / {} 的阈值与指标必须同时有或同时无",
                g.carrier,
                g.window
            );
            if let Some(m) = g.gate_metric {
                assert!(
                    m == "frame_p50" || m == "frame_p95",
                    "{} / {} 卡了一个判不了的指标 {m}",
                    g.carrier,
                    g.window
                );
            }
        }
    }

    #[test]
    fn collect_skips_missing_root() {
        assert!(collect(Path::new("/nonexistent/phonefarm")).is_empty());
    }
}
