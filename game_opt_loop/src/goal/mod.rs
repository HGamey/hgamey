//! 目标模式: 人和 harness 一起定一个目标 (goal.md), harness 一代一代地自己跑、自己判、自己停。
//!
//! 规格: `~/projects/_wt/goal-spec/GOAL_SPEC.md`; 契约: `contracts/goal*.schema.json`。
//!
//! ```text
//!   goal draft --intent "..." --game <包名> --out <run 目录>
//!       → intent.txt / goal.md / human.md / state.json (awaiting_confirm) / events.jsonl
//!   (人改 goal.md, 点「确认并开始」)
//!   goal run <run 目录>
//!       → 冻结 goal.md (记 sha256) → 拿锁 goal-<run_id>
//!       → 每代: 读 human.md → 覆盖 / 偏好 → optimize 跑一代 (各层 1 代) → 判 gain → 写 state/events
//!       → 停 (target_reached / budget_exhausted / no_gain / human_stop / error) → report.md
//! ```
//!
//! 分工照旧: 这里只决定**跑哪几层、跑几代、什么时候停**; 出方案、上机、判定都是 optimize
//! 与 phonefarm 的事, 一行没搬。

pub mod human;
pub mod spec;

use crate::optimize;
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const STATE_SCHEMA: &str = "game_opt_loop/goal_state@1";

/// 最好的那个候选 (state.json 的 best)。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Best {
    pub candidate: String,
    pub metric: String,
    pub delta_pct: f64,
    pub verdict: String,
}

/// state.json。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct State {
    pub schema: String,
    pub run_id: String,
    /// drafting | awaiting_confirm | running | paused | done | failed
    pub status: String,
    pub goal_summary: String,
    pub generation: u32,
    pub max_generations: u32,
    pub elapsed_min: f64,
    pub budget_min: f64,
    pub best: Option<Best>,
    pub progress_to_target: f64,
    pub device: Option<String>,
    pub human_md_rev: u32,
    pub updated_at: String,
    /// 冻结时 goal.md 的 sha256 (运行中 goal.md 被改了就拒绝继续)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goal_sha256: Option<String>,
    /// 停下的原因 (done/failed 时)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stop_reason: Option<String>,
    /// 连续没有收益的代数。
    #[serde(default)]
    pub no_gain_streak: u32,
}

/// 一次目标运行的目录。
pub struct RunDir {
    pub dir: PathBuf,
}

impl RunDir {
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }
    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }
    pub fn run_id(&self) -> String {
        self.dir
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| "goal".into())
    }

    /// 追加一条事件。
    pub fn event(&self, gen: u32, kind: &str, mut ev: Value) {
        ev["type"] = json!(kind);
        hooks_for(self).emit(gen, ev);
    }

    pub fn write_state(&self, st: &mut State) -> Result<()> {
        st.updated_at = chrono::Local::now().to_rfc3339();
        let tmp = self.path("state.json.tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(st)?)?;
        std::fs::rename(&tmp, self.path("state.json"))?;
        Ok(())
    }

    pub fn read_state(&self) -> Result<State> {
        let raw = std::fs::read_to_string(self.path("state.json"))
            .with_context(|| format!("读不了 {}", self.path("state.json").display()))?;
        Ok(serde_json::from_str(&raw)?)
    }
}

/// 跨运行的档案：run 目录的上一级放一份 `archive.jsonl`，每个测过的候选一条
/// （含迁移/重建前的旧目录的结论）。出题的一方经 `prior_history` 读到——
/// 不再每次宕机迁移后就失忆、重测已证无效的候选。
#[derive(Debug, Clone, Serialize, Deserialize)]
struct ArchiveEntry {
    ts: String,
    run_id: String,
    game: String,
    item: optimize::proposer::HistoryItem,
}

fn archive_file(rd: &RunDir) -> PathBuf {
    rd.dir
        .parent()
        .map(|p| p.join("archive.jsonl"))
        .unwrap_or_else(|| rd.path("archive.jsonl"))
}

/// 读档案里同一个游戏的全部结论（坏行跳过，一行坏不拖整场）。
fn archive_seed(rd: &RunDir, game: &str) -> Vec<optimize::proposer::HistoryItem> {
    let Ok(text) = std::fs::read_to_string(archive_file(rd)) else { return Vec::new() };
    text.lines()
        .filter_map(|l| serde_json::from_str::<ArchiveEntry>(l).ok())
        .filter(|e| e.game == game)
        .map(|e| e.item)
        .collect()
}

/// 这一代测过的结论追加进档案。
fn archive_append(rd: &RunDir, game: &str, items: &[optimize::proposer::HistoryItem]) {
    if items.is_empty() {
        return;
    }
    let ts = chrono::Local::now().to_rfc3339();
    let mut out = String::new();
    for item in items {
        let e = ArchiveEntry { ts: ts.clone(), run_id: rd.run_id(), game: game.into(), item: item.clone() };
        if let Ok(l) = serde_json::to_string(&e) {
            out.push_str(&l);
            out.push('\n');
        }
    }
    if !out.is_empty() {
        let p = archive_file(rd);
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
            use std::io::Write;
            let _ = f.write_all(out.as_bytes());
        }
    }
}

fn hooks_for(rd: &RunDir) -> optimize::GoalHooks {
    optimize::GoalHooks {
        events: Some(rd.path("events.jsonl")),
        ..Default::default()
    }
}

fn sha256(s: &str) -> String {
    let mut h = Sha256::new();
    h.update(s.as_bytes());
    format!("{:x}", h.finalize())
}

// ───────────────────────────── 起草 ─────────────────────────────

/// 起草用什么。
pub enum Drafter<'a> {
    /// 出方案用的同一套大模型。
    Model(&'a dyn optimize::proposer::Complete),
    /// 本地模板 (节点上没 key、或 --proposer local)。
    Template,
}

pub const DRAFT_SYSTEM: &str = "你帮人把一句话的手机游戏优化需求起草成一份结构化目标 goal.md。\
只输出 goal.md 全文, 不要解释, 不要代码块围栏。格式: 第一行 `---`, 然后 YAML front-matter, 再一行 `---`, 然后几句大白话正文。\n\
front-matter 字段 (只能用这些, 取值只能在列出的范围里):\n\
game: <包名, 原样用给你的>\n\
device: any\n\
objective: {metric: power_watt|frame_p95|fps_mean|gpu_busy_ms, direction: lower|higher, target_pct: <1-30 的数>}\n\
constraints: 列表, 每项一个单键映射, 如 - fps_mean: no_worse / - frame_p95: no_worse / - image: psnr_min_db 30 (主指标不要再写进 constraints)\n\
layers: [sysparam, gray] 的子集 (sysparam = 系统参数, 不动画面; gray = 画面流程改写, 会动画面)\n\
forbid: [] (人明确说不许动的参数, 没说就空)\n\
budget: {wall_minutes: 30-480, max_generations: 2-10, min_generations: 1-max}\n\
stop_when: [target_reached, budget_exhausted, {no_gain: 2}]\n\
proposer: llm\n\
gate: off\n\
per_generation: 2\n\
正文写清楚: 人要什么、为什么这样定主指标与约束、人话里有哪些偏好。人没说的不要编, 用保守缺省。";

/// `goal draft`: 起草 goal.md, state = awaiting_confirm。回 (goal.md 路径, 是不是模板起草的)。
pub fn draft(intent: &str, game: &str, out: &Path, drafter: Drafter<'_>) -> Result<(PathBuf, bool)> {
    std::fs::create_dir_all(out)?;
    let rd = RunDir::new(out);
    if rd.path("state.json").exists() {
        let st = rd.read_state()?;
        if !matches!(st.status.as_str(), "drafting" | "awaiting_confirm") {
            bail!("{} 已经在跑或跑完了 (status={}), 不能重新起草", out.display(), st.status);
        }
    }
    std::fs::write(rd.path("intent.txt"), intent)?;
    let mut st = State {
        schema: STATE_SCHEMA.into(),
        run_id: rd.run_id(),
        status: "drafting".into(),
        goal_summary: intent.chars().take(80).collect(),
        generation: 0,
        max_generations: 0,
        elapsed_min: 0.0,
        budget_min: 0.0,
        best: None,
        progress_to_target: 0.0,
        device: None,
        human_md_rev: 0,
        updated_at: String::new(),
        goal_sha256: None,
        stop_reason: None,
        no_gain_streak: 0,
    };
    rd.write_state(&mut st)?;

    let mut by_template = true;
    let mut doc_text = None;
    if let Drafter::Model(m) = drafter {
        let user = format!("包名: {game}\n人的原话: {intent}\n\n起草 goal.md。");
        match m.complete(DRAFT_SYSTEM, &user) {
            Ok(raw) => {
                let text = strip_fence(&raw);
                match spec::parse(&text) {
                    Ok(d) if d.spec.game == game => {
                        doc_text = Some(spec::render(&d.spec, &d.body)?);
                        by_template = false;
                    }
                    Ok(d) => rd.event(0, "note", json!({"stage": "draft",
                        "message": format!("大模型草稿的 game 是 {:?}, 不是 {game:?}, 改用模板", d.spec.game)})),
                    Err(e) => rd.event(0, "note", json!({"stage": "draft",
                        "message": format!("大模型草稿不合格式 ({e:#}), 改用模板")})),
                }
            }
            Err(e) => rd.event(0, "note", json!({"stage": "draft",
                "message": format!("大模型起草失败 ({e:#}), 改用模板")})),
        }
    }
    let text = match doc_text {
        Some(t) => t,
        None => {
            let (s, b) = spec::template(intent, game);
            spec::render(&s, &b)?
        }
    };
    let doc = spec::parse(&text)?;
    std::fs::write(rd.path("goal.md"), &text)?;
    if !rd.path("human.md").exists() {
        std::fs::write(rd.path("human.md"), "# 人工干预：第一条非注释行写 action: go | pause | stop（也认 指令: 继续|暂停|停止），这是权威指令，\n# 写了它正文里的「暂停/停止」字样就不会被误读。下面还可以写：预算 +30 分钟 / 不要动 xxx / 只试 系统参数 /\n# 以及任何大白话（原样转给出主意的一方，优先照办）。\n")?;
    }
    rd.event(0, "note", json!({"stage": "draft", "by": if by_template { "template" } else { "llm" },
        "message": "goal.md 已起草, 等人确认"}));
    st.status = "awaiting_confirm".into();
    st.goal_summary = doc.spec.summary();
    st.max_generations = doc.spec.budget.max_generations;
    st.budget_min = doc.spec.budget.wall_minutes as f64;
    rd.write_state(&mut st)?;
    Ok((rd.path("goal.md"), by_template))
}

fn strip_fence(raw: &str) -> String {
    let t = raw.trim();
    if let Some(rest) = t.strip_prefix("```") {
        let rest = rest.split_once('\n').map(|x| x.1).unwrap_or("");
        return rest.trim_end().trim_end_matches("```").trim().to_string();
    }
    t.to_string()
}

/// `goal show`: 一次目标运行的现状 (纯文本)。
pub fn show(dir: &Path, last_events: usize) -> Result<String> {
    let rd = RunDir::new(dir);
    let read = |n: &str| std::fs::read_to_string(rd.path(n)).unwrap_or_else(|_| format!("<没有 {n}>\n"));
    let mut o = String::new();
    o.push_str(&format!("== {} ==\n\n-- state.json --\n{}\n", dir.display(), read("state.json")));
    o.push_str(&format!("\n-- goal.md --\n{}\n", read("goal.md")));
    o.push_str(&format!("-- human.md --\n{}\n", read("human.md")));
    let ev = read("events.jsonl");
    let lines: Vec<&str> = ev.lines().collect();
    o.push_str(&format!("-- events.jsonl (最后 {} 条 / 共 {}) --\n", last_events.min(lines.len()), lines.len()));
    for l in lines.iter().skip(lines.len().saturating_sub(last_events)) {
        o.push_str(l);
        o.push('\n');
    }
    if rd.path("report.md").exists() {
        o.push_str(&format!("\n-- report.md --\n{}", read("report.md")));
    }
    Ok(o)
}

/// `goal cancel`: 把一次卡住的运行收尾 (进程已经不在了, state 还是 running/paused)。
/// 不碰设备, 只写 state / stop 事件 / report。
pub fn cancel(dir: &Path, reason: &str) -> Result<State> {
    let rd = RunDir::new(dir);
    let mut st = rd.read_state()?;
    if matches!(st.status.as_str(), "done" | "failed") {
        bail!("状态已经是 {}, 不用收尾", st.status);
    }
    st.status = "done".into();
    st.stop_reason = Some("human_stop".into());
    rd.write_state(&mut st)?;
    rd.event(st.generation, "stop", json!({"reason": "human_stop", "summary": format!("人工收尾: {reason}")}));
    Ok(st)
}

/// `goal edit`: 确认前整份替换 goal.md。先校验; 已经开跑的不许改 (冻结的就是冻结的)。
pub fn edit(dir: &Path, new_goal: &Path) -> Result<()> {
    let rd = RunDir::new(dir);
    let mut st = rd.read_state()?;
    if !matches!(st.status.as_str(), "drafting" | "awaiting_confirm") {
        bail!("状态是 {}, goal.md 已冻结, 不能再改", st.status);
    }
    let text = std::fs::read_to_string(new_goal).with_context(|| format!("读不了 {}", new_goal.display()))?;
    let doc = spec::parse(&text)?;
    std::fs::write(rd.path("goal.md"), &text)?;
    rd.event(0, "note", json!({"stage": "draft", "message": "人改了 goal.md (整份替换)"}));
    st.goal_summary = doc.spec.summary();
    st.max_generations = doc.spec.budget.max_generations;
    st.budget_min = doc.spec.budget.wall_minutes as f64;
    rd.write_state(&mut st)?;
    Ok(())
}

/// 把这次用的输入文件留一份在 run 目录的 inputs/ 下 (证据: 这次的白名单与旋钮从哪来)。
fn keep_input(rd: &RunDir, src: Option<&PathBuf>, name: &str) -> Option<PathBuf> {
    let src = src?;
    let dst = rd.path("inputs").join(name);
    if src != &dst {
        let _ = std::fs::create_dir_all(rd.path("inputs"));
        if std::fs::copy(src, &dst).is_err() {
            return Some(src.clone());
        }
    }
    Some(dst)
}

// ───────────────────────────── 运行 ─────────────────────────────

/// SIGINT / SIGTERM: 置叫停标志, 并把 SIGINT 转给登记着的 phonefarm 子进程 (它们收到后
/// 自己走还原流程)。进程组不动 —— 节点按进程组逐级发 INT/TERM/KILL 停任务, 另开进程组
/// 会让那一下漏掉 goal run 和它的子进程。只有 `goal run` 命令行入口调它。
#[cfg(unix)]
pub fn install_signal_handlers() {
    extern "C" fn on_signal(_sig: libc::c_int) {
        if !optimize::interrupted() {
            optimize::interrupt();
            optimize::children::signal_all(libc::SIGINT);
        }
    }
    unsafe {
        libc::signal(libc::SIGINT, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGTERM, on_signal as *const () as libc::sighandler_t);
    }
}

/// `goal run` 的运行参数 (与目标无关的、环境里的东西)。
pub struct RunOpts<'a> {
    pub phonefarm: PathBuf,
    pub secrets: PathBuf,
    pub llm_provider: Option<String>,
    pub sysparam_probe: Option<PathBuf>,
    pub frame_audit: Option<PathBuf>,
    pub profiles_dir: PathBuf,
    /// 设备锁: 人工暂停时释放，继续后重新获取。
    pub lock: Option<LockOpts>,
    /// 暂停时多久看一次 human.md。
    pub poll: Duration,
    /// 测试用: 每代开始前回调 (代数)。
    pub before_generation: Option<&'a dyn Fn(u32)>,
    /// true = 断点续跑: 状态 running/paused、跑到一半进程死了，从最后完成的一代之后接着跑。
    pub resume: bool,
}

/// 云端出方案单代超时: Actions 排队 + 装 pi + Kimi, 实测一次约 1.5 分钟, 留足余量。
pub const CLOUD_TIMEOUT_S: u64 = 1200;
/// 云端出方案适配器的缺省位置 (相对 game_opt_loop 仓库根, 也就是 goal run 的工作目录)。
/// 环境变量 `GOL_CLOUD_PROPOSER` 可换成别的可执行 (例如本机直接跑 tools/proposers/pi_kimi.py)。
pub const CLOUD_ADAPTER: &str = "tools/proposers/cloud_actions.py";

/// goal.md 里的 `proposer` → optimize 认的出方案 spec。
///
/// - `llm` / `local`: 原样。
/// - `cloud`: `cmd:<GOL_CLOUD_PROPOSER 或 tools/proposers/cloud_actions.py>`。用缺省适配器时
///   先确认 gh 用得上 (它靠 gh 触发 GitHub Actions、取回结果): 环境里有 `GH_TOKEN` (受限 token)
///   且装了 gh, 或 gh 已登录。不然第一代必然出不了方案。
pub fn proposer_spec_for(p: &str) -> Result<String> {
    match p {
        "llm" | "local" => Ok(p.to_string()),
        "cloud" => {
            let custom = std::env::var("GOL_CLOUD_PROPOSER").ok().filter(|s| !s.trim().is_empty());
            let exe = custom.clone().unwrap_or_else(|| CLOUD_ADAPTER.to_string());
            if !std::path::Path::new(&exe).is_file() {
                bail!(
                    "proposer: cloud 要的适配器 {exe} 不存在 (goal run 要在 game_opt_loop 仓库根下跑, \
                     或用 GOL_CLOUD_PROPOSER 指定)"
                );
            }
            // 受限 token (只能触发这一个仓库的 workflow、读 artifact 的 fine-grained PAT) 放在
            // GH_TOKEN 里时, gh 直接用它, 不需要登录态; 只要求 gh 本身装着。
            let has_token = ["GH_TOKEN", "GITHUB_TOKEN"]
                .iter()
                .any(|k| std::env::var(k).map(|v| !v.trim().is_empty()).unwrap_or(false));
            if custom.is_none() && has_token {
                let gh_ok = std::process::Command::new("gh")
                    .arg("--version")
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status()
                    .map(|s| s.success())
                    .unwrap_or(false);
                if !gh_ok {
                    bail!("proposer: cloud 要 gh 命令行 (GH_TOKEN 已设, 但这台机器上没有 gh)");
                }
            } else if custom.is_none() {
                let ok = std::process::Command::new("gh")
                    .args(["auth", "status"])
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status()
                    .map(|s| s.success())
                    .unwrap_or(false);
                if !ok {
                    bail!(
                        "proposer: cloud 靠 gh 触发 GitHub Actions, 但这台机器上 gh 没登录 (gh auth status 失败), \
                         也没有 GH_TOKEN。设 GH_TOKEN (只能触发本仓库 workflow、读 artifact 的受限 token) 或登录后再跑, \
                         或用 GOL_CLOUD_PROPOSER 换成本机的 tools/proposers/pi_kimi.py"
                    );
                }
            }
            Ok(format!("cmd:{exe}"))
        }
        other => bail!("proposer 只认 llm / local / cloud, 收到 {other}"),
    }
}

pub struct LockOpts {
    pub wait: Option<Duration>,
}

/// 一代的结算。
#[derive(Debug, Clone, Default)]
struct GenResult {
    candidates: usize,
    kept: usize,
    /// (候选, 主指标变化 %, 判定) —— 只收「留下且主指标显著改善」的。
    gains: Vec<(String, f64, String)>,
}

fn layers_effective(spec: &spec::GoalSpec, only: &Option<Vec<String>>) -> Vec<String> {
    let mut l: Vec<String> = spec
        .layers
        .iter()
        .filter(|x| x.as_str() != "upop")
        .cloned()
        .collect();
    if let Some(o) = only {
        l.retain(|x| o.contains(x));
    }
    l
}

fn kinds_of(layers: &[String]) -> Vec<optimize::eval::Kind> {
    layers
        .iter()
        .filter_map(|l| match l.as_str() {
            "sysparam" => Some(optimize::eval::Kind::Sysparam),
            "gray" => Some(optimize::eval::Kind::Gray),
            "whitebox" => Some(optimize::eval::Kind::Whitebox),
            _ => None,
        })
        .collect()
}

/// 一代的报告里挑出「留下且主指标显著朝目标方向变」的候选。
fn settle(report_json: &Path, spec: &spec::GoalSpec) -> Result<GenResult> {
    let r: Value = serde_json::from_str(&std::fs::read_to_string(report_json)?)?;
    let key = spec.metric_key();
    let mut g = GenResult::default();
    for e in r["archive"].as_array().into_iter().flatten() {
        g.candidates += 1;
        if e["kept"].as_bool() != Some(true) {
            continue;
        }
        g.kept += 1;
        let won = e["verdict"]["wins"]
            .as_array()
            .is_some_and(|w| w.iter().any(|x| x.as_str() == Some(key)));
        let delta = e["verdict"]["per_metric"][key]["diff_pct"].as_f64();
        if let (true, Some(d)) = (won, delta) {
            let right_way = if spec.objective.direction == "lower" { d < 0.0 } else { d > 0.0 };
            if right_way {
                g.gains.push((
                    e["id"].as_str().unwrap_or("").to_string(),
                    d,
                    e["status"].as_str().unwrap_or("").to_string(),
                ));
            }
        }
    }
    Ok(g)
}

/// `goal run`: 读冻结的 goal.md, 一代一代跑到停。回最终 state。
pub fn run(dir: &Path, opts: &RunOpts<'_>) -> Result<State> {
    let rd = RunDir::new(dir);
    let goal_text = std::fs::read_to_string(rd.path("goal.md"))
        .with_context(|| format!("{} 里没有 goal.md (先 goal draft)", dir.display()))?;
    let doc = spec::parse(&goal_text)?;
    let spec = &doc.spec;
    let sha = sha256(&goal_text);
    let mut st = match rd.read_state() {
        Ok(s) => s,
        Err(_) => State {
            schema: STATE_SCHEMA.into(),
            run_id: rd.run_id(),
            status: "awaiting_confirm".into(),
            goal_summary: String::new(),
            generation: 0,
            max_generations: 0,
            elapsed_min: 0.0,
            budget_min: 0.0,
            best: None,
            progress_to_target: 0.0,
            device: None,
            human_md_rev: 0,
            updated_at: String::new(),
            goal_sha256: None,
            stop_reason: None,
            no_gain_streak: 0,
        },
    };
    // resume=false 时：同以前，只有「确认后一代没跑」才能再启动。
    // resume=true（goal resume）：跑到一半进程死了（宕机/被杀），从最后完成的一代之后接着跑。
    let resuming = match st.status.as_str() {
        "awaiting_confirm" | "drafting" => false,
        // 冻结了、但一代都还没开始 (例如排队等锁时被杀掉): 可以接着跑, 前提是 goal.md 没被改过
        "running" | "paused" if st.generation == 0 && st.goal_sha256.as_deref() == Some(sha.as_str()) => {
            rd.event(0, "note", json!({"message": "接着跑: 上次确认目标后还没开始第一代就停了"}));
            false
        }
        "running" | "paused" if st.goal_sha256.as_deref() == Some(sha.as_str()) => {
            if !opts.resume {
                bail!(
                    "这次目标运行已跑到第 {} 代、状态 {} 但进程已不在。\n  进程确实死了要接着跑: game_opt_loop goal resume {};\n  记错了、想让它彻底收场: game_opt_loop goal cancel {}",
                    st.generation,
                    st.status,
                    rd.dir.display(),
                    rd.dir.display()
                );
            }
            true
        }
        s => bail!(
            "这次目标运行的状态是 {s} (已跑到第 {} 代), 不能再启动; 要重跑请新建一个 run 目录, \
             卡住的运行用 `goal cancel` 收尾",
            st.generation
        ),
    };
    // 云端出方案: 先确认这台机器上起得来, 别冻结了目标才发现第一代就出不了方案。
    let proposer_spec = proposer_spec_for(&spec.proposer)?;

    // ── 冻结（断点续跑时跳过：已冻结过，goal.md 未改已在上面核过）──
    let (sysparam_probe, frame_audit, mut per_gen, elapsed0, start_gen) = if resuming {
        // 重建已完成代数：gen{g}/report.json 能结算的才算完成；中途死掉的那代重跑。
        let mut per_gen: Vec<(u32, GenResult)> = Vec::new();
        let mut completed = 0u32;
        for g in 1..=st.generation {
            let rp = rd.path("optimize").join(format!("gen{g}")).join("report.json");
            match settle(&rp, spec) {
                Ok(gr) => {
                    per_gen.push((g, gr));
                    completed = g;
                }
                Err(_) => break,
            }
        }
        let elapsed0 = st.elapsed_min;
        st.generation = completed;
        st.status = "running".into();
        rd.write_state(&mut st)?;
        rd.event(completed, "note", json!({"message": format!(
            "断点续跑: 前 {completed} 代已完成并保留, 从第 {} 代继续; 已花的 {} 分钟照算, 宕机那段时间不计时",
            completed + 1, elapsed0.round())}));
        (opts.sysparam_probe.clone(), opts.frame_audit.clone(), per_gen, elapsed0, completed)
    } else {
        std::fs::write(rd.path("goal.frozen.md"), &goal_text)?;
        let sysparam_probe = keep_input(&rd, opts.sysparam_probe.as_ref(), "probe.txt");
        let frame_audit = keep_input(&rd, opts.frame_audit.as_ref(), "frame_audit.json");
        st.goal_sha256 = Some(sha.clone());
        st.status = "running".into();
        st.goal_summary = spec.summary();
        st.max_generations = spec.budget.max_generations;
        st.budget_min = spec.budget.wall_minutes as f64;
        st.device = (spec.device != "any").then(|| spec.device.clone());
        rd.write_state(&mut st)?;
        rd.event(0, "start", json!({"goal_summary": st.goal_summary, "goal_sha256": sha,
            "layers": spec.layers, "objective": spec.objective, "budget": spec.budget}));
        if spec.layers.iter().any(|l| l == "upop") {
            rd.event(0, "note", json!({"message": "「加画面算子」这一层还不能自动跑, 这次跳过"}));
        }
        (sysparam_probe, frame_audit, Vec::new(), 0.0, 0)
    };

    let label = format!("goal-{}", rd.run_id());
    let serial = (spec.device != "any").then_some(spec.device.as_str());
    let mut guard = match &opts.lock {
        Some(l) => match crate::devlock::Guard::acquire(
            opts.phonefarm.clone(), serial, label.clone(), l.wait,
        ) {
            Ok(g) => Some(g),
            Err(e) if optimize::interrupted() => {
                // 排队时被叫停: 没碰设备, 按人叫停收尾
                rd.event(0, "note", json!({"message": format!("排队等手机时被叫停, 没动手机: {e:#}")}));
                st.status = "done".into();
                st.stop_reason = Some("human_stop".into());
                rd.write_state(&mut st)?;
                rd.event(0, "stop", json!({"reason": "human_stop", "summary": "排队等手机时被叫停, 一代都没跑"}));
                return Ok(st);
            }
            Err(e) => {
                // 等锁超时: 没碰设备。状态保持「已冻结、一代没开始」, 同一个目录可以再 goal run 接着排。
                rd.event(0, "note", json!({"message": format!("没排到手机, 没动手机: {e:#}")}));
                return Err(e);
            }
        },
        None => None,
    };

    let t0 = Instant::now();
    // 断点续跑: human.md 当前内容直接生效，不重复记 steer 事件、不涨 rev
    let cur_human = std::fs::read_to_string(rd.path("human.md")).unwrap_or_default();
    let (mut last_human, mut steer) = if resuming {
        // 断点续跑: human.md 当前内容直接生效，不重复记 steer 事件、不涨 rev
        (Some(cur_human.clone()), human::parse(&cur_human))
    } else if human::parse(&cur_human) == human::Steer::default() {
        // 空文件或 draft 带的注释模板：不算指令，不配 steer 事件、不涨 rev
        (Some(cur_human), human::Steer::default())
    } else {
        (None, human::Steer::default())
    };
    let mut history: Vec<optimize::proposer::HistoryItem> = archive_seed(&rd, &spec.game);
    if !history.is_empty() {
        rd.event(0, "note", json!({"message": format!("带档案 {} 条结论出题（含过往运行的已测候选，避免重测）", history.len())}));
    }
    let mut steer_log: Vec<(u32, u32, String, String)> = if resuming {
        // 从 events.jsonl 重建掌舵记录（最终报告用）
        std::fs::read_to_string(rd.path("events.jsonl"))
            .unwrap_or_default()
            .lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .filter(|e| e["type"] == "steer")
            .map(|e| {
                (e["gen"].as_u64().unwrap_or(0) as u32,
                 e["human_md_rev"].as_u64().unwrap_or(0) as u32,
                 e["text"].as_str().unwrap_or_default().to_string(),
                 e["interpreted"].as_str().unwrap_or_default().to_string())
            })
            .collect()
    } else {
        Vec::new()
    };
    let mut errors_in_a_row = 0u32;
    let no_gain_limit = spec.no_gain();
    let stop_on_target = spec.stops_on("target_reached") || spec.stop_when.is_empty();

    let stop = |st: &mut State, reason: &str, summary: String| -> Result<()> {
        st.status = if reason == "error" { "failed" } else { "done" }.into();
        st.stop_reason = Some(reason.into());
        rd.write_state(st)?;
        rd.event(st.generation, "stop", json!({"reason": reason, "summary": summary}));
        Ok(())
    };

    let mut gen = start_gen;
    loop {
        // ── 掌舵: 每代开始前读 human.md ──
        if let Some(cb) = opts.before_generation {
            cb(gen + 1);
        }
        let text = std::fs::read_to_string(rd.path("human.md")).unwrap_or_default();
        if last_human.as_deref() != Some(text.as_str()) {
            let changed = last_human.is_some() || !text.trim().is_empty();
            last_human = Some(text.clone());
            if changed {
                steer = human::parse(&text);
                st.human_md_rev += 1;
                let interp = steer.interpreted();
                rd.event(gen, "steer", json!({"human_md_rev": st.human_md_rev, "text": text, "interpreted": interp}));
                steer_log.push((gen, st.human_md_rev, text.clone(), interp));
                rd.write_state(&mut st)?;
            }
        }
        st.elapsed_min = elapsed0 + t0.elapsed().as_secs_f64() / 60.0;
        st.budget_min = (spec.budget.wall_minutes as i64 + steer.budget_add_min).max(0) as f64;

        if optimize::interrupted() {
            stop(&mut st, "human_stop", "人按了停止".into())?;
            break;
        }
        if steer.stop {
            stop(&mut st, "human_stop", "人在掌舵指令里写了停止".into())?;
            break;
        }
        if steer.pause {
            st.status = "paused".into();
            rd.write_state(&mut st)?;
            rd.event(gen, "note", json!({"message": "人要求暂停: 等掌舵指令再改才继续, 这期间把手机让给别人"}));
            drop(guard.take());
            let seen = last_human.clone().unwrap_or_default();
            loop {
                std::thread::sleep(opts.poll);
                let now = std::fs::read_to_string(rd.path("human.md")).unwrap_or_default();
                if now != seen {
                    break;
                }
            }
            if let Some(l) = &opts.lock {
                guard = Some(crate::devlock::Guard::acquire(
                    opts.phonefarm.clone(), serial, label.clone(), l.wait,
                )?);
            }
            st.status = "running".into();
            rd.write_state(&mut st)?;
            continue;
        }
        if gen >= spec.budget.max_generations {
            stop(&mut st, "budget_exhausted", format!("{} 代用完", spec.budget.max_generations))?;
            break;
        }
        if st.elapsed_min >= st.budget_min {
            let msg = format!("时间预算 {} 分钟用完", st.budget_min);
            stop(&mut st, "budget_exhausted", msg)?;
            break;
        }

        // ── 跑一代 ──
        gen += 1;
        st.generation = gen;
        rd.write_state(&mut st)?;
        let layers = layers_effective(spec, &steer.only_layers);
        if layers.is_empty() {
            stop(&mut st, "error", "按 goal.md 与 human.md, 一层都不剩可跑".into())?;
            break;
        }
        let mut forbid = spec.forbid.clone();
        forbid.extend(steer.forbid.iter().cloned());
        // 出方案的一方看到的偏好: 人刚刚的指令 (最高优先级) + goal.md 正文。
        let mut pref = Vec::new();
        if !steer.free_text.trim().is_empty() {
            pref.push(format!("人刚刚的指令: {}", steer.free_text.trim()));
        }
        if !doc.body.trim().is_empty() {
            pref.push(format!("目标说明: {}", doc.body.trim()));
        }
        let steer_text = (!pref.is_empty()).then(|| pref.join("\n"));
        rd.event(gen, "plan", json!({"layers": layers, "per_generation": spec.per_generation,
            "forbid": forbid, "budget_left_min": (st.budget_min - st.elapsed_min).max(0.0)}));

        let gate = match spec.gate.as_str() {
            "off" => None,
            g => Some(optimize::gate::Options {
                spec: "jev".into(),
                threshold: optimize::gate::DEFAULT_THRESHOLD,
                shadow: g == "shadow",
                key_file: None,
            }),
        };
        let gen_dir = rd.path("optimize").join(format!("gen{gen}"));
        std::fs::create_dir_all(&gen_dir)?;
        let cfg = optimize::Config {
            game: spec.game.clone(),
            package: Some(spec.game.clone()),
            serial: st.device.clone(),
            extra_devices: vec![],
            budget: optimize::plan::Budget { seconds: 24 * 3600 },
            plan_only: false,
            out_dir: gen_dir,
            phonefarm: opts.phonefarm.clone(),
            secrets: opts.secrets.clone(),
            use_llm: spec.proposer == "llm",
            llm_provider: opts.llm_provider.clone(),
            llm_timeout_s: None,
            seed: 20261011 + gen as u64,
            quality_reference: None,
            sysparam_probe: sysparam_probe.clone(),
            proposer: optimize::proposer::Options {
                specs: vec![proposer_spec.clone()],
                timeout_s: if spec.proposer == "cloud" {
                    CLOUD_TIMEOUT_S
                } else {
                    optimize::proposer::DEFAULT_TIMEOUT_S
                },
                gate,
            },
            frame_audit: frame_audit.clone(),
            screen: spec.screen,
            shape: optimize::plan::Shape {
                layers: Some(kinds_of(&layers)),
                generations: Some(1),
                children: Some(spec.per_generation),
            },
            profiles_dir: opts.profiles_dir.clone(),
            game_profile: None,
            campaign: None,
            goal: optimize::GoalHooks {
                gen_offset: gen - 1,
                prior_history: history.clone(),
                steer: steer_text,
                forbid,
                psnr_min_db: spec.psnr_min_db(),
                events: Some(rd.path("events.jsonl")),
                objective: Some((spec.objective.metric.clone(), spec.objective.direction.clone())),
            },
        };
        let result = optimize::run(&cfg);
        if optimize::interrupted() {
            // 当前这一代被打断: 正在上机的那个候选由 phonefarm 收到 SIGINT 自己还原,
            // 这一代已经量完的候选照样记账 (报告在的话)。
            if let Ok(o) = &result {
                if let Ok(g) = settle(Path::new(&o.report_json), spec) {
                    per_gen.push((gen, g));
                }
            }
            rd.event(gen, "note", json!({"message": format!("第 {gen} 代被人叫停, 正在测的那组改动已经撤回")}));
            stop(&mut st, "human_stop", format!("人按了停止, 第 {gen} 代中途停下"))?;
            break;
        }
        let outcome = match result {
            Ok(o) => o,
            Err(e) => {
                errors_in_a_row += 1;
                rd.event(gen, "error", json!({"message": format!("{e:#}")}));
                if errors_in_a_row >= 2 {
                    stop(&mut st, "error", format!("连续两代出错, 最后一次: {e:#}"))?;
                    break;
                }
                continue;
            }
        };
        errors_in_a_row = 0;
        archive_append(&rd, &spec.game, &outcome.history);
        history.extend(outcome.history.iter().cloned());
        let g = settle(Path::new(&outcome.report_json), spec)?;

        // ── 结算 ──
        for (cand, d, verdict) in &g.gains {
            rd.event(gen, "keep", json!({"candidate_id": cand, "metric": spec.objective.metric,
                "delta_pct": d, "verdict": verdict}));
            let better = st.best.as_ref().is_none_or(|b| d.abs() > b.delta_pct.abs());
            if better {
                st.best = Some(Best {
                    candidate: cand.clone(),
                    metric: spec.objective.metric.clone(),
                    delta_pct: *d,
                    verdict: verdict.clone(),
                });
            }
        }
        if g.gains.is_empty() {
            st.no_gain_streak += 1;
        } else {
            st.no_gain_streak = 0;
        }
        st.progress_to_target = st
            .best
            .as_ref()
            .map(|b| (b.delta_pct.abs() / spec.objective.target_pct).min(1.0))
            .unwrap_or(0.0);
        st.elapsed_min = elapsed0 + t0.elapsed().as_secs_f64() / 60.0;
        rd.write_state(&mut st)?;
        per_gen.push((gen, g));

        if stop_on_target && st.progress_to_target >= 1.0 {
            let msg = format!("达到目标: {}", best_line(&st));
            stop(&mut st, "target_reached", msg)?;
            break;
        }
        if let Some(n) = no_gain_limit {
            if st.no_gain_streak >= n && gen >= spec.budget.min_generations {
                let msg = format!("连续 {} 代没有任何候选在主指标上带来显著改善: 这条路没收益", st.no_gain_streak);
                stop(&mut st, "no_gain", msg)?;
                break;
            }
        }
    }
    drop(guard);
    write_report(&rd, &doc, &st, &per_gen, &steer_log)?;
    Ok(st)
}

fn best_line(st: &State) -> String {
    match &st.best {
        Some(b) => format!("{} 让 {} 变化 {:+.2}% ({})", b.candidate, b.metric, b.delta_pct, b.verdict),
        None => "没有".into(),
    }
}

fn write_report(
    rd: &RunDir,
    doc: &spec::GoalDoc,
    st: &State,
    per_gen: &[(u32, GenResult)],
    steer_log: &[(u32, u32, String, String)],
) -> Result<()> {
    let mut o = String::new();
    o.push_str(&format!("# 目标运行 {}\n\n", st.run_id));
    o.push_str(&format!("**目标**: {}\n\n", st.goal_summary));
    let reason = st.stop_reason.as_deref().unwrap_or("?");
    let reason_text = match reason {
        "target_reached" => "达到目标",
        "budget_exhausted" => "预算用完",
        "no_gain" => "这条路没收益 (连续几代没有任何候选在主指标上带来显著改善)",
        "human_stop" => "人叫停",
        "error" => "出错停下",
        r => r,
    };
    o.push_str(&format!(
        "**结果**: {reason_text}。跑了 {} 代, 用时 {:.1} 分钟 (预算 {:.0})。最好的候选: {}。\n\n",
        st.generation,
        st.elapsed_min,
        st.budget_min,
        best_line(st)
    ));
    o.push_str(&format!("进度 (最好的改善 / 目标幅度): {:.0}%\n\n", st.progress_to_target * 100.0));
    o.push_str("## 每一代\n\n| 代 | 上机候选 | 留下 | 主指标显著朝目标方向 |\n|---|---|---|---|\n");
    for (g, r) in per_gen {
        o.push_str(&format!(
            "| {g} | {} | {} | {} |\n",
            r.candidates,
            r.kept,
            if r.gains.is_empty() {
                "无".to_string()
            } else {
                r.gains
                    .iter()
                    .map(|(c, d, _)| format!("{c} {d:+.2}%"))
                    .collect::<Vec<_>>()
                    .join(", ")
            }
        ));
    }
    o.push_str("\n每一代的候选、数字和证据在 `optimize/gen<N>/report.md`。\n\n");
    if !steer_log.is_empty() {
        o.push_str("## 人的掌舵\n\n");
        for (g, rev, text, interp) in steer_log {
            o.push_str(&format!(
                "- 第 {} 代开始前读到 human.md 第 {rev} 版: {interp}\n  > {}\n",
                g + 1,
                text.trim().replace('\n', "\n  > ")
            ));
        }
        o.push('\n');
    }
    o.push_str("## 目标原文 (冻结)\n\n");
    o.push_str(&format!("{}\n", doc.body));
    std::fs::write(rd.path("report.md"), o)?;
    Ok(())
}

#[cfg(test)]
mod tests {

    /// 受限 token 放在 GH_TOKEN 里时, 云端出方案不要求 gh 登录态 (装着 gh 就行)。
    #[test]
    fn cloud_accepts_a_restricted_gh_token_instead_of_a_login() {
        if std::process::Command::new("gh").arg("--version").output().is_err() {
            return; // 这台机器没装 gh, 测不了
        }
        std::env::remove_var("GOL_CLOUD_PROPOSER");
        std::env::set_var("GH_TOKEN", "github_pat_test_only");
        let got = proposer_spec_for("cloud");
        std::env::remove_var("GH_TOKEN");
        assert_eq!(got.unwrap(), format!("cmd:{CLOUD_ADAPTER}"));
        assert_eq!(proposer_spec_for("local").unwrap(), "local");
        assert!(proposer_spec_for("magic").is_err());
    }

    use super::*;

    #[test]
    fn fenced_drafts_are_unwrapped() {
        assert_eq!(strip_fence("```markdown\n---\na: 1\n---\nx\n```"), "---\na: 1\n---\nx");
        assert_eq!(strip_fence("---\nx"), "---\nx");
    }

    #[test]
    fn upop_is_dropped_and_only_layers_filter() {
        let (mut s, _) = spec::template("省电", "p");
        s.layers = vec!["sysparam".into(), "gray".into(), "upop".into()];
        assert_eq!(layers_effective(&s, &None), vec!["sysparam", "gray"]);
        assert_eq!(layers_effective(&s, &Some(vec!["gray".into()])), vec!["gray"]);
    }

    #[test]
    fn archive_roundtrip_filters_game_and_tolerates_bad_lines() {
        let tmp = std::env::temp_dir().join(format!("gol-archive-test-{}", std::process::id()));
        let rd = RunDir::new(tmp.join("run-a"));
        std::fs::create_dir_all(&rd.dir).unwrap();
        let item = |id: &str| optimize::proposer::HistoryItem {
            candidate_id: id.into(),
            generation: 1,
            candidate: optimize::proposer::Candidate::sysparam("t", Default::default()),
            status: "PASS".into(),
            measurement_valid: true,
            kept: false,
            reason: None,
            p_value: 1.0,
            per_metric: Default::default(),
            summary: vec![],
        };
        archive_append(&rd, "com.a.game", &[item("a1"), item("a2")]);
        archive_append(&rd, "com.b.game", &[item("b1")]);
        // 塞一行坏数据：不能拖垮读取
        let p = archive_file(&rd);
        let mut c = std::fs::read_to_string(&p).unwrap();
        c.push_str("{not json}\n");
        std::fs::write(&p, c).unwrap();
        // 相邻的另一个 run 目录（模拟宕机迁移后的新目录）读到同一份档案
        let rd2 = RunDir::new(tmp.join("run-b"));
        let seed = archive_seed(&rd2, "com.a.game");
        assert_eq!(seed.len(), 2);
        assert_eq!(seed[0].candidate_id, "a1");
        assert!(archive_seed(&rd2, "com.c.game").is_empty());
        std::fs::remove_dir_all(&tmp).ok();
    }
}
