//! goal.md: YAML front-matter (结构化目标) + 正文 (大白话, 给出方案的大模型读)。
//!
//! 格式见 `~/projects/_wt/goal-spec/GOAL_SPEC.md` 与 `contracts/goal.schema.json`。

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

/// 主指标。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Objective {
    /// power_watt | frame_p95 | fps_mean | gpu_busy_ms
    pub metric: String,
    /// lower | higher
    pub direction: String,
    /// 期望改善幅度 (百分比); 达到即可提前结束。
    pub target_pct: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Budget {
    pub wall_minutes: u64,
    pub max_generations: u32,
    #[serde(default = "one")]
    pub min_generations: u32,
}

fn one() -> u32 {
    1
}
fn any() -> String {
    "any".into()
}
fn default_layers() -> Vec<String> {
    vec!["sysparam".into(), "gray".into()]
}
fn llm() -> String {
    "llm".into()
}
fn off() -> String {
    "off".into()
}
fn two() -> u32 {
    2
}

/// goal.md 的 front-matter。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GoalSpec {
    /// 包名。
    pub game: String,
    /// any = 空闲的同型号; 或者一个序列号。
    #[serde(default = "any")]
    pub device: String,
    pub objective: Objective,
    /// 不能变差的东西, 每项一个单键映射: `- fps_mean: no_worse` / `- image: psnr_min_db 30`。
    #[serde(default)]
    pub constraints: Vec<serde_yaml::Mapping>,
    /// 允许动的层: sysparam / gray / upop。
    #[serde(default = "default_layers")]
    pub layers: Vec<String>,
    /// 明确禁止的旋钮或节点 (参数 id / 写回范围 / 开关名)。
    #[serde(default)]
    pub forbid: Vec<String>,
    pub budget: Budget,
    /// 任一满足就停: target_reached / budget_exhausted / {no_gain: N}。
    #[serde(default)]
    pub stop_when: Vec<serde_yaml::Value>,
    /// llm | local | cloud
    #[serde(default = "llm")]
    pub proposer: String,
    /// off | shadow | jev
    #[serde(default = "off")]
    pub gate: String,
    /// 每一代每层上机几个候选 (规格 v1 没写, 缺省 2)。
    #[serde(default = "two")]
    pub per_generation: u32,
}

/// 读出来的整份 goal.md。
#[derive(Debug, Clone)]
pub struct GoalDoc {
    pub spec: GoalSpec,
    /// 正文 (front-matter 之后的大白话)。
    pub body: String,
}

pub const METRICS: [&str; 4] = ["power_watt", "frame_p95", "fps_mean", "gpu_busy_ms"];
pub const LAYERS: [&str; 4] = ["sysparam", "gray", "whitebox", "upop"];

/// 拆 front-matter。
pub fn parse(text: &str) -> Result<GoalDoc> {
    let t = text.trim_start_matches('\u{feff}').trim_start();
    let Some(rest) = t.strip_prefix("---") else {
        bail!("goal.md 要以 `---` 开头的 YAML front-matter 开始");
    };
    let end = rest
        .find("\n---")
        .context("goal.md 的 front-matter 没有结束的 `---`")?;
    let yaml = &rest[..end];
    let body = rest[end + 4..].trim_start_matches(['-', '\r', '\n']).trim().to_string();
    let spec: GoalSpec = serde_yaml::from_str(yaml).context("goal.md 的 front-matter 不是合法的目标格式")?;
    let doc = GoalDoc { spec, body };
    doc.spec.validate()?;
    Ok(doc)
}

impl GoalSpec {
    pub fn validate(&self) -> Result<()> {
        if self.game.trim().is_empty() {
            bail!("game 不能空 (要包名)");
        }
        if !METRICS.contains(&self.objective.metric.as_str()) {
            bail!("objective.metric 只能是 {METRICS:?} 之一, 收到 {:?}", self.objective.metric);
        }
        if !matches!(self.objective.direction.as_str(), "lower" | "higher") {
            bail!("objective.direction 只能是 lower / higher");
        }
        if !(self.objective.target_pct > 0.0 && self.objective.target_pct <= 100.0) {
            bail!("objective.target_pct 要在 (0, 100] 之间");
        }
        if self.layers.is_empty() {
            bail!("layers 不能空");
        }
        for l in &self.layers {
            if !LAYERS.contains(&l.as_str()) {
                bail!("layers 只认 {LAYERS:?}, 收到 {l:?}");
            }
        }
        let b = &self.budget;
        if b.wall_minutes == 0 || b.max_generations == 0 {
            bail!("budget.wall_minutes 与 max_generations 要 > 0");
        }
        if b.min_generations > b.max_generations {
            bail!("budget.min_generations 不能大于 max_generations");
        }
        if !matches!(self.proposer.as_str(), "llm" | "local" | "cloud") {
            bail!("proposer 只认 llm / local / cloud");
        }
        if !matches!(self.gate.as_str(), "off" | "shadow" | "jev") {
            bail!("gate 只认 off / shadow / jev");
        }
        if self.per_generation == 0 || self.per_generation > 6 {
            bail!("per_generation 要在 1..=6");
        }
        Ok(())
    }

    /// stop_when 里的 no_gain: N (没写就是 None = 不按它停)。
    pub fn no_gain(&self) -> Option<u32> {
        self.stop_when.iter().find_map(|v| {
            v.as_mapping()?
                .get(serde_yaml::Value::from("no_gain"))?
                .as_u64()
                .map(|n| n as u32)
        })
    }

    pub fn stops_on(&self, name: &str) -> bool {
        self.stop_when.iter().any(|v| v.as_str() == Some(name))
    }

    /// constraints 里的画面门槛 `image: psnr_min_db 30`。
    pub fn psnr_min_db(&self) -> Option<f64> {
        self.constraints.iter().find_map(|m| {
            let v = m.get(serde_yaml::Value::from("image"))?.as_str()?;
            let n = v.trim().strip_prefix("psnr_min_db")?.trim();
            n.parse().ok()
        })
    }

    /// 一句话的目标摘要 (给人看的大白话, 实时看板的标题): 游戏用中文名, 层名不用代号。
    pub fn summary(&self) -> String {
        let game = crate::optimize::games::resolve(
            &self.game,
            Some(&self.game),
            &crate::optimize::games::CarrierRoots::default(),
        )
        .display;
        let game = if game.is_empty() { self.game.clone() } else { game };
        let what = match self.objective.metric.as_str() {
            "power_watt" => "整机耗电",
            "frame_p95" => "卡顿 (帧时 p95)",
            "fps_mean" => "平均帧率",
            "gpu_busy_ms" => "GPU 忙碌时间",
            m => m,
        };
        let dir = if self.objective.direction == "lower" { "降" } else { "提高" };
        let layers: Vec<&str> = self
            .layers
            .iter()
            .map(|l| match l.as_str() {
                "sysparam" => "改系统设置",
                "gray" => "精简画面步骤",
                "whitebox" => "改游戏渲染设置",
                "upop" => "加画面算子",
                x => x,
            })
            .collect();
        let m = self.budget.wall_minutes;
        let time = if m % 60 == 0 { format!("{} 小时", m / 60) } else { format!("{m} 分钟") };
        format!(
            "{game}：{what}{dir} {}%（{}；最多 {} 代、{time}）",
            fmt_pct(self.objective.target_pct),
            layers.join("、"),
            self.budget.max_generations
        )
    }

    /// 回包 verdict.per_metric / wins 里对应主指标的键。
    pub fn metric_key(&self) -> &'static str {
        match self.objective.metric.as_str() {
            "power_watt" => "power_w_mean",
            "frame_p95" => "frame_p95",
            "fps_mean" => "fps_mean",
            _ => "gpu_busy_ms",
        }
    }
}

fn fmt_pct(x: f64) -> String {
    if x.fract() == 0.0 { format!("{}", x as i64) } else { format!("{x}") }
}

/// 写回 goal.md (front-matter + 正文)。
pub fn render(spec: &GoalSpec, body: &str) -> Result<String> {
    let yaml = serde_yaml::to_string(spec)?;
    Ok(format!("---\n{}---\n{}\n", yaml, body.trim()))
}

/// 本地模板起草: 从一句话里认主指标, 其余取保守缺省。
pub fn template(intent: &str, game: &str) -> (GoalSpec, String) {
    let lower = intent.to_lowercase();
    let has = |ks: &[&str]| ks.iter().any(|k| lower.contains(k));
    let (metric, direction) = if has(&["帧率", "fps"]) {
        ("fps_mean", "higher")
    } else if has(&["卡顿", "流畅", "帧时", "掉帧", "frame", "jank"]) {
        ("frame_p95", "lower")
    } else if has(&["gpu"]) {
        ("gpu_busy_ms", "lower")
    } else {
        ("power_watt", "lower")
    };
    let mut constraints = Vec::new();
    for (k, v) in [("fps_mean", "no_worse"), ("frame_p95", "no_worse"), ("image", "psnr_min_db 30")] {
        if k == metric {
            continue;
        }
        let mut m = serde_yaml::Mapping::new();
        m.insert(k.into(), v.into());
        constraints.push(m);
    }
    let mut ng = serde_yaml::Mapping::new();
    ng.insert("no_gain".into(), 2u64.into());
    let spec = GoalSpec {
        game: game.to_string(),
        device: "any".into(),
        objective: Objective {
            metric: metric.into(),
            direction: direction.into(),
            target_pct: 5.0,
        },
        constraints,
        layers: default_layers(),
        forbid: vec![],
        budget: Budget {
            wall_minutes: 180,
            max_generations: 6,
            min_generations: 3,
        },
        stop_when: vec![
            "target_reached".into(),
            "budget_exhausted".into(),
            serde_yaml::Value::Mapping(ng),
        ],
        proposer: "llm".into(),
        gate: "off".into(),
        per_generation: 2,
    };
    let body = format!(
        "（本地模板起草, 没有经过大模型。）\n\n人的原话: {intent}\n\n\
         按原话把主指标定为 {metric} ({direction})。帧率与帧时不许显著变差, 画面与对照比 PSNR 不低于 30 dB。\
         系统参数与画面流程改写两层都可以动, 先试便宜、不动画面的系统参数。"
    );
    (spec, body)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "---\ngame: com.miHoYo.Yuanshen\ndevice: any\nobjective:\n  metric: power_watt\n  direction: lower\n  target_pct: 5\nconstraints:\n  - fps_mean: no_worse\n  - frame_p95: no_worse\n  - image: psnr_min_db 30\nlayers: [sysparam, gray]\nforbid: [gpu.max_pwrlevel]\nbudget:\n  wall_minutes: 180\n  max_generations: 6\n  min_generations: 3\nstop_when:\n  - target_reached\n  - budget_exhausted\n  - no_gain: 2\nproposer: llm\ngate: off\n---\n省电, 但别卡。\n";

    #[test]
    fn the_spec_example_parses() {
        let d = parse(SAMPLE).unwrap();
        assert_eq!(d.spec.objective.metric, "power_watt");
        assert_eq!(d.spec.no_gain(), Some(2));
        assert!(d.spec.stops_on("target_reached"));
        assert_eq!(d.spec.psnr_min_db(), Some(30.0));
        assert_eq!(d.spec.forbid, vec!["gpu.max_pwrlevel"]);
        assert_eq!(d.spec.per_generation, 2);
        assert_eq!(d.body, "省电, 但别卡。");
        assert_eq!(d.spec.metric_key(), "power_w_mean");
    }

    #[test]
    fn the_summary_is_plain_language() {
        let mut d = parse(SAMPLE).unwrap().spec;
        assert_eq!(d.summary(), "原神：整机耗电降 5%（改系统设置、精简画面步骤；最多 6 代、3 小时）");
        d.game = "com.unity.megacity.metro".into();
        d.objective.target_pct = 10.0;
        d.layers = vec!["sysparam".into(), "gray".into(), "whitebox".into()];
        d.budget.max_generations = 4;
        d.budget.wall_minutes = 240;
        assert_eq!(
            d.summary(),
            "Megacity：整机耗电降 10%（改系统设置、精简画面步骤、改游戏渲染设置；最多 4 代、4 小时）"
        );
        d.objective.metric = "fps_mean".into();
        d.objective.direction = "higher".into();
        d.budget.wall_minutes = 90;
        assert!(d.summary().contains("平均帧率提高 10%") && d.summary().contains("90 分钟"), "{}", d.summary());
    }

    #[test]
    fn render_round_trips() {
        let d = parse(SAMPLE).unwrap();
        let again = parse(&render(&d.spec, &d.body).unwrap()).unwrap();
        assert_eq!(again.spec, d.spec);
        assert_eq!(again.body, d.body);
    }

    #[test]
    fn bad_goals_are_refused() {
        for (from, to) in [
            ("metric: power_watt", "metric: happiness"),
            ("direction: lower", "direction: sideways"),
            ("layers: [sysparam, gray]", "layers: [sysparam, magic]"),
            ("min_generations: 3", "min_generations: 9"),
            ("gate: off", "gate: maybe"),
        ] {
            assert!(parse(&SAMPLE.replace(from, to)).is_err(), "{to}");
        }
        assert!(parse("no front matter").is_err());
    }

    #[test]
    fn the_template_reads_the_main_metric_from_the_sentence() {
        assert_eq!(template("原神太费电了", "p").0.objective.metric, "power_watt");
        assert_eq!(template("团战的时候卡顿", "p").0.objective.metric, "frame_p95");
        let (s, b) = template("帧率再高一点", "p");
        assert_eq!((s.objective.metric.as_str(), s.objective.direction.as_str()), ("fps_mean", "higher"));
        s.validate().unwrap();
        assert!(parse(&render(&s, &b).unwrap()).is_ok());
    }
}
