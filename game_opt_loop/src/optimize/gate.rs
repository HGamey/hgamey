//! 上机前守门: 候选过了 harness 校验之后、上真机之前, 先问一个便宜的判断
//! 「这个候选值不值得花一次真机评测」。
//!
//! ```text
//!   出方案的一方 ──► harness 校验 (白名单/目录/个数) ──► 守门 (Jev, 每候选一问, ~0.1 s)
//!                                                          │ p ≥ 阈值        │ p < 阈值
//!                                                          ▼                 ▼
//!                                                       上真机 (~22 min)   记进「本地打回」, 不上机
//! ```
//!
//! 守门只能**少评**, 不能改判: 它不碰判定, 判定仍在 phonefarm。代价是可能把一个本来会
//! 留下的候选挡在门外, 所以:
//!
//! - 缺省不开 (`--gate` 不给就和以前一样);
//! - `--gate-shadow`: 只问只记不挡, 每个候选的 p 落进证据, 等真机结论回来再算守门准不准
//!   (挡掉的里面有几个其实会 PASS);
//! - 守门自己出错 (网络、key、回包) 一律放行, 原因落证据 —— 守门坏了不该让流水线停。
//!
//! 实现上是 [`Proposer`] 的装饰器 (与 `Counting` 同一个套路): 层跑器一行不改,
//! 换谁来出方案都被同一道门守着。

use super::proposer::{screen, Candidate, HistoryItem, Input, Proposed, Proposer};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::time::Duration;

/// TypeSafe System One 接口。
pub const JEV_URL: &str = "https://api.typesafe.ai/v1/systemone";
pub const JEV_MODEL: &str = "jev-latest";
/// 缺省阈值: p(值得上机) 低于它就不上机。
pub const DEFAULT_THRESHOLD: f64 = 0.3;
/// 给守门看的历史条数上限 (最近的那几条)。
const HISTORY_TAIL: usize = 12;

/// `--gate` 相关选项。
#[derive(Debug, Clone)]
pub struct Options {
    /// `jev` (目前只有这一种)。
    pub spec: String,
    pub threshold: f64,
    /// 只问只记, 不挡。
    pub shadow: bool,
    /// key 文件。缺省: 环境 `TYPESAFE_API_KEY` (key 本身), 再缺省环境 `TYPESAFE_KEY_FILE` (key 文件路径)。
    pub key_file: Option<PathBuf>,
}

/// 一次守门判断的结果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Decision {
    pub candidate: String,
    /// p(值得上机)。守门出错时为 None。
    pub p: Option<f64>,
    /// pass / drop / shadow_drop (影子模式下本该挡的) / error_pass。
    pub action: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// 一个判断「值不值得上机」的东西。
pub trait Judge {
    fn describe(&self) -> String;
    /// p(值得上机) ∈ [0, 1]。
    fn worth(&self, state: &Value) -> Result<f64>;
}

/// TypeSafe Jev。
pub struct Jev {
    client: reqwest::blocking::Client,
    key: String,
    url: String,
    model: String,
}

impl Jev {
    pub fn new(key: String) -> Result<Self> {
        Ok(Self {
            client: reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(30))
                .build()?,
            key,
            url: JEV_URL.into(),
            model: JEV_MODEL.into(),
        })
    }

    /// 找 key: 显式文件 > 环境 `TYPESAFE_API_KEY` (key 本身) > 环境 `TYPESAFE_KEY_FILE` (文件路径)。
    /// 不写死任何家目录下的默认路径 —— 机密放在哪由部署决定 (例如 `<工作根>/.hgamey/typesafe.key`)。
    pub fn load_key(file: Option<&Path>) -> Result<String> {
        if let Some(f) = file {
            return read_key(f);
        }
        if let Ok(k) = std::env::var("TYPESAFE_API_KEY") {
            if !k.trim().is_empty() {
                return Ok(k.trim().to_string());
            }
        }
        match std::env::var("TYPESAFE_KEY_FILE") {
            Ok(f) if !f.trim().is_empty() => read_key(Path::new(f.trim())),
            _ => bail!(
                "没有守门 key: 用 --gate-key-file <文件>, 或设环境变量 TYPESAFE_API_KEY (key) / TYPESAFE_KEY_FILE (文件路径)"
            ),
        }
    }
}

fn read_key(p: &Path) -> Result<String> {
    let k =
        std::fs::read_to_string(p).with_context(|| format!("读不到守门 key {}", p.display()))?;
    let k = k.trim().to_string();
    if k.is_empty() {
        bail!("守门 key 文件是空的: {}", p.display());
    }
    Ok(k)
}

/// 问的那个问题。bool 问题在 System One 的线上类型叫 `noul`。
pub fn question() -> Value {
    json!({
        "worth_device_time": {
            "type": "noul",
            "instructions": "这是手机游戏性能优化流水线里的一个候选改动, 上真机评测一次要 20 分钟左右。\
    根据设备、这一层的判定口径、这个候选改了什么、以及这一层已经上过机的候选和真机结论, \
    判断这个候选值不值得花一次真机评测: 有合理的理由期望它能显著改善帧时/帧率/功耗且不退化, \
    并且不是已被真机否定过的候选的近似重复。",
            "criteria": {
                "true": "值得上机: 有合理的改善预期, 且不是已被否定方向的重复",
                "false": "不值得上机: 几乎是已被否定候选的重复, 或没有合理的改善预期, 或风险明显大于收益"
            }
        }
    })
}

impl Judge for Jev {
    fn describe(&self) -> String {
        format!("Jev ({})", self.model)
    }

    fn worth(&self, state: &Value) -> Result<f64> {
        let body = json!({"model": self.model, "state": state, "questions": question()});
        let resp = self
            .client
            .post(&self.url)
            .bearer_auth(&self.key)
            .json(&body)
            .send()
            .context("守门请求发不出去")?;
        let status = resp.status();
        let text = resp.text().unwrap_or_default();
        if !status.is_success() {
            bail!(
                "守门 HTTP {status}: {}",
                text.chars().take(200).collect::<String>()
            );
        }
        parse_answer(&text)
    }
}

/// System One 回包 → p。纯函数。
pub fn parse_answer(text: &str) -> Result<f64> {
    let v: Value = serde_json::from_str(text).context("守门回包不是 JSON")?;
    let a = &v["answers"]["worth_device_time"];
    if a["type"] != "noul" {
        bail!(
            "守门回包里没有 noul 答案: {}",
            text.chars().take(200).collect::<String>()
        );
    }
    let p = a["noul"].as_f64().context("noul 不是数")?;
    if !(0.0..=1.0).contains(&p) {
        bail!("noul 超出 [0,1]: {p}");
    }
    Ok(p)
}

/// 给守门看的状态: 设备、层、判定口径、这个候选、最近的历史。只用出方案输入里已有的东西。
pub fn state_for(input: &Input, c: &Candidate) -> Value {
    // ABORT (没量准) 既不是正样本也不是负样本, 不给守门看: 回放里发现它会把
    // 「和一个 ABORT 候选参数相同」当成被否定过 (docs/DESIGN_CLOUD_PROPOSER.md)。
    let valid: Vec<&HistoryItem> = input
        .history
        .iter()
        .filter(|h| h.measurement_valid)
        .collect();
    let hist: Vec<Value> = valid[valid.len().saturating_sub(HISTORY_TAIL)..]
        .iter()
        .copied()
        .map(|h: &HistoryItem| {
            json!({
                "change": h.candidate.summary(),
                "status": h.status,
                "measurement_valid": h.measurement_valid,
                "kept": h.kept,
                "reason": h.reason,
                "per_metric": h.summary,
            })
        })
        .collect();
    json!({
        "task": "手机游戏性能优化: 判断候选值不值得上真机评测",
        "device": {"model": input.device.model, "platform": input.device.platform,
                   "root": input.device.root, "note": input.device.note},
        "target": {"game": input.target.game, "workload": input.target.workload},
        "layer": input.kind.display(),
        "judging": {"metrics": input.judging.metrics, "rule": input.judging.rule},
        "candidate": {"change": c.summary(), "why": c.why},
        "history": hist,
    })
}

/// 守门装饰器。
pub struct Gated<'a> {
    inner: &'a dyn Proposer,
    judge: Box<dyn Judge + 'a>,
    threshold: f64,
    shadow: bool,
    decisions: RefCell<Vec<Decision>>,
}

impl<'a> Gated<'a> {
    pub fn new(
        inner: &'a dyn Proposer,
        judge: Box<dyn Judge + 'a>,
        threshold: f64,
        shadow: bool,
    ) -> Self {
        Self {
            inner,
            judge,
            threshold,
            shadow,
            decisions: RefCell::new(Vec::new()),
        }
    }

    /// 整程的守门记录 (进报告)。
    pub fn decisions(&self) -> Vec<Decision> {
        self.decisions.borrow().clone()
    }
}

impl Proposer for Gated<'_> {
    fn spec(&self) -> String {
        self.inner.spec()
    }

    fn describe(&self) -> String {
        format!(
            "{} + 上机前守门 {} (阈值 {:.2}{})",
            self.inner.describe(),
            self.judge.describe(),
            self.threshold,
            if self.shadow {
                ", 影子模式只记不挡"
            } else {
                ""
            }
        )
    }

    fn propose(&self, input: &Input) -> Proposed {
        let mut p = self.inner.propose(input);
        // 只问过得了 harness 校验的: 校验会挡的不必花守门的钱。
        let pass_screen: Vec<String> = screen(input, &p)
            .accepted
            .iter()
            .map(Candidate::summary)
            .collect();
        let mut kept = Vec::new();
        for c in std::mem::take(&mut p.candidates) {
            let name = c.summary();
            if !pass_screen.contains(&name) {
                kept.push(c);
                continue;
            }
            let d = match self.judge.worth(&state_for(input, &c)) {
                Ok(prob) => {
                    let low = prob < self.threshold;
                    let action = match (low, self.shadow) {
                        (false, _) => "pass",
                        (true, true) => "shadow_drop",
                        (true, false) => "drop",
                    };
                    Decision {
                        candidate: name.clone(),
                        p: Some(prob),
                        action: action.into(),
                        error: None,
                    }
                }
                Err(e) => Decision {
                    candidate: name.clone(),
                    p: None,
                    action: "error_pass".into(),
                    error: Some(format!("{e:#}")),
                },
            };
            if d.action == "drop" {
                p.rejected.push(format!(
                    "{name}: 上机前守门 p={:.2} < {:.2}, 不上机",
                    d.p.unwrap_or_default(),
                    self.threshold
                ));
            } else {
                kept.push(c);
            }
            self.decisions.borrow_mut().push(d);
        }
        p.candidates = kept;
        p
    }
}

/// 按选项建守门。
pub fn build<'a>(inner: &'a dyn Proposer, o: &Options) -> Result<Gated<'a>> {
    if !(0.0..=1.0).contains(&o.threshold) {
        bail!("--gate-threshold 要在 [0,1] 里, 收到 {}", o.threshold);
    }
    let judge: Box<dyn Judge> = match o.spec.trim() {
        "jev" => Box::new(Jev::new(Jev::load_key(o.key_file.as_deref())?)?),
        s => bail!("不认识的守门 `{s}`: 目前只有 jev"),
    };
    Ok(Gated::new(inner, judge, o.threshold, o.shadow))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize::proposer::Builtin;

    struct Fixed(Vec<Result<f64, String>>, RefCell<usize>);
    impl Judge for Fixed {
        fn describe(&self) -> String {
            "替身".into()
        }
        fn worth(&self, _s: &Value) -> Result<f64> {
            let i = *self.1.borrow();
            *self.1.borrow_mut() += 1;
            self.0[i % self.0.len()].clone().map_err(anyhow::Error::msg)
        }
    }

    fn input() -> Input {
        crate::optimize::proposer::tests_support::sysparam_input(3)
    }

    #[test]
    fn parses_the_wire_answer() {
        let p = parse_answer(
            r#"{"model":"jev-1.13.0","answers":{"worth_device_time":{"type":"noul","noul":0.39}},"usage":{"input_tokens":423,"output_tokens":22}}"#,
        )
        .unwrap();
        assert!((p - 0.39).abs() < 1e-9);
        assert!(parse_answer(r#"{"answers":{}}"#).is_err());
        assert!(
            parse_answer(r#"{"answers":{"worth_device_time":{"type":"noul","noul":1.5}}}"#)
                .is_err()
        );
    }

    #[test]
    fn drops_below_threshold_and_records_every_decision() {
        let local = Builtin::local();
        let inp = input();
        let plain = local.propose(&inp);
        assert!(plain.candidates.len() >= 2, "替身输入应出至少两个候选");
        let judge = Fixed(vec![Ok(0.9), Ok(0.1)], RefCell::new(0));
        let g = Gated::new(&local, Box::new(judge), 0.3, false);
        let p = g.propose(&inp);
        let d = g.decisions();
        assert_eq!(d.len(), plain.candidates.len());
        let dropped = d.iter().filter(|x| x.action == "drop").count();
        assert!(dropped >= 1);
        assert_eq!(p.candidates.len(), plain.candidates.len() - dropped);
        assert!(p.rejected.iter().any(|r| r.contains("上机前守门")));
    }

    #[test]
    fn shadow_mode_never_drops() {
        let local = Builtin::local();
        let inp = input();
        let n = local.propose(&inp).candidates.len();
        let g = Gated::new(
            &local,
            Box::new(Fixed(vec![Ok(0.0)], RefCell::new(0))),
            0.3,
            true,
        );
        let p = g.propose(&inp);
        assert_eq!(p.candidates.len(), n);
        assert!(g.decisions().iter().all(|d| d.action == "shadow_drop"));
    }

    #[test]
    fn a_broken_gate_lets_everything_through() {
        let local = Builtin::local();
        let inp = input();
        let n = local.propose(&inp).candidates.len();
        let g = Gated::new(
            &local,
            Box::new(Fixed(vec![Err("网络断了".into())], RefCell::new(0))),
            0.3,
            false,
        );
        assert_eq!(g.propose(&inp).candidates.len(), n);
        assert!(g
            .decisions()
            .iter()
            .all(|d| d.action == "error_pass" && d.error.is_some()));
    }

    #[test]
    fn the_state_carries_the_candidate_and_recent_history() {
        let inp = input();
        let c = Builtin::local().propose(&inp).candidates.remove(0);
        let s = state_for(&inp, &c);
        assert_eq!(s["candidate"]["change"], c.summary());
        assert!(s["judging"]["metrics"].is_array());
        assert!(s["history"].as_array().unwrap().len() <= HISTORY_TAIL);
        // ABORT 不进守门看的历史。
        let mut inp2 = input();
        let mut h = crate::optimize::proposer::tests_support::history_item(c.clone(), false);
        inp2.history.push(h.clone());
        assert_eq!(state_for(&inp2, &c)["history"].as_array().unwrap().len(), 0);
        h.measurement_valid = true;
        inp2.history.push(h);
        assert_eq!(state_for(&inp2, &c)["history"].as_array().unwrap().len(), 1);
    }

    /// 真打一次 Jev (要 key 和网络; 缺省跳过)。`GOL_LIVE_JEV=1 cargo test live_jev`。
    #[test]
    fn live_jev() {
        if std::env::var("GOL_LIVE_JEV").is_err() {
            return;
        }
        let jev = Jev::new(Jev::load_key(None).unwrap()).unwrap();
        let inp = input();
        let c = Builtin::local().propose(&inp).candidates.remove(0);
        let p = jev.worth(&state_for(&inp, &c)).unwrap();
        eprintln!("live Jev p = {p}");
        assert!((0.0..=1.0).contains(&p));
    }
}
