//! 出方案接口 (`game_opt_loop/proposer@1`): 谁来出方案可以换, 裁判和流水线不换。
//!
//! ```text
//!            Input (JSON)                                Output (JSON)
//!   harness ───────────────►  出方案的一方  ──────────────►  harness
//!   设备能力 / 计划 / 层                                    candidates[]: v2 评测信封里
//!   白名单与约束 / 冻结的判定口径                            候选那一部分 (kind + why +
//!   历史候选与真机判定                                       payload.params | payload.rewrites)
//! ```
//!
//! 出方案的一方**只**决定「这一轮改什么」。信封上其余的东西 —— 设备、负载、测试条件、
//! 统计协议、写入计划、白名单副本、`require_hit` —— 全部由 harness 填, 候选里带了这些字段
//! 整条打回。所以换一个出方案的一方, 它够不着判定: 判定在 phonefarm, 协议在看到候选
//! 之前冻结, 白名单校验在 harness 这边对**所有**出方案的一方一视同仁地再做一遍。
//!
//! 三个实现:
//!
//! | spec | 是什么 |
//! |---|---|
//! | `llm` | 内置: 大模型出方案, 挂了退回本地变异器 (改造前的缺省行为, 一字未改) |
//! | `local` | 内置: 本地确定性变异器 (= `--no-llm`), 同种子同结果 |
//! | `cmd:<可执行文件>` | 外部进程: stdin 收一份 Input, stdout 回一份 Output |
//!
//! 契约见 `contracts/proposer_input.schema.json` / `contracts/proposer_output.schema.json`,
//! 给第三方的说明见 `docs/INTEGRATION.md`。

use super::eval::{Conditions, Kind, MetricDetail, MetricSpec, Rewrite};
use super::sysparam::{self, Origin, Params, Whitelist};
use super::{caps, gray, plan};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// 协议名。Input 与 Output 都带着它, 认错了协议直接拒。
pub const PROTOCOL: &str = "game_opt_loop/proposer@1";
/// 协议版本。加字段不升版本 (上层对新字段宽容); 改已有字段的语义才升。
pub const VERSION: u32 = 1;
/// 外部进程单次出方案的超时缺省值 (秒)。
pub const DEFAULT_TIMEOUT_S: u64 = 600;

/// 「给一段系统提示词 + 用户提示词, 回一段文本」。内置大模型出方案只依赖这一点,
/// 测试里用替身就能把大模型那条路完整走一遍。
pub trait Complete {
    fn complete(&self, system: &str, user: &str) -> Result<String>;
}

impl Complete for crate::engine::generator::Generator {
    fn complete(&self, system: &str, user: &str) -> Result<String> {
        crate::engine::generator::Generator::complete(self, system, user)
    }
}

// ───────────────────────────── 输入 ─────────────────────────────

/// 交给出方案一方的全部东西。落盘成 `<out>/proposer/<层>_g<代>.input.json`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Input {
    pub protocol: String,
    pub version: u32,
    /// 这一轮要的是哪一层的候选。
    pub kind: Kind,
    /// 第几代 (从 1 起)。
    pub generation: u32,
    /// 这一代最多要几个候选。多给的 harness 不上机。
    pub n: u32,
    /// 随机种子。确定性的出方案一方应当只依赖 (输入, seed)。
    pub seed: u64,
    pub device: DeviceInfo,
    pub target: TargetInfo,
    pub plan: PlanInfo,
    /// 冻结的测试条件 (只读)。
    pub conditions: Conditions,
    /// 冻结的判定口径 (只读)。出方案的一方看得到, 改不了。
    pub judging: Judging,
    /// 这一层准改什么。
    pub constraints: Constraints,
    /// 这一层之前上过机的候选与真机判定 (按时间顺序, 含 ABORT)。
    pub history: Vec<HistoryItem>,
    /// 人的掌舵指令 (目标模式 human.md 里机读不了的部分, 原文)。**最高优先级**的偏好:
    /// 与别的考虑冲突时以它为准 (硬约束仍是白名单 / 开关目录 / 冻结判定)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub steer: Option<String>,
}

/// 设备能力 (摘自 caps.json)。`None` = 摸底时没探出来, 不是「否」。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub serial: String,
    pub platform: caps::Platform,
    pub model: Option<String>,
    pub sdk: Option<String>,
    pub root: Option<bool>,
    pub charging_suspend: Option<bool>,
    pub vulkan_layer: Option<bool>,
    pub soc_temp_c: Option<f32>,
    /// 一句话的设备说明 (内置大模型出方案时原样放进提示词)。
    pub note: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TargetInfo {
    pub game: String,
    pub package: Option<String>,
    pub box_kind: super::games::BoxKind,
    /// 一句话的负载说明 (内置大模型出方案时原样放进提示词)。
    pub workload: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlanInfo {
    pub route: String,
    pub generations: u32,
    pub children: u32,
    /// 每臂轮数。
    pub rounds: u32,
    pub carrier: super::eval::CarrierKind,
    /// 这一层估计要花的真机时间 (秒)。
    pub estimate_s: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Judging {
    pub metrics: Vec<MetricSpec>,
    pub p_threshold: f64,
    pub multiple_comparison: Option<String>,
    pub stat: Option<String>,
    pub rule: String,
}

/// 这一层准改什么。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Constraints {
    /// ③: 白名单 (参数 id → 准写的取值表)。只能从里面挑, 一个字都不能改。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub whitelist: Option<Whitelist>,
    /// ③: 温控保护关键词。命中即拒, 不看别的。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub deny_keywords: Vec<String>,
    /// ②: 改写开关目录。只有 `status = landed` 的能用。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub knobs: Option<Vec<KnobInfo>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KnobInfo {
    pub id: String,
    pub status: gray::KnobStatus,
    pub what: String,
    pub why: String,
    /// 需要范围的开关准用的范围清单 (storeop_dont_care: frame-audit 判定确定没人读的附件)。
    /// rewrite.scope 只能原样取其中一个的 `scope`。空 = 这个开关不带范围。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub scopes: Vec<gray::StoreTarget>,
}

/// 从约束里还原写回改写准用的范围清单。
pub fn gray_targets(c: &Constraints) -> Vec<gray::StoreTarget> {
    c.knobs
        .iter()
        .flatten()
        .filter(|k| k.id == "storeop_dont_care")
        .flat_map(|k| k.scopes.clone())
        .collect()
}

/// 一条历史: 上过机的一个候选, 以及真机给的结论。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HistoryItem {
    pub candidate_id: String,
    pub generation: u32,
    pub candidate: Candidate,
    /// PASS / REJECT / ABORT ...
    pub status: String,
    /// false = ABORT (没量准), 既不是正样本也不是负样本。
    pub measurement_valid: bool,
    pub kept: bool,
    pub reason: Option<String>,
    pub p_value: f64,
    /// 逐指标明细 (原样来自回包的 verdict.per_metric)。
    #[serde(default)]
    pub per_metric: BTreeMap<String, MetricDetail>,
    /// 逐指标明细的人话版 —— 就是内置大模型看到的那几行。
    #[serde(default)]
    pub summary: Vec<String>,
}

// ───────────────────────────── 输出 ─────────────────────────────

/// 一个候选: v2 评测信封里由出方案一方决定的那一部分。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Candidate {
    pub kind: Kind,
    #[serde(default)]
    pub why: String,
    pub payload: CandidatePayload,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CandidatePayload {
    Sysparam { params: Params },
    Gray { rewrites: Vec<Rewrite> },
}

impl Candidate {
    pub fn sysparam(why: impl Into<String>, params: Params) -> Self {
        Self {
            kind: Kind::Sysparam,
            why: why.into(),
            payload: CandidatePayload::Sysparam { params },
        }
    }

    pub fn gray(why: impl Into<String>, rewrites: Vec<Rewrite>) -> Self {
        Self {
            kind: Kind::Gray,
            why: why.into(),
            payload: CandidatePayload::Gray { rewrites },
        }
    }

    pub fn params(&self) -> Option<&Params> {
        match &self.payload {
            CandidatePayload::Sysparam { params } => Some(params),
            CandidatePayload::Gray { .. } => None,
        }
    }

    pub fn rewrites(&self) -> Option<&[Rewrite]> {
        match &self.payload {
            CandidatePayload::Gray { rewrites } => Some(rewrites),
            CandidatePayload::Sysparam { .. } => None,
        }
    }

    /// 从外部进程回的一条 JSON 解析候选。只认契约里的字段, 多一个都整条打回:
    /// 出方案的一方带上 `protocol` / `conditions` / `plan` / `whitelist`, 说明它在试图
    /// 碰 harness 的那一半, 这种候选不上机。
    pub fn from_value(v: &Value, want: Kind) -> Result<Self, String> {
        let obj = v.as_object().ok_or("候选不是 JSON 对象")?;
        if let Some(k) = obj.keys().find(|k| !matches!(k.as_str(), "kind" | "why" | "payload")) {
            return Err(format!(
                "候选里有契约之外的字段 `{k}` —— 候选只能带 kind / why / payload, 其余由 harness 填"
            ));
        }
        let kind: Kind = serde_json::from_value(obj.get("kind").cloned().unwrap_or(Value::Null))
            .map_err(|_| "kind 缺失或不认识 (只认 sysparam / gray)".to_string())?;
        if kind != want {
            return Err(format!(
                "这一轮要的是 {} 的候选, 回的是 {}",
                want.id(),
                kind.id()
            ));
        }
        let why = match obj.get("why") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(s)) => s.chars().take(300).collect(),
            Some(_) => return Err("why 必须是字符串".into()),
        };
        let payload = obj
            .get("payload")
            .and_then(Value::as_object)
            .ok_or("payload 缺失或不是对象")?;
        let only = |key: &str| -> Result<&Value, String> {
            if let Some(k) = payload.keys().find(|k| k.as_str() != key) {
                return Err(format!(
                    "payload 里只认 `{key}`, 多了 `{k}` —— 写入计划、白名单、require_hit 这些由 harness 填"
                ));
            }
            payload.get(key).ok_or(format!("payload 缺 `{key}`"))
        };
        match kind {
            Kind::Sysparam => {
                let raw = only("params")?.as_object().ok_or("params 必须是对象")?;
                let mut params = Params::new();
                for (k, v) in raw {
                    let s = v
                        .as_str()
                        .ok_or(format!("params.{k} 必须是字符串 (取值表里的原样文本)"))?;
                    params.insert(k.clone(), s.to_string());
                }
                Ok(Self::sysparam(why, params))
            }
            Kind::Gray => {
                let rewrites: Vec<Rewrite> = serde_json::from_value(only("rewrites")?.clone())
                    .map_err(|e| format!("rewrites 形状不对: {e}"))?;
                Ok(Self::gray(why, rewrites))
            }
            Kind::Shader => Err("optimize 不驱动 ① 画面算子层".into()),
        }
    }
}

/// 外部进程回的整份输出。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Output {
    pub protocol: String,
    pub version: u32,
    pub candidates: Vec<Value>,
    /// 出方案的一方自报的名字/版本 (只进证据)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub proposer: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
}

/// 一次出方案的结果 (还没过 harness 的校验)。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Proposed {
    pub candidates: Vec<Candidate>,
    /// 出方案的一方自己就已经打回的 (`"<改动>: <原因>"`)。外部进程回的形状不对的候选也记在这里。
    pub rejected: Vec<String>,
    pub origin: Option<Origin>,
    /// 出了什么岔子 (大模型挂了 / 外部进程退出码非 0 / 回包解析不了)。
    pub error: Option<String>,
    /// 原始回包 —— 落进证据, 不进判定。
    pub raw: Option<String>,
    /// 这次出方案调了几次大模型 (经费口径)。
    pub model_calls: u32,
}

/// 出方案的一方。
pub trait Proposer {
    /// 机器可读的名字: `llm` / `local` / `cmd:<路径>`。
    fn spec(&self) -> String;
    /// 给人看的一行 (计划与报告里用)。
    fn describe(&self) -> String;
    fn propose(&self, input: &Input) -> Proposed;
}

// ───────────────────────────── 内置 ─────────────────────────────

/// 内置出方案: 有大模型先问大模型, 挂了 / 没有就本地确定性变异器。
///
/// 逻辑一行没搬: 直接调 `sysparam::Proposer` 与 `gray::Proposer`, 只是把历史
/// 从统一的 [`HistoryItem`] 还原成它们原来吃的样子。`tests/proposer_golden.rs`
/// 钉着改造前后下发的请求逐字节一致。
pub struct Builtin {
    completer: Option<Box<dyn Complete>>,
    spec: String,
    describe: String,
}

impl Builtin {
    /// 只用本地确定性变异器 (= `--no-llm`)。
    pub fn local() -> Self {
        Self {
            completer: None,
            spec: "local".into(),
            describe: "本地确定性变异器".into(),
        }
    }

    /// 大模型 + 本地兜底。`completer` 为 None (没有可用密钥) 时整程退回本地。
    pub fn llm(completer: Option<Box<dyn Complete>>, label: impl Into<String>) -> Self {
        Self {
            completer,
            spec: "llm".into(),
            describe: label.into(),
        }
    }
}

/// [`HistoryItem`] → ③ 原来吃的历史: 只留量准了的 (ABORT 什么都没测到,
/// 当成「这组不行」会让模型学到一条不存在的规律)。
pub fn sysparam_history(items: &[HistoryItem]) -> Vec<sysparam::History> {
    items
        .iter()
        .filter(|h| h.measurement_valid)
        .filter_map(|h| {
            Some(sysparam::History {
                params: h.candidate.params()?.clone(),
                verdict: h.status.clone(),
                reason: h.reason.clone().unwrap_or_default(),
                per_metric: h.summary.clone(),
            })
        })
        .collect()
}

/// [`HistoryItem`] → ② 原来吃的历史: 不管量没量准都算「提过了」,
/// 否则下一代会原样再提一遍。
pub fn gray_history(items: &[HistoryItem]) -> Vec<sysparam::History> {
    items
        .iter()
        .filter_map(|h| {
            let knobs: Vec<String> = h.candidate.rewrites()?.iter().map(gray::rewrite_label).collect();
            Some(sysparam::History {
                params: knobs.iter().map(|k| (k.clone(), "on".to_string())).collect(),
                verdict: h.status.clone(),
                reason: format!("{} — {}", knobs.join("+"), h.candidate.why),
                per_metric: if h.measurement_valid {
                    h.summary.clone()
                } else {
                    vec!["这一轮没量准 (ABORT), 数字不算数".into()]
                },
            })
        })
        .collect()
}

impl Proposer for Builtin {
    fn spec(&self) -> String {
        self.spec.clone()
    }

    fn describe(&self) -> String {
        self.describe.clone()
    }

    fn propose(&self, input: &Input) -> Proposed {
        let generator = self.completer.as_deref();
        let model_calls = u32::from(generator.is_some());
        // 人的掌舵指令跟在负载说明后面进提示词, 标明最高优先级。
        let workload = match input.steer.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            Some(st) => format!(
                "{}\n\n# 人的掌舵指令 (最高优先级: 与上面任何考虑冲突时以这里为准; 白名单与开关目录仍是硬约束)\n{st}",
                input.target.workload
            ),
            None => input.target.workload.clone(),
        };
        match input.kind {
            Kind::Sysparam => {
                let wl = input.constraints.whitelist.clone().unwrap_or_default();
                let b = sysparam::Proposer {
                    generator,
                    seed: input.seed,
                }
                .propose(
                    &input.device.note,
                    &workload,
                    &wl,
                    &sysparam_history(&input.history),
                    input.n,
                );
                Proposed {
                    candidates: b
                        .accepted
                        .into_iter()
                        .map(|p| Candidate::sysparam(p.why, p.params))
                        .collect(),
                    rejected: b
                        .rejected
                        .iter()
                        .map(|(p, why)| format!("{p:?}: {why}"))
                        .collect(),
                    origin: b.origin,
                    error: b.llm_error,
                    raw: b.llm_raw,
                    model_calls,
                }
            }
            Kind::Gray => {
                let targets = gray_targets(&input.constraints);
                let b = gray::Proposer { generator, targets: &targets }.propose(
                    &input.device.note,
                    &workload,
                    &gray_history(&input.history),
                    input.n,
                );
                Proposed {
                    candidates: b
                        .accepted
                        .into_iter()
                        .map(|p| Candidate::gray(p.why, p.rewrites))
                        .collect(),
                    rejected: b
                        .rejected
                        .iter()
                        .map(|(k, why)| format!("{k}: {why}"))
                        .collect(),
                    origin: b.origin,
                    error: b.llm_error,
                    raw: b.llm_raw,
                    model_calls,
                }
            }
            Kind::Shader => Proposed {
                error: Some("optimize 不驱动 ① 画面算子层".into()),
                ..Default::default()
            },
        }
    }
}

// ───────────────────────────── 外部进程 ─────────────────────────────

/// 外部进程出方案: `cmd:<可执行文件> [参数...]`。
///
/// 一代调一次: stdin 写一份 [`Input`] (写完关掉), stdout 读一份 [`Output`]。
/// stdout 只能有那一个 JSON 对象, 日志请写 stderr。退出码非 0、超时、回包解析不了,
/// 都记成这一代的 `error` —— **不**退回本地变异器: 对比两个出方案的一方时,
/// 悄悄换人出主意会让对比失真。
pub struct External {
    program: String,
    args: Vec<String>,
    timeout: Duration,
    spec: String,
}

impl External {
    /// `spec` 是 `cmd:` 后面那一段, 按空白切成程序与参数 (路径里不能有空格)。
    pub fn parse(spec: &str, timeout: Duration) -> Result<Self> {
        let mut parts = spec.split_whitespace();
        let Some(program) = parts.next() else {
            bail!("`cmd:` 后面要跟一个可执行文件路径");
        };
        Ok(Self {
            program: program.to_string(),
            args: parts.map(str::to_string).collect(),
            timeout,
            spec: format!("cmd:{spec}"),
        })
    }

    fn call(&self, stdin_json: &str) -> Result<(Option<i32>, String, String)> {
        let mut child = Command::new(&self.program)
            .args(&self.args)
            .env("GOL_PROPOSER_PROTOCOL", PROTOCOL)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .with_context(|| format!("起不来外部出方案进程 `{}`", self.program))?;

        // 写 stdin、读 stdout/stderr 各走一个线程: 输入大 (白名单 + 历史) 而对方
        // 先往 stdout 写的话, 单线程顺序读写会互相等死。
        let mut stdin = child.stdin.take().expect("stdin piped");
        let input = stdin_json.to_string();
        let writer = std::thread::spawn(move || {
            let _ = stdin.write_all(input.as_bytes());
        });
        let mut out = child.stdout.take().expect("stdout piped");
        let reader = std::thread::spawn(move || {
            let mut s = String::new();
            let _ = out.read_to_string(&mut s);
            s
        });
        let mut err = child.stderr.take().expect("stderr piped");
        let ereader = std::thread::spawn(move || {
            let mut s = String::new();
            let _ = err.read_to_string(&mut s);
            s
        });

        let start = Instant::now();
        let status = loop {
            if let Some(st) = child.try_wait()? {
                break st;
            }
            if start.elapsed() > self.timeout {
                let _ = child.kill();
                let _ = child.wait();
                bail!("外部出方案进程超过 {} 秒没回, 已杀掉", self.timeout.as_secs());
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let _ = writer.join();
        let stdout = reader.join().unwrap_or_default();
        let stderr = ereader.join().unwrap_or_default();
        Ok((status.code(), stdout, stderr))
    }
}

/// 外部进程的 stdout → 候选。纯函数。
pub fn parse_output(stdout: &str, want: Kind) -> Result<(Vec<Candidate>, Vec<String>), String> {
    let out: Output = serde_json::from_str(stdout.trim()).map_err(|e| {
        format!(
            "stdout 不是一份 proposer@1 输出 ({e}); 开头是: {}",
            stdout.trim().chars().take(200).collect::<String>()
        )
    })?;
    if out.protocol != PROTOCOL || out.version != VERSION {
        return Err(format!(
            "协议对不上: 回的是 {}/v{}, 要的是 {PROTOCOL}/v{VERSION}",
            out.protocol, out.version
        ));
    }
    let mut ok = Vec::new();
    let mut bad = Vec::new();
    for (i, v) in out.candidates.iter().enumerate() {
        match Candidate::from_value(v, want) {
            Ok(c) => ok.push(c),
            Err(e) => bad.push(format!("候选 #{}: {e}", i + 1)),
        }
    }
    Ok((ok, bad))
}

impl Proposer for External {
    fn spec(&self) -> String {
        self.spec.clone()
    }

    fn describe(&self) -> String {
        format!("外部进程 {} ({PROTOCOL}, stdin/stdout JSON)", self.spec)
    }

    fn propose(&self, input: &Input) -> Proposed {
        let mut p = Proposed {
            origin: Some(Origin::External),
            ..Default::default()
        };
        let json = match serde_json::to_string(input) {
            Ok(j) => j,
            Err(e) => {
                p.error = Some(format!("输入序列化失败: {e}"));
                return p;
            }
        };
        let (code, stdout, stderr) = match self.call(&json) {
            Ok(v) => v,
            Err(e) => {
                p.error = Some(e.to_string());
                return p;
            }
        };
        p.raw = Some(stdout.clone());
        if code != Some(0) {
            p.error = Some(format!(
                "外部出方案进程退出码 {code:?}: {}",
                stderr.lines().rev().take(5).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join(" | ")
            ));
            return p;
        }
        match parse_output(&stdout, input.kind) {
            Ok((cands, bad)) => {
                p.candidates = cands;
                p.rejected = bad;
            }
            Err(e) => p.error = Some(e),
        }
        p
    }
}

// ───────────────────────────── harness 侧 ─────────────────────────────

/// 过了 harness 校验的结果。
#[derive(Debug, Clone, Default)]
pub struct Screened {
    pub accepted: Vec<Candidate>,
    /// `"<改动>: <原因>"`, 出方案一方自己打回的在前。
    pub rejected: Vec<String>,
}

/// harness 对**所有**出方案的一方一视同仁的校验: 白名单 / 开关目录 / 个数。
///
/// 内置的那两个在自己内部已经校验过一遍 (它们要靠校验结果决定退不退回本地),
/// 这里再过一遍是冗余的 —— 故意的: 校验在流水线上而不在出方案的一方手里,
/// 换谁来出方案都绕不过去。
pub fn screen(input: &Input, proposed: &Proposed) -> Screened {
    let mut s = Screened {
        accepted: Vec::new(),
        rejected: proposed.rejected.clone(),
    };
    for c in &proposed.candidates {
        let verdict = match (&c.payload, input.kind) {
            (CandidatePayload::Sysparam { params }, Kind::Sysparam) if c.kind == Kind::Sysparam => {
                let wl = input.constraints.whitelist.clone().unwrap_or_default();
                sysparam::validate(params, &wl).map_err(|e| format!("{params:?}: {e}"))
            }
            (CandidatePayload::Gray { rewrites }, Kind::Gray) if c.kind == Kind::Gray => {
                gray::validate(rewrites, &gray_targets(&input.constraints)).map_err(|e| {
                    format!(
                        "{}: {e}",
                        rewrites.iter().map(gray::rewrite_label).collect::<Vec<_>>().join("+")
                    )
                })
            }
            _ => Err(format!(
                "{:?}: 这一轮要的是 {} 的候选",
                c.payload,
                input.kind.id()
            )),
        };
        match verdict {
            Ok(()) if s.accepted.len() < input.n as usize => s.accepted.push(c.clone()),
            Ok(()) => s.rejected.push(format!(
                "{}: 超出这一代要的 {} 个, 不上机",
                c.summary(),
                input.n
            )),
            Err(e) => s.rejected.push(e),
        }
    }
    s
}

impl Candidate {
    /// 一行说清改了什么。
    pub fn summary(&self) -> String {
        match &self.payload {
            CandidatePayload::Sysparam { params } => params
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join(", "),
            CandidatePayload::Gray { rewrites } => rewrites
                .iter()
                .map(gray::rewrite_label)
                .collect::<Vec<_>>()
                .join("+"),
        }
    }
}

/// 一层的出方案约束 (由 harness 从白名单 / 开关目录生成)。
pub fn constraints_for(kind: Kind, wl: Option<&Whitelist>) -> Constraints {
    constraints_with_targets(kind, wl, &[])
}

/// 同 [`constraints_for`], ② 层带上写回改写准用的范围清单。
pub fn constraints_with_targets(
    kind: Kind,
    wl: Option<&Whitelist>,
    targets: &[gray::StoreTarget],
) -> Constraints {
    match kind {
        Kind::Sysparam => Constraints {
            whitelist: wl.cloned(),
            deny_keywords: sysparam::DENY_KEYWORDS.iter().map(|s| s.to_string()).collect(),
            knobs: None,
        },
        Kind::Gray => Constraints {
            whitelist: None,
            deny_keywords: Vec::new(),
            knobs: Some(
                gray::CATALOG
                    .iter()
                    .map(|k| KnobInfo {
                        id: k.id.into(),
                        status: k.status,
                        what: k.what.into(),
                        why: k.why.into(),
                        scopes: if k.needs_scope { targets.to_vec() } else { vec![] },
                    })
                    .collect(),
            ),
        },
        Kind::Shader => Constraints::default(),
    }
}

/// 冻结的判定口径 → 给出方案一方看的那一份。
pub fn judging_for(p: &super::eval::Protocol) -> Judging {
    Judging {
        metrics: p.metrics.clone().unwrap_or_default(),
        p_threshold: p.p_threshold,
        multiple_comparison: p.multiple_comparison.clone(),
        stat: p.stat.clone(),
        rule: "每个候选与对照 A/B 交替各跑 rounds 轮, 在 phonefarm 端做统计检验: \
               任一指标显著改善且没有任何指标显著变差 = 保留, 否则淘汰; constraint=true 的指标\
               只许不变差。ABORT = 这次没量准, 不算样本。判定在接收端, 出方案的一方改不了。"
            .into(),
    }
}

/// 按 spec 建出方案的一方。
///
/// - `llm`: 内置, 按 secrets 挑大模型; 没有可用密钥时整程退回本地变异器 (与改造前一致)。
/// - `local`: 内置, 本地确定性变异器。
/// - `cmd:<可执行文件> [参数...]`: 外部进程。
pub fn from_spec(
    spec: &str,
    secrets: &Path,
    llm_provider: Option<&str>,
    llm_timeout_s: Option<u64>,
    timeout: Duration,
) -> Result<Box<dyn Proposer>> {
    let spec = spec.trim();
    if spec == "local" {
        return Ok(Box::new(Builtin::local()));
    }
    if spec == "llm" {
        let secrets = crate::engine::generator::load_secrets(secrets);
        return Ok(Box::new(
            match crate::engine::generator::Generator::from_secrets_with_timeout(
                &secrets,
                llm_provider,
                llm_timeout_s,
            ) {
                Ok(g) => {
                    let label = format!("大模型 {} (挂了退回本地变异器)", g.provider_name());
                    Builtin::llm(Some(Box::new(g)), label)
                }
                Err(e) => Builtin::llm(
                    None,
                    format!("大模型不可用 ({e}) → 全程走本地变异器"),
                ),
            },
        ));
    }
    if let Some(rest) = spec.strip_prefix("cmd:") {
        let ext = External::parse(rest, timeout)?;
        let prog = PathBuf::from(&ext.program);
        if prog.components().count() > 1 && !prog.is_file() {
            bail!("外部出方案程序不存在: {}", prog.display());
        }
        return Ok(Box::new(ext));
    }
    bail!("不认识的出方案 `{spec}`: 只认 llm / local / cmd:<可执行文件>")
}

/// 计划里「谁来出主意」那一行, 不必真的把出方案的一方建出来 (`--plan` 不该去碰密钥或起进程)。
pub fn describe_spec(spec: &str) -> String {
    match spec.trim() {
        "llm" => "大模型 (挂了退回本地变异器)".into(),
        "local" => "本地确定性变异器".into(),
        s => match s.strip_prefix("cmd:") {
            Some(rest) => format!("外部进程 cmd:{rest} ({PROTOCOL})"),
            None => format!("不认识的出方案 `{s}`"),
        },
    }
}

/// 把一次出方案的输入与结果落盘 (`<dir>/<层>_g<代>.{input,output}.json`)。
/// 证据用: 第三方接入出问题时, 拿着这两份文件就能离线复现。
pub fn write_evidence(dir: &Path, input: &Input, proposed: &Proposed, screened: &Screened) {
    let _ = std::fs::create_dir_all(dir);
    let stem = format!("{}_g{}", input.kind.id(), input.generation);
    let _ = std::fs::write(
        dir.join(format!("{stem}.input.json")),
        serde_json::to_string_pretty(input).unwrap_or_default(),
    );
    let out = serde_json::json!({
        "proposed": proposed,
        "accepted": screened.accepted,
        "rejected": screened.rejected,
    });
    let _ = std::fs::write(
        dir.join(format!("{stem}.output.json")),
        serde_json::to_string_pretty(&out).unwrap_or_default(),
    );
}

/// `optimize` 的出方案选项 (`--proposer` / `--proposer-timeout-s`)。
#[derive(Debug, Clone)]
pub struct Options {
    /// 空 = 按 `--no-llm` 选内置 (`llm` 或 `local`), 与接口改造之前一致。
    pub specs: Vec<String>,
    /// 外部出方案进程单次超时 (秒)。
    pub timeout_s: u64,
    /// 上机前守门 (`--gate jev`), 缺省不开。见 [`super::gate`]。
    pub gate: Option<super::gate::Options>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            specs: Vec::new(),
            timeout_s: DEFAULT_TIMEOUT_S,
            gate: None,
        }
    }
}

impl Options {
    /// 这一次用哪个出方案的一方 (一次只许一个)。
    pub fn spec(&self, use_llm: bool) -> Result<String> {
        match self.specs.as_slice() {
            [] => Ok(if use_llm { "llm" } else { "local" }.to_string()),
            [one] if one.trim() == "llm" && !use_llm => {
                bail!("--no-llm 与 --proposer llm 互相矛盾")
            }
            [one] => Ok(one.trim().to_string()),
            many => bail!(
                "optimize::run 一次只用一个出方案的一方, 收到 {} 个 (多个请走对比模式 compare::run): {}",
                many.len(),
                many.join(", ")
            ),
        }
    }

    /// 计划里「谁来出主意」那一行。None = 没显式指定, 沿用计划自己的说法。
    pub fn plan_line(&self) -> Option<String> {
        match self.specs.as_slice() {
            [one] => Some(describe_spec(one)),
            _ => None,
        }
    }
}

/// 一次跑下来出方案一方的账: 是谁、问了几次、其中调了几次大模型。进 report.json。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Summary {
    pub spec: String,
    pub describe: String,
    pub calls: u32,
    pub model_calls: u32,
    /// 上机前守门的每一次判断 (开了 `--gate` 才有)。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gate: Vec<super::gate::Decision>,
}

/// 记账用的包装: 对内原样转交, 对外多数几个数。
pub struct Counting<'a> {
    inner: &'a dyn Proposer,
    calls: std::cell::Cell<u32>,
    model_calls: std::cell::Cell<u32>,
}

impl<'a> Counting<'a> {
    pub fn new(inner: &'a dyn Proposer) -> Self {
        Self {
            inner,
            calls: Default::default(),
            model_calls: Default::default(),
        }
    }

    pub fn summary(&self) -> Summary {
        Summary {
            spec: self.inner.spec(),
            describe: self.inner.describe(),
            calls: self.calls.get(),
            model_calls: self.model_calls.get(),
            gate: Vec::new(),
        }
    }
}

impl Proposer for Counting<'_> {
    fn spec(&self) -> String {
        self.inner.spec()
    }
    fn describe(&self) -> String {
        self.inner.describe()
    }
    fn propose(&self, input: &Input) -> Proposed {
        let p = self.inner.propose(input);
        self.calls.set(self.calls.get() + 1);
        self.model_calls.set(self.model_calls.get() + p.model_calls);
        p
    }
}

/// 一代出方案的结果, 已过 harness 校验。`T` 是这一层原来用的候选类型,
/// 层跑器拿到的东西与改造前同形 (`accepted` / `rejected` / `origin` / `llm_error`)。
#[derive(Debug, Clone)]
pub struct Batch<T> {
    pub accepted: Vec<T>,
    /// `"<改动>: <原因>"`。
    pub rejected: Vec<String>,
    pub origin: Option<Origin>,
    /// 出方案出了什么岔子 (大模型挂了 / 外部进程出错)。字段名沿用改造前。
    pub llm_error: Option<String>,
}

/// 出一代: 问出方案的一方 → harness 校验 → 输入与结果落进 `evidence_dir`。
pub fn generation(p: &dyn Proposer, input: &Input, evidence_dir: &Path) -> Batch<Candidate> {
    let proposed = p.propose(input);
    let screened = screen(input, &proposed);
    write_evidence(evidence_dir, input, &proposed, &screened);
    Batch {
        accepted: screened.accepted,
        rejected: screened.rejected,
        origin: proposed.origin,
        llm_error: proposed.error,
    }
}

impl Batch<Candidate> {
    fn map<T>(self, f: impl Fn(Candidate, Origin) -> Option<T>) -> Batch<T> {
        let origin = self.origin.unwrap_or(Origin::LocalMutation);
        Batch {
            accepted: self.accepted.into_iter().filter_map(|c| f(c, origin)).collect(),
            rejected: self.rejected,
            origin: self.origin,
            llm_error: self.llm_error,
        }
    }

    /// 还原成 ③ 原来的候选类型。
    pub fn sysparam(self) -> Batch<sysparam::Proposal> {
        self.map(|c, origin| match c.payload {
            CandidatePayload::Sysparam { params } => Some(sysparam::Proposal {
                why: c.why,
                params,
                origin,
            }),
            CandidatePayload::Gray { .. } => None,
        })
    }

    /// 还原成 ② 原来的候选类型。
    pub fn gray(self) -> Batch<gray::Proposal> {
        self.map(|c, origin| match c.payload {
            CandidatePayload::Gray { rewrites } => Some(gray::Proposal {
                why: c.why,
                rewrites,
                origin,
            }),
            CandidatePayload::Sysparam { .. } => None,
        })
    }
}

/// 一个上过机的候选 → 一条出方案历史。
pub fn history_item(
    id: &str,
    generation: u32,
    candidate: Candidate,
    rep: &super::eval::Report,
    summary: Vec<String>,
) -> HistoryItem {
    HistoryItem {
        candidate_id: id.to_string(),
        generation,
        candidate,
        status: rep.status.clone(),
        measurement_valid: rep.is_measurement_valid(),
        kept: rep.kept(),
        reason: rep.verdict.reason.clone(),
        p_value: rep.verdict.p_value,
        per_metric: rep.verdict.per_metric.clone().unwrap_or_default(),
        summary,
    }
}

/// 一次出方案的输入骨架 (不含约束与历史, 由层跑器补)。
#[allow(clippy::too_many_arguments)]
pub fn input_for(
    step: &plan::Step,
    caps: &caps::Caps,
    plan: &plan::Plan,
    generation: u32,
    seed: u64,
    device_note: String,
    workload_note: String,
    constraints: Constraints,
    history: Vec<HistoryItem>,
) -> Input {
    let d = &caps.device;
    Input {
        protocol: PROTOCOL.into(),
        version: VERSION,
        kind: step.layer,
        generation,
        n: step.children,
        seed,
        device: DeviceInfo {
            serial: d.serial.clone(),
            platform: d.platform,
            model: d.model.clone(),
            sdk: d.sdk.clone(),
            root: d.root.value,
            charging_suspend: d.charging_suspend.value,
            vulkan_layer: d.vulkan_layer.value,
            soc_temp_c: d.soc_temp_c,
            note: device_note,
        },
        target: TargetInfo {
            game: caps.target.display.clone(),
            package: caps.target.package.clone(),
            box_kind: caps.carrier.box_kind,
            workload: workload_note,
        },
        plan: PlanInfo {
            route: step.route.clone(),
            generations: step.generations,
            children: step.children,
            rounds: step.rounds,
            carrier: step.carrier,
            estimate_s: step.estimate_s,
        },
        conditions: plan.conditions.clone(),
        judging: judging_for(&plan::protocol_for(step, caps)),
        constraints,
        history,
        steer: None,
    }
}

/// 给本 crate 其它模块的单测用的输入 (与本模块单测同一份)。
#[cfg(test)]
pub(crate) mod tests_support {
    pub fn sysparam_input(n: u32) -> super::Input {
        super::tests::input(super::Kind::Sysparam, n, 20260923, vec![])
    }

    pub fn history_item(candidate: super::Candidate, measurement_valid: bool) -> super::HistoryItem {
        super::HistoryItem {
            candidate_id: "h1".into(),
            generation: 1,
            candidate,
            status: if measurement_valid { "REJECT" } else { "ABORT" }.into(),
            measurement_valid,
            kept: false,
            reason: None,
            p_value: 1.0,
            per_metric: Default::default(),
            summary: vec![],
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    const PROBE: &str = "\
cpu.policies=0
cpu.policy0.avail_freqs=300000 700000 1200000
cpu.policy0.scaling_min_freq.writable=yes
cpu.policy0.scaling_min_freq.effect=live
cpu.policy0.scaling_min_freq.cur=300000
gpu.num_pwrlevels=5
gpu.min_pwrlevel.writable=yes
gpu.min_pwrlevel.effect=live
gpu.min_pwrlevel.cur=4
bus.DDR.boost_freq.writable=yes
bus.DDR.boost_freq.effect=live
bus.DDR.boost_freq.cur=547000
bus.DDR.avail_freqs=547000 1555000 5333000
";

    fn wl() -> Whitelist {
        sysparam::build_whitelist(&sysparam::parse_probe(PROBE))
    }

    pub(crate) fn input(kind: Kind, n: u32, seed: u64, history: Vec<HistoryItem>) -> Input {
        Input {
            protocol: PROTOCOL.into(),
            version: VERSION,
            kind,
            generation: 1,
            n,
            seed,
            device: DeviceInfo {
                serial: "S".into(),
                platform: caps::Platform::Adb,
                model: Some("NX809J".into()),
                sdk: None,
                root: Some(true),
                charging_suspend: Some(true),
                vulkan_layer: Some(true),
                soc_temp_c: Some(41.3),
                note: "红魔 NX809J".into(),
            },
            target: TargetInfo {
                game: "原神".into(),
                package: Some("com.miHoYo.Yuanshen".into()),
                box_kind: super::super::games::BoxKind::BlackBox,
                workload: "转视角 36 秒".into(),
            },
            plan: PlanInfo {
                route: "r".into(),
                generations: 1,
                children: n,
                rounds: 5,
                carrier: super::super::eval::CarrierKind::GameReplay,
                estimate_s: 0,
            },
            conditions: super::super::plan::conditions_from(&dummy_caps()),
            judging: judging_for(&super::super::eval::Protocol::for_kind(kind, 5, 36, 40.0)),
            constraints: constraints_for(kind, Some(&wl())),
            history,
            steer: None,
        }
    }

    fn dummy_caps() -> caps::Caps {
        let target = super::super::games::resolve(
            "原神",
            None,
            &super::super::games::CarrierRoots {
                carriers_dir: "/nonexistent".into(),
                siblings_dir: "/nonexistent".into(),
            },
        );
        caps::assemble(&target, "S", "root_uid=0\nzone=cpu-0-0 41300\n")
    }

    /// 改造前的入口 (`sysparam::Proposer` 直接调) 与新接口下的 `local` 同种子同结果。
    #[test]
    fn local_sysparam_through_the_interface_equals_the_old_entry_point() {
        for seed in [1u64, 7, 42, 20260923] {
            let old = sysparam::Proposer {
                generator: None,
                seed,
            }
            .propose("红魔 NX809J", "转视角 36 秒", &wl(), &[], 3);
            let new = Builtin::local().propose(&input(Kind::Sysparam, 3, seed, vec![]));
            let old_params: Vec<&Params> = old.accepted.iter().map(|p| &p.params).collect();
            let new_params: Vec<&Params> = new.candidates.iter().filter_map(|c| c.params()).collect();
            assert_eq!(old_params, new_params, "seed {seed}");
            assert_eq!(new.origin, Some(Origin::LocalMutation));
            assert_eq!(new.model_calls, 0);
            // 再跑一遍, 一字不差。
            assert_eq!(
                new.candidates,
                Builtin::local().propose(&input(Kind::Sysparam, 3, seed, vec![])).candidates
            );
        }
    }

    #[test]
    fn local_gray_through_the_interface_equals_the_old_entry_point() {
        let old = gray::Proposer { generator: None, targets: &[] }.propose("d", "w", &[], 2);
        let new = Builtin::local().propose(&input(Kind::Gray, 2, 0, vec![]));
        let old_k: Vec<&Vec<Rewrite>> = old.accepted.iter().map(|p| &p.rewrites).collect();
        let new_k: Vec<&[Rewrite]> = new.candidates.iter().filter_map(|c| c.rewrites()).collect();
        assert_eq!(old_k.len(), new_k.len());
        for (a, b) in old_k.iter().zip(new_k) {
            assert_eq!(a.as_slice(), b);
        }
    }

    /// 替身大模型: 记下收到的提示词, 回一段固定文本。
    struct Fake {
        reply: Result<String, String>,
        seen: Arc<Mutex<Vec<String>>>,
    }

    impl Complete for Fake {
        fn complete(&self, system: &str, user: &str) -> Result<String> {
            self.seen.lock().unwrap().push(format!("{system}\n---\n{user}"));
            self.reply.clone().map_err(|e| anyhow::anyhow!(e))
        }
    }

    fn fake(reply: Result<&str, &str>) -> (Builtin, Arc<Mutex<Vec<String>>>) {
        let seen = Arc::new(Mutex::new(Vec::new()));
        let b = Builtin::llm(
            Some(Box::new(Fake {
                reply: reply.map(str::to_string).map_err(str::to_string),
                seen: seen.clone(),
            })),
            "替身",
        );
        (b, seen)
    }

    /// 大模型那条路: 提示词与改造前同一个函数生成, 合法的收、越界的打回且原因不变。
    #[test]
    fn the_llm_path_sends_the_same_prompt_and_screens_the_same_way() {
        let (b, seen) = fake(Ok(r#"[{"why":"抬总线下限","params":{"bus.DDR.boost_freq":"5333000"}},
                                    {"why":"越界","params":{"bus.DDR.boost_freq":"9"}}]"#));
        let hist = vec![HistoryItem {
            candidate_id: "sys_g1c1".into(),
            generation: 1,
            candidate: Candidate::sysparam("x", Params::from([("gpu.min_pwrlevel".into(), "2".into())])),
            status: "REJECT".into(),
            measurement_valid: true,
            kept: false,
            reason: Some("没有任何指标显著改善".into()),
            p_value: 0.31,
            per_metric: BTreeMap::new(),
            summary: vec!["frame_p95: -0.91% (p=0.3143) 无显著变化".into()],
        }];
        let inp = input(Kind::Sysparam, 3, 1, hist.clone());
        let p = b.propose(&inp);
        assert_eq!(p.origin, Some(Origin::Llm));
        assert_eq!(p.model_calls, 1);
        assert_eq!(p.candidates.len(), 1);
        assert_eq!(p.rejected.len(), 1);
        assert!(p.rejected[0].contains("不在合法取值表"), "{:?}", p.rejected);

        let want = format!(
            "{}\n---\n{}",
            sysparam::SYSTEM_PROMPT,
            sysparam::user_prompt("红魔 NX809J", "转视角 36 秒", &wl(), &sysparam_history(&hist), 3)
        );
        assert_eq!(seen.lock().unwrap()[0], want);

        let s = screen(&inp, &p);
        assert_eq!(s.accepted.len(), 1);
        assert_eq!(s.rejected, p.rejected, "harness 再校验一遍不该改变结论");
    }

    /// 大模型挂了 → 这一代退回本地变异器, 产出与纯本地同种子一致。
    #[test]
    fn a_failing_llm_falls_back_to_the_same_local_output() {
        let (b, _) = fake(Err("429 限流"));
        let p = b.propose(&input(Kind::Sysparam, 3, 42, vec![]));
        assert_eq!(p.origin, Some(Origin::LocalMutation));
        assert_eq!(p.error.as_deref(), Some("429 限流"));
        assert_eq!(
            p.candidates,
            Builtin::local().propose(&input(Kind::Sysparam, 3, 42, vec![])).candidates
        );
    }

    /// ABORT 不进 ③ 的历史, 但进 ② 的历史 (改造前就是这样)。
    #[test]
    fn history_is_restored_the_way_each_layer_used_to_see_it() {
        let aborted = HistoryItem {
            candidate_id: "g".into(),
            generation: 1,
            candidate: Candidate::gray(
                "省带宽",
                vec![Rewrite {
                    knob: "loadop_dont_care".into(),
                    scope: None,
                    args: None,
                }],
            ),
            status: "ABORT".into(),
            measurement_valid: false,
            kept: false,
            reason: None,
            p_value: 1.0,
            per_metric: BTreeMap::new(),
            summary: vec![],
        };
        let g = gray_history(std::slice::from_ref(&aborted));
        assert_eq!(g.len(), 1);
        assert_eq!(g[0].reason, "loadop_dont_care — 省带宽");
        assert_eq!(g[0].per_metric, vec!["这一轮没量准 (ABORT), 数字不算数".to_string()]);
        assert!(sysparam_history(&[aborted]).is_empty());
    }

    #[test]
    fn external_output_is_parsed_strictly() {
        let ok = r#"{"protocol":"game_opt_loop/proposer@1","version":1,"candidates":[
            {"kind":"sysparam","why":"抬总线","payload":{"params":{"bus.DDR.boost_freq":"5333000"}}},
            {"kind":"sysparam","payload":{"params":{"a":"1"},"plan":[]}},
            {"kind":"sysparam","payload":{"params":{"a":"1"}},"protocol":{"p_threshold":1}},
            {"kind":"gray","payload":{"rewrites":[{"knob":"loadop_dont_care"}]}},
            {"kind":"sysparam","payload":{"params":{"bus.DDR.boost_freq":5333000}}}
        ]}"#;
        let (c, bad) = parse_output(ok, Kind::Sysparam).unwrap();
        assert_eq!(c.len(), 1);
        assert_eq!(c[0].params().unwrap()["bus.DDR.boost_freq"], "5333000");
        assert_eq!(bad.len(), 4, "{bad:?}");
        assert!(bad[0].contains("plan"), "{bad:?}");
        assert!(bad[1].contains("protocol"), "{bad:?}");
        assert!(bad[2].contains("gray"), "{bad:?}");
        assert!(bad[3].contains("字符串"), "{bad:?}");

        assert!(parse_output("日志\n{}", Kind::Sysparam).is_err());
        assert!(parse_output(
            r#"{"protocol":"other@1","version":1,"candidates":[]}"#,
            Kind::Sysparam
        )
        .unwrap_err()
        .contains("协议对不上"));
    }

    /// harness 的校验对外部进程一样生效: 白名单外的、温控的、超额的一律不上机。
    #[test]
    fn screening_applies_to_every_proposer() {
        let inp = input(Kind::Sysparam, 1, 0, vec![]);
        let p = Proposed {
            candidates: vec![
                Candidate::sysparam("", Params::from([("thermal.zone0.trip".into(), "1".into())])),
                Candidate::sysparam("", Params::from([("bus.DDR.boost_freq".into(), "5333000".into())])),
                Candidate::sysparam("", Params::from([("bus.DDR.boost_freq".into(), "1555000".into())])),
                Candidate::gray("", vec![]),
            ],
            origin: Some(Origin::External),
            ..Default::default()
        };
        let s = screen(&inp, &p);
        assert_eq!(s.accepted.len(), 1);
        assert_eq!(s.rejected.len(), 3, "{:?}", s.rejected);
        assert!(s.rejected[0].contains("不在白名单"));
        assert!(s.rejected[1].contains("超出这一代要的 1 个"));
        assert!(s.rejected[2].contains("这一轮要的是 sysparam"));
    }

    /// 外部进程端到端: 真起一个 sh 脚本, stdin 进 stdout 出。
    #[cfg(unix)]
    #[test]
    fn an_external_process_round_trips() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("gol_ext_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("p.sh");
        std::fs::write(
            &script,
            "#!/bin/sh\ncat > \"$(dirname \"$0\")/seen.json\"\necho 日志写 stderr >&2\n\
             echo '{\"protocol\":\"game_opt_loop/proposer@1\",\"version\":1,\"candidates\":[\
             {\"kind\":\"sysparam\",\"why\":\"固定给一个\",\"payload\":{\"params\":{\"gpu.min_pwrlevel\":\"2\"}}}]}'\n",
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();

        let ext = External::parse(&script.display().to_string(), Duration::from_secs(10)).unwrap();
        let inp = input(Kind::Sysparam, 1, 0, vec![]);
        let p = ext.propose(&inp);
        assert!(p.error.is_none(), "{:?}", p.error);
        assert_eq!(p.origin, Some(Origin::External));
        assert_eq!(p.candidates.len(), 1);
        let s = screen(&inp, &p);
        assert_eq!(s.accepted.len(), 1);

        // 对方收到的就是完整的 Input。
        let seen: Input =
            serde_json::from_str(&std::fs::read_to_string(dir.join("seen.json")).unwrap()).unwrap();
        assert_eq!(seen.protocol, PROTOCOL);
        assert!(seen.constraints.whitelist.unwrap().contains_key("bus.DDR.boost_freq"));

        // 退出码非 0 → error, 不退回本地。
        std::fs::write(&script, "#!/bin/sh\necho 坏了 >&2\nexit 3\n").unwrap();
        let p = ext.propose(&inp);
        assert!(p.candidates.is_empty());
        assert!(p.error.unwrap().contains("坏了"));

        // 超时 → 杀掉。
        std::fs::write(&script, "#!/bin/sh\nsleep 5\n").unwrap();
        let slow = External::parse(&script.display().to_string(), Duration::from_millis(300)).unwrap();
        assert!(slow.propose(&inp).error.unwrap().contains("超过"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn unknown_specs_are_refused() {
        let t = Duration::from_secs(1);
        let p = Path::new("/nonexistent/secrets.env");
        assert!(from_spec("gpt", p, None, None, t).is_err());
        assert!(from_spec("cmd:", p, None, None, t).is_err());
        assert!(from_spec("cmd:/nonexistent/x.sh", p, None, None, t).is_err());
        assert_eq!(from_spec("local", p, None, None, t).unwrap().spec(), "local");
        // 没有密钥时 llm 整程退回本地, 与改造前一致。
        let l = from_spec("llm", p, None, None, t).unwrap();
        assert_eq!(l.spec(), "llm");
        assert!(l.describe().contains("本地变异器"));
    }
}
