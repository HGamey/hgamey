//! 契约一致性: Rust 结构体序列化出来的东西, 必须符合 `contracts/*.schema.json`。
//!
//! 为什么要有这组测试: 契约文件是给 phonefarm 那侧看的, 但它不参与 Rust 编译,
//! 改 struct 的时候没有任何东西会提醒你去同步 schema。这里用一个只覆盖
//! 我们实际用到的那几个关键字 (required / type / enum / const / pattern) 的
//! 迷你校验器把两边钉在一起 —— 不为通用性, 只为「漂移了会红」。

use game_opt_loop::engine::{
    Candidate, CandidateOrigin, EvalReport, EvalStatus, MetricSource, Metrics, ParetoArchive,
    Verdict,
};
use game_opt_loop::harness::PhonefarmDriver;
use game_opt_loop::tracks::SR;
use serde_json::Value;
use std::path::{Path, PathBuf};

fn contracts_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("contracts")
}

fn load_schema(name: &str) -> Value {
    let path = contracts_dir().join(name);
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读取契约 {} 失败: {e}", path.display()));
    serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("契约 {} 不是合法 JSON: {e}", path.display()))
}

/// 迷你 JSON Schema 校验器。返回违规说明列表, 空 = 通过。
fn validate(instance: &Value, schema: &Value, path: &str, out: &mut Vec<String>) {
    // $ref: 只支持同目录下的相对文件引用。
    if let Some(r) = schema.get("$ref").and_then(Value::as_str) {
        let target = load_schema(r);
        validate(instance, &target, path, out);
        return;
    }

    if let Some(expected) = schema.get("const") {
        if instance != expected {
            out.push(format!("{path}: 期望常量 {expected}, 实际 {instance}"));
        }
    }

    if let Some(options) = schema.get("enum").and_then(Value::as_array) {
        if !options.contains(instance) {
            out.push(format!("{path}: {instance} 不在枚举 {options:?} 内"));
        }
    }

    if let Some(ty) = schema.get("type") {
        let allowed: Vec<&str> = match ty {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => vec![],
        };
        let actual = match instance {
            Value::Null => "null",
            Value::Bool(_) => "boolean",
            Value::Number(n) => {
                if n.is_i64() || n.is_u64() {
                    "integer"
                } else {
                    "number"
                }
            }
            Value::String(_) => "string",
            Value::Array(_) => "array",
            Value::Object(_) => "object",
        };
        // JSON 里整数也算 number。
        let ok = allowed.contains(&actual)
            || (actual == "integer" && allowed.contains(&"number"));
        if !allowed.is_empty() && !ok {
            out.push(format!("{path}: 期望类型 {allowed:?}, 实际 {actual}"));
        }
    }

    if let (Some(pat), Some(s)) = (
        schema.get("pattern").and_then(Value::as_str),
        instance.as_str(),
    ) {
        // 只需要支持契约里实际用到的 ^[0-9a-f]{16}$ 这一种。
        if pat == "^[0-9a-f]{16}$" {
            let ok = s.len() == 16 && s.chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase());
            if !ok {
                out.push(format!("{path}: `{s}` 不满足 pattern {pat}"));
            }
        }
    }

    if let Some(obj) = instance.as_object() {
        if let Some(required) = schema.get("required").and_then(Value::as_array) {
            for key in required.iter().filter_map(Value::as_str) {
                if !obj.contains_key(key) {
                    out.push(format!("{path}: 缺少必填字段 `{key}`"));
                }
            }
        }
        if let Some(props) = schema.get("properties").and_then(Value::as_object) {
            for (key, sub_schema) in props {
                if let Some(v) = obj.get(key) {
                    validate(v, sub_schema, &format!("{path}.{key}"), out);
                }
            }
        }
    }

    if let (Some(arr), Some(items)) = (instance.as_array(), schema.get("items")) {
        for (i, v) in arr.iter().enumerate() {
            validate(v, items, &format!("{path}[{i}]"), out);
        }
    }

    // allOf + if/then: 契约 v2 用它们做「按 kind 分叉」的判别式联合。
    // 不支持这两个关键字的话, 校验器会把每一类 payload 都当成「没写要求」,
    // 于是 kind 与 payload 形状对不上也照样过 —— 那正是这份契约最需要钉住的一件事。
    if let Some(all) = schema.get("allOf").and_then(Value::as_array) {
        for (i, sub) in all.iter().enumerate() {
            match (sub.get("if"), sub.get("then")) {
                (Some(cond), Some(then)) => {
                    let mut scratch = Vec::new();
                    validate(instance, cond, path, &mut scratch);
                    if scratch.is_empty() {
                        validate(instance, then, path, out);
                    }
                }
                _ => validate(instance, sub, &format!("{path}#allOf[{i}]"), out),
            }
        }
    }
}

fn assert_conforms(instance: &Value, schema_name: &str) {
    let schema = load_schema(schema_name);
    let mut problems = Vec::new();
    validate(instance, &schema, schema_name.trim_end_matches(".schema.json"), &mut problems);
    assert!(
        problems.is_empty(),
        "与 {schema_name} 不符:\n  {}",
        problems.join("\n  ")
    );
}

fn sample_candidate() -> Candidate {
    Candidate::new(
        "gen2_llm1",
        2,
        "sr",
        Some("gen1_loc3".into()),
        1.5,
        game_opt_loop::tracks::embedded_template(game_opt_loop::TrackMode::Sr).to_string(),
        CandidateOrigin::Llm,
        Some("4x4 tap / polywindow".into()),
    )
}

fn sample_report(source: MetricSource) -> EvalReport {
    EvalReport {
        extensions: Default::default(),
        candidate_id: "gen2_llm1".into(),
        status: EvalStatus::Pass,
        metrics: Metrics {
            operator_latency_ms: 1.12,
            fps_p95_ms: Some(16.9),
            power_watt: Some(4.71),
            psnr_db: Some(38.62),
        },
        verdict: Verdict {
            is_pareto_improvement: true,
            p_value: 0.0031,
        },
        source,
        quality_reference: None,
        power_source: None,
    }
}

#[test]
fn candidate_matches_its_contract() {
    let v = serde_json::to_value(sample_candidate()).unwrap();
    assert_conforms(&v, "candidate.schema.json");
}

#[test]
fn eval_report_matches_its_contract_for_both_sources() {
    for source in [MetricSource::Phonefarm, MetricSource::StaticCostModel] {
        let v = serde_json::to_value(sample_report(source)).unwrap();
        assert_conforms(&v, "eval_report.schema.json");
    }
}

/// status 的每一个取值都必须落在契约枚举里 —— 少一个都会让 phonefarm 侧解析炸掉。
#[test]
fn every_eval_status_is_in_the_contract_enum() {
    for status in [
        EvalStatus::Pass,
        EvalStatus::Slow,
        EvalStatus::PoorQuality,
        EvalStatus::Crash,
        EvalStatus::Reverted,
    ] {
        let mut r = sample_report(MetricSource::Phonefarm);
        r.status = status;
        let v = serde_json::to_value(&r).unwrap();
        assert_conforms(&v, "eval_report.schema.json");
    }
}

#[test]
fn every_candidate_origin_is_in_the_contract_enum() {
    for origin in [
        CandidateOrigin::Baseline,
        CandidateOrigin::Llm,
        CandidateOrigin::LocalMutation,
    ] {
        let mut c = sample_candidate();
        c.origin = origin;
        let v = serde_json::to_value(&c).unwrap();
        assert_conforms(&v, "candidate.schema.json");
    }
}

#[test]
fn eval_request_matches_its_contract() {
    let driver = PhonefarmDriver::new().with_serial(Some("R5CT30ABCDE".into()));
    let candidate = sample_candidate();
    let request = driver.build_request(
        &candidate,
        &SR,
        Path::new("runs/evo/candidates/gen2_llm1.comp"),
        Path::new("runs/evo/candidates/gen2_llm1.spv"),
    );
    let v = serde_json::to_value(&request).unwrap();
    assert_conforms(&v, "eval_request.schema.json");
    // 第 0 代没有冠军可比, 该字段就不该出现 —— 不拿 0 冒充"冠军耗时 0ms"。
    assert!(
        v.get("incumbent_latency_ms").is_none(),
        "没有在位冠军时不该下发这个字段"
    );
}

/// 带在位冠军耗时的请求同样合契约, 且数字原样送达。
///
/// 这个字段是给 refbench 载体用的: 那边 A 臂不挂算子, 基线臂的算子耗时
/// 按定义为 0, 不带冠军耗时的话"候选更快吗"恒假。
#[test]
fn eval_request_carries_the_incumbent_when_there_is_one() {
    let driver = PhonefarmDriver::new();
    let candidate = sample_candidate();
    let request = driver.build_request_against(
        &candidate,
        &SR,
        Path::new("runs/evo/candidates/gen2_llm1.comp"),
        Path::new("runs/evo/candidates/gen2_llm1.spv"),
        Some(0.187),
    );
    let v = serde_json::to_value(&request).unwrap();
    assert_conforms(&v, "eval_request.schema.json");
    assert_eq!(
        v["incumbent_latency_ms"].as_f64().map(|x| (x * 1000.0).round()),
        Some(187.0)
    );
}

/// 画质真值的来源随请求下发, 且合契约。
///
/// 不带这一项的话接收端退回 runner 内置的程序化图案 —— 那把尺子跟真实画面
/// 差约 11 dB, 按它挑出来的"画质更好的算子"未必在真实画面上也更好。
#[test]
fn eval_request_carries_the_quality_reference() {
    let candidate = sample_candidate();
    let paths = (
        Path::new("runs/evo/candidates/gen2_llm1.comp"),
        Path::new("runs/evo/candidates/gen2_llm1.spv"),
    );

    let bare = PhonefarmDriver::new().build_request(&candidate, &SR, paths.0, paths.1);
    let v = serde_json::to_value(&bare).unwrap();
    assert_conforms(&v, "eval_request.schema.json");
    assert!(
        v.get("quality_reference").is_none(),
        "没指定时不该凭空写一个来源进去"
    );

    let with_ref = PhonefarmDriver::new()
        .with_quality_reference(Some("../phonefarm/tasks/genshin_refframes".into()))
        .build_request(&candidate, &SR, paths.0, paths.1);
    let v = serde_json::to_value(&with_ref).unwrap();
    assert_conforms(&v, "eval_request.schema.json");
    assert_eq!(
        v["quality_reference"],
        "../phonefarm/tasks/genshin_refframes"
    );
}

/// 接收端回报的画质真值出处合契约, 并被原样解析进档案。
#[test]
fn eval_report_carries_where_the_quality_truth_came_from() {
    let mut report = sample_report(MetricSource::Phonefarm);
    report.quality_reference = Some(game_opt_loop::engine::QualityReference {
        kind: "real_frames".into(),
        source: Some("../phonefarm/tasks/genshin_refframes".into()),
        frames: 12,
        frame_files: vec!["frame_00000.png".into(), "frame_00001.png".into()],
        candidate_psnr_db: vec![Some(31.2), None],
        baseline_psnr_db: vec![Some(29.1), Some(28.7)],
    });
    let v = serde_json::to_value(&report).unwrap();
    assert_conforms(&v, "eval_report.schema.json");
    assert_eq!(v["quality_reference"]["kind"], "real_frames");
    assert_eq!(v["quality_reference"]["frames"], 12);

    // 程序化图案也是一个合法的、必须被说出来的来源。
    report.quality_reference = Some(game_opt_loop::engine::QualityReference {
        kind: "procedural".into(),
        source: None,
        frames: 1,
        frame_files: vec![],
        candidate_psnr_db: vec![],
        baseline_psnr_db: vec![],
    });
    assert_conforms(
        &serde_json::to_value(&report).unwrap(),
        "eval_report.schema.json",
    );

    // 旧版接收端不报这一项, 照样解析得动 —— 加字段不能把已有回包变成解析错误。
    let legacy = r#"{"candidate_id":"c","status":"PASS",
        "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":7.3,"power_watt":6.5,"psnr_db":31.3},
        "verdict":{"is_pareto_improvement":false,"p_value":0.004},"source":"phonefarm"}"#;
    let back: EvalReport = serde_json::from_str(legacy).unwrap();
    assert!(back.quality_reference.is_none());
}

/// 供电状态随报告回传, 且合契约。非放电态下 power_watt 为 null。
#[test]
fn eval_report_carries_the_power_source() {
    let mut report = sample_report(MetricSource::Phonefarm);

    // 放电态 (停充成功): 有瓦数, 且可比。
    report.power_source = Some(game_opt_loop::engine::PowerSource {
        rail: "battery".into(),
        battery_status: "Discharging".into(),
        battery_current_now_ua: Some(-1_200_000),
        battery_capacity_pct: Some(84),
        on_battery: true,
        charging_suspended_via: Some("qcom-battery".into()),
    });
    let v = serde_json::to_value(&report).unwrap();
    assert_conforms(&v, "eval_report.schema.json");
    assert_eq!(v["power_source"]["on_battery"], true);
    assert!(report.power_exclusion_reason().is_none());

    // 充电态: 契约要求 power_watt 为 null, 且这一项不参与支配排序。
    report.metrics.power_watt = None;
    report.power_source = Some(game_opt_loop::engine::PowerSource {
        rail: "usb".into(),
        battery_status: "Charging".into(),
        battery_current_now_ua: Some(1_500_000),
        battery_capacity_pct: Some(84),
        on_battery: false,
        charging_suspended_via: None,
    });
    let v = serde_json::to_value(&report).unwrap();
    assert_conforms(&v, "eval_report.schema.json");
    assert!(v["metrics"]["power_watt"].is_null());
    assert!(report.power_exclusion_reason().is_some());

    // 旧版接收端不报这一项, 照样解析得动。
    let legacy = r#"{"candidate_id":"c","status":"PASS",
        "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":7.3,"power_watt":6.5,"psnr_db":31.3},
        "verdict":{"is_pareto_improvement":false,"p_value":0.004},"source":"phonefarm"}"#;
    let back: EvalReport = serde_json::from_str(legacy).unwrap();
    assert!(back.power_source.is_none());
    // 没报供电状态时默认排除 —— 分辨不出情况时, 少比一维是安全的一侧。
    assert!(back.power_exclusion_reason().is_some());
}

#[test]
fn pareto_archive_matches_its_contract() {
    let mut archive = ParetoArchive::new(SR.id, SR.budget_ms, SR.quality_baseline_db);
    let candidate = sample_candidate();
    assert!(archive.insert(
        &candidate,
        sample_report(MetricSource::Phonefarm),
        "2026-09-22T00:00:00Z".into()
    ));
    let v = serde_json::to_value(&archive).unwrap();
    assert_conforms(&v, "pareto_archive.schema.json");
}

/// 指纹必须是 16 位小写十六进制 —— 契约里写了 pattern, 这里确认实现真的照办。
#[test]
fn fingerprints_look_like_the_contract_says() {
    for src in ["#version 450\nvoid main() {}", "", "随便什么东西"] {
        let fp = game_opt_loop::engine::fingerprint(src);
        assert_eq!(fp.len(), 16, "指纹长度不对: {fp}");
        assert!(
            fp.chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()),
            "指纹不是小写十六进制: {fp}"
        );
    }
}

/// 校验器本身要能抓到问题, 否则上面几条全是空转。
#[test]
fn the_mini_validator_actually_catches_violations() {
    let schema = load_schema("eval_report.schema.json");

    let mut missing = serde_json::to_value(sample_report(MetricSource::Phonefarm)).unwrap();
    missing.as_object_mut().unwrap().remove("metrics");
    let mut problems = Vec::new();
    validate(&missing, &schema, "r", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("metrics")),
        "漏掉必填字段没被抓到: {problems:?}"
    );

    let mut bad_enum = serde_json::to_value(sample_report(MetricSource::Phonefarm)).unwrap();
    bad_enum["status"] = Value::String("TOTALLY_FINE".into());
    let mut problems = Vec::new();
    validate(&bad_enum, &schema, "r", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("枚举")),
        "非法枚举值没被抓到: {problems:?}"
    );

    let mut bad_type = serde_json::to_value(sample_report(MetricSource::Phonefarm)).unwrap();
    bad_type["metrics"]["psnr_db"] = Value::String("很高".into());
    let mut problems = Vec::new();
    validate(&bad_type, &schema, "r", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("类型")),
        "类型错误没被抓到: {problems:?}"
    );
}

// ─────────────────────────────────────────────────────────────────────────────
// v2 契约: 三类改动面 (shader / sysparam / gray) 走同一个信封
//
// 这一组测试只吃 JSON 字面量, 不经过任何 Rust 结构体。故意的: 契约是给
// **phonefarm 那侧**看的, 它写的是回包长什么样, 而不是本仓库的 struct 长什么样。
// 拿 struct 序列化出来的东西去校验契约, 只能证明两边自洽; 拿一份手写的、
// 对面将来会真的发过来的 JSON 去校验, 才能证明契约本身说得清楚。
// ─────────────────────────────────────────────────────────────────────────────

fn v2_request_json(kind: &str, payload: Value) -> Value {
    serde_json::json!({
        "version": 2,
        "kind": kind,
        "candidate_id": "g1c1",
        "serial": "91253241019A",
        "package": "com.miHoYo.Yuanshen",
        "workload": {
            "carrier": "game_replay",
            "script": "../phonefarm/knobs/gray/workload_spin_touch_v1.json",
            "seconds": 36
        },
        "conditions": {
            "cool_c": 42.3,
            "temp_cap_c": 45.0,
            "require_root": true,
            "require_discharging": true,
            "charging_suspend": {
                "enabled": true,
                "node": "/sys/class/qcom-battery/charging_enabled"
            },
            "fan": "unknown",
            "ingame_fps_setting": null
        },
        "protocol": {
            "cool_c": 42.3,
            "replay_seconds": 36,
            "rounds": 5,
            "alternating": true,
            "p_threshold": 0.05,
            "stat": "exact_permutation",
            "multiple_comparison": "bonferroni_on_wins",
            "metrics": [
                {"id": "fps_p95_ms", "better": "lower"},
                {"id": "fps_mean", "better": "higher"},
                {"id": "power_watt", "better": "lower"}
            ]
        },
        "payload": payload
    })
}

fn shader_payload() -> Value {
    serde_json::json!({
        "track": "sr",
        "shader_path": "runs/evo/candidates/g1c1.comp",
        "spirv_path": "runs/evo/candidates/g1c1.spv",
        "budget_ms": 1.5,
        "quality_baseline_db": 34.5,
        "incumbent_latency_ms": 0.477,
        "quality_reference": "../phonefarm/tasks/genshin_refframes",
        "inject": { "via": "vulkan_layer", "layer": "VK_LAYER_refknobs_readonly" }
    })
}

fn sysparam_payload() -> Value {
    serde_json::json!({
        "why": "这台机器上访存下限吃紧",
        "params": { "bus.DDR.boost_freq": "5333000" },
        "plan": [{
            "kind": "sysfs",
            "path": "/sys/devices/system/cpu/bus_dcvs/DDR/boost_freq",
            "value": "5333000"
        }],
        "whitelist": {
            "bus.DDR.boost_freq": {
                "kind": "sysfs",
                "path": "/sys/devices/system/cpu/bus_dcvs/DDR/boost_freq",
                "values": ["547000", "1555000", "5333000"],
                "current": "547000",
                "group": null
            }
        },
        "deny_keywords": ["thermal", "trip_point", "cooling", "fan", "tsens", "bcl", "throttl"]
    })
}

fn gray_payload() -> Value {
    serde_json::json!({
        "why": "省一次 tile 回读",
        "layer": null,
        "rewrites": [{ "knob": "loadop_dont_care", "scope": null, "args": null }],
        "require_hit": true,
        "side_effect_ack": "层加载成功后立刻清属性"
    })
}

/// 三类请求都得合同一份契约 —— 这就是「统一测评接口」那句话的全部含义。
#[test]
fn every_v2_request_kind_matches_the_contract() {
    for (kind, payload) in [
        ("shader", shader_payload()),
        ("sysparam", sysparam_payload()),
        ("gray", gray_payload()),
    ] {
        assert_conforms(&v2_request_json(kind, payload), "eval_request.schema.json");
    }
}

/// 契约里的分叉必须真的把 payload 钉住: kind=gray 却塞一份 shader 的 payload
/// 要被抓出来。不然「三类走同一个信封」就只是个说法。
#[test]
fn the_contract_actually_pins_each_payload_to_its_kind() {
    let schema = load_schema("eval_request.schema.json");

    let v = v2_request_json("gray", shader_payload());
    let mut problems = Vec::new();
    validate(&v, &schema, "req", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("rewrites")),
        "gray 的 payload 少了 rewrites 却没被抓到: {problems:?}"
    );

    let v = v2_request_json("sysparam", gray_payload());
    let mut problems = Vec::new();
    validate(&v, &schema, "req", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("params")),
        "sysparam 的 payload 少了 params 却没被抓到: {problems:?}"
    );
}

/// v2 信封少了 conditions 要被抓到 —— 没有测试条件的数字跨轮不可比。
#[test]
fn a_v2_request_without_conditions_is_refused() {
    let mut v = v2_request_json("sysparam", sysparam_payload());
    v.as_object_mut().unwrap().remove("conditions");
    let mut problems = Vec::new();
    validate(&v, &load_schema("eval_request.schema.json"), "req", &mut problems);
    assert!(
        problems.iter().any(|p| p.contains("conditions")),
        "{problems:?}"
    );
}

/// v1 的扁平请求仍然合契约 —— `phonefarm gpu-op` 那条路还在跑,
/// 加 v2 不能把它作废。
#[test]
fn the_v1_request_shape_is_still_accepted() {
    let driver = PhonefarmDriver::new();
    let request = driver.build_request(
        &sample_candidate(),
        &SR,
        Path::new("a.comp"),
        Path::new("a.spv"),
    );
    let v = serde_json::to_value(&request).unwrap();
    assert_eq!(v["version"], 1);
    assert_conforms(&v, "eval_request.schema.json");

    // 而且 v1 缺了它自己那几个必填字段时照样会被抓出来。
    let mut broken = v.clone();
    broken.as_object_mut().unwrap().remove("shader_path");
    let mut problems = Vec::new();
    validate(&broken, &load_schema("eval_request.schema.json"), "req", &mut problems);
    assert!(problems.iter().any(|p| p.contains("shader_path")), "{problems:?}");
}

/// v2 报告合契约, 而且 null 原样是 null。
#[test]
fn a_v2_report_matches_the_contract() {
    let raw = r#"{
      "version": 2, "kind": "sysparam", "candidate_id": "g1c1", "status": "REJECT",
      "metrics": {
        "operator_latency_ms": null, "fps_p95_ms": 36.28, "fps_mean": 29.83,
        "power_watt": null, "psnr_db": null, "soc_temp_max_c": 43.1,
        "unavailable": [{"field":"power_watt","reason":"设备在充电, 电池轨量到的是流入功率"}]
      },
      "verdict": {
        "is_pareto_improvement": false, "p_value": 0.4127, "stat": "exact_permutation",
        "alpha_win": 0.025, "alpha_regression": 0.05, "reachable": true,
        "wins": [], "regressions": [],
        "per_metric": {"fps_p95_ms": {"status":"无显著变化","a_mean":36.3,"b_mean":36.28,
                       "diff_mean":-0.02,"diff_pct":-0.06,"p":0.4127}},
        "reason": "没有任何指标显著改善"
      },
      "source": "phonefarm",
      "power_source": {"rail":"battery","battery_status":"Charging","on_battery":false,
                       "battery_current_now_ua":1072000,"battery_capacity_pct":91,
                       "charging_suspended_via":null},
      "conditions_observed": {"cool_reached_c":42.0,"cool_timeout":false,"apply_ok":true,
                              "snapshot_identical":true,"restored":true,"fan":"unknown"},
      "evidence_dir": "runs_sysparam/x/gen1/cand1"
    }"#;
    let v: Value = serde_json::from_str(raw).unwrap();
    assert_conforms(&v, "eval_report.schema.json");
    assert!(v["metrics"]["power_watt"].is_null(), "null 不能变成 0");
}

/// ABORT 报告同样合契约。它是「这次没量准」, 与 REJECT 的「量到了, 不好」
/// 是两件事 —— 契约必须容得下它, 上层才分得开。
#[test]
fn an_abort_report_is_contract_valid() {
    let raw = r#"{
      "version": 2, "kind": "gray", "candidate_id": "gray_g1c1", "status": "ABORT",
      "metrics": {"operator_latency_ms": null, "fps_p95_ms": null,
                  "power_watt": null, "psnr_db": null},
      "verdict": {"is_pareto_improvement": false, "p_value": 1.0,
                  "reason": "改写一次都没命中 (hits=0), 本轮作废"},
      "source": "phonefarm",
      "conditions_observed": {"hits": 0, "apply_ok": false, "restored": true}
    }"#;
    assert_conforms(
        &serde_json::from_str::<Value>(raw).unwrap(),
        "eval_report.schema.json",
    );
}

/// 契约的 status 枚举必须覆盖上层会遇到的每一个取值。
#[test]
fn every_status_the_upper_layer_understands_is_in_the_contract() {
    let schema = load_schema("eval_report.schema.json");
    let allowed = schema["properties"]["status"]["enum"].as_array().unwrap();
    for s in ["PASS", "REJECT", "SLOW", "POOR_QUALITY", "CRASH", "REVERTED", "ABORT"] {
        assert!(
            allowed.contains(&Value::String(s.into())),
            "契约的 status 枚举里少了 {s}"
        );
    }
}
