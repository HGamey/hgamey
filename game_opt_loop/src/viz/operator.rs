//! 层① 算子: 读 `runs/<run>/{archive.json, gen_*.json}`。
//!
//! 这里刻意**复用 crate 自己的结构体** (`ParetoArchive` / `GenerationRecord`)
//! 而不是另写一套面板专用的 DTO: 档案格式改了, 编译期就会红, 不会出现
//! "面板还在按老格式读、显示出来的数早就不是档案里那份"这种事。

use crate::engine::evolution::{GenerationRecord, OutcomeStage};
use crate::engine::{EvalStatus, MetricSource, ParetoArchive};
use crate::viz::model::{
    evidence_file, CandidateRow, Condition, ParetoChart, ParetoPoint, Round, RoundVerdict, Stat,
};
use crate::viz::stats::round_to;
use std::collections::HashSet;
use std::path::{Path, PathBuf};

/// 一次演化 (一个 `runs/<run>/` 目录)。
pub struct EvolutionRun {
    pub name: String,
    pub dir: PathBuf,
    pub archive: Option<ParetoArchive>,
    pub generations: Vec<GenerationRecord>,
    pub problems: Vec<String>,
}

/// 扫 `runs/` 下所有含 `archive.json` 或 `gen_*.json` 的目录, 按名字排序。
pub fn discover(runs_dir: &Path) -> Vec<EvolutionRun> {
    let Ok(entries) = std::fs::read_dir(runs_dir) else {
        return Vec::new();
    };
    let mut dirs: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| p.is_dir())
        .collect();
    dirs.sort();
    dirs.iter().filter_map(|d| load_run(d)).collect()
}

fn load_run(dir: &Path) -> Option<EvolutionRun> {
    let mut problems = Vec::new();
    let archive_path = dir.join("archive.json");
    let archive = if archive_path.exists() {
        match ParetoArchive::load(&archive_path) {
            Ok(a) => Some(a),
            Err(e) => {
                problems.push(format!("{} 读不出来: {e}", archive_path.display()));
                None
            }
        }
    } else {
        None
    };

    let mut gen_paths: Vec<PathBuf> = std::fs::read_dir(dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|e| e.path())
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("gen_") && n.ends_with(".json"))
        })
        .collect();
    // 按代号数字排序, 不能按字典序 —— gen_10 要排在 gen_2 后面。
    gen_paths.sort_by_key(|p| generation_index(p).unwrap_or(u32::MAX));

    let mut generations = Vec::new();
    for p in &gen_paths {
        match std::fs::read_to_string(p).map_err(|e| e.to_string()).and_then(|raw| {
            serde_json::from_str::<GenerationRecord>(&raw).map_err(|e| e.to_string())
        }) {
            Ok(rec) => generations.push(rec),
            Err(e) => problems.push(format!("{} 读不出来: {e}", p.display())),
        }
    }

    if archive.is_none() && generations.is_empty() && problems.is_empty() {
        return None;
    }
    Some(EvolutionRun {
        name: dir
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| dir.display().to_string()),
        dir: dir.to_path_buf(),
        archive,
        generations,
        problems,
    })
}

/// `…/gen_12.json` → 12。
fn generation_index(path: &Path) -> Option<u32> {
    path.file_stem()?
        .to_str()?
        .strip_prefix("gen_")?
        .parse()
        .ok()
}

/// 单个候选的判定。
///
/// - 没走完全程 (重复指纹 / 静态预检打回 / 评测出错) → **无效**: 没测过就没有结论。
/// - 评了, 且进了支配前沿 → 合格。
/// - 评了但没进前沿, 或画质/耗时被一票否决 → 淘汰。
fn classify(
    stage: OutcomeStage,
    status: Option<EvalStatus>,
    on_front: Option<bool>,
) -> (RoundVerdict, String) {
    match stage {
        OutcomeStage::DuplicateSkipped => {
            (RoundVerdict::Invalid, "指纹与既有算子重复, 未上机".to_string())
        }
        OutcomeStage::GateRejected => {
            (RoundVerdict::Invalid, "静态预检打回, 未上机".to_string())
        }
        OutcomeStage::EvalError => (RoundVerdict::Invalid, "评测过程出错".to_string()),
        OutcomeStage::Evaluated => match status {
            Some(EvalStatus::Pass) => match on_front {
                Some(true) => (RoundVerdict::Qualified, "进入支配前沿".to_string()),
                Some(false) => (RoundVerdict::Eliminated, "通过评测但被前沿支配".to_string()),
                None => (RoundVerdict::Eliminated, "通过评测 (档案读不到, 无法判前沿)".to_string()),
            },
            Some(EvalStatus::PoorQuality) => {
                (RoundVerdict::Eliminated, "画质低于基线, 一票否决".to_string())
            }
            Some(EvalStatus::Slow) => (RoundVerdict::Eliminated, "超出耗时预算".to_string()),
            Some(EvalStatus::Crash) => (RoundVerdict::Eliminated, "真机崩溃".to_string()),
            Some(EvalStatus::Reverted) => (RoundVerdict::Eliminated, "已回滚".to_string()),
            None => (RoundVerdict::Invalid, "标称已评测但没有报告".to_string()),
        },
    }
}

fn source_text(src: MetricSource) -> &'static str {
    match src {
        MetricSource::Phonefarm => "真机实测",
        // 这行字不能省: 成本模型的数是估计值, 不带 p 值, 不能当性能结论看。
        MetricSource::StaticCostModel => "静态估计 (非测量)",
    }
}

/// 把一次演化的每一代变成一轮。
pub fn rounds(run: &EvolutionRun, gol_root: &Path) -> Vec<Round> {
    let front_ids: Option<HashSet<String>> = run
        .archive
        .as_ref()
        .map(|a| a.front().iter().map(|e| e.id.clone()).collect());

    run.generations
        .iter()
        .map(|gen| round_for_generation(run, gen, front_ids.as_ref(), gol_root))
        .collect()
}

fn round_for_generation(
    run: &EvolutionRun,
    gen: &GenerationRecord,
    front_ids: Option<&HashSet<String>>,
    gol_root: &Path,
) -> Round {
    let mut candidates = Vec::new();
    for o in &gen.outcomes {
        let status = o.report.as_ref().map(|r| r.status);
        let on_front = front_ids.map(|f| f.contains(&o.id));
        let (verdict, verdict_text) = classify(o.stage, status, on_front);
        let metrics = o.report.as_ref().map(|r| &r.metrics);
        candidates.push(CandidateRow {
            id: o.id.clone(),
            origin: origin_text(&o.origin).to_string(),
            notes: o.notes.clone(),
            stage: stage_text(o.stage).to_string(),
            verdict,
            verdict_text,
            latency_ms: metrics.map(|m| round_to(m.operator_latency_ms as f64, 4)),
            frame_p95_ms: metrics
                .and_then(|m| m.fps_p95_ms)
                .map(|v| round_to(v as f64, 3)),
            power_watt: metrics
                .and_then(|m| m.power_watt)
                .map(|v| round_to(v as f64, 4)),
            psnr_db: metrics
                .and_then(|m| m.psnr_db)
                .map(|v| round_to(v as f64, 3)),
            p_value: o.report.as_ref().map(|r| r.verdict.p_value),
            on_front,
            power_excluded_reason: power_excluded_reason(run, &o.id),
            violations: o
                .gate
                .as_ref()
                .map(|g| g.violations.iter().map(|v| v.code.to_string()).collect())
                .unwrap_or_default(),
        });
    }

    let qualified = candidates
        .iter()
        .filter(|c| c.verdict == RoundVerdict::Qualified)
        .count();
    let eliminated = candidates
        .iter()
        .filter(|c| c.verdict == RoundVerdict::Eliminated)
        .count();
    let invalid = candidates
        .iter()
        .filter(|c| c.verdict == RoundVerdict::Invalid)
        .count();

    // 一代的判定: 只要有候选进了前沿就算这一代有收获。
    let (verdict, verdict_text) = if qualified > 0 {
        (
            RoundVerdict::Qualified,
            format!("{qualified} 个候选进入前沿"),
        )
    } else if eliminated > 0 {
        (
            RoundVerdict::Eliminated,
            format!("{eliminated} 个候选测完但都没进前沿"),
        )
    } else {
        (
            RoundVerdict::Invalid,
            format!("{invalid} 个候选都没测上 (重复/预检打回/出错)"),
        )
    };

    let quality_ref = gen
        .outcomes
        .iter()
        .filter_map(|o| o.report.as_ref())
        .filter_map(|r| r.quality_reference.as_ref())
        .next();

    let headline = vec![
        Stat::toned(
            "本代最优画质",
            gen.gen_best_psnr.map(|v| format!("{v:.2}")),
            Some("dB"),
            "good",
        )
        .with_hint("绝对 dB 只在同一组参考图内可比"),
        Stat::plain(
            "历史最低耗时",
            gen.best_latency_so_far.map(|v| format!("{v:.3}")),
            Some("ms"),
        ),
        Stat::plain("前沿规模", Some(gen.front_size.to_string()), Some("个")),
        Stat::toned("合格", Some(qualified.to_string()), Some("个"), "good"),
        Stat::toned("淘汰", Some(eliminated.to_string()), Some("个"), "bad"),
        Stat::toned("无效", Some(invalid.to_string()), Some("个"), "warn"),
    ];

    let mut conditions = vec![
        Condition::new("指标来源", Some(source_text(gen.metric_source).to_string()), Some("gen_*.json")),
        Condition::new(
            "画质真值",
            quality_ref.map(|q| {
                let kind = if q.kind == "real_frames" {
                    "真机截帧"
                } else {
                    "程序化图案"
                };
                format!("{kind} × {} 张", q.frames)
            }),
            Some("report.quality_reference"),
        ),
        Condition::new(
            "参考帧来源",
            quality_ref.and_then(|q| q.source.clone()),
            Some("report.quality_reference.source"),
        ),
        Condition::new(
            "供电状态",
            power_source_text(run, gen),
            Some("report.power_source"),
        ),
        Condition::new("变异来源", Some(origin_counts_text(gen)), Some("gen_*.json")),
        Condition::new(
            "LLM",
            Some(if gen.llm.attempted {
                format!(
                    "{} → 产出 {} 个",
                    gen.llm.provider.clone().unwrap_or_else(|| "未记服务名".into()),
                    gen.llm.produced
                )
            } else {
                "未启用 (本地确定性变异器)".to_string()
            }),
            Some("gen_*.json"),
        ),
        // 风扇/游戏版本这两项设备侧根本没回传, 只在人写的条件文档里 ——
        // 面板不去猜, 如实报未知, 并把文档挂在证据里。
        Condition::new("主动散热", None, None),
        Condition::new("游戏版本", None, None),
    ];
    if let Some(err) = &gen.llm.error {
        conditions.push(Condition::new("LLM 报错", Some(err.clone()), Some("gen_*.json")));
    }

    let mut evidence = vec![evidence_file(
        gol_root,
        &run.dir.join(format!("gen_{}.json", gen.generation)),
        &format!("第 {} 代逐候选记录", gen.generation),
    )];
    let archive_path = run.dir.join("archive.json");
    if archive_path.exists() {
        evidence.push(evidence_file(gol_root, &archive_path, "Pareto 档案"));
    }

    let mut notes = Vec::new();
    if !gen.gate_rejections.is_empty() {
        let codes: Vec<String> = gen
            .gate_rejections
            .iter()
            .map(|(code, n)| format!("{code}×{n}"))
            .collect();
        notes.push(format!("静态预检打回: {}", codes.join(", ")));
    }

    Round {
        id: format!("op:{}:{}", run.name, gen.generation),
        layer: "operator",
        title: format!("{} · 第 {} 代", run.name, gen.generation),
        when: Some(gen.started_at.clone()),
        verdict,
        verdict_text,
        metric_source: source_text(gen.metric_source).to_string(),
        headline,
        candidates,
        arms: None,
        criteria: Vec::new(),
        conditions,
        pareto: pareto_chart(run, gen.generation, quality_ref.map(|q| q.kind.clone())),
        evidence,
        notes,
    }
}

/// 档案里记着这条的功耗为什么没参与支配排序。
fn power_excluded_reason(run: &EvolutionRun, id: &str) -> Option<String> {
    run.archive
        .as_ref()?
        .entries
        .iter()
        .find(|e| e.id == id)?
        .power_excluded_reason
        .clone()
}

/// 供电状态。真机回包里带 `power_source` 才有; 旧数据一律未知。
fn power_source_text(run: &EvolutionRun, gen: &GenerationRecord) -> Option<String> {
    let from_gen = gen
        .outcomes
        .iter()
        .filter_map(|o| o.report.as_ref())
        .filter_map(|r| r.power_source.as_ref())
        .next()
        .cloned();
    let ps = from_gen.or_else(|| {
        run.archive
            .as_ref()?
            .entries
            .iter()
            .filter_map(|e| e.report.power_source.as_ref())
            .next()
            .cloned()
    })?;
    Some(format!(
        "{} 轨 / {} / {}",
        ps.rail,
        ps.battery_status,
        if ps.on_battery {
            "放电中 (功耗可比)"
        } else {
            "非放电态 (功耗不可比)"
        }
    ))
}

fn origin_counts_text(gen: &GenerationRecord) -> String {
    if gen.origin_counts.is_empty() {
        return "未记".to_string();
    }
    gen.origin_counts
        .iter()
        .map(|(k, n)| format!("{}×{n}", origin_label(k)))
        .collect::<Vec<_>>()
        .join(", ")
}

fn origin_label(raw: &str) -> &str {
    match raw {
        "local_mutation" => "本地变异",
        "llm" => "LLM",
        "baseline" => "基线",
        other => other,
    }
}

fn origin_text(origin: &crate::engine::CandidateOrigin) -> &'static str {
    match serde_json::to_value(origin)
        .ok()
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default()
        .as_str()
    {
        "local_mutation" => "本地变异",
        "llm" => "LLM",
        "baseline" => "基线",
        _ => "其它",
    }
}

fn stage_text(stage: OutcomeStage) -> &'static str {
    match stage {
        OutcomeStage::DuplicateSkipped => "重复跳过",
        OutcomeStage::GateRejected => "预检打回",
        OutcomeStage::EvalError => "评测出错",
        OutcomeStage::Evaluated => "已评测",
    }
}

/// Pareto 散点: 横轴耗时, 纵轴画质, 前沿点单独上色。
///
/// 只画到 `up_to_generation` 为止的档案条目 —— 这样逐代翻页时能看出前沿怎么长出来的。
fn pareto_chart(
    run: &EvolutionRun,
    up_to_generation: u32,
    quality_kind: Option<String>,
) -> Option<ParetoChart> {
    let archive = run.archive.as_ref()?;
    let front: HashSet<&str> = archive.front().iter().map(|e| e.id.as_str()).collect();
    let points: Vec<ParetoPoint> = archive
        .entries
        .iter()
        .filter(|e| e.generation <= up_to_generation)
        .map(|e| ParetoPoint {
            id: e.id.clone(),
            generation: e.generation,
            x: round_to(e.report.metrics.operator_latency_ms as f64, 4),
            y: e.report.metrics.psnr_db.map(|v| round_to(v as f64, 3)),
            on_front: front.contains(e.id.as_str()),
            origin: origin_text(&e.origin).to_string(),
        })
        .collect();
    if points.is_empty() {
        return None;
    }
    Some(ParetoChart {
        x_label: "算子耗时 (ms)".to_string(),
        y_label: "画质 PSNR (dB)".to_string(),
        quality_reference: quality_kind.map(|k| {
            if k == "real_frames" {
                "真机截帧".to_string()
            } else {
                k
            }
        }),
        points,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generation_index_parses_and_sorts_numerically() {
        assert_eq!(generation_index(Path::new("runs/x/gen_2.json")), Some(2));
        assert_eq!(generation_index(Path::new("runs/x/gen_10.json")), Some(10));
        assert_eq!(generation_index(Path::new("runs/x/archive.json")), None);
    }

    #[test]
    fn unevaluated_candidates_are_invalid_not_eliminated() {
        for stage in [
            OutcomeStage::DuplicateSkipped,
            OutcomeStage::GateRejected,
            OutcomeStage::EvalError,
        ] {
            let (v, text) = classify(stage, None, Some(false));
            assert_eq!(v, RoundVerdict::Invalid, "{stage:?} 应算无效: {text}");
        }
    }

    #[test]
    fn front_membership_decides_qualified() {
        let (v, _) = classify(OutcomeStage::Evaluated, Some(EvalStatus::Pass), Some(true));
        assert_eq!(v, RoundVerdict::Qualified);
        let (v, _) = classify(OutcomeStage::Evaluated, Some(EvalStatus::Pass), Some(false));
        assert_eq!(v, RoundVerdict::Eliminated);
    }

    #[test]
    fn poor_quality_is_eliminated() {
        let (v, text) = classify(
            OutcomeStage::Evaluated,
            Some(EvalStatus::PoorQuality),
            Some(false),
        );
        assert_eq!(v, RoundVerdict::Eliminated);
        assert!(text.contains("画质"));
    }

    #[test]
    fn evaluated_without_report_is_invalid() {
        let (v, text) = classify(OutcomeStage::Evaluated, None, None);
        assert_eq!(v, RoundVerdict::Invalid);
        assert!(text.contains("没有报告"));
    }

    #[test]
    fn missing_archive_does_not_claim_a_front_verdict() {
        let (v, text) = classify(OutcomeStage::Evaluated, Some(EvalStatus::Pass), None);
        assert_eq!(v, RoundVerdict::Eliminated);
        assert!(text.contains("档案读不到"), "读不到档案要说出来: {text}");
    }

    #[test]
    fn static_cost_model_source_is_labelled_as_not_measured() {
        assert!(source_text(MetricSource::StaticCostModel).contains("非测量"));
        assert_eq!(source_text(MetricSource::Phonefarm), "真机实测");
    }

    #[test]
    fn discover_returns_nothing_for_missing_dir() {
        assert!(discover(Path::new("/nonexistent/runs")).is_empty());
    }
}
