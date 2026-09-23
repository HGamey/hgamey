//! 面板数据读取的整机测试。
//!
//! 夹具 `tests/fixtures/viz/` 里全是**从真机产物原样拷来的真文件** ——
//! 演化档案、A/B 验收报告、逐轮 summary、设备状态快照、设备锁日志。
//! 不手搓 JSON: 手搓的夹具只能证明"代码能读我自己写的格式",
//! 证明不了"代码能读 phonefarm 真的吐出来的那份"。
//!
//! 夹具里的数字都能在原始报告里找到对应, 断言里注明了出处。

use game_opt_loop::viz::model::RoundVerdict;
use game_opt_loop::viz::{self, VizPaths};
use std::path::PathBuf;

fn fixture_paths() -> VizPaths {
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/viz");
    VizPaths {
        gol_root: base.join("gol"),
        phonefarm_root: base.join("phonefarm"),
        lock_root: base.join("lock"),
    }
}

fn dashboard() -> viz::Dashboard {
    viz::build(&fixture_paths())
}

fn layer<'a>(d: &'a viz::Dashboard, id: &str) -> &'a viz::model::Layer {
    d.layers
        .iter()
        .find(|l| l.id == id)
        .unwrap_or_else(|| panic!("面板少了 {id} 这一层"))
}

fn round<'a>(d: &'a viz::Dashboard, id: &str) -> &'a viz::model::Round {
    d.layers
        .iter()
        .flat_map(|l| l.rounds.iter())
        .find(|r| r.id == id)
        .unwrap_or_else(|| panic!("找不到轮次 {id}"))
}

fn condition<'a>(r: &'a viz::model::Round, label: &str) -> &'a viz::model::Condition {
    r.conditions
        .iter()
        .find(|c| c.label == label)
        .unwrap_or_else(|| panic!("{} 这一轮少了测试条件「{label}」", r.id))
}

#[test]
fn the_three_layers_are_always_present_and_counted() {
    let d = dashboard();
    assert_eq!(d.layers.len(), 3);
    assert!(d.paths.phonefarm_present);

    for l in &d.layers {
        assert_eq!(
            l.rounds_total,
            l.qualified + l.eliminated + l.invalid,
            "层{} 的轮数必须等于三色之和 (仅证据不计入)",
            l.index
        );
        assert!(l.problems.is_empty(), "层{} 读夹具不该出问题: {:?}", l.index, l.problems);
    }
}

// ───────────────────────── 层① 算子 ─────────────────────────

#[test]
fn operator_generations_are_read_from_the_real_archive() {
    let d = dashboard();
    let l = layer(&d, "operator");
    // 夹具只放了 evo_realframes_20260923 的两代。
    assert_eq!(l.rounds_total, 2);
    assert_eq!(l.qualified, 1, "只有第 1 代有候选进了前沿");

    let r = round(&d, "op:evo_realframes_20260923:1");
    assert_eq!(r.verdict, RoundVerdict::Qualified);
    assert_eq!(r.metric_source, "真机实测");
    assert_eq!(r.candidates.len(), 3);

    // 档案 (runs/evo_realframes_20260923/archive.json) 里的前沿是
    // [gen0_baseline, gen1_loc3] —— 与 evidence/M2_realframes_conditions.md 一致。
    let loc3 = r.candidates.iter().find(|c| c.id == "gen1_loc3").unwrap();
    assert_eq!(loc3.verdict, RoundVerdict::Qualified);
    assert_eq!(loc3.on_front, Some(true));

    let loc1 = r.candidates.iter().find(|c| c.id == "gen1_loc1").unwrap();
    assert_eq!(loc1.verdict, RoundVerdict::Eliminated, "测了但没进前沿 = 淘汰");
    assert_eq!(loc1.latency_ms, Some(0.5304), "gen_1.json 记的是 0.53044045");

    // gen1_loc2 指纹重复, 从没上过机 —— 必须是「无效」, 不能算淘汰。
    let loc2 = r.candidates.iter().find(|c| c.id == "gen1_loc2").unwrap();
    assert_eq!(loc2.verdict, RoundVerdict::Invalid);
    assert!(loc2.verdict_text.contains("重复"));
    assert_eq!(loc2.latency_ms, None, "没测过就不该有数字");
}

#[test]
fn quality_reference_is_carried_so_absolute_db_is_not_compared_across_references() {
    let d = dashboard();
    let r = round(&d, "op:evo_realframes_20260923:1");
    let c = condition(r, "画质真值");
    assert_eq!(c.value.as_deref(), Some("真机截帧 × 19 张"));

    let pareto = r.pareto.as_ref().expect("层① 该有 Pareto 散点");
    assert_eq!(pareto.quality_reference.as_deref(), Some("真机截帧"));
    assert!(pareto.points.iter().any(|p| p.on_front));
    assert!(
        pareto.points.iter().all(|p| p.generation <= 1),
        "第 1 代的图里不该出现第 2 代才有的点"
    );
}

#[test]
fn unreported_power_source_shows_as_unknown_instead_of_a_made_up_value() {
    let d = dashboard();
    let r = round(&d, "op:evo_realframes_20260923:1");
    // 这一批真机回包里还没有 power_source 字段。面板必须留空,
    // 让前端印「未知/未测」—— 绝不能填一个看着正常的默认值。
    assert_eq!(condition(r, "供电状态").value, None);
    assert_eq!(condition(r, "主动散热").value, None, "风扇状态设备侧没回传");
    assert_eq!(condition(r, "游戏版本").value, None);
}

// ──────────────────── 层② 图形接口改写 ────────────────────

#[test]
fn a_directionally_good_but_insignificant_result_is_eliminated_not_qualified() {
    let d = dashboard();
    let r = round(&d, "gfx:graylayer-loadop");
    // runs_graylayer/report.json: diff -0.51%, p = 0.3254 → criterion_3 未过。
    assert_eq!(r.verdict, RoundVerdict::Eliminated);
    assert!(
        r.verdict_text.contains("统计显著"),
        "要点名是哪条判据没过: {}",
        r.verdict_text
    );

    let arms = r.arms.as_ref().expect("两臂式报告该出得来对比图数据");
    assert_eq!(arms.metric, "frame_p95");
    assert_eq!(arms.a_values.len(), 5);
    assert_eq!(arms.b_values.len(), 5);
    assert_eq!(arms.unit, "ms");

    let sig = r
        .criteria
        .iter()
        .find(|c| c.name == "统计显著")
        .expect("该有统计显著这条判据");
    assert_eq!(sig.pass, Some(false));
}

#[test]
fn an_arm_without_a_report_is_invalid_not_a_negative_verdict() {
    // 层② 的 ab2 目录只有逐轮数据、没有验收报告。夹具里没拷它,
    // 所以这里验的是"没拷就不该凭空冒出来一轮"。
    let d = dashboard();
    assert!(
        !d.layers
            .iter()
            .flat_map(|l| l.rounds.iter())
            .any(|r| r.id == "gfx:graylayer-ab2"),
        "目录不存在时不该造出一轮"
    );
}

#[test]
fn evidence_only_dirs_are_listed_but_not_counted_in_the_three_way_tally() {
    let d = dashboard();
    let l = layer(&d, "graphics");
    assert_eq!(l.evidence_only, 1, "夹具里只拷了 04_reverify 一个证据目录");
    let r = round(&d, "gfx:ev-04");
    assert_eq!(r.verdict, RoundVerdict::Evidence);
    assert!(!r.evidence.is_empty());
    // RESULT.md 排最前, 演示时一点就开。
    assert!(r.evidence[0].rel.ends_with("RESULT.md"));
    assert_eq!(
        condition(r, "机读判定").value,
        None,
        "没有 report.json 就不该声称有机读判定"
    );
}

// ───────────────────── 层③ 系统参数 ─────────────────────

#[test]
fn a_significant_result_passes_all_criteria() {
    let d = dashboard();
    let l = layer(&d, "sysparam");
    assert_eq!(l.rounds_total, 2);
    assert_eq!(l.qualified, 2);

    let r = round(&d, "sys:genshin-ddr");
    assert_eq!(r.verdict, RoundVerdict::Qualified);
    // loop_v1/runs/report.json: 5 条判据全过。
    assert!(r.verdict_text.starts_with("5/5"));
    assert_eq!(r.criteria.len(), 5);
    assert!(r.criteria.iter().all(|c| c.pass == Some(true)));
}

#[test]
fn the_changed_knob_is_discovered_by_diffing_the_two_arms_snapshots() {
    let d = dashboard();
    let r = round(&d, "sys:genshin-ddr");
    let diff = condition(r, "两臂快照差异")
        .value
        .clone()
        .expect("两臂都有 snap_at_run.txt 时该算得出差异");
    // 这一轮动的就是总线频率投票 —— 靠对比快照看出来, 不靠人写标签。
    assert!(diff.contains("bus.DDR.boost_freq"), "实际差异: {diff}");
    assert!(diff.contains("547000 → 5333000"), "实际差异: {diff}");
}

#[test]
fn test_conditions_come_from_the_run_files_and_stay_unknown_when_absent() {
    let d = dashboard();
    let r = round(&d, "sys:genshin-ddr");
    assert_eq!(
        condition(r, "被测应用").value.as_deref(),
        Some("com.miHoYo.Yuanshen"),
        "包名来自 workload.log"
    );
    assert_eq!(
        condition(r, "实测限帧").value.as_deref(),
        Some("30 fps"),
        "限帧来自 attribution.json 的 detected_fps_cap"
    );
    assert_eq!(
        condition(r, "电池温度 (处理臂首轮)").value.as_deref(),
        Some("45.0 ℃"),
        "温度来自 workload.log 的最后一次读数"
    );
    // 设备快照里 peak/min refresh rate 都是 null (系统没设, 交给应用自己限帧)。
    assert_eq!(condition(r, "帧率设置").value, None);
}

#[test]
fn the_probe_shaped_report_is_parsed_too() {
    let d = dashboard();
    let r = round(&d, "sys:refbench-bw");
    assert_eq!(r.verdict, RoundVerdict::Qualified);
    // runs_refbench/report.json 的对比块藏在 criterion_3_knob_effective 里。
    let arms = r.arms.as_ref().expect("探针式报告也该出得来对比图数据");
    assert_eq!(arms.metric, "frame_p95");
    assert_eq!(arms.b_values.len(), 5);
}

// ───────────────────── 载体可复现性 ─────────────────────

#[test]
fn carrier_dispersion_matches_the_numbers_in_the_reports() {
    let d = dashboard();
    let find = |carrier: &str, window: &str| {
        d.carriers
            .iter()
            .find(|c| c.carrier == carrier && c.window == window)
            .unwrap_or_else(|| panic!("少了载体 {carrier} / {window}"))
    };

    // refbench 对照臂: report.json 自己算出来的 frame_p50 离散度是 0.2658%。
    let refbench = find("refbench (白档)", "带宽场景 · 对照臂");
    assert_eq!(refbench.n_runs, 5);
    let p50 = refbench.p50_dispersion_pct.expect("该算得出 p50 离散度");
    assert!((p50 - 0.2658).abs() < 0.002, "算出来是 {p50}, 报告是 0.2658");
    // refbench 的 0.5% 卡的是**中位**帧时 (report.json 的 repeatability_metric)。
    // 它的 p95 离散度是 0.4526%, 贴着 0.5% —— 若错拿 p95 去判, 一次更抖的尾巴
    // 就会让面板说"偏大", 而报告里写的是通过。
    assert_eq!(refbench.gate_metric, Some("frame_p50"));
    assert_eq!(refbench.status, "pass");
    assert!(refbench.problems.is_empty());

    // 原神世界场景: ab_report.json 里 frame_p95 离散度是 2.673%。
    let genshin = find("原神", "世界场景 · 60fps");
    let p95 = genshin.p95_dispersion_pct.expect("该算得出 p95 离散度");
    assert!((p95 - 2.673).abs() < 0.002, "算出来是 {p95}, 报告是 2.673");

    // Megacity 还没有冻结判据 —— 只报数, 不判。
    let mc = find("Megacity", "城市巡航 · 基线");
    assert_eq!(mc.n_runs, 3);
    assert_eq!(mc.gate_pct, None);
    assert_eq!(mc.gate_metric, None);
    assert_eq!(mc.status, "unknown", "没有阈值就不该给出合格/不合格");
    assert!(mc.p95_dispersion_pct.is_some(), "没阈值也要把离散度报出来");
}

// ───────────────────────── 设备 ─────────────────────────

#[test]
fn the_device_lock_is_read_from_its_files_and_not_by_running_the_script() {
    let d = dashboard();
    let holder = d.device.holder.as_ref().expect("夹具里锁是被持有的");
    assert_eq!(holder.label, "sysknob-loop");
    assert_eq!(holder.since.as_deref(), Some("2026-09-23 10:12:31"));
    assert!(!d.device.queue.is_empty(), "夹具里排着队");
    assert_eq!(d.device.queue[0], "knobs-layer");
    assert!(d.device.problems.is_empty());

    // 锁日志按时间倒序, 最新的排最前。
    let history = &d.device.history;
    assert!(!history.is_empty());
    assert_eq!(history[0].at, "2026-09-23 10:12:31");
    assert_eq!(history[0].action, "acquire");
    assert!(history.iter().all(|e| matches!(
        e.action.as_str(),
        "acquire" | "release" | "steal"
    )));
}

#[test]
fn recent_device_tasks_carry_their_goal() {
    let d = dashboard();
    let t = d
        .device
        .recent_tasks
        .iter()
        .find(|t| t.task == "loop_v1_smoke")
        .expect("夹具里有一次 loop_v1_smoke 回放");
    assert!(
        t.goal.as_deref().is_some_and(|g| g.contains("workload_spin_v1.json")),
        "目标应从 log.jsonl 的 goal 行读出来: {:?}",
        t.goal
    );
}

// ───────────────────────── 撤回提示 ─────────────────────────

#[test]
fn the_retracted_power_conclusion_is_surfaced_at_the_top() {
    let d = dashboard();
    let n = d
        .notices
        .iter()
        .find(|n| n.level == "retracted")
        .expect("M1 的功耗结论已撤回, 必须顶在最上面");
    assert!(n.title.contains("power_watt"));
    assert!(n.title.contains("M1_report.json"));
    assert!(n.body.contains("仍然有效"), "撤回了什么、什么还算数, 都要写清楚");
    assert!(n.evidence.is_some(), "撤回提示要能点开原始报告");
}

/// 版本库里跟着的那份真报告 (`evidence/M1_report.json`) 也必须解析得出撤回。
///
/// 上一条用的是夹具拷贝; 这一条盯的是仓库里活的那份 —— 它被改动时这里会红。
#[test]
fn the_tracked_evidence_report_still_declares_its_retraction() {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let raw = std::fs::read_to_string(repo.join("evidence/M1_report.json"))
        .expect("evidence/M1_report.json 是进版本库的");
    let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
    let r = v.get("retraction").expect("M1 报告里该有 retraction 段");
    assert_eq!(r.get("scope").and_then(|s| s.as_str()), Some("power_watt"));

    let paths = VizPaths {
        gol_root: repo.clone(),
        phonefarm_root: repo.join("绝不存在的目录"),
        lock_root: repo.join("绝不存在的目录"),
    };
    let d = viz::build(&paths);
    assert!(d.notices.iter().any(|n| n.level == "retracted"));
    assert!(
        d.notices.iter().any(|n| n.level == "warn"),
        "phonefarm 不在时要明说, 而不是画一页空白"
    );
}
