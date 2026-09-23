//! 统一优胜档案 + 总报告。
//!
//! 三层的候选长得完全不一样 (一组系统参数 / 一组渲染改写 / 一个着色器),
//! 但它们回的是同一份指标表, 所以**档案只有一份**: 同一个负载、同一套
//! 测试条件下量出来的东西本来就该放在一起比。分成三份档案的代价是,
//! 到最后没人答得上"这台机器上最划算的那一档到底是什么"。
//!
//! 报告里必须写清楚的四件事 (缺一条这份报告就没法被别人复核):
//!
//! 1. 每层用没用、为什么;
//! 2. 哪些候选留下了、哪些被淘汰了;
//! 3. 数字 —— 连同它是在什么供电状态、对着什么参考图量出来的;
//! 4. 测试条件 —— 风扇、游戏内帧率设置、等冷目标、温度上限。

use super::eval::{Kind, MetricsV2, Report as EvalReport, VerdictV2};
use super::plan::Plan;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// 一个候选到底改了什么。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Change {
    Sysparam {
        params: BTreeMap<String, String>,
    },
    Gray {
        rewrites: Vec<String>,
    },
    Shader {
        track: String,
        shader_path: String,
    },
}

impl Change {
    /// 一行说清改了什么。
    pub fn summary(&self) -> String {
        match self {
            Change::Sysparam { params } => params
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join(", "),
            Change::Gray { rewrites } => rewrites.join("+"),
            Change::Shader { track, shader_path } => format!("{track}: {shader_path}"),
        }
    }
}

/// 档案里的一条。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entry {
    pub id: String,
    pub layer: Kind,
    pub generation: u32,
    /// 出主意的一方说这组想验证什么。
    pub why: String,
    pub change: Change,
    pub status: String,
    pub metrics: MetricsV2,
    pub verdict: VerdictV2,
    /// 这一轮**实际**在什么条件下量的。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conditions_observed: Option<super::eval::ConditionsObserved>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub power_source: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_reference: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub evidence_dir: Option<String>,
    /// 留下了没有。
    pub kept: bool,
    /// 这条记录的测量本身算不算数。ABORT 的记录 `false` ——
    /// 它既不是正样本也不是负样本, 不该被喂回给出主意的那一方。
    pub measurement_valid: bool,
}

impl Entry {
    pub fn from_report(
        id: impl Into<String>,
        layer: Kind,
        generation: u32,
        why: impl Into<String>,
        change: Change,
        r: &EvalReport,
    ) -> Self {
        Self {
            id: id.into(),
            layer,
            generation,
            why: why.into(),
            change,
            status: r.status.clone(),
            metrics: r.metrics.clone(),
            verdict: r.verdict.clone(),
            conditions_observed: r.conditions_observed.clone(),
            power_source: r.power_source.clone(),
            quality_reference: r.quality_reference.clone(),
            evidence_dir: r.evidence_dir.clone(),
            kept: r.kept(),
            measurement_valid: r.is_measurement_valid(),
        }
    }
}

/// 一层跑完之后的结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LayerOutcome {
    pub layer: Kind,
    pub layer_display: &'static str,
    /// 用没用这一层。
    pub used: bool,
    /// 为什么用 / 为什么没用。
    pub reason: String,
    /// 没用的话还差什么。
    pub blockers: Vec<String>,
    pub route: Option<String>,
    /// 出主意的那一方 (llm / local), 以及 LLM 出了什么岔子。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub proposer: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub llm_error: Option<String>,
    pub proposed: usize,
    /// 本地校验就打回的 (没上机)。
    pub rejected_locally: Vec<String>,
    pub evaluated: usize,
    pub kept: usize,
    /// 测量不算数的那些 (ABORT)。
    pub aborted: usize,
    /// 下发失败的那些 (进程起不来 / 回包解析不了 / 退出码 2)。
    ///
    /// 与 ABORT 又是两回事: ABORT 是"设备上跑了, 但这次不算数",
    /// 这里是"根本没跑成"。一个候选炸了不该把前面几小时的测量一起带走,
    /// 所以它只进这份清单, 不往上抛。
    pub eval_errors: Vec<String>,
}

impl LayerOutcome {
    pub fn unused(layer: Kind, reason: impl Into<String>, blockers: Vec<String>) -> Self {
        Self {
            layer,
            layer_display: layer.display(),
            used: false,
            reason: reason.into(),
            blockers,
            route: None,
            proposer: None,
            llm_error: None,
            proposed: 0,
            rejected_locally: Vec::new(),
            evaluated: 0,
            kept: 0,
            aborted: 0,
            eval_errors: Vec::new(),
        }
    }
}

/// 总报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Report {
    pub schema: &'static str,
    pub tool: &'static str,
    pub tool_version: &'static str,
    pub generated_at: String,
    pub game: String,
    pub package: Option<String>,
    pub serial: String,
    pub device_model: Option<String>,
    /// 这次跑的是计划还是真跑。
    pub plan_only: bool,
    pub plan: Plan,
    pub layers: Vec<LayerOutcome>,
    /// 统一档案: 三层的候选都在这里。
    pub archive: Vec<Entry>,
    /// 留下来的那些 (按层分组的 id)。
    pub kept: Vec<String>,
    /// 一句话结论。
    pub headline: String,
    /// 这份报告里的数字来自哪里。
    pub disclaimer: &'static str,
}

pub const DISCLAIMER: &str = "档案里的每个数字都来自 phonefarm 的真机采样与冻结规则判定。\
power_watt 只有在 power_source.on_battery = true 时才是整机功耗; psnr_db 的绝对值\
只在同一个 quality_reference 下可比; status=ABORT 的记录测量本身不算数, 既不是正样本\
也不是负样本。";

/// 拼总报告。
pub fn build(
    plan: &Plan,
    caps: &super::caps::Caps,
    layers: Vec<LayerOutcome>,
    archive: Vec<Entry>,
    plan_only: bool,
) -> Report {
    let kept: Vec<String> = archive.iter().filter(|e| e.kept).map(|e| e.id.clone()).collect();
    // 有效样本与 ABORT 分开数。全是 ABORT 的一轮说"没有任何指标显著改善"
    // 是错的 —— 那一轮压根没量准, 谈不上改善不改善。
    let valid = archive.iter().filter(|e| e.measurement_valid).count();
    let aborted = archive.len() - valid;
    let headline = if plan_only {
        format!(
            "只摸底没改设备: {} 层可开, {} 层开不了",
            plan.steps.len(),
            plan.skipped.len()
        )
    } else if archive.is_empty() {
        "一个候选都没跑成 —— 见各层的原因".to_string()
    } else if valid == 0 {
        format!("{aborted} 个候选全都没量准 (ABORT), 这一轮什么都没学到 —— 见各条的理由")
    } else if kept.is_empty() {
        format!(
            "跑了 {valid} 个有效候选, 一个都没留下 (没有任何指标显著改善, 或存在显著退化){}",
            if aborted > 0 {
                format!("; 另有 {aborted} 个没量准, 不计在内")
            } else {
                String::new()
            }
        )
    } else {
        format!(
            "跑了 {valid} 个有效候选, 留下 {} 个{}",
            kept.len(),
            if aborted > 0 {
                format!("; 另有 {aborted} 个没量准, 不计在内")
            } else {
                String::new()
            }
        )
    };

    Report {
        schema: "game_opt_loop/optimize_report@1",
        tool: "game_opt_loop",
        tool_version: crate::VERSION,
        generated_at: crate::engine::evolution::now(),
        game: plan.game.clone(),
        package: plan.package.clone(),
        serial: plan.serial.clone(),
        device_model: caps.device.model.clone(),
        plan_only,
        plan: plan.clone(),
        layers,
        archive,
        kept,
        headline,
        disclaimer: DISCLAIMER,
    }
}

fn num(v: Option<f64>, unit: &str) -> String {
    match v {
        Some(x) => format!("{x:.3}{unit}"),
        // "未测量"与 0 是两件事。表格里也不许含糊。
        None => "未测量".to_string(),
    }
}

/// 渲染 report.md。
pub fn markdown(r: &Report, caps: &super::caps::Caps) -> String {
    let mut o = String::new();
    o.push_str(&format!("# {} 优化报告\n\n", r.game));
    o.push_str(&format!("{}\n\n", r.headline));
    o.push_str(&format!(
        "- 生成时间: {}\n- 工具: {} v{}\n- 目标: {} ({})\n- 设备: {} {}\n",
        r.generated_at,
        r.tool,
        r.tool_version,
        r.game,
        r.package.as_deref().unwrap_or("包名未知"),
        r.serial,
        r.device_model.as_deref().unwrap_or("型号未知"),
    ));
    if r.plan_only {
        o.push_str("- **本次只摸底 + 排计划, 没有对设备做任何写入。**\n");
    }

    // ── 测试条件 ──
    let c = &r.plan.conditions;
    o.push_str("\n## 测试条件 (看到候选数据之前冻结)\n\n");
    o.push_str("| 条件 | 值 |\n|---|---|\n");
    o.push_str(&format!(
        "| 等冷目标 | {} |\n",
        c.cool_c.map(|v| format!("{v:.1}C")).unwrap_or("不等冷".into())
    ));
    o.push_str(&format!(
        "| 温度上限 | {} |\n",
        c.temp_cap_c.map(|v| format!("{v:.1}C")).unwrap_or("无".into())
    ));
    o.push_str(&format!(
        "| 停充 | {} |\n",
        match &c.charging_suspend {
            Some(s) if s.enabled => format!("会尝试 ({})", s.node.as_deref().unwrap_or("接收端自探")),
            _ => "停不了 —— 充电态下功耗如实留空".to_string(),
        }
    ));
    o.push_str(&format!("| 风扇 | {} |\n", c.fan));
    o.push_str(&format!(
        "| 游戏内帧率设置 | {} |\n",
        c.ingame_fps_setting.as_deref().unwrap_or("未记录")
    ));
    o.push_str(&format!(
        "| 电池 | {} {}% |\n",
        caps.device.battery.status.as_deref().unwrap_or("未知"),
        caps.device
            .battery
            .capacity_pct
            .map(|x| x.to_string())
            .unwrap_or("?".into())
    ));
    o.push_str(&format!(
        "| 摸底时 SoC 温度 | {} |\n",
        caps.device
            .soc_temp_c
            .map(|t| format!("{t:.1}C"))
            .unwrap_or("未测到".into())
    ));

    // ── 每层用没用 ──
    o.push_str("\n## 三层各自用没用\n\n");
    for l in &r.layers {
        o.push_str(&format!(
            "### {} — {}\n\n{}\n\n",
            l.layer_display,
            if l.used { "用了" } else { "没用" },
            l.reason
        ));
        if let Some(route) = &l.route {
            o.push_str(&format!("- 路线: `{route}`\n"));
        }
        for b in &l.blockers {
            o.push_str(&format!("- 还差: {b}\n"));
        }
        if l.used {
            o.push_str(&format!(
                "- 出主意: {}\n- 提了 {} 个, 本地打回 {} 个, 上机 {} 个, 留下 {} 个, 测量不算数 {} 个\n",
                l.proposer.as_deref().unwrap_or("?"),
                l.proposed,
                l.rejected_locally.len(),
                l.evaluated,
                l.kept,
                l.aborted,
            ));
            if let Some(e) = &l.llm_error {
                o.push_str(&format!("- 大模型这一层没用上: {e}\n"));
            }
            for rej in &l.rejected_locally {
                o.push_str(&format!("  - 本地打回: {rej}\n"));
            }
            for err in &l.eval_errors {
                // "没跑成"与"跑了但不算数"要分开列, 否则读的人会把工装故障
                // 当成这一层的结论。
                o.push_str(&format!("  - 下发失败 (没跑成): {err}\n"));
            }
        }
        o.push('\n');
    }

    // ── 候选明细 ──
    o.push_str("## 候选与数字\n\n");
    if r.archive.is_empty() {
        o.push_str("这一轮没有任何候选上机。\n\n");
    } else {
        o.push_str(
            "| 候选 | 层 | 改了什么 | 判定 | 帧时p95 | 帧率 | 功耗 | 画质 | p | 留下 |\n\
             |---|---|---|---|---|---|---|---|---|---|\n",
        );
        for e in &r.archive {
            o.push_str(&format!(
                "| {} | {} | {} | {} | {} | {} | {} | {} | {:.4} | {} |\n",
                e.id,
                e.layer.id(),
                e.change.summary(),
                e.status,
                num(e.metrics.fps_p95_ms, "ms"),
                num(e.metrics.fps_mean, ""),
                num(e.metrics.power_watt, "W"),
                num(e.metrics.psnr_db, "dB"),
                e.verdict.p_value,
                if e.kept { "是" } else { "否" },
            ));
        }
        o.push('\n');

        // 为什么被淘汰 —— 表里的 p 值说不出"是哪个指标退化了"。
        let rejected: Vec<&Entry> = r.archive.iter().filter(|e| !e.kept).collect();
        if !rejected.is_empty() {
            o.push_str("### 淘汰的理由\n\n");
            for e in rejected {
                o.push_str(&format!(
                    "- `{}` ({}) — {}\n",
                    e.id,
                    e.status,
                    e.verdict.reason.as_deref().unwrap_or("接收端没给理由")
                ));
                if e.status == "ABORT" {
                    o.push_str(
                        "  - 这条是**测量不算数**, 不是「这组没用」—— 不会被喂回给出主意的那一方\n",
                    );
                }
            }
            o.push('\n');
        }
    }

    // ── 已知的坑 ──
    if !r.plan.pitfalls.is_empty() {
        o.push_str("## 已知的坑\n\n");
        for p in &r.plan.pitfalls {
            o.push_str(&format!("- **{}**\n  - {}\n", p.symptom, p.workaround));
        }
        o.push('\n');
    }

    o.push_str(&format!("---\n\n{}\n", r.disclaimer));
    o
}

/// 落盘 report.json + report.md。
pub fn write(r: &Report, caps: &super::caps::Caps, dir: &Path) -> Result<(String, String)> {
    std::fs::create_dir_all(dir)?;
    let json = dir.join("report.json");
    let md = dir.join("report.md");
    std::fs::write(&json, serde_json::to_string_pretty(r)?)
        .with_context(|| format!("写入 {} 失败", json.display()))?;
    std::fs::write(&md, markdown(r, caps))
        .with_context(|| format!("写入 {} 失败", md.display()))?;
    Ok((json.display().to_string(), md.display().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize::caps;
    use crate::optimize::games::{self, CarrierRoots};
    use crate::optimize::plan::{self, Budget};
    use std::path::PathBuf;

    const ROOTED: &str = "model=NX809J\nroot_uid=0\nvklayers=\nbattery_status=Discharging\n\
battery_capacity=84\nbattery_current_now=-1200000\ntemp_mc=41300\n\
charge_node=/sys/class/qcom-battery/charging_enabled\n\
pkg_path=package:/data/app/x/base.apk\n";

    fn fixture() -> (caps::Caps, Plan) {
        let t = games::resolve(
            "原神",
            None,
            &CarrierRoots {
                carriers_dir: PathBuf::from("/nonexistent"),
                siblings_dir: PathBuf::from("/nonexistent"),
            },
        );
        let c = caps::assemble(&t, "S1", ROOTED);
        let p = plan::build(&c, Budget::parse("6h").unwrap(), true);
        (c, p)
    }

    fn entry(id: &str, status: &str, kept: bool) -> Entry {
        Entry {
            id: id.into(),
            layer: Kind::Sysparam,
            generation: 1,
            why: "抬总线下限".into(),
            change: Change::Sysparam {
                params: BTreeMap::from([(
                    "bus.DDR.boost_freq".to_string(),
                    "5333000".to_string(),
                )]),
            },
            status: status.into(),
            metrics: MetricsV2 {
                fps_p95_ms: Some(36.1),
                fps_mean: Some(29.8),
                power_watt: None,
                ..Default::default()
            },
            verdict: VerdictV2 {
                is_pareto_improvement: kept,
                p_value: 0.0079,
                reason: Some("改善 frame_p95 且无显著退化".into()),
                ..Default::default()
            },
            conditions_observed: None,
            power_source: None,
            quality_reference: None,
            evidence_dir: None,
            kept,
            measurement_valid: status != "ABORT",
        }
    }

    #[test]
    fn a_plan_only_report_says_so_loudly() {
        let (c, p) = fixture();
        let r = build(&p, &c, vec![], vec![], true);
        assert!(r.plan_only);
        let md = markdown(&r, &c);
        assert!(md.contains("没有对设备做任何写入"), "{md}");
    }

    /// 报告必须写清测试条件 —— 不写的话这些数字跨轮不可比。
    #[test]
    fn the_report_always_states_the_test_conditions() {
        let (c, p) = fixture();
        let md = markdown(&build(&p, &c, vec![], vec![], false), &c);
        for must in ["测试条件", "风扇", "游戏内帧率设置", "等冷目标", "温度上限"] {
            assert!(md.contains(must), "报告里少了 `{must}`");
        }
    }

    /// 没测到的数字写"未测量", 不写 0。
    #[test]
    fn unmeasured_numbers_are_never_printed_as_zero() {
        let (c, p) = fixture();
        let r = build(&p, &c, vec![], vec![entry("g1c1", "PASS", true)], false);
        let md = markdown(&r, &c);
        assert!(md.contains("未测量"), "{md}");
        assert!(!md.contains("0.000W"), "功耗没测到却印成了 0 W");
    }

    /// ABORT 不算正负样本, 报告里要明说。
    #[test]
    fn abort_is_called_out_as_an_invalid_measurement() {
        let (c, p) = fixture();
        let r = build(&p, &c, vec![], vec![entry("g1c2", "ABORT", false)], false);
        assert!(!r.archive[0].measurement_valid);
        let md = markdown(&r, &c);
        assert!(md.contains("测量不算数"), "{md}");
    }

    /// 全是 ABORT 的一轮不能被说成"没有任何指标显著改善" —— 那一轮压根没量准,
    /// 谈不上改善不改善。这正是本模块反复要分开的两件事。
    #[test]
    fn an_all_abort_run_does_not_claim_nothing_improved() {
        let (c, p) = fixture();
        let r = build(
            &p,
            &c,
            vec![],
            vec![entry("g1c1", "ABORT", false), entry("g1c2", "ABORT", false)],
            false,
        );
        assert!(r.headline.contains("没量准"), "{}", r.headline);
        assert!(
            !r.headline.contains("显著改善"),
            "把没量准说成了没改善: {}",
            r.headline
        );

        // 混着来的时候, 有效样本与没量准的要分开数。
        let r = build(
            &p,
            &c,
            vec![],
            vec![
                entry("g1c1", "PASS", true),
                entry("g1c2", "ABORT", false),
                entry("g1c3", "REJECT", false),
            ],
            false,
        );
        assert!(r.headline.contains("2 个有效候选"), "{}", r.headline);
        assert!(r.headline.contains("1 个没量准"), "{}", r.headline);
    }

    /// "没跑成"与"跑了但不算数"在报告里分开列 —— 混在一起的话, 读的人会把
    /// 工装故障当成这一层的结论。
    #[test]
    fn failed_dispatches_are_listed_apart_from_aborted_measurements() {
        let (c, p) = fixture();
        let mut l = LayerOutcome::unused(Kind::Sysparam, "有 root", vec![]);
        l.used = true;
        l.proposed = 2;
        l.evaluated = 1;
        l.aborted = 1;
        l.eval_errors = vec!["sys_g1c2: phonefarm eval 退出码 Some(2)".into()];
        let md = markdown(&build(&p, &c, vec![l], vec![], false), &c);
        assert!(md.contains("下发失败 (没跑成)"), "{md}");
    }

    /// 三层的候选进同一份档案。
    #[test]
    fn one_archive_holds_every_layer() {
        let (c, p) = fixture();
        let mut a = entry("g1c1", "PASS", true);
        let mut b = entry("g1c2", "PASS", true);
        b.id = "gray1".into();
        b.layer = Kind::Gray;
        b.change = Change::Gray {
            rewrites: vec!["loadop_dont_care".into()],
        };
        a.layer = Kind::Sysparam;
        let r = build(&p, &c, vec![], vec![a, b], false);
        assert_eq!(r.kept.len(), 2);
        let md = markdown(&r, &c);
        assert!(md.contains("loadop_dont_care"));
        assert!(md.contains("bus.DDR.boost_freq"));
    }

    /// 每一层都要在报告里交代用没用、为什么。
    #[test]
    fn every_layer_is_accounted_for() {
        let (c, p) = fixture();
        let layers = vec![
            LayerOutcome::unused(Kind::Shader, "黑盒且挂不了层", vec!["没有白盒载体".into()]),
            LayerOutcome::unused(Kind::Gray, "挂不了层", vec![]),
            LayerOutcome::unused(Kind::Sysparam, "没有 root", vec![]),
        ];
        let md = markdown(&build(&p, &c, layers, vec![], false), &c);
        for name in ["① 画面算子", "② 画面流程改写", "③ 系统参数"] {
            assert!(md.contains(name), "报告里漏了 {name}");
        }
        assert!(md.contains("没有白盒载体"));
    }

    #[test]
    fn writing_produces_both_files() {
        let (c, p) = fixture();
        let dir = std::env::temp_dir().join(format!("gol_report_{}", std::process::id()));
        let r = build(&p, &c, vec![], vec![], true);
        let (j, m) = write(&r, &c, &dir).unwrap();
        assert!(Path::new(&j).is_file());
        assert!(Path::new(&m).is_file());
        // report.json 必须能被重新读回来 —— 它是给别的工具消费的。
        let back: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&j).unwrap()).unwrap();
        assert_eq!(back["schema"], "game_opt_loop/optimize_report@1");
        std::fs::remove_dir_all(&dir).ok();
    }
}
