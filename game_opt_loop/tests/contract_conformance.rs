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
        candidate_id: "gen2_llm1".into(),
        status: EvalStatus::Pass,
        metrics: Metrics {
            operator_latency_ms: 1.12,
            fps_p95_ms: Some(16.9),
            power_watt: 4.71,
            psnr_db: 38.62,
        },
        verdict: Verdict {
            is_pareto_improvement: true,
            p_value: 0.0031,
        },
        source,
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
