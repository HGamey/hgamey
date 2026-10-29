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
        // 夹具测试不碰设备 —— 手机那条通路另有单测。
        fleet: None,
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
        assert!(
            l.problems.is_empty(),
            "层{} 读夹具不该出问题: {:?}",
            l.index,
            l.problems
        );
    }
}

// ───────────────────────── 层① 算子 ─────────────────────────

#[test]
fn operator_generations_are_read_from_the_real_archive() {
    let d = dashboard();
    let l = layer(&d, "operator");
    // 夹具只放了 evo_realframes_20260923 的两代。
    assert_eq!(l.rounds_total, 0);
    assert_eq!(l.evidence_only, 2, "历史两代均未资格化，仅证据");
    assert_eq!(l.qualified, 0);

    let r = round(&d, "op:evo_realframes_20260923:1");
    assert_eq!(r.verdict, RoundVerdict::Evidence);
    assert_eq!(r.metric_source, "phonefarm 报告（未认证采集）");
    assert_eq!(r.candidates.len(), 3);

    // 档案 (runs/evo_realframes_20260923/archive.json) 里的前沿是
    // [gen0_baseline, gen1_loc3] —— 与 evidence/M2_realframes_conditions.md 一致。
    let loc3 = r.candidates.iter().find(|c| c.id == "gen1_loc3").unwrap();
    assert_eq!(loc3.verdict, RoundVerdict::Evidence);
    assert_eq!(loc3.on_front, Some(true));
    assert_eq!(
        loc3.evidence.as_ref().unwrap().qualification_state,
        game_opt_loop::evidence::QualificationState::LegacyUnqualified
    );

    let loc1 = r.candidates.iter().find(|c| c.id == "gen1_loc1").unwrap();
    assert_eq!(
        loc1.verdict,
        RoundVerdict::Evidence,
        "旧报告仍保留数值和原状态，不能补资格"
    );
    assert_eq!(
        loc1.latency_ms,
        Some(0.5304),
        "gen_1.json 记的是 0.53044045"
    );

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
fn historical_negative_judgment_remains_visible_without_backfilled_qualification() {
    let d = dashboard();
    let r = round(&d, "gfx:graylayer-loadop");
    // runs_graylayer/report.json: diff -0.51%, p = 0.3254 → criterion_3 未过。
    assert_eq!(r.verdict, RoundVerdict::Evidence);
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
    assert_eq!(
        l.evidence_only, 2,
        "04_reverify 目录及未资格化的旧 A/B 均仅证据"
    );
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
    assert_eq!(l.rounds_total, 0);
    assert_eq!(l.evidence_only, 2);
    assert_eq!(l.qualified, 0);

    let r = round(&d, "sys:genshin-ddr");
    assert_eq!(r.verdict, RoundVerdict::Evidence);
    // loop_v1/runs/report.json: 5 条判据全过。
    assert!(r.verdict_text.contains("5/5"));
    assert!(r.verdict_text.contains("未资格化"));
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
    assert_eq!(r.verdict, RoundVerdict::Evidence);
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
    assert!(
        (p50 - 0.2658).abs() < 0.002,
        "算出来是 {p50}, 报告是 0.2658"
    );
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
fn retired_owner_files_are_history_and_never_current_occupancy() {
    let d = dashboard();
    assert!(d.device.holder.is_none(), "historical owner is not a current fd holder");
    assert!(d.device.queue.is_empty(), "the label FIFO has been retired");
    assert!(d.device.problems.is_empty());

    // 锁日志按时间倒序, 最新的排最前。
    let history = &d.device.history;
    assert!(!history.is_empty());
    assert_eq!(history[0].at, "2026-09-23 10:12:31");
    assert_eq!(history[0].action, "acquire");
    assert!(history
        .iter()
        .all(|e| matches!(e.action.as_str(), "acquire" | "release" | "steal")));
}

/// 带 serial 的对局记录要能按设备回溯出「最近一次操作」;
/// 旧日志没记 serial, 照旧不进这张表 (面板那边显示未知)。
#[test]
fn last_touch_is_looked_up_by_serial_and_old_logs_stay_unknown() {
    let d = dashboard();
    let touch = d
        .device
        .last_touch
        .get("91253241019A")
        .expect("夹具里有一份带 serial 的对局记录");
    assert_eq!(touch.task, "loop_v1_smoke");
    assert!(touch.at.is_some(), "得说得出是什么时候");

    // 夹具里两份记录, 只有一份带 serial —— 另一份不该凭空进表
    assert_eq!(d.device.last_touch.len(), 1);
    assert!(
        d.device.scanned_runs >= 2,
        "两份都要翻过, 才谈得上「翻了但那份没记 serial」: {}",
        d.device.scanned_runs
    );
    assert!(!d.device.last_touch.contains_key("从来没出现过的机器"));
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
        t.goal
            .as_deref()
            .is_some_and(|g| g.contains("workload_spin_v1.json")),
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
    assert!(
        n.body.contains("仍然有效"),
        "撤回了什么、什么还算数, 都要写清楚"
    );
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
        fleet: None,
    };
    let d = viz::build(&paths);
    assert!(d.notices.iter().any(|n| n.level == "retracted"));
    assert!(
        d.notices.iter().any(|n| n.level == "warn"),
        "phonefarm 不在时要明说, 而不是画一页空白"
    );
}

#[test]
fn operator_view_preserves_receiver_contract_and_never_grants_game_proof() {
    use game_opt_loop::engine::evolution::GenerationRecord;
    use game_opt_loop::viz::operator::{self, EvolutionRun};
    use serde_json::json;
    let paths = fixture_paths();
    let dir = paths.gol_root.join("runs/evo_realframes_20260923");
    let original: GenerationRecord =
        serde_json::from_str(&std::fs::read_to_string(dir.join("gen_1.json")).unwrap()).unwrap();
    let archive = game_opt_loop::engine::ParetoArchive::load(&dir.join("archive.json")).unwrap();
    for (name, qualification, replay, expected) in [
        (
            "valid declaration",
            json!({"stage":"conclusion","eligible":true,"evidence_origin":"live_device"}),
            None,
            RoundVerdict::Qualified,
        ),
        (
            "blocked",
            json!({"stage":"conclusion","eligible":false,"evidence_origin":"live_device"}),
            None,
            RoundVerdict::Invalid,
        ),
        ("null", serde_json::Value::Null, None, RoundVerdict::Invalid),
        (
            "malformed",
            json!({"stage":"conclusion","eligible":"true","evidence_origin":"live_device"}),
            None,
            RoundVerdict::Invalid,
        ),
        (
            "replay",
            json!({"stage":"conclusion","eligible":true,"evidence_origin":"live_device"}),
            Some(serde_json::Value::Null),
            RoundVerdict::Invalid,
        ),
        (
            "synthetic",
            json!({"stage":"conclusion","eligible":true,"evidence_origin":"synthetic_fixture"}),
            None,
            RoundVerdict::Evidence,
        ),
        (
            "unknown origin",
            json!({"stage":"conclusion","eligible":true}),
            None,
            RoundVerdict::Evidence,
        ),
    ] {
        let mut gen = original.clone();
        let report = gen
            .outcomes
            .iter_mut()
            .find(|o| o.id == "gen1_loc3")
            .unwrap()
            .report
            .as_mut()
            .unwrap();
        report
            .extensions
            .insert("qualification".into(), qualification.clone());
        if let Some(marker) = replay {
            report
                .extensions
                .insert("qualification_only".into(), marker);
        }
        let unknown = json!({"clock":"inferred","display_verified":false,"nested":[null, "<&>"]});
        report
            .extensions
            .insert("metric_provenance".into(), unknown.clone());
        report.extensions.insert(
            "conditions_observed".into(),
            json!({"fan":null,"snapshot_identical":false}),
        );
        let run = EvolutionRun {
            name: "explicit_contract_fixture".into(),
            dir: dir.clone(),
            archive: Some(archive.clone()),
            generations: vec![gen],
            problems: vec![],
        };
        let rounds = operator::rounds(&run, &paths.gol_root);
        let row = rounds[0]
            .candidates
            .iter()
            .find(|c| c.id == "gen1_loc3")
            .unwrap();
        assert_eq!(row.verdict, expected, "{name}");
        assert_eq!(
            row.on_front,
            Some(true),
            "archive membership remains historical"
        );
        let evidence = row.evidence.as_ref().unwrap();
        assert_eq!(evidence.receiver_report["qualification"], qualification);
        assert_eq!(evidence.receiver_report["metric_provenance"], unknown);
        assert_eq!(
            evidence.receiver_report["conditions_observed"]["fan"],
            serde_json::Value::Null
        );
        assert!(!evidence.view_grants_game_proof);
        assert!(!evidence.view_allows_promotion);
        let serialized = serde_json::to_value(&rounds).unwrap();
        assert!(serialized.to_string().contains("snapshot_identical"));
    }
    let mut gen = original;
    gen.outcomes
        .iter_mut()
        .find(|o| o.id == "gen1_loc3")
        .unwrap()
        .report
        .as_mut()
        .unwrap()
        .quality_reference
        .as_mut()
        .unwrap()
        .kind = "new_unknown_reference".into();
    let run = EvolutionRun {
        name: "unknown_quality_fixture".into(),
        dir,
        archive: Some(archive),
        generations: vec![gen],
        problems: vec![],
    };
    let rounds = operator::rounds(&run, &paths.gol_root);
    // The first report supplies the generation-level reference; all candidates' exact kind is retained.
    let row = rounds[0]
        .candidates
        .iter()
        .find(|c| c.id == "gen1_loc3")
        .unwrap();
    assert_eq!(
        row.evidence.as_ref().unwrap().receiver_report["quality_reference"]["kind"],
        "new_unknown_reference"
    );
}

#[test]
fn ab_view_retains_raw_negative_and_unknown_fields_and_checks_qualification() {
    use serde_json::json;
    let fixtures = fixture_paths();
    let original: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(fixtures.phonefarm_root.join("loop_v1/runs/report.json")).unwrap(),
    )
    .unwrap();
    let temporary = std::env::temp_dir().join(format!(
        "gol-viz-contract-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(temporary.join("phonefarm/loop_v1/runs")).unwrap();
    let paths = VizPaths {
        gol_root: temporary.join("gol"),
        phonefarm_root: temporary.join("phonefarm"),
        lock_root: temporary.join("lock"),
        fleet: None,
    };
    for (q, source, expected) in [
        (None, None, RoundVerdict::Evidence),
        (
            Some(json!({"stage":"conclusion","eligible":true,"evidence_origin":"recorded_device"})),
            None,
            RoundVerdict::Qualified,
        ),
        (
            Some(json!({"stage":"conclusion","eligible":true,"evidence_origin":"recorded_device"})),
            Some("static_cost_model"),
            RoundVerdict::Evidence,
        ),
        (
            Some(
                json!({"stage":"conclusion","eligible":false,"evidence_origin":"recorded_device"}),
            ),
            None,
            RoundVerdict::Invalid,
        ),
        (Some(serde_json::Value::Null), None, RoundVerdict::Invalid),
        (
            Some(
                json!({"stage":"conclusion","eligible":true,"evidence_origin":"synthetic_fixture"}),
            ),
            None,
            RoundVerdict::Evidence,
        ),
    ] {
        let mut v = original.clone();
        if let Some(q) = q {
            v["qualification"] = q;
        }
        if let Some(source) = source {
            v["source"] = json!(source);
        }
        v["metric_provenance"] = json!({"clock":"inferred","display_verified":false});
        v["conditions_observed"] = json!({"fan":null});
        v["real_game_optimization_proven"] = json!(true); // declaration cannot authorize view proof
        std::fs::write(
            paths.phonefarm_root.join("loop_v1/runs/report.json"),
            serde_json::to_vec(&v).unwrap(),
        )
        .unwrap();
        let d = viz::build(&paths);
        let r = round(&d, "sys:genshin-ddr");
        assert_eq!(r.verdict, expected);
        assert_eq!(r.receiver_report.as_ref().unwrap(), &v);
        assert!(!r.view_grants_game_proof);
        assert!(!r.view_allows_promotion);
        assert!(r.metric_source.contains("未认证"));
    }
    let mut contradictory = original.clone();
    contradictory["qualification"] =
        json!({"stage":"conclusion","eligible":true,"evidence_origin":"recorded_device"});
    contradictory["criterion_3_statistically_significant"]["pass"] = json!(false);
    std::fs::write(
        paths.phonefarm_root.join("loop_v1/runs/report.json"),
        serde_json::to_vec(&contradictory).unwrap(),
    )
    .unwrap();
    let d = viz::build(&paths);
    let r = round(&d, "sys:genshin-ddr");
    assert_eq!(r.verdict, RoundVerdict::Invalid);
    assert!(r.verdict_text.contains("逐项未通过"));
    assert!(r.criteria.iter().any(|c| c.pass == Some(false)));
    assert_eq!(r.receiver_report.as_ref().unwrap(), &contradictory);

    for broken in [
        "missing_criterion",
        "unknown_pass",
        "excess_evaluated",
        "wrong_passed",
    ] {
        let mut v = original.clone();
        v["qualification"] =
            json!({"stage":"conclusion","eligible":true,"evidence_origin":"recorded_device"});
        match broken {
            "missing_criterion" => {
                v.as_object_mut()
                    .unwrap()
                    .remove("criterion_3_statistically_significant");
            }
            "unknown_pass" => {
                v["criterion_3_statistically_significant"]["pass"] = serde_json::Value::Null
            }
            "excess_evaluated" => v["summary"]["criteria_evaluated"] = json!(6),
            "wrong_passed" => v["summary"]["criteria_passed"] = json!(4),
            _ => unreachable!(),
        }
        std::fs::write(
            paths.phonefarm_root.join("loop_v1/runs/report.json"),
            serde_json::to_vec(&v).unwrap(),
        )
        .unwrap();
        let d = viz::build(&paths);
        assert_eq!(
            round(&d, "sys:genshin-ddr").verdict,
            RoundVerdict::Invalid,
            "{broken}"
        );
    }
    std::fs::remove_dir_all(temporary).unwrap();
}

#[test]
#[cfg(unix)]
fn current_occupancy_comes_from_the_selected_phonefarm_status() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!("gol-viz-fd-status-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let pf = dir.join("selected-phonefarm");
    std::fs::write(&pf, "#!/bin/sh\n[ \"$*\" = 'lease status --json' ] || exit 9\ncat \"$(dirname \"$0\")/status.json\"\n").unwrap();
    std::fs::set_permissions(&pf, std::fs::Permissions::from_mode(0o755)).unwrap();
    let paths = fixture_paths();
    let load = || viz::device::load(&paths.lock_root, &paths.phonefarm_root, Some(&pf), 5);
    std::fs::write(dir.join("status.json"), r#"{"status":"held","entries":[{"label":"live-A","since":"now"},{"label":"live-B"}]}"#).unwrap();
    let view = load();
    assert_eq!(view.holder.unwrap().label, "live-A, live-B");
    assert!(view.queue.is_empty());
    assert!(view.problems.is_empty());
    std::fs::write(dir.join("status.json"), r#"{"status":"clear","entries":[]}"#).unwrap();
    assert!(load().holder.is_none());
    std::fs::write(&pf, "#!/bin/sh\necho 'cannot read kernel lock' >&2\nexit 7\n").unwrap();
    let view = load();
    assert!(view.holder.is_none());
    assert!(view.problems.iter().any(|p| p.contains("cannot read kernel lock")));
    std::fs::remove_dir_all(dir).unwrap();
}
