//! 技能库: 把一局验证通过的 `run` 轨迹编译成命名技能, 之后零模型调用回放; 回放对不上就停下,
//! 交还给模型(`run`)从当前画面继续。
//!
//! 产品行为参考 Poki(AGPL-3.0)的 saved skills / learned routes: 按「点了什么」而不是按坐标记,
//! 每步先核对起点画面、做完核对到达画面, 不确定就停, 输入的文字变成参数, 连续两次失败标为过期。
//! 实现、文件格式与阈值都是本仓库自己的。
//!
//! # 文件
//! `<tasks_root>/_skills/<名字>.json`, schema `phonefarm/skill@1`:
//! ```json
//! {"name":"…","goal":"在设置里搜索{{text_1}}","app":"com.android.settings",
//!  "params":{"text_1":"蓝牙"},
//!  "steps":[{"a":"tap","what":"搜索","at":[512,88],"when":["设置","WLAN"],"expect":["搜索设置"]},
//!           {"a":"type","text":"{{text_1}}"}],
//!  "success":["蓝牙"], "source":{"task":"…","run":"…"},
//!  "stats":{"runs":0,"ok":0,"fail_streak":0,"stale":false}}
//! ```
//! `at` 是 0~999 归一化落点, 只在目标无文字(图标)且起点画面完全吻合时才用。
//!
//! # 判定口径
//! - 起点(when): 记录时起点画面上最多 5 个稳定短文字; 回放时至少过半在场才动手。
//! - 到达(expect): 下一画面新出现的最多 3 个短文字; 回放时 6 s 内任一出现即算到达。
//! - 目标: 文字完全相等优先, 其次包含; 多个候选时取离记录落点最近的, 且要比第二近的近一倍以上。
//! - 每步过审批门(approval.rs), 与 `run` 同一套规则。

use crate::approval;
use crate::device::{Device, FullNode, Node};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const NORM: i64 = 999;
const WHEN_MAX: usize = 5;
const EXPECT_MAX: usize = 3;
const LABEL_MAX_CHARS: usize = 12;
const EXPECT_WAIT_MS: u64 = 6000;
const STALE_AFTER: u32 = 2;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Step {
    pub a: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub what: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub at: Option<[i64; 2]>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub to: Option<[i64; 2]>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub when: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub expect: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Stats {
    pub runs: u32,
    pub ok: u32,
    pub fail_streak: u32,
    pub stale: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_stop: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Skill {
    #[serde(default = "schema_id")]
    pub schema: String,
    pub name: String,
    pub goal: String,
    #[serde(default)]
    pub app: String,
    #[serde(default)]
    pub params: std::collections::BTreeMap<String, String>,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub success: Vec<String>,
    #[serde(default)]
    pub source: Value,
    #[serde(default)]
    pub stats: Stats,
}

fn schema_id() -> String {
    "phonefarm/skill@1".into()
}

fn norm(s: &str) -> String {
    s.trim().to_lowercase().chars().filter(|c| !c.is_whitespace()).collect()
}

/// 适合当路标的文字: 短、不含数字(时间、计数、金额会变)。
fn stable_label(t: &str) -> bool {
    let t = t.trim();
    let n = t.chars().count();
    n >= 1 && n <= LABEL_MAX_CHARS && !t.chars().any(|c| c.is_ascii_digit())
}

pub fn skills_dir(tasks_root: &str) -> PathBuf {
    Path::new(tasks_root).join("_skills")
}

pub fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 64 && !name.starts_with('.')
        && name.chars().all(|c| c.is_alphanumeric() || c == '-' || c == '_')
}

pub fn load(dir: &Path, name: &str) -> Result<Skill, String> {
    if !valid_name(name) {
        return Err(format!("技能名不合法: {name}(只用字母数字-_)"));
    }
    let p = dir.join(format!("{name}.json"));
    let t = fs::read_to_string(&p).map_err(|e| format!("读不到 {}: {e}", p.display()))?;
    serde_json::from_str(&t).map_err(|e| format!("{} 解析失败: {e}", p.display()))
}

pub fn save(dir: &Path, s: &Skill) -> Result<PathBuf, String> {
    if !valid_name(&s.name) {
        return Err(format!("技能名不合法: {}", s.name));
    }
    fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let p = dir.join(format!("{}.json", s.name));
    let tmp = dir.join(format!(".{}.json.tmp", s.name));
    fs::write(&tmp, serde_json::to_vec_pretty(s).unwrap_or_default()).map_err(|e| e.to_string())?;
    fs::rename(&tmp, &p).map_err(|e| e.to_string())?;
    Ok(p)
}

pub fn list(dir: &Path) -> Vec<Skill> {
    let mut v: Vec<Skill> = fs::read_dir(dir).into_iter().flatten().flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .filter_map(|e| fs::read_to_string(e.path()).ok())
        .filter_map(|t| serde_json::from_str(&t).ok())
        .collect();
    v.sort_by(|a: &Skill, b: &Skill| a.name.cmp(&b.name));
    v
}

// ───────────────────────── 编译 ─────────────────────────

struct Screen {
    texts: Vec<(String, [i32; 4])>,
    w: i32,
    h: i32,
    pkg: String,
}

fn screen_of(rec: &Value, run_dir: &Path) -> Screen {
    let mut texts = Vec::new();
    let src = rec.get("nodes").and_then(|v| v.as_array()).filter(|a| !a.is_empty())
        .or_else(|| rec.get("els").and_then(|v| v.as_array()));
    let (mut mx, mut my) = (0, 0);
    for e in src.into_iter().flatten() {
        let b = e.get("b").and_then(|b| b.as_array()).map(|a| {
            let g = |i: usize| a.get(i).and_then(|x| x.as_i64()).unwrap_or(0) as i32;
            [g(0), g(1), g(2), g(3)]
        }).unwrap_or([0; 4]);
        mx = mx.max(b[2]);
        my = my.max(b[3]);
        if let Some(t) = e.get("t").and_then(|t| t.as_str()) {
            if !t.trim().is_empty() {
                texts.push((t.trim().to_string(), b));
            }
        }
    }
    // 归一化要用截图尺寸; 读不到图就退回元素外包框(偏小, 只影响图标落点精度)
    let (w, h) = rec.get("img").and_then(|v| v.as_str())
        .and_then(|rel| image::image_dimensions(run_dir.join(rel)).ok())
        .map(|(w, h)| (w as i32, h as i32))
        .unwrap_or((mx.max(1), my.max(1)));
    Screen { texts, w, h, pkg: rec.get("pkg").and_then(|v| v.as_str()).unwrap_or("").to_string() }
}

fn pick_when(s: &Screen) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for (t, _) in &s.texts {
        if out.len() >= WHEN_MAX { break; }
        if stable_label(t) && !out.iter().any(|o| norm(o) == norm(t))
            && s.texts.iter().filter(|(u, _)| norm(u) == norm(t)).count() == 1 {
            out.push(t.clone());
        }
    }
    out
}

fn pick_expect(before: &Screen, after: &Screen) -> Vec<String> {
    let old: std::collections::HashSet<String> = before.texts.iter().map(|(t, _)| norm(t)).collect();
    let mut out: Vec<String> = Vec::new();
    for (t, _) in &after.texts {
        if out.len() >= EXPECT_MAX { break; }
        if stable_label(t) && !old.contains(&norm(t)) && !out.iter().any(|o| norm(o) == norm(t)) {
            out.push(t.clone());
        }
    }
    out
}

fn smallest_text_at(s: &Screen, x: i32, y: i32) -> Option<String> {
    s.texts.iter()
        .filter(|(_, b)| b[0] <= x && x <= b[2] && b[1] <= y && y <= b[3])
        .min_by_key(|(_, b)| ((b[2] - b[0]) as i64) * ((b[3] - b[1]) as i64))
        .map(|(t, _)| t.clone())
}

const SKIP_ACTS: &[&str] = &["done", "handoff", "inspect", "find", "get_state", "history", "goto", "wait"];

/// 从一局的 log.jsonl 编译技能。`require_achieved`: 只收复核通过的局。
pub fn compile(run_dir: &Path, name: &str, require_achieved: bool) -> Result<Skill, String> {
    let text = fs::read_to_string(run_dir.join("log.jsonl")).map_err(|e| format!("读不到 log.jsonl: {e}"))?;
    let recs: Vec<Value> = text.lines().filter_map(|l| serde_json::from_str(l).ok()).collect();
    let goal = recs.iter().find(|r| r["r"] == "goal").and_then(|r| r["t"].as_str()).unwrap_or("").to_string();
    let end = recs.iter().rev().find(|r| r["r"] == "end");
    let achieved = end.and_then(|e| e["achieved"].as_bool()).unwrap_or(false);
    if require_achieved && !achieved {
        return Err("该局未通过复核(achieved≠true), 不编译成技能; 确认要收就加 --force".into());
    }
    let mut steps: Vec<Step> = Vec::new();
    let mut params: std::collections::BTreeMap<String, String> = std::collections::BTreeMap::new();
    let mut app = String::new();
    let mut cur: Option<Screen> = None;
    // 待定步骤: 动作已记, 等它的 diff 决定去留, 等下一屏补 expect
    let mut pending: Option<(Step, bool)> = None; // (step, diff 已确认有效)
    for r in &recs {
        match r["r"].as_str().unwrap_or("") {
            "screen" => {
                let s = screen_of(r, run_dir);
                if app.is_empty() && !s.pkg.is_empty() { app = s.pkg.clone(); }
                if let Some((mut st, true)) = pending.take() {
                    if let Some(prev) = &cur {
                        if matches!(st.a.as_str(), "tap" | "long_press" | "launch" | "back" | "swipe" | "scroll_up" | "scroll_down") {
                            st.expect = pick_expect(prev, &s);
                        }
                    }
                    steps.push(st);
                }
                cur = Some(s);
            }
            "act" => {
                // 上一个动作没等到下一屏(连续计划步之间不重采)也照收, 只是没有 expect
                if let Some((st, true)) = pending.take() { steps.push(st); }
                let a = r["a"].as_str().unwrap_or("").to_string();
                if SKIP_ACTS.contains(&a.as_str()) || a.starts_with("tool:") { continue; }
                if r["by"].as_str() == Some("(盲移)") { continue; }
                let mut st = Step { a: a.clone(), ..Default::default() };
                let s = cur.as_ref();
                let nrm = |v: &Value, span: i32| v.as_i64().map(|x| (x * NORM / span.max(1) as i64).clamp(0, NORM));
                match a.as_str() {
                    "tap" | "long_press" => {
                        let (w, h) = s.map(|s| (s.w, s.h)).unwrap_or((1, 1));
                        if let (Some(x), Some(y)) = (nrm(&r["x"], w), nrm(&r["y"], h)) { st.at = Some([x, y]); }
                        st.what = r["what"].as_str().map(|w| w.trim().trim_start_matches("icon:").trim().to_string())
                            .filter(|w| !w.is_empty() && !r["what"].as_str().unwrap_or("").starts_with("icon:"))
                            .or_else(|| s.and_then(|s| smallest_text_at(s, r["x"].as_i64()? as i32, r["y"].as_i64()? as i32)));
                        st.when = s.map(pick_when).unwrap_or_default();
                    }
                    "swipe" => {
                        let (w, h) = s.map(|s| (s.w, s.h)).unwrap_or((1, 1));
                        if let (Some(x), Some(y), Some(x2), Some(y2)) =
                            (nrm(&r["x"], w), nrm(&r["y"], h), nrm(&r["x2"], w), nrm(&r["y2"], h)) {
                            st.at = Some([x, y]);
                            st.to = Some([x2, y2]);
                        }
                    }
                    "type" => {
                        let lit = r["text"].as_str().unwrap_or("").to_string();
                        let key = params.iter().find(|(_, v)| **v == lit).map(|(k, _)| k.clone())
                            .unwrap_or_else(|| format!("text_{}", params.len() + 1));
                        params.insert(key.clone(), lit);
                        st.text = Some(format!("{{{{{key}}}}}"));
                    }
                    "launch" => {
                        st.text = r["text"].as_str().map(str::to_string);
                        if let Some(t) = &st.text { if app.is_empty() { app = t.clone(); } }
                        st.when = Vec::new();
                    }
                    _ => {}
                }
                // 除 launch 外每步都核对起点画面: 输入、返回、滑动做在错的页面上同样有害
                if st.when.is_empty() && st.a != "launch" {
                    st.when = s.map(pick_when).unwrap_or_default();
                }
                pending = Some((st, false));
            }
            "diff" => {
                if let Some((st, ok)) = pending.as_mut() {
                    let d = r["d"].as_str().unwrap_or("");
                    let dead = d.starts_with("rejected") || d.starts_with("escape") || d.starts_with("树✗");
                    // 画面没动的点击不收(没起作用); type/clear/back 等无画面变化也可能是有效的, 照收
                    let no_change = d == "none" && matches!(st.a.as_str(), "tap" | "long_press" | "swipe" | "scroll_up" | "scroll_down");
                    if dead || no_change { pending = None; } else { *ok = true; }
                }
            }
            _ => {}
        }
    }
    if let Some((st, true)) = pending.take() { steps.push(st); }
    if steps.is_empty() {
        return Err("这一局没有可回放的有效动作".into());
    }
    // 目标里引号括起来的文字若是某步的点名目标(如「长按 X 那条」), 也变成参数, 换个 X 就能复用
    for q in quoted(&goal) {
        if params.values().any(|v| *v == q) { continue; }
        let hit = steps.iter().any(|st| st.what.as_deref() == Some(q.as_str()));
        if !hit { continue; }
        let key = format!("text_{}", params.len() + 1);
        let ph = format!("{{{{{key}}}}}");
        for st in steps.iter_mut() {
            if st.what.as_deref() == Some(q.as_str()) { st.what = Some(ph.clone()); }
            for w in st.when.iter_mut().chain(st.expect.iter_mut()) { if *w == q { *w = ph.clone(); } }
        }
        params.insert(key, q);
    }
    // 目标模板: 输入过的文字在目标里出现就换成占位符
    let mut tmpl = goal.clone();
    for (k, v) in &params {
        if !v.is_empty() && tmpl.contains(v.as_str()) {
            tmpl = tmpl.replacen(v.as_str(), &format!("{{{{{k}}}}}"), 1);
        }
    }
    // 终态词里出现的输入文字同样换成占位符, 否则换参数回放时终态永远对不上
    let success: Vec<String> = recs.iter().rev().find(|r| r["r"] == "hook" && r["kind"] == "asserts")
        .and_then(|r| r["words"].as_array()).map(|a| a.iter().filter_map(|x| x.as_str().map(|w| {
            let mut w = w.to_string();
            for (k, v) in &params {
                if !v.is_empty() && w.contains(v.as_str()) { w = w.replace(v.as_str(), &format!("{{{{{k}}}}}")); }
            }
            w
        })).collect())
        .unwrap_or_default();
    Ok(Skill {
        schema: schema_id(),
        name: name.to_string(),
        goal: tmpl,
        app,
        params,
        steps,
        success,
        source: json!({"run": run_dir.file_name().and_then(|s| s.to_str()), "achieved": achieved}),
        stats: Stats::default(),
    })
}

/// 目标句里成对引号(「」“”"")括起来的片段。
fn quoted(goal: &str) -> Vec<String> {
    let mut out = Vec::new();
    for (open, close) in [('「', '」'), ('“', '”'), ('"', '"')] {
        let mut rest = goal;
        while let Some(i) = rest.find(open) {
            let after = &rest[i + open.len_utf8()..];
            let Some(j) = after.find(close) else { break };
            let q = after[..j].trim();
            if !q.is_empty() && q.chars().count() <= 40 { out.push(q.to_string()); }
            rest = &after[j + close.len_utf8()..];
        }
    }
    out
}

/// 用目标模板匹配一句新目标, 取出参数。模板里没有占位符时要求全文相同(去空白)。
pub fn match_goal(tmpl: &str, goal: &str) -> Option<std::collections::BTreeMap<String, String>> {
    let mut parts: Vec<(bool, String)> = Vec::new(); // (是占位符, 文本)
    let mut rest = tmpl;
    while let Some(i) = rest.find("{{") {
        let j = rest[i..].find("}}")? + i;
        parts.push((false, rest[..i].to_string()));
        parts.push((true, rest[i + 2..j].to_string()));
        rest = &rest[j + 2..];
    }
    parts.push((false, rest.to_string()));
    let g: String = goal.trim().to_string();
    let mut out = std::collections::BTreeMap::new();
    let mut pos = 0usize;
    let mut i = 0;
    while i < parts.len() {
        let (is_ph, t) = &parts[i];
        if !is_ph {
            let lit = t.trim();
            if lit.is_empty() { i += 1; continue; }
            let at = g[pos..].find(lit)?;
            if i == 0 && at != 0 { return None; }
            pos += at + lit.len();
            i += 1;
        } else {
            // 占位符取到下一个字面段为止
            let next = parts.get(i + 1).map(|(_, t)| t.trim().to_string()).unwrap_or_default();
            let end = if next.is_empty() { g.len() } else { pos + g[pos..].find(&next)? };
            let val = g[pos..end].trim().trim_matches(|c| c == '"' || c == '“' || c == '”' || c == '「' || c == '」');
            if val.is_empty() { return None; }
            out.insert(t.clone(), val.to_string());
            pos = end;
            i += 1;
        }
    }
    if g[pos..].trim().is_empty() { Some(out) } else { None }
}

// ───────────────────────── 回放 ─────────────────────────

#[derive(Debug, PartialEq)]
pub enum Stop {
    Done,
    Precondition { step: usize, missing: Vec<String> },
    Target { step: usize, what: String, why: String },
    NotArrived { step: usize, expect: Vec<String> },
    Denied { step: usize, reason: String },
    Success { missing: Vec<String> },
    Capture { step: usize },
    /// 动作没发到手机(not_sent)或发出后不确定是否生效(unknown)。不是技能的错, 也绝不重放这一步。
    Receipt { step: usize, state: &'static str, why: String, sensitive: bool },
}

impl Stop {
    pub fn code(&self) -> &'static str {
        match self {
            Stop::Done => "done",
            Stop::Precondition { .. } => "step_precondition_failed",
            Stop::Target { .. } => "target_not_found",
            Stop::NotArrived { .. } => "step_not_completed",
            Stop::Denied { .. } => "approval_denied",
            Stop::Success { .. } => "final_condition_failed",
            Stop::Capture { .. } => "capture_failed",
            Stop::Receipt { state: "not_sent", .. } => "action_not_sent",
            Stop::Receipt { .. } => "action_unconfirmed",
        }
    }
    pub fn describe(&self) -> String {
        match self {
            Stop::Done => "全部步骤完成".into(),
            Stop::Precondition { step, missing } => format!("第{}步起点画面不符, 缺 {:?}", step + 1, missing),
            Stop::Target { step, what, why } => format!("第{}步找不到目标「{what}」: {why}", step + 1),
            Stop::NotArrived { step, expect } => format!("第{}步做完没到预期画面 {:?}", step + 1, expect),
            Stop::Denied { step, reason } => format!("第{}步审批未通过: {reason}", step + 1),
            Stop::Success { missing } => format!("终态缺 {:?}", missing),
            Stop::Capture { step } => format!("第{}步读不到画面", step + 1),
            Stop::Receipt { step, state, why, sensitive } => match *state {
                "not_sent" => format!("第{}步动作没发到手机({why}), 屏幕没变", step + 1),
                _ => format!("第{}步动作发出后没收到确认({why}), 可能已经生效也可能没有; 以当前屏幕为准{}", step + 1,
                             if *sensitive { ", 这是敏感操作, 先确认是否已完成, 绝不能直接再做一次" } else { "" }),
            },
        }
    }
}

/// type 落在哪个输入框: 上次点击落点所在的框; 认不出时, 只有一个框就是它, 多个框里有密码类的就按密码框算。
/// 返回 (框里的文字, 框的 id/类名标记)。
fn type_targets(full: &[FullNode], last_tap: Option<(i32, i32)>) -> (Vec<String>, Vec<String>) {
    let edits: Vec<&FullNode> = full.iter().filter(|f| {
        let c = f.class.to_lowercase();
        c.contains("edittext") || c.contains("textfield") || c.contains("securetext") || c.contains("textinput")
    }).collect();
    let mut chosen: Vec<&FullNode> = edits.iter().copied()
        .filter(|f| last_tap.is_some_and(|(x, y)| f.b[0] <= x && x <= f.b[2] && f.b[1] <= y && y <= f.b[3])).collect();
    if chosen.is_empty() {
        chosen = if edits.len() == 1 { edits } else {
            edits.into_iter().filter(|f| crate::runtime::password_like(&f.class, f.id.as_deref(), &f.t)).collect()
        };
    }
    let texts = chosen.iter().filter(|f| !f.t.is_empty()).map(|f| f.t.clone()).collect();
    let mut ids: Vec<String> = chosen.iter().filter_map(|f| f.id.clone()).collect();
    if chosen.iter().any(|f| f.class.to_lowercase().contains("secure")) { ids.push("secure-password".into()); }
    (texts, ids)
}

pub fn fill(t: &str, params: &std::collections::BTreeMap<String, String>) -> String {
    let mut s = t.to_string();
    for (k, v) in params {
        s = s.replace(&format!("{{{{{k}}}}}"), v);
    }
    s
}

/// 在当前元素里找目标。返回设备像素中心。
pub fn locate(els: &[Node], what: &str, hint: Option<(i32, i32)>) -> Result<(i32, i32), String> {
    let w = norm(what);
    if w.is_empty() { return Err("目标文字为空".into()); }
    let center = |b: [i32; 4]| ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2);
    let exact: Vec<[i32; 4]> = els.iter().filter(|e| norm(&e.t) == w).map(|e| e.b).collect();
    let cands = if !exact.is_empty() { exact } else {
        els.iter().filter(|e| { let t = norm(&e.t); t.contains(&w) && t.chars().count() <= w.chars().count() + 6 })
            .map(|e| e.b).collect()
    };
    match cands.len() {
        0 => Err("当前画面没有这个文字".into()),
        1 => Ok(center(cands[0])),
        _ => {
            let Some((hx, hy)) = hint else { return Err(format!("有{}个同名候选且没有记录落点", cands.len())); };
            let mut d: Vec<(i64, [i32; 4])> = cands.iter().map(|b| {
                let (cx, cy) = center(*b);
                (((cx - hx) as i64).pow(2) + ((cy - hy) as i64).pow(2), *b)
            }).collect();
            d.sort_by_key(|x| x.0);
            // 最近的要比第二近的近一倍(距离平方 4 倍)
            if d[0].0 * 4 <= d[1].0 { Ok(center(d[0].1)) } else { Err(format!("有{}个同名候选, 落点分不开", cands.len())) }
        }
    }
}

fn present(els: &[Node], words: &[String]) -> Vec<String> {
    words.iter().filter(|w| els.iter().any(|e| norm(&e.t).contains(&norm(w)))).cloned().collect()
}

pub struct RunCtx<'a> {
    pub device: &'a Device,
    pub policy: &'a approval::Policy,
    pub params: std::collections::BTreeMap<String, String>,
    pub settle_ms: u64,
    pub els_timeout_ms: u64,
    pub out_dir: PathBuf,
    pub serial: String,
}

fn read(ctx: &RunCtx) -> Option<(Vec<Node>, Vec<FullNode>)> {
    for _ in 0..3 {
        let (els, full, _, _) = ctx.device.els_full(ctx.els_timeout_ms);
        if !els.is_empty() { return Some((els, full)); }
        std::thread::sleep(Duration::from_millis(400));
    }
    None
}

/// 回放。每步写一行 JSON 到 `out_dir/skill-run.jsonl`。
pub fn replay(skill: &Skill, ctx: &RunCtx) -> Stop {
    let _ = fs::create_dir_all(&ctx.out_dir);
    let logp = ctx.out_dir.join("skill-run.jsonl");
    let mut logf = fs::OpenOptions::new().create(true).append(true).open(&logp).ok();
    let mut put = |v: Value| {
        if let Some(f) = logf.as_mut() { let _ = std::io::Write::write_all(f, format!("{v}\n").as_bytes()); }
    };
    put(json!({"r":"skill_start","name":skill.name,"params":ctx.params,"serial":ctx.serial}));
    let (sw, sh) = ctx.device.size();
    let px = |p: [i64; 2]| ((p[0] * sw as i64 / NORM) as i32, (p[1] * sh as i64 / NORM) as i32);
    let mut grants = approval::Grants::default();
    if !skill.app.is_empty() && ctx.device.foreground_pkg() != skill.app {
        let _ = ctx.device.launch(&skill.app, "");
        std::thread::sleep(Duration::from_millis(ctx.settle_ms.max(1500)));
    }
    let mut last_tap: Option<(i32, i32)> = None;
    for (i, st) in skill.steps.iter().enumerate() {
        let Some((els, full)) = read(ctx) else { put(json!({"r":"stop","code":"capture_failed","step":i})); return Stop::Capture { step: i }; };
        let st = &Step {
            what: st.what.as_deref().map(|w| fill(w, &ctx.params)),
            when: st.when.iter().map(|w| fill(w, &ctx.params)).collect(),
            expect: st.expect.iter().map(|w| fill(w, &ctx.params)).collect(),
            ..st.clone()
        };
        if !st.when.is_empty() {
            let have = present(&els, &st.when);
            if have.len() * 2 < st.when.len() {
                let missing: Vec<String> = st.when.iter().filter(|w| !have.contains(w)).cloned().collect();
                put(json!({"r":"stop","code":"step_precondition_failed","step":i,"missing":missing}));
                return Stop::Precondition { step: i, missing };
            }
        }
        // 解析落点
        let mut tap_at: Option<(i32, i32)> = None;
        if st.a == "tap" || st.a == "long_press" {
            let hint = st.at.map(px);
            match st.what.as_deref() {
                Some(w) => match locate(&els, w, hint) {
                    Ok(p) => tap_at = Some(p),
                    Err(why) => {
                        put(json!({"r":"stop","code":"target_not_found","step":i,"what":w,"why":why}));
                        return Stop::Target { step: i, what: w.to_string(), why };
                    }
                },
                None => {
                    // 图标: 起点画面全部吻合才敢按坐标点
                    let all = !st.when.is_empty() && present(&els, &st.when).len() == st.when.len();
                    match (hint, all) {
                        (Some(p), true) => tap_at = Some(p),
                        _ => {
                            let why = "无文字目标且起点画面不完全吻合, 不按坐标盲点".to_string();
                            put(json!({"r":"stop","code":"target_not_found","step":i,"what":"(图标)","why":why}));
                            return Stop::Target { step: i, what: "(图标)".into(), why };
                        }
                    }
                }
            }
        }
        // 审批
        let text_filled = st.text.as_deref().map(|t| fill(t, &ctx.params));
        let pkg = ctx.device.foreground_pkg();
        let target_texts: Vec<String> = match (st.a.as_str(), tap_at) {
            ("tap" | "long_press", Some((x, y))) => {
                let mut v: Vec<&Node> = els.iter().filter(|e| e.b[0] <= x && x <= e.b[2] && e.b[1] <= y && y <= e.b[3]).collect();
                v.sort_by_key(|e| ((e.b[2] - e.b[0]) as i64) * ((e.b[3] - e.b[1]) as i64));
                v.iter().take(2).map(|e| e.t.clone()).collect()
            }
            ("type", _) => type_targets(&full, last_tap).0,
            ("swipe", _) => match st.at.map(px) {
                Some((x, y)) => {
                    let mut v: Vec<&Node> = els.iter().filter(|e| e.b[0] <= x && x <= e.b[2] && e.b[1] <= y && y <= e.b[3]).collect();
                    v.sort_by_key(|e| ((e.b[2] - e.b[0]) as i64) * ((e.b[3] - e.b[1]) as i64));
                    v.iter().take(2).map(|e| e.t.clone()).collect()
                }
                None => Vec::new(),
            },
            _ => Vec::new(),
        };
        let ids: Vec<String> = if st.a == "type" { type_targets(&full, last_tap).1 } else { Vec::new() };
        let screen: Vec<&str> = els.iter().map(|e| e.t.as_str()).collect();
        let facts = approval::Facts {
            action: &st.a, what: st.what.as_deref(), text: text_filled.as_deref(),
            target_texts: target_texts.iter().map(String::as_str).collect(), screen_texts: screen, pkg: &pkg,
            target_ids: ids.iter().map(String::as_str).collect(),
        };
        let mut asked_human = false;
        let mut sensitive = false;
        if let Some(hit) = approval::classify(&facts, ctx.policy) {
            sensitive = true;
            let base = target_texts.iter().find(|t| !t.trim().is_empty()).cloned().or_else(|| st.what.clone()).unwrap_or_default();
            let ctx_texts: Vec<&str> = els.iter().map(|e| e.t.as_str()).collect();
            let key = format!("{base}#{}", approval::context_sig(&ctx_texts));
            let allow = match ctx.policy.mode_for(hit.category) {
                approval::Mode::Auto => true,
                approval::Mode::Deny => false,
                approval::Mode::Ask if grants.covers(&pkg, hit.category, &key, ctx.policy.grant_s, hit.follow_up) => true,
                approval::Mode::Ask => {
                    let dir = approval::approval_dir(&ctx.out_dir.to_string_lossy());
                    let id = approval::new_id(&format!("skill-{}", skill.name), i as u32 + 1);
                    let _ = fs::create_dir_all(&dir);
                    let shot = dir.join(format!("{id}.jpg"));
                    let screenshot = ctx.device.screen(&shot.to_string_lossy()).map(|_| shot.to_string_lossy().to_string());
                    let req = approval::Request {
                        id, category: hit.category, reason: hit.reason.clone(),
                        action_line: format!("技能 {} 第{}步 {} {}", skill.name, i + 1, st.a, st.what.as_deref().or(text_filled.as_deref()).unwrap_or("")),
                        goal: fill(&skill.goal, &ctx.params), task: format!("skill:{}", skill.name), run_id: String::new(),
                        step: i as u32 + 1, serial: ctx.serial.clone(), pkg: pkg.clone(), screenshot,
                    };
                    let d = approval::request_and_wait(&dir, &req, ctx.policy.timeout_s);
                    if d.allow { grants.add(&pkg, hit.category, &key); asked_human = true; }
                    d.allow
                }
            };
            put(json!({"r":"approval","step":i,"category":hit.category.id(),"reason":hit.reason,"allow":allow}));
            if !allow {
                put(json!({"r":"stop","code":"approval_denied","step":i}));
                return Stop::Denied { step: i, reason: hit.reason };
            }
        }
        // 等过人: 画面可能已经变了, 重新读屏, 起点路标、前台应用、目标都要对得上, 否则停
        if asked_human {
            if ctx.device.foreground_pkg() != pkg {
                put(json!({"r":"stop","code":"step_precondition_failed","step":i,"why":"批准后前台应用变了"}));
                return Stop::Precondition { step: i, missing: vec![format!("前台应用 {pkg}")] };
            }
            let Some((els2, _)) = read(ctx) else { return Stop::Capture { step: i }; };
            let have = present(&els2, &st.when);
            // 按坐标点(没有点名)的步骤要求路标全在, 其余过半
            let need_all = st.what.is_none() && tap_at.is_some();
            if !st.when.is_empty() && (if need_all { have.len() < st.when.len() } else { have.len() * 2 < st.when.len() }) {
                let missing: Vec<String> = st.when.iter().filter(|w| !have.contains(w)).cloned().collect();
                put(json!({"r":"stop","code":"step_precondition_failed","step":i,"missing":missing,"why":"批准后画面变了"}));
                return Stop::Precondition { step: i, missing };
            }
            if let (Some(w), true) = (st.what.as_deref(), tap_at.is_some()) {
                match locate(&els2, w, st.at.map(px)) {
                    Ok(p) => tap_at = Some(p),
                    Err(why) => {
                        put(json!({"r":"stop","code":"target_not_found","step":i,"what":w,"why":why}));
                        return Stop::Target { step: i, what: w.to_string(), why: format!("批准后{why}") };
                    }
                }
            }
        }
        // 执行
        let _ = ctx.device.take_receipt();
        match st.a.as_str() {
            "tap" => { let (x, y) = tap_at.unwrap(); ctx.device.tap(x, y); last_tap = Some((x, y)); }
            "long_press" => { let (x, y) = tap_at.unwrap(); ctx.device.long_press(x, y, 700); }
            "swipe" => if let (Some(a), Some(b)) = (st.at, st.to) { let (x, y) = px(a); let (x2, y2) = px(b); ctx.device.swipe(x, y, x2, y2); },
            "scroll_up" => ctx.device.scroll_up(),
            "scroll_down" => ctx.device.scroll_down(),
            "back" => { ctx.device.back(); last_tap = None; }
            "home" => { ctx.device.home(); last_tap = None; }
            "clear" => ctx.device.clear_field(),
            "type" => ctx.device.type_text(text_filled.as_deref().unwrap_or("")),
            "launch" => { let _ = ctx.device.launch(st.text.as_deref().unwrap_or(""), ""); }
            _ => {}
        }
        let receipt = ctx.device.take_receipt();
        put(json!({"r":"act","step":i,"a":st.a,"what":st.what,"at":tap_at,"text":text_filled,"receipt":receipt.id()}));
        if receipt != crate::platform::Receipt::Sent {
            // 回放不重试: 没发出就停在这里交还; 不确定更不能再按一次
            if matches!(receipt, crate::platform::Receipt::Unknown(_)) && sensitive { grants.clear(); }
            put(json!({"r":"stop","code":"receipt","step":i,"state":receipt.id(),"why":receipt.why()}));
            return Stop::Receipt { step: i, state: receipt.id(), why: receipt.why().to_string(), sensitive };
        }
        std::thread::sleep(Duration::from_millis(ctx.settle_ms));
        if !st.expect.is_empty() {
            let t0 = Instant::now();
            let mut ok = false;
            while t0.elapsed() < Duration::from_millis(EXPECT_WAIT_MS) {
                if let Some((els2, _)) = read(ctx) {
                    if !present(&els2, &st.expect).is_empty() { ok = true; break; }
                }
                std::thread::sleep(Duration::from_millis(500));
            }
            if !ok {
                put(json!({"r":"stop","code":"step_not_completed","step":i,"expect":st.expect}));
                return Stop::NotArrived { step: i, expect: st.expect.clone() };
            }
        }
    }
    if !skill.success.is_empty() {
        let words: Vec<String> = skill.success.iter().map(|w| fill(w, &ctx.params)).collect();
        let els = read(ctx).map(|x| x.0).unwrap_or_default();
        let have = present(&els, &words);
        if have.len() < words.len() {
            let missing: Vec<String> = words.iter().filter(|w| !have.contains(w)).cloned().collect();
            put(json!({"r":"stop","code":"final_condition_failed","missing":missing}));
            return Stop::Success { missing };
        }
    }
    put(json!({"r":"stop","code":"done"}));
    Stop::Done
}

/// 记账: 成功清零连败; 连败达到阈值标过期(列表里标出, `run --skill auto` 不再选它)。
pub fn record(skill: &mut Skill, stop: &Stop) {
    skill.stats.runs += 1;
    if *stop == Stop::Done {
        skill.stats.ok += 1;
        skill.stats.fail_streak = 0;
        skill.stats.stale = false;
        skill.stats.last_stop = None;
    } else if !matches!(stop, Stop::Denied { .. } | Stop::Receipt { .. }) {
        // 审批被拒、传输出错都不是技能的错
        skill.stats.fail_streak += 1;
        skill.stats.last_stop = Some(stop.describe());
        if skill.stats.fail_streak >= STALE_AFTER { skill.stats.stale = true; }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nd(t: &str, b: [i32; 4]) -> Node { Node { t: t.into(), b } }

    #[test]
    fn locate_exact_then_contains_then_disambiguate() {
        let els = vec![nd("搜索", [0, 0, 100, 50]), nd("搜索设置项", [0, 100, 300, 150]), nd("WLAN", [0, 200, 100, 250])];
        assert_eq!(locate(&els, "搜索", None).unwrap(), (50, 25));
        assert_eq!(locate(&els, "wlan", None).unwrap(), (50, 225));
        assert!(locate(&els, "蓝牙", None).is_err());
        let two = vec![nd("确定", [0, 0, 100, 100]), nd("确定", [0, 1000, 100, 1100])];
        assert!(locate(&two, "确定", None).is_err());
        assert_eq!(locate(&two, "确定", Some((50, 990))).unwrap(), (50, 1050));
        let close = vec![nd("确定", [0, 0, 100, 100]), nd("确定", [0, 120, 100, 220])];
        assert!(locate(&close, "确定", Some((50, 110))).is_err(), "两个候选离落点差不多远时不猜");
    }

    #[test]
    fn goal_template_matching() {
        let m = match_goal("在设置里搜索{{text_1}}并打开", "在设置里搜索 蓝牙 并打开").unwrap();
        assert_eq!(m.get("text_1").unwrap(), "蓝牙");
        assert!(match_goal("在设置里搜索{{text_1}}并打开", "在相册里搜索猫").is_none());
        assert_eq!(match_goal("打开计算器", " 打开计算器 ").unwrap().len(), 0);
        assert!(match_goal("打开计算器", "打开计算器算1+1").is_none());
        let m2 = match_goal("给{{text_1}}发{{text_2}}", "给张三发「晚上好」").unwrap();
        assert_eq!((m2["text_1"].as_str(), m2["text_2"].as_str()), ("张三", "晚上好"));
    }

    #[test]
    fn quoted_segments() {
        assert_eq!(quoted("长按「pftest 买牛奶！」那条，再点“删除”"), vec!["pftest 买牛奶！".to_string(), "删除".to_string()]);
        assert!(quoted("没有引号").is_empty());
    }

    #[test]
    fn fill_params() {
        let mut p = std::collections::BTreeMap::new();
        p.insert("text_1".to_string(), "蓝牙".to_string());
        assert_eq!(fill("{{text_1}}", &p), "蓝牙");
    }

    #[test]
    fn stale_after_two_failures_but_not_on_denial() {
        let mut s = Skill { schema: schema_id(), name: "x".into(), goal: "g".into(), app: String::new(),
            params: Default::default(), steps: vec![], success: vec![], source: Value::Null, stats: Stats::default() };
        record(&mut s, &Stop::Denied { step: 0, reason: "r".into() });
        assert_eq!(s.stats.fail_streak, 0);
        record(&mut s, &Stop::NotArrived { step: 1, expect: vec![] });
        assert!(!s.stats.stale);
        record(&mut s, &Stop::Precondition { step: 0, missing: vec![] });
        assert!(s.stats.stale);
        record(&mut s, &Stop::Done);
        assert!(!s.stats.stale && s.stats.fail_streak == 0 && s.stats.ok == 1);
    }

    #[test]
    fn compile_from_log() {
        let dir = std::env::temp_dir().join(format!("pf-skill-compile-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let lines = [
            json!({"v":1}),
            json!({"r":"goal","t":"在设置里搜索蓝牙"}),
            json!({"r":"hook","kind":"asserts","words":["蓝牙"]}),
            json!({"r":"screen","n":1,"pkg":"com.android.settings","img":"step1.jpg","els":[{"t":"设置","b":[0,0,500,100]},{"t":"搜索","b":[0,100,1000,200]},{"t":"WLAN","b":[0,300,500,400]},{"t":"12:30","b":[900,0,1000,50]}]}),
            json!({"r":"act","n":1,"a":"tap","x":500,"y":150,"what":"搜索","by":"m"}),
            json!({"r":"diff","n":1,"d":"新页"}),
            json!({"r":"screen","n":2,"pkg":"com.android.settings","els":[{"t":"搜索设置","b":[0,100,1000,200]},{"t":"取消","b":[800,100,1000,200]}]}),
            json!({"r":"act","n":2,"a":"tap","x":10,"y":10,"by":"m"}),
            json!({"r":"diff","n":2,"d":"none"}),
            json!({"r":"act","n":3,"a":"type","text":"蓝牙","by":"m"}),
            json!({"r":"diff","n":3,"d":"文字变化"}),
            json!({"r":"screen","n":4,"pkg":"com.android.settings","els":[{"t":"蓝牙","b":[0,300,500,400]}]}),
            json!({"r":"act","n":4,"a":"done","by":"m"}),
            json!({"r":"end","achieved":true}),
        ];
        image::RgbImage::new(1000, 1000).save(dir.join("step1.jpg")).unwrap();
        fs::write(dir.join("log.jsonl"), lines.iter().map(|l| l.to_string()).collect::<Vec<_>>().join("\n")).unwrap();
        let s = compile(&dir, "bt-search", true).unwrap();
        assert_eq!(s.app, "com.android.settings");
        assert_eq!(s.goal, "在设置里搜索{{text_1}}");
        assert_eq!(s.params["text_1"], "蓝牙");
        assert_eq!(s.success, vec!["{{text_1}}".to_string()], "终态词参数化");
        assert_eq!(s.steps.len(), 2, "画面没动的点击不收: {:?}", s.steps);
        assert_eq!(s.steps[0].what.as_deref(), Some("搜索"));
        assert_eq!(s.steps[0].at, Some([499, 149]));
        assert!(s.steps[0].when.contains(&"WLAN".to_string()) && !s.steps[0].when.contains(&"12:30".to_string()));
        assert_eq!(s.steps[0].expect, vec!["搜索设置".to_string(), "取消".to_string()]);
        assert_eq!(s.steps[1].text.as_deref(), Some("{{text_1}}"));
        assert!(s.steps[1].when.contains(&"搜索设置".to_string()), "输入步也带起点路标: {:?}", s.steps[1].when);
        // 未通过复核的局默认不收
        let t = fs::read_to_string(dir.join("log.jsonl")).unwrap().replace("\"achieved\":true", "\"achieved\":false");
        fs::write(dir.join("log.jsonl"), t).unwrap();
        assert!(compile(&dir, "x", true).is_err());
        assert!(compile(&dir, "x", false).is_ok());
        let _ = fs::remove_dir_all(&dir);
    }
}

// ───────────────────────── CLI ─────────────────────────

pub const USAGE: &str = "技能:  skill list
       skill show <名字>
       skill save --task <T> --run <局ID> --name <名字> [--force]     (把复核通过的一局编成技能)
       skill match \"<目标>\"                                          (按目标模板找技能并取参数)
       skill run <名字> [--serial S] [--set k=v ...] [--fallback --task <T>]  (零模型回放; 断了可交还模型续跑)";

fn load_cfg() -> Result<crate::Config, String> {
    let t = fs::read_to_string("phonefarm.toml").map_err(|e| format!("读不到 phonefarm.toml: {e}"))?;
    toml::from_str(&t).map_err(|e| format!("phonefarm.toml 解析失败: {e}"))
}

pub fn find_run_dir(task_dir: &str, run_prefix: &str) -> Option<PathBuf> {
    let runs = Path::new(task_dir).join("runs");
    let mut hits: Vec<PathBuf> = fs::read_dir(&runs).ok()?.flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with(run_prefix)).map(|e| e.path()).collect();
    hits.sort();
    if hits.len() == 1 { hits.pop() } else { None }
}

/// 找最合适的技能: 目标模板匹配且未过期; 多个时取成功次数多的。
pub fn best_match(dir: &Path, goal: &str) -> Option<(Skill, std::collections::BTreeMap<String, String>)> {
    list(dir).into_iter().filter(|s| !s.stats.stale)
        .filter_map(|s| match_goal(&s.goal, goal).map(|m| (s, m)))
        .max_by_key(|(s, _)| (s.stats.ok, s.steps.len() as u32))
}

pub fn cli(args: &[String]) -> i32 {
    let cfg = match load_cfg() { Ok(c) => c, Err(e) => { eprintln!("{e}"); return 2; } };
    let root = crate::tasks_root(&cfg.data_dir);
    let dir = skills_dir(&root);
    let opt = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    let flag = |k: &str| args.iter().any(|a| a == k);
    match args.first().map(String::as_str) {
        Some("list") => {
            for s in list(&dir) {
                println!("{}\t{}步\t成功{}/{}\t{}{}", s.name, s.steps.len(), s.stats.ok, s.stats.runs, s.goal,
                         if s.stats.stale { "\t[过期]" } else { "" });
            }
            0
        }
        Some("show") => match args.get(1).map(|n| load(&dir, n)) {
            Some(Ok(s)) => { println!("{}", serde_json::to_string_pretty(&s).unwrap_or_default()); 0 }
            Some(Err(e)) => { eprintln!("{e}"); 1 }
            None => { eprintln!("{USAGE}"); 2 }
        },
        Some("save") => {
            let (Some(task), Some(run), Some(name)) = (opt("--task"), opt("--run"), opt("--name")) else { eprintln!("{USAGE}"); return 2; };
            let task_dir = format!("{root}/{task}");
            let Some(rd) = find_run_dir(&task_dir, &run) else { eprintln!("任务 {task} 下找不到唯一的局 {run}"); return 1; };
            match compile(&rd, &name, !flag("--force")).and_then(|mut s| {
                s.source["task"] = json!(task);
                save(&dir, &s).map(|p| (s, p))
            }) {
                Ok((s, p)) => { println!("技能 {} 已保存: {}步, 参数 {:?}, 目标模板「{}」 → {}", s.name, s.steps.len(), s.params, s.goal, p.display()); 0 }
                Err(e) => { eprintln!("✗ {e}"); 1 }
            }
        }
        Some("match") => {
            let goal = args.get(1).cloned().unwrap_or_default();
            match best_match(&dir, &goal) {
                Some((s, m)) => { println!("{}\t{:?}", s.name, m); 0 }
                None => { println!("(没有匹配的技能)"); 1 }
            }
        }
        Some("run") => {
            let Some(name) = args.get(1).filter(|a| !a.starts_with("--")).cloned() else { eprintln!("{USAGE}"); return 2; };
            let mut skill = match load(&dir, &name) { Ok(s) => s, Err(e) => { eprintln!("{e}"); return 1; } };
            let mut params = skill.params.clone();
            let mut it = args.iter();
            while let Some(a) = it.next() {
                if a == "--set" {
                    if let Some((k, v)) = it.next().and_then(|kv| kv.split_once('=')) { params.insert(k.to_string(), v.to_string()); }
                }
            }
            let serial = opt("--serial");
            let fallback_task = opt("--task");
            let code = run_with_fallback(&cfg, &dir, &mut skill, params, serial, if flag("--fallback") { fallback_task } else { None }, Vec::new());
            code
        }
        _ => { eprintln!("{USAGE}"); 2 }
    }
}

/// 回放技能; 断了且给了 fallback 任务名时, 交还模型从当前画面续跑(目标原样, 附上断点说明)。
pub fn run_with_fallback(cfg: &crate::Config, dir: &Path, skill: &mut Skill,
                         params: std::collections::BTreeMap<String, String>, serial: Option<String>,
                         fallback_task: Option<String>, asserts: Vec<String>) -> i32 {
    let tmp = std::env::temp_dir().join(format!("phonefarm-skill-{}", std::process::id())).to_string_lossy().to_string();
    let _ = fs::create_dir_all(&tmp);
    let device = Device::new(serial.clone(), tmp);
    let policy = approval::Policy::from_toml(cfg.approval.as_ref());
    let stamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let out_dir = std::env::var("PF_JOB_OUT").ok().filter(|s| !s.is_empty()).map(PathBuf::from)
        .unwrap_or_else(|| dir.join("runs").join(format!("{}-{stamp}", skill.name)));
    let ctx = RunCtx {
        device: &device, policy: &policy, params: params.clone(), settle_ms: cfg.settle_ms,
        els_timeout_ms: cfg.els_timeout_ms, out_dir: out_dir.clone(),
        serial: crate::device::resolve_serial(serial.as_deref()).unwrap_or_default(),
    };
    let t0 = Instant::now();
    let stop = replay(skill, &ctx);
    record(skill, &stop);
    let _ = save(dir, skill);
    println!("skill: name={} stop={} ({}) wall={:.1}s model_calls=0 log={}", skill.name, stop.code(), stop.describe(),
             t0.elapsed().as_secs_f64(), out_dir.join("skill-run.jsonl").display());
    if stop == Stop::Done { return 0; }
    if matches!(stop, Stop::Denied { .. }) { return 1; }
    let Some(task) = fallback_task else { return 1; };
    drop(device);
    let goal = fill(&skill.goal, &params);
    let goal2 = format!("{goal}\n(已用技能 {} 零模型走了一段, 在此中断: {}。不要从头重来, 先看当前画面再继续)", skill.name, stop.describe());
    println!("↪ 交还模型续跑: task={task}");
    let res = crate::runtime::episode(cfg, &task, &goal2, serial, None, false, 40, None, asserts, false, None);
    println!("summary: run={} stop={} steps={} calls={} achieved={}", res.run_id, res.stop, res.steps, res.calls, res.achieved);
    if res.achieved { 0 } else { 1 }
}
