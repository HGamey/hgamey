//! 本地只读可视化面板。
//!
//! `game_opt_loop serve` 起一个只读 HTTP 服务, 把三层优化闭环散在两个仓库里的
//! 产物读成一份 JSON, 前端静态资源内嵌进二进制 —— **不引任何外网资源, 离线可用**。
//!
//! 三层是:
//!
//! | 层 | 改什么 | 数据在哪 |
//! |---|---|---|
//! | ① 算子 | 超分算子源码自主进化 | `game_opt_loop/runs/<run>/` |
//! | ② 图形接口改写 | Vulkan 灰档层改写应用的 render pass | `phonefarm/loop_v1/runs_graylayer/` |
//! | ③ 系统参数 | 内核/总线旋钮 | `phonefarm/loop_v1/runs*/` |
//!
//! 贯穿全模块的一条规矩: **读不到就是读不到**。缺字段一律 `None`,
//! 前端渲染成「未知/未测」。不编数, 不用 0 填空。

pub mod abtest;
pub mod carrier;
pub mod device;
pub mod model;
pub mod operator;
pub mod runlog;
pub mod serve;
pub mod stats;

use model::{
    evidence_file, Condition, EvidenceFile, Layer, Notice, Round, RoundVerdict, Stat,
};
use serde::Serialize;
use std::path::{Path, PathBuf};

/// 面板要读的几个根目录。
#[derive(Debug, Clone)]
pub struct VizPaths {
    /// `game_opt_loop` 仓库根 (层① 与 evidence)。
    pub gol_root: PathBuf,
    /// `phonefarm` 仓库根 (层②③ 与真机任务)。
    pub phonefarm_root: PathBuf,
    /// `devlock.d` / `devlock.log` 所在目录。
    pub lock_root: PathBuf,
}

impl VizPaths {
    /// 从 `game_opt_loop` 仓库根推出其余路径 —— phonefarm 与它同级,
    /// 这跟 `M0_report.json` 里记的 `phonefarm_binary: ../phonefarm/phonefarm` 一致。
    pub fn from_gol_root(gol_root: PathBuf) -> Self {
        let phonefarm_root = gol_root
            .parent()
            .map(|p| p.join("phonefarm"))
            .unwrap_or_else(|| PathBuf::from("../phonefarm"));
        VizPaths {
            gol_root,
            phonefarm_root,
            lock_root: PathBuf::from("/private/tmp/claude-501"),
        }
    }

    /// `/api/file` 允许读的根 —— 只有这两个仓库, 其它一律拒。
    pub fn allowed_roots(&self) -> Vec<PathBuf> {
        [&self.gol_root, &self.phonefarm_root]
            .iter()
            .filter_map(|p| p.canonicalize().ok())
            .collect()
    }
}

/// 面板的全部数据。前端拉一次 `/api/dashboard` 就够。
#[derive(Debug, Clone, Serialize)]
pub struct Dashboard {
    pub generated_at: String,
    pub version: &'static str,
    pub paths: PathsView,
    pub layers: Vec<Layer>,
    pub carriers: Vec<carrier::Carrier>,
    pub device: device::DeviceView,
    pub notices: Vec<Notice>,
}

#[derive(Debug, Clone, Serialize)]
pub struct PathsView {
    pub game_opt_loop: String,
    pub phonefarm: String,
    pub phonefarm_present: bool,
    pub lock_root: String,
}

/// 把三层 + 载体 + 设备 + 提示全读一遍。
///
/// 任何一块读不出来只影响它自己 —— 面板不会因为 phonefarm 没挂上就整页白屏。
pub fn build(paths: &VizPaths) -> Dashboard {
    Dashboard {
        generated_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
        version: crate::VERSION,
        paths: PathsView {
            game_opt_loop: paths.gol_root.display().to_string(),
            phonefarm: paths.phonefarm_root.display().to_string(),
            phonefarm_present: paths.phonefarm_root.is_dir(),
            lock_root: paths.lock_root.display().to_string(),
        },
        layers: vec![
            operator_layer(paths),
            graphics_layer(paths),
            sysparam_layer(paths),
        ],
        carriers: carrier::collect(&paths.phonefarm_root),
        device: device::load(&paths.lock_root, &paths.phonefarm_root, 8),
        notices: notices(paths),
    }
}

// ───────────────────────────── 层① 算子 ─────────────────────────────

fn operator_layer(paths: &VizPaths) -> Layer {
    let runs = operator::discover(&paths.gol_root.join("runs"));
    let mut rounds = Vec::new();
    let mut problems = Vec::new();
    for run in &runs {
        problems.extend(run.problems.clone());
        rounds.extend(operator::rounds(run, &paths.gol_root));
    }
    // 新的排前面: 演示时先看最近一轮。
    rounds.reverse();
    if runs.is_empty() {
        problems.push(format!(
            "{} 下没有找到任何演化产物 (runs/ 不进版本库, 需要本机跑过才有)",
            paths.gol_root.join("runs").display()
        ));
    }
    Layer {
        id: "operator",
        index: 1,
        name: "算子",
        subtitle: "超分算子源码自主进化",
        rounds_total: 0,
        qualified: 0,
        eliminated: 0,
        invalid: 0,
        evidence_only: 0,
        rounds,
        problems,
    }
    .tally()
}

// ────────────────── 层②③ 共用: 从一份 A/B 报告造一轮 ──────────────────

/// 一个 A/B 轮的装配说明。
struct AbSpec {
    layer: &'static str,
    id: String,
    title: String,
    /// 报告文件 (相对 phonefarm 根)。
    report: &'static str,
    /// 对照臂 / 处理臂运行目录的父目录 (相对 phonefarm 根)。
    arm_parent: &'static str,
    a_prefix: &'static str,
    b_prefix: &'static str,
    /// 这一轮动的是什么, 一句话。
    what: String,
    extra_evidence: Vec<&'static str>,
}

fn ab_round(paths: &VizPaths, spec: &AbSpec) -> Option<Round> {
    let root = &paths.phonefarm_root;
    let report_path = root.join(spec.report);
    let report = abtest::AbReport::load(&report_path).ok()?;
    let (verdict, verdict_text) = report.verdict();
    let primary = report.primary();

    let arm_parent = root.join(spec.arm_parent);
    let a_dirs = numbered_dirs(&arm_parent, spec.a_prefix);
    let b_dirs = numbered_dirs(&arm_parent, spec.b_prefix);

    let mut headline = Vec::new();
    if let Some(cmp) = primary {
        let improved = cmp.diff_pct.is_some_and(|d| d < 0.0);
        headline.push(
            Stat::toned(
                format!("{} 变化", cmp.metric),
                cmp.diff_pct.map(|d| format!("{d:+.2}")),
                Some("%"),
                if cmp.significant == Some(true) && improved {
                    "good"
                } else if improved {
                    "warn"
                } else {
                    "bad"
                },
            )
            .with_hint("负数 = 帧时变小 = 更好"),
        );
        headline.push(Stat::toned(
            "p 值",
            cmp.p_value.map(|p| format!("{p:.4}")),
            None,
            match cmp.significant {
                Some(true) => "good",
                Some(false) => "bad",
                None => "plain",
            },
        ));
        headline.push(Stat::plain(
            "95% 置信区间",
            cmp.ci95.map(|(lo, hi)| format!("{lo:.3} ~ {hi:.3}")),
            Some("ms"),
        ));
    }
    let base_disp = report
        .baseline_dispersion
        .iter()
        .find(|(m, _)| Some(m.as_str()) == report.primary_metric.as_deref())
        .map(|(_, d)| *d);
    headline.push(
        Stat::toned(
            "对照臂离散度",
            base_disp.map(|d| format!("{d:.2}")),
            Some("%"),
            match base_disp {
                Some(d) if d <= 5.0 => "good",
                Some(_) => "bad",
                None => "plain",
            },
        )
        .with_hint("载体自己散得比效应还大, 结论就立不住"),
    );
    headline.push(Stat::plain(
        "每臂轮数",
        Some(format!(
            "{} / {}",
            report.baseline_runs.len().max(a_dirs.len()),
            report.knob_runs.len().max(b_dirs.len())
        )),
        Some("轮"),
    ));

    let first_a = a_dirs.first();
    let first_b = b_dirs.first();
    let snap_a = first_a.and_then(|d| runlog::DeviceSnapshot::load(d));
    let snap_b = first_b.and_then(|d| runlog::DeviceSnapshot::load(d));
    let (temp_b, pkg_b) = first_b
        .map(|d| runlog::workload_facts(d))
        .unwrap_or((None, None));
    let attribution = first_b.and_then(|d| runlog::Attribution::load(d));

    let mut conditions = vec![
        Condition::new("动的是什么", Some(spec.what.clone()), Some(spec.report)),
        Condition::new("被测应用", pkg_b, Some("workload.log")),
        Condition::new(
            "帧率设置",
            snap_b.as_ref().and_then(|s| s.refresh_rate_setting()),
            Some("snap_at_run.txt"),
        ),
        Condition::new(
            "实测限帧",
            attribution
                .as_ref()
                .and_then(|a| a.measure_f64("detected_fps_cap"))
                .map(|v| format!("{v:.0} fps")),
            Some("attribution.json"),
        ),
        Condition::new(
            "电池温度 (处理臂首轮)",
            temp_b.map(|t| format!("{t:.1} ℃")),
            Some("workload.log"),
        ),
        Condition::new(
            "热事件",
            attribution
                .as_ref()
                .and_then(|a| a.measure_f64("n_thermal_events"))
                .map(|v| format!("{v:.0} 次")),
            Some("attribution.json"),
        ),
        Condition::new(
            "处理臂首轮归因",
            attribution.as_ref().and_then(|a| a.verdict.clone()),
            Some("attribution.json"),
        ),
        Condition::new(
            "还能动的余量",
            attribution.as_ref().and_then(|a| a.actionable.clone()),
            Some("attribution.json"),
        ),
        // 这两项设备侧没有任何文件回传, 只在人写的 RESULT.md 里 —— 不猜。
        Condition::new("主动散热", None, None),
        Condition::new("供电状态", None, None),
        Condition::new("游戏版本", None, None),
    ];

    // 两臂设备快照的差异 —— "这一轮到底改了哪个参数" 不靠人写标签, 靠对比。
    if let (Some(a), Some(b)) = (&snap_a, &snap_b) {
        let diff = a.diff(b);
        let text = if diff.is_empty() {
            "两臂设备状态快照逐行一致".to_string()
        } else {
            diff.iter()
                .take(6)
                .map(|(k, av, bv)| format!("{k}: {av} → {bv}"))
                .collect::<Vec<_>>()
                .join("; ")
        };
        conditions.push(Condition::new(
            "两臂快照差异",
            Some(text),
            Some("snap_at_run.txt"),
        ));
    }

    let mut evidence = vec![evidence_file(root, &report_path, "A/B 验收报告")];
    for (label, dir) in [("对照臂首轮", first_a), ("处理臂首轮", first_b)] {
        if let Some(d) = dir {
            for f in ["summary.json", "attribution.json", "snap_at_run.txt"] {
                let p = d.join(f);
                if p.exists() {
                    evidence.push(evidence_file(root, &p, &format!("{label} {f}")));
                }
            }
        }
    }
    for extra in &spec.extra_evidence {
        let p = root.join(extra);
        if p.exists() {
            evidence.push(evidence_file(
                root,
                &p,
                p.file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| (*extra).to_string())
                    .as_str(),
            ));
        }
    }

    let mut notes = Vec::new();
    if let Some(goal) = &report.goal {
        notes.push(goal.clone());
    }
    if let Some(v) = &report.attribution_verdict {
        notes.push(format!("归因: {v}"));
    }
    if let Some(e) = &report.attribution_evidence {
        notes.push(e.clone());
    }

    Some(Round {
        id: spec.id.clone(),
        layer: spec.layer,
        title: spec.title.clone(),
        when: std::fs::metadata(&report_path)
            .ok()
            .and_then(|m| m.modified().ok())
            .map(|t| {
                let dt: chrono::DateTime<chrono::Local> = t.into();
                dt.to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
            }),
        verdict,
        verdict_text,
        metric_source: "真机实测".to_string(),
        headline,
        candidates: Vec::new(),
        arms: report.arms(),
        criteria: report.criteria.clone(),
        conditions,
        pareto: None,
        evidence,
        notes,
    })
}

/// 目录名 = 前缀 + 数字, 按数字排序。
fn numbered_dirs(parent: &Path, prefix: &str) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(parent) else {
        return Vec::new();
    };
    let mut out: Vec<(u32, PathBuf)> = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .filter_map(|p| {
            let name = p.file_name()?.to_str()?.to_string();
            let n: u32 = name.strip_prefix(prefix)?.parse().ok()?;
            Some((n, p))
        })
        .collect();
    out.sort_by_key(|(n, _)| *n);
    out.into_iter().map(|(_, p)| p).collect()
}

/// 只有证据、没有 A/B 报告的那些目录 —— 挂上链接, 但**不计进三色计数**。
fn evidence_round(
    paths: &VizPaths,
    layer: &'static str,
    id: &str,
    title: &str,
    dir_rel: &str,
    note: &str,
) -> Option<Round> {
    let dir = paths.phonefarm_root.join(dir_rel);
    if !dir.is_dir() {
        return None;
    }
    let mut evidence: Vec<EvidenceFile> = Vec::new();
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_file())
        .collect();
    files.sort();
    for f in files.iter().take(12) {
        let label = f.file_name()?.to_string_lossy().to_string();
        evidence.push(evidence_file(&paths.phonefarm_root, f, &label));
    }
    // RESULT.md 是人写的结论, 排到最前面, 演示时一点就开。
    evidence.sort_by_key(|e| !e.rel.ends_with("RESULT.md"));
    Some(Round {
        id: id.to_string(),
        layer,
        title: title.to_string(),
        when: None,
        verdict: RoundVerdict::Evidence,
        verdict_text: note.to_string(),
        metric_source: "见文件".to_string(),
        headline: vec![Stat::plain(
            "证据文件",
            Some(evidence.len().to_string()),
            Some("份"),
        )],
        candidates: Vec::new(),
        arms: None,
        criteria: Vec::new(),
        conditions: vec![Condition::new(
            "机读判定",
            None,
            Some("本目录没有 report.json, 结论在 RESULT.md 里由人写"),
        )],
        pareto: None,
        evidence,
        notes: Vec::new(),
    })
}

/// 跑了两臂但没出报告的目录 —— 那是**无法判定**, 老实算无效。
fn unreported_round(
    paths: &VizPaths,
    layer: &'static str,
    id: &str,
    title: &str,
    dir_rel: &str,
    a_prefix: &str,
    b_prefix: &str,
) -> Option<Round> {
    let dir = paths.phonefarm_root.join(dir_rel);
    if !dir.is_dir() {
        return None;
    }
    if dir.join("report.json").exists() {
        return None;
    }
    let a = numbered_dirs(&dir, a_prefix);
    let b = numbered_dirs(&dir, b_prefix);
    if a.is_empty() && b.is_empty() {
        return None;
    }
    let evidence = a
        .iter()
        .chain(b.iter())
        .filter(|d| d.join("summary.json").exists())
        .map(|d| {
            evidence_file(
                &paths.phonefarm_root,
                &d.join("summary.json"),
                &format!(
                    "{} 汇总",
                    d.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default()
                ),
            )
        })
        .collect();
    Some(Round {
        id: id.to_string(),
        layer,
        title: title.to_string(),
        when: None,
        verdict: RoundVerdict::Invalid,
        verdict_text: format!(
            "只有逐轮数据 ({} 对照 / {} 处理), 没有验收报告, 判定不出来",
            a.len(),
            b.len()
        ),
        metric_source: "真机实测".to_string(),
        headline: vec![
            Stat::toned("对照臂", Some(a.len().to_string()), Some("轮"), "warn"),
            Stat::toned("处理臂", Some(b.len().to_string()), Some("轮"), "warn"),
        ],
        candidates: Vec::new(),
        arms: None,
        criteria: Vec::new(),
        conditions: vec![Condition::new("验收报告", None, Some("目录下没有 report.json"))],
        pareto: None,
        evidence,
        notes: Vec::new(),
    })
}

// ──────────────────────── 层② 图形接口改写 ────────────────────────

fn graphics_layer(paths: &VizPaths) -> Layer {
    let mut rounds = Vec::new();
    let mut problems = Vec::new();

    let spec = AbSpec {
        layer: "graphics",
        id: "gfx:graylayer-loadop".to_string(),
        title: "原神世界场景 · loadOp LOAD → DONT_CARE".to_string(),
        report: "loop_v1/runs_graylayer/report.json",
        arm_parent: "loop_v1/runs_graylayer/ab",
        a_prefix: "ctrl",
        b_prefix: "knob",
        what: "Vulkan 灰档层把应用的 render pass loadOp 从 LOAD 改成 DONT_CARE (应用不知情)"
            .to_string(),
        extra_evidence: vec![
            "loop_v1/runs_graylayer/ab/knob1/knobs_layer_out.json",
            "knobs/evidence/06_genshin_world/RESULT.md",
            "knobs/evidence/06_genshin_world/ab_arms.tsv",
        ],
    };
    match ab_round(paths, &spec) {
        Some(r) => rounds.push(r),
        None => problems.push(format!(
            "{} 读不出来",
            paths.phonefarm_root.join(spec.report).display()
        )),
    }

    if let Some(r) = unreported_round(
        paths,
        "graphics",
        "gfx:graylayer-ab2",
        "原神世界场景 · 第二批 (未出报告)",
        "loop_v1/runs_graylayer/ab2",
        "ctrl",
        "knob",
    ) {
        rounds.push(r);
    }

    for (id, title, dir, note) in [
        (
            "gfx:ev-05",
            "改写机制验证 · refbench 四臂",
            "knobs/evidence/05_loadop",
            "只验改写是否真生效, 不产性能结论",
        ),
        (
            "gfx:ev-01",
            "灰档层探针 · refbench",
            "knobs/evidence/01_probe_refbench",
            "层能不能加载进白档靶子",
        ),
        (
            "gfx:ev-02",
            "灰档层落靶 · 原神",
            "knobs/evidence/02_target_genshin",
            "层能不能加载进商业游戏",
        ),
        (
            "gfx:ev-04",
            "撤层后复核",
            "knobs/evidence/04_reverify",
            "把层撤掉后设备回到原状",
        ),
    ] {
        if let Some(r) = evidence_round(paths, "graphics", id, title, dir, note) {
            rounds.push(r);
        }
    }

    Layer {
        id: "graphics",
        index: 2,
        name: "图形接口改写",
        subtitle: "Vulkan 灰档层改写 render pass",
        rounds_total: 0,
        qualified: 0,
        eliminated: 0,
        invalid: 0,
        evidence_only: 0,
        rounds,
        problems,
    }
    .tally()
}

// ───────────────────────── 层③ 系统参数 ─────────────────────────

fn sysparam_layer(paths: &VizPaths) -> Layer {
    let mut rounds = Vec::new();
    let mut problems = Vec::new();

    let specs = [
        AbSpec {
            layer: "sysparam",
            id: "sys:genshin-ddr".to_string(),
            title: "原神巡航 · 总线/DDR 旋钮".to_string(),
            report: "loop_v1/runs/report.json",
            arm_parent: "loop_v1/runs",
            a_prefix: "ctrl",
            b_prefix: "knob",
            what: "内核侧总线与 DDR 频率投票 (具体改了哪个键看下面的两臂快照差异)"
                .to_string(),
            extra_evidence: vec![],
        },
        AbSpec {
            layer: "sysparam",
            id: "sys:refbench-bw".to_string(),
            title: "refbench 带宽场景 · 旋钮".to_string(),
            report: "loop_v1/runs_refbench/report.json",
            arm_parent: "loop_v1/runs_refbench",
            a_prefix: "bw_ctrl",
            b_prefix: "bw_knob",
            what: "白档靶子自带的 loadop 旋钮 (有已知答案, 用来校准整条链路)".to_string(),
            extra_evidence: vec![],
        },
    ];
    for spec in &specs {
        match ab_round(paths, spec) {
            Some(r) => rounds.push(r),
            None => problems.push(format!(
                "{} 读不出来",
                paths.phonefarm_root.join(spec.report).display()
            )),
        }
    }

    Layer {
        id: "sysparam",
        index: 3,
        name: "系统参数",
        subtitle: "内核 / 总线旋钮",
        rounds_total: 0,
        qualified: 0,
        eliminated: 0,
        invalid: 0,
        evidence_only: 0,
        rounds,
        problems,
    }
    .tally()
}

// ─────────────────────────────── 提示 ───────────────────────────────

/// 页顶提示条: 撤回的结论、缺的数据源。
///
/// M1 的功耗结论已经撤回 —— 这一条**必须**顶在最上面, 不能只躺在 JSON 里。
fn notices(paths: &VizPaths) -> Vec<Notice> {
    let mut out = Vec::new();
    let evidence_dir = paths.gol_root.join("evidence");

    for name in ["M1_report.json", "M0_report.json", "M2_realframes_report.json"] {
        let path = evidence_dir.join(name);
        let Ok(raw) = std::fs::read_to_string(&path) else {
            continue;
        };
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
            continue;
        };
        let Some(r) = v.get("retraction") else {
            continue;
        };
        let scope = r.get("scope").and_then(|s| s.as_str()).unwrap_or("部分结论");
        let date = r.get("date").and_then(|s| s.as_str()).unwrap_or("未记日期");
        let mut body = r
            .get("statement")
            .and_then(|s| s.as_str())
            .unwrap_or("报告里未写撤回理由")
            .to_string();
        if let Some(still) = r.get("still_valid").and_then(|s| s.as_str()) {
            body.push_str("\n仍然有效: ");
            body.push_str(still);
        }
        if let Some(sup) = r.get("superseded_by").and_then(|s| s.as_str()) {
            body.push_str("\n被取代于: ");
            body.push_str(sup);
        }
        out.push(Notice {
            level: "retracted",
            title: format!("{name} 的 {scope} 已撤回 ({date})"),
            body,
            evidence: Some(evidence_file(&paths.gol_root, &path, name)),
        });
    }

    if !paths.phonefarm_root.is_dir() {
        out.push(Notice {
            level: "warn",
            title: "phonefarm 目录没找到".to_string(),
            body: format!(
                "层②③ 与载体可复现性都读不到。找的是 {}, 可以用 --phonefarm-root 指过去。",
                paths.phonefarm_root.display()
            ),
            evidence: None,
        });
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phonefarm_root_defaults_to_sibling() {
        let p = VizPaths::from_gol_root(PathBuf::from("/a/b/game_opt_loop"));
        assert_eq!(p.phonefarm_root, PathBuf::from("/a/b/phonefarm"));
    }

    #[test]
    fn numbered_dirs_sorts_numerically_not_lexically() {
        let tmp = std::env::temp_dir().join(format!("gol_viz_numdirs_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        for n in ["ctrl1", "ctrl2", "ctrl10", "ctrl_extra", "knob1"] {
            std::fs::create_dir_all(tmp.join(n)).unwrap();
        }
        let dirs = numbered_dirs(&tmp, "ctrl");
        let names: Vec<String> = dirs
            .iter()
            .map(|d| d.file_name().unwrap().to_string_lossy().to_string())
            .collect();
        assert_eq!(names, vec!["ctrl1", "ctrl2", "ctrl10"]);
        std::fs::remove_dir_all(&tmp).unwrap();
    }

    #[test]
    fn missing_phonefarm_yields_a_notice_not_a_panic() {
        let paths = VizPaths {
            gol_root: PathBuf::from("/nonexistent/gol"),
            phonefarm_root: PathBuf::from("/nonexistent/phonefarm"),
            lock_root: PathBuf::from("/nonexistent/lock"),
        };
        let dash = build(&paths);
        assert_eq!(dash.layers.len(), 3);
        assert!(!dash.paths.phonefarm_present);
        assert!(dash.notices.iter().any(|n| n.level == "warn"));
        // 三层都在, 只是空的 —— 页面结构不该因为数据缺失而变形。
        assert!(dash.layers.iter().all(|l| l.rounds_total == 0));
    }

    #[test]
    fn allowed_roots_only_contain_existing_dirs() {
        let paths = VizPaths {
            gol_root: PathBuf::from(env!("CARGO_MANIFEST_DIR")),
            phonefarm_root: PathBuf::from("/nonexistent/phonefarm"),
            lock_root: PathBuf::from("/nonexistent/lock"),
        };
        let roots = paths.allowed_roots();
        assert_eq!(roots.len(), 1, "不存在的根不该进白名单");
    }
}
