//! 中途纠偏: 作业跑着的时候, 人可以留一句话(「别用搜索, 从分类进」「换成第二个地址」),
//! 下一次决策前读进来, 作为人工留言放进模型上下文, 并作废还没执行的旧计划。
//!
//! 文件通道与审批同构: 留言方(center)往 `<作业目录>/steer/<id>.json` 写
//! `{"schema":"phonefarm/steer@1","id","text","by","at"}`; phonefarm 读到后写 `<id>.ack.json`
//! (第几步读到的), 同一条不会读两次。留言只影响模型怎么想, 不绕过审批。

use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

pub const SCHEMA: &str = "phonefarm/steer@1";
/// 上下文里最多带最近几条留言
pub const KEEP: usize = 3;
const TEXT_MAX_CHARS: usize = 500;

#[derive(Debug, Clone, PartialEq)]
pub struct Msg {
    pub id: String,
    pub text: String,
    pub by: String,
}

/// 留言目录: PF_STEER_DIR > 作业目录/steer(PF_JOB_OUT 的上一级, 与 approvals 并列) > 局目录/steer。
pub fn steer_dir(run_dir: &str) -> PathBuf {
    if let Ok(d) = std::env::var("PF_STEER_DIR") {
        if !d.trim().is_empty() {
            return PathBuf::from(d);
        }
    }
    if let Ok(o) = std::env::var("PF_JOB_OUT") {
        if !o.trim().is_empty() {
            let out = Path::new(&o);
            return out.parent().unwrap_or(out).join("steer");
        }
    }
    Path::new(run_dir).join("steer")
}

fn valid_id(id: &str) -> bool {
    id.starts_with("st-") && id.len() <= 120 && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

/// 取出还没读过的留言(按 at 排序), 每条写回执。目录不存在 = 没有留言。
pub fn take_new(dir: &Path, step: u32) -> Vec<Msg> {
    let Ok(rd) = fs::read_dir(dir) else { return Vec::new() };
    let mut found: Vec<(f64, Msg)> = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().to_string();
        let Some(id) = name.strip_suffix(".json") else { continue };
        if id.ends_with(".ack") || !valid_id(id) || dir.join(format!("{id}.ack.json")).exists() {
            continue;
        }
        let Ok(t) = fs::read_to_string(e.path()) else { continue };
        let Ok(v) = serde_json::from_str::<Value>(&t) else { continue }; // 写了一半: 下一轮再读
        if v["schema"] != SCHEMA || v["id"].as_str() != Some(id) {
            continue;
        }
        let text: String = v["text"].as_str().unwrap_or("").trim().chars().take(TEXT_MAX_CHARS).collect();
        if text.is_empty() {
            continue;
        }
        let by = v["by"].as_str().unwrap_or("?").chars().take(40).collect();
        found.push((v["at"].as_f64().unwrap_or(0.0), Msg { id: id.to_string(), text, by }));
    }
    found.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    found.into_iter().map(|(_, m)| {
        let _ = fs::write(dir.join(format!("{}.ack.json", m.id)), json!({"step": step, "at": now}).to_string());
        m
    }).collect()
}

/// 放进模型上下文的一段(最近 KEEP 条, 新的在后)。
pub fn render(msgs: &[Msg]) -> String {
    if msgs.is_empty() {
        return String::new();
    }
    let lines: Vec<String> = msgs.iter().rev().take(KEEP).rev().map(|m| format!("  - {}", m.text)).collect();
    format!("人工留言(任务进行中有人给你的指示, 优先于你原来的打算; 和目标冲突时以留言为准, 但敏感操作照样要审批):\n{}", lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_messages_are_taken_once_in_order() {
        let dir = std::env::temp_dir().join(format!("pf-steer-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        assert!(take_new(&dir, 1).is_empty(), "没有目录 = 没有留言");
        fs::create_dir_all(&dir).unwrap();
        let put = |id: &str, text: &str, at: f64| {
            fs::write(dir.join(format!("{id}.json")), json!({"schema": SCHEMA, "id": id, "text": text, "by": "dash", "at": at}).to_string()).unwrap();
        };
        put("st-2-b", "换成第二个地址", 2.0);
        put("st-1-a", "别用搜索, 从分类进", 1.0);
        fs::write(dir.join("st-3-c.json"), "{\"schema\":").unwrap(); // 写了一半
        fs::write(dir.join("st-4-d.json"), json!({"schema": SCHEMA, "id": "st-other", "text": "x"}).to_string()).unwrap();
        let got = take_new(&dir, 5);
        assert_eq!(got.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(), ["别用搜索, 从分类进", "换成第二个地址"]);
        assert!(dir.join("st-1-a.ack.json").exists());
        assert!(take_new(&dir, 6).is_empty(), "读过的不再读");
        let r = render(&got);
        assert!(r.contains("人工留言") && r.ends_with("换成第二个地址"));
        let _ = fs::remove_dir_all(&dir);
    }
}
