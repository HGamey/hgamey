//! 测试共用: 迷你 JSON Schema 校验器 (只覆盖契约里实际用到的关键字)。
#![allow(dead_code)]

use serde_json::Value;
use std::path::{Path, PathBuf};

pub fn contracts_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("contracts")
}

pub fn load_schema(name: &str) -> Value {
    let path = contracts_dir().join(name);
    let raw = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("读取契约 {} 失败: {e}", path.display()));
    serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("契约 {} 不是合法 JSON: {e}", path.display()))
}

/// 迷你 JSON Schema 校验器。返回违规说明列表, 空 = 通过。
pub fn validate(instance: &Value, schema: &Value, path: &str, out: &mut Vec<String>) {
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
        // additionalProperties: proposer 契约靠它把 harness 的字段挡在候选外面。
        if let Some(extra) = schema.get("additionalProperties") {
            let declared = schema.get("properties").and_then(Value::as_object);
            for (key, v) in obj {
                if declared.is_some_and(|p| p.contains_key(key)) {
                    continue;
                }
                match extra {
                    Value::Bool(false) => out.push(format!("{path}: 多了契约之外的字段 `{key}`")),
                    Value::Object(_) => validate(v, extra, &format!("{path}.{key}"), out),
                    _ => {}
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

pub fn assert_conforms(instance: &Value, schema_name: &str) {
    let schema = load_schema(schema_name);
    let mut problems = Vec::new();
    validate(instance, &schema, schema_name.trim_end_matches(".schema.json"), &mut problems);
    assert!(
        problems.is_empty(),
        "与 {schema_name} 不符:\n  {}",
        problems.join("\n  ")
    );
}

