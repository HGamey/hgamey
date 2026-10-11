//! 端到端: 跑真的多代演化闭环, 检查它的不变量。
//!
//! 全部走 `--dry-run` 等价路径 (静态成本模型) 且关掉 LLM:
//! 测试不该依赖真机在线, 也不该依赖某个外部推理服务此刻可用。
//! LLM 通道本身由 `engine::generator` 的单测覆盖 (prompt 编排、回包解析、降级)。

use game_opt_loop::engine::archive::ParetoArchive;
use game_opt_loop::engine::cost_model::NX809J_1080P;
use game_opt_loop::engine::evolution::{Evolution, EvolutionConfig};
use game_opt_loop::engine::{MetricSource, Validator};
use game_opt_loop::{tracks, TrackMode};
use std::path::PathBuf;

fn temp_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "gol_test_{tag}_{}_{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn config(track: TrackMode, out: PathBuf, seed: u64, generations: u32) -> EvolutionConfig {
    EvolutionConfig {
        track,
        generations,
        children: 4,
        budget_ms: tracks::spec(track).budget_ms,
        out_dir: out,
        dry_run: true,
        seed,
        prefer_provider: None,
        secrets_path: PathBuf::from("/nonexistent/secrets.env"),
        use_llm: false,
        llm_timeout_s: None,
        device: NX809J_1080P,
        carrier: "headless".into(),
        scene_frames: 900,
        scene_intensity: 5,
        cool_c: 32.0,
        quality_reference: None,
        campaign: None,
        phonefarm: PathBuf::from("/nonexistent/phonefarm"),
        serial: None,
    }
}

fn run(track: TrackMode, tag: &str, seed: u64, generations: u32) -> (ParetoArchive, PathBuf) {
    let out = temp_dir(tag);
    let mut evo = Evolution::new(config(track, out.clone(), seed, generations)).unwrap();
    assert!(!evo.llm_available(), "测试必须走离线路径");
    let summary = evo.run().unwrap();

    assert_eq!(summary.generations, generations);
    assert_eq!(summary.metric_source, MetricSource::StaticCostModel);
    (evo.archive().clone(), out)
}

#[test]
fn a_full_dry_run_produces_the_expected_artifacts() {
    let (_archive, out) = run(TrackMode::Sr, "artifacts", 20260921, 3);

    assert!(out.join("archive.json").is_file(), "缺 archive.json");
    for g in 1..=3 {
        assert!(
            out.join(format!("gen_{g}.json")).is_file(),
            "缺 gen_{g}.json"
        );
    }
    // 只有过了门禁的算子才允许落盘。
    let candidates = out.join("candidates");
    assert!(candidates.is_dir(), "缺 candidates 目录");
    let comps: Vec<_> = std::fs::read_dir(&candidates)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().is_some_and(|x| x == "comp"))
        .collect();
    assert!(!comps.is_empty(), "一个算子都没落盘");

    let validator = Validator::new();
    for entry in comps {
        let src = std::fs::read_to_string(entry.path()).unwrap();
        let report = validator.gate(&src, &tracks::SR);
        assert!(
            report.passed,
            "落盘的算子 {:?} 过不了门禁: {:?}",
            entry.file_name(),
            report.violations
        );

        // 每个 .comp 旁边必须有同名 .spv —— eval_request.spirv_path 指的就是它,
        // phonefarm 拿这个路径把字节码推上真机。
        let spv = entry.path().with_extension("spv");
        assert!(
            spv.is_file(),
            "{:?} 没有对应的 .spv, 真机下发会死在文件不存在上",
            entry.file_name()
        );
        assert_spirv_module(&spv);
    }

    // A/B 对照的 A 臂必须存在, 否则 t 检验无从谈起。
    let baseline = candidates.join("baseline.spv");
    assert!(baseline.is_file(), "缺少 baseline.spv, A/B 对照没有 A 臂");
    assert_spirv_module(&baseline);

    let _ = std::fs::remove_dir_all(&out);
}

/// 断言这是一份真的 SPIR-V 模块, 而不仅仅是个非空文件。
///
/// 只检查"文件存在且不为空"是不够的: 写错字节序、写进 words 的十进制文本、
/// 或者写了半截, 都能通过那种检查, 然后在真机上变成驱动报错甚至 GPU 挂起。
fn assert_spirv_module(path: &std::path::Path) {
    let bytes = std::fs::read(path).unwrap();
    assert!(
        bytes.len() >= 20 && bytes.len().is_multiple_of(4),
        "{}: SPIR-V 长度 {} 不合法 (至少 20 字节且为 4 的倍数)",
        path.display(),
        bytes.len()
    );
    let magic = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
    assert_eq!(
        magic,
        0x0723_0203,
        "{}: SPIR-V 魔数不对 (读到 0x{magic:08X}), 字节序或内容写错了",
        path.display()
    );
    let version = u32::from_le_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]);
    assert_eq!((version >> 16) & 0xFF, 1, "{}: SPIR-V 主版本应为 1", path.display());
}

/// 落盘的字节码必须与门禁当场编出来的那一份逐字节相同 ——
/// 真机跑的和本地验过的不是同一个东西, 整条闭环的结论就不成立。
#[test]
fn the_persisted_spirv_matches_what_the_gate_compiled() {
    let (_archive, out) = run(TrackMode::Sr, "spvmatch", 20260921, 2);
    let candidates = out.join("candidates");
    let validator = Validator::new();

    let mut checked = 0usize;
    for entry in std::fs::read_dir(&candidates).unwrap().filter_map(Result::ok) {
        let path = entry.path();
        if path.extension().is_none_or(|x| x != "comp") {
            continue;
        }
        let src = std::fs::read_to_string(&path).unwrap();
        let gate = validator.gate(&src, &tracks::SR);
        let words = gate.spirv.as_ref().expect("门禁应当产出 SPIR-V");
        let expected: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();

        let actual = std::fs::read(path.with_extension("spv")).unwrap();
        assert_eq!(
            actual,
            expected,
            "{}: 落盘的 SPIR-V 与门禁编出来的不一致",
            path.display()
        );
        assert_eq!(
            gate.spirv_bytes,
            Some(expected.len()),
            "spirv_bytes 与实际字节数对不上"
        );
        checked += 1;
    }
    assert!(checked > 0, "一个候选都没检查到");

    let _ = std::fs::remove_dir_all(&out);
}

/// 同一个种子必须给出逐字节一致的档案 —— 否则归档没有复现价值。
#[test]
fn the_same_seed_reproduces_the_same_archive() {
    let (a, out_a) = run(TrackMode::Sr, "repro_a", 4242, 3);
    let (b, out_b) = run(TrackMode::Sr, "repro_b", 4242, 3);

    let ids_a: Vec<&str> = a.entries.iter().map(|e| e.id.as_str()).collect();
    let ids_b: Vec<&str> = b.entries.iter().map(|e| e.id.as_str()).collect();
    assert_eq!(ids_a, ids_b, "同种子两次运行的候选集合不同");

    let fps_a: Vec<&str> = a.entries.iter().map(|e| e.fingerprint.as_str()).collect();
    let fps_b: Vec<&str> = b.entries.iter().map(|e| e.fingerprint.as_str()).collect();
    assert_eq!(fps_a, fps_b, "同种子两次运行的算子源码不同");

    let _ = std::fs::remove_dir_all(&out_a);
    let _ = std::fs::remove_dir_all(&out_b);
}

/// 换种子要换出不同的搜索轨迹, 否则种子参数是摆设。
#[test]
fn a_different_seed_explores_differently() {
    let (a, out_a) = run(TrackMode::Sr, "seed_a", 1, 3);
    let (b, out_b) = run(TrackMode::Sr, "seed_b", 999_777, 3);

    let fps_a: Vec<&str> = a.entries.iter().map(|e| e.fingerprint.as_str()).collect();
    let fps_b: Vec<&str> = b.entries.iter().map(|e| e.fingerprint.as_str()).collect();
    assert_ne!(fps_a, fps_b, "换了种子却跑出完全一样的轨迹");

    let _ = std::fs::remove_dir_all(&out_a);
    let _ = std::fs::remove_dir_all(&out_b);
}

/// 前沿里的每一条都必须通过全部硬约束。这是 §2 红线 3 的可执行形式:
/// 一个又快又糊的算子绝不能因为"快"就挤进优胜集。
#[test]
fn every_front_entry_satisfies_the_hard_constraints() {
    let (archive, out) = run(TrackMode::Sr, "constraints", 20260921, 5);
    let spec = &tracks::SR;

    let front = archive.front();
    assert!(!front.is_empty(), "五代下来前沿是空的, 演化没有产出");

    for e in &front {
        let m = &e.report.metrics;
        assert!(
            m.operator_latency_ms <= spec.budget_ms,
            "{} 耗时 {:.3} ms 超预算 {:.2}",
            e.id,
            m.operator_latency_ms,
            spec.budget_ms
        );
        // 画质没量到时 (真机链路可能回 null) 这条约束无从校验, 不拿默认值冒充。
        if let Some(q) = m.psnr_db {
            assert!(
                q >= spec.quality_baseline_db,
                "{} 画质 {:.3} dB 低于基线 {:.3}",
                e.id,
                q,
                spec.quality_baseline_db
            );
        }
    }

    // 前沿内部必须互不支配。
    for a in &front {
        for b in &front {
            if a.id == b.id {
                continue;
            }
            assert!(
                !a.objectives().dominates(&b.objectives()),
                "{} 支配了同在前沿的 {}",
                a.id,
                b.id
            );
        }
    }

    let _ = std::fs::remove_dir_all(&out);
}

/// 演化要真的往前走: 累计最优画质序列不能倒退。
#[test]
fn the_cumulative_best_quality_never_regresses() {
    let out = temp_dir("monotone");
    let mut evo = Evolution::new(config(TrackMode::Sr, out.clone(), 20260921, 5)).unwrap();
    let summary = evo.run().unwrap();

    let mut prev: Option<f32> = None;
    for v in summary.best_psnr_series.iter().flatten() {
        if let Some(p) = prev {
            assert!(
                *v >= p - 1e-4,
                "累计最优画质出现倒退: {p:.3} → {v:.3} (序列 {:?})",
                summary.best_psnr_series
            );
        }
        prev = Some(*v);
    }
    assert!(prev.is_some(), "五代下来一个合格算子都没有");

    let _ = std::fs::remove_dir_all(&out);
}

/// 档案里不该出现重复指纹 —— 重复就是白白浪费一次真机 60s 回放。
#[test]
fn the_archive_never_holds_duplicate_fingerprints() {
    let (archive, out) = run(TrackMode::Sr, "dedup", 20260921, 5);
    let mut seen = std::collections::HashSet::new();
    for e in &archive.entries {
        assert!(
            seen.insert(e.fingerprint.clone()),
            "指纹 {} 在档案里出现了两次",
            e.fingerprint
        );
    }
    let _ = std::fs::remove_dir_all(&out);
}

/// dry-run 的每一条记录都必须自报「我是估计值」。
#[test]
fn dry_run_records_are_all_labelled_as_estimates() {
    let (archive, out) = run(TrackMode::Sr, "labels", 20260921, 3);
    assert!(!archive.entries.is_empty());
    for e in &archive.entries {
        assert_eq!(
            e.report.source,
            MetricSource::StaticCostModel,
            "{} 没有标成估计值",
            e.id
        );
        assert!(!e.report.source.is_measured());
    }
    // 没有真机数据 → 实测前沿必须为空, 不能拿估计值冒充。
    assert!(
        archive.measured_front().is_empty(),
        "dry-run 竟然产出了'实测'前沿"
    );
    let _ = std::fs::remove_dir_all(&out);
}

/// 插帧赛道同样要能跑通, 而且算子必须符合它自己的 binding 契约。
#[test]
fn the_frame_gen_track_closes_the_loop_too() {
    let (archive, out) = run(TrackMode::FrameGen, "framegen", 7, 3);
    assert!(!archive.entries.is_empty());
    assert_eq!(archive.track, "frame_gen");

    let validator = Validator::new();
    for e in &archive.entries {
        let report = validator.gate(&e.shader_source, &tracks::FRAME_GEN);
        assert!(report.passed, "{} 不符合插帧赛道契约: {:?}", e.id, report.violations);
        // 同一个算子套到超分赛道上必然缺槽位 —— 赛道契约真的在起作用。
        let cross = validator.gate(&e.shader_source, &tracks::SR);
        assert!(!cross.passed, "{} 居然同时满足两条赛道的契约", e.id);
    }
    let _ = std::fs::remove_dir_all(&out);
}

/// 断点续跑: 同一个输出目录再跑一轮, 应该在既有档案上继续累积而不是推倒重来。
#[test]
fn a_second_run_resumes_from_the_existing_archive() {
    let out = temp_dir("resume");

    let mut first = Evolution::new(config(TrackMode::Sr, out.clone(), 20260921, 2)).unwrap();
    first.run().unwrap();
    let after_first = first.archive().entries.len();
    assert!(after_first > 0);

    let mut second = Evolution::new(config(TrackMode::Sr, out.clone(), 555, 2)).unwrap();
    second.run().unwrap();
    assert!(
        second.archive().entries.len() >= after_first,
        "续跑把已有档案弄丢了: {} → {}",
        after_first,
        second.archive().entries.len()
    );

    let _ = std::fs::remove_dir_all(&out);
}

/// 真机 2026-09-22 产出的那份报告, 上游必须读得进来。
/// headless 算子标尺量不到帧时 p95, 该字段是 null —— 契约必须容得下「未测量」。
#[test]
fn the_upstream_can_parse_a_real_device_report() {
    let raw = r#"{
      "candidate_id":"gen1_loc1","status":"PASS",
      "metrics":{"operator_latency_ms":0.922552,"fps_p95_ms":null,
                 "power_watt":2.9701035,"psnr_db":42.826},
      "verdict":{"is_pareto_improvement":false,"p_value":1.5344861816377811e-6},
      "source":"phonefarm"}"#;
    let r = game_opt_loop::harness::parse_eval_report(raw)
        .expect("真机报告解析失败 —— 两仓库的契约对不上");
    assert!(r.metrics.fps_p95_ms.is_none(), "未测量的帧时必须是 None, 不能被当成 0");
    assert_eq!(r.candidate_id, "gen1_loc1");
    assert!((r.metrics.operator_latency_ms - 0.922552).abs() < 1e-6);
    assert!(r.verdict.p_value < 0.01);
}
