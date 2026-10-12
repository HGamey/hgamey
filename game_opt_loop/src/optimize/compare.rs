//! 对比模式 (最小版): 同一游戏、同一设备、同样预算, 依次跑两个 (或更多) 出方案的一方,
//! 出一份对比报告 —— 各自留下了哪些改动、效果、花了多少次真机评测。
//!
//! ```text
//!   optimize --proposer A --proposer B ...
//!        │
//!        ├─ 臂 1: optimize::run (只换 --proposer A, 其余参数原样) → <out>/arm1_<A>/report.json
//!        ├─ 臂 2: optimize::run (只换 --proposer B, 设备钉成臂 1 用的那台) → <out>/arm2_<B>/report.json
//!        └─ 读回各臂 report.json → <out>/compare.json + compare.md
//! ```
//!
//! 公平性只靠「其余参数一字不差」: 同一个 `Config` 只换出方案的一方与输出目录。
//! 每个候选在 phonefarm 里各自和基线做 A/B, 所以两臂的判定口径一样; 但两臂是
//! **先后**跑的, 机器热态、游戏版本、电量都可能漂, 报告里如实写明顺序与起止时间。
//! 这里**不**判谁赢 —— 只把两边的账摆在一起。

use super::eval::Kind;
use super::report::Entry;
use super::Config;
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::time::Instant;

/// 一臂的账。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Arm {
    pub label: String,
    pub spec: String,
    pub describe: String,
    pub out_dir: String,
    pub report_json: String,
    pub serial: String,
    /// 起始时间 (离线拼的对比里是这一臂报告的生成时间)。
    pub started_at: String,
    /// 用时 (秒)。离线拼的对比里不知道, 为 None。
    pub wall_s: Option<u64>,
    /// 问了出方案的一方几次 / 其中调了几次大模型。
    pub proposer_calls: u32,
    pub model_calls: u32,
    /// 提出了多少个候选 (含本地打回的)。
    pub proposed: usize,
    /// harness 校验就打回、没上机的。
    pub rejected_locally: usize,
    /// 上真机评测的次数 (= 下发并拿回报告的候选数)。
    pub device_evals: usize,
    /// 其中没量准的 (ABORT)。
    pub aborted: usize,
    /// 下发了但根本没跑成的。
    pub eval_errors: usize,
    /// 出方案出过的岔子 (大模型挂了 / 外部进程出错), 每层第一条。
    pub proposer_errors: Vec<String>,
    /// 留下的改动。
    pub kept: Vec<KeptChange>,
    pub headline: String,
}

/// 留下的一个改动和它的效果。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KeptChange {
    pub id: String,
    pub layer: Kind,
    pub change: String,
    pub why: String,
    pub p_value: f64,
    /// 逐指标 `<指标>: <状态> <diff_pct>%`, 原样来自 phonefarm 的判定。
    pub effects: Vec<String>,
}

/// 对比报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Comparison {
    pub schema: String,
    pub generated_at: String,
    pub game: String,
    pub serial: String,
    pub budget_s: u64,
    /// 离线拼的对比里报告不记种子, 为 None。
    pub seed: Option<u64>,
    pub arms: Vec<Arm>,
    pub notes: Vec<String>,
}

/// 只读 report.json 里对比要用的那几样 (Report 本身带 `&'static str`, 读不回来)。
#[derive(Deserialize)]
struct ReportView {
    #[serde(default)]
    game: String,
    #[serde(default)]
    generated_at: String,
    #[serde(default)]
    plan: Option<PlanView>,
    serial: String,
    headline: String,
    #[serde(default)]
    layers: Vec<LayerView>,
    #[serde(default)]
    archive: Vec<Entry>,
    #[serde(default)]
    proposer: Option<super::proposer::Summary>,
}

#[derive(Deserialize)]
struct PlanView {
    #[serde(default)]
    budget_s: u64,
}

#[derive(Deserialize)]
struct LayerView {
    #[serde(default)]
    proposed: usize,
    #[serde(default)]
    rejected_locally: Vec<String>,
    #[serde(default)]
    evaluated: usize,
    #[serde(default)]
    aborted: usize,
    #[serde(default)]
    eval_errors: Vec<String>,
    #[serde(default)]
    llm_error: Option<String>,
}

/// 输出目录里一臂的名字: `arm<i>_<spec 里能当文件名的部分>`。
pub fn arm_label(i: usize, spec: &str) -> String {
    let base = spec
        .trim()
        .strip_prefix("cmd:")
        .map(|rest| {
            let prog = rest.split_whitespace().next().unwrap_or("cmd");
            let name = Path::new(prog)
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|| "cmd".into());
            format!("cmd_{name}")
        })
        .unwrap_or_else(|| spec.trim().to_string());
    let safe: String = base
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '.' || c == '-' { c } else { '_' })
        .collect();
    format!("arm{}_{}", i + 1, safe)
}

/// 读回一臂的 report.json, 记成账。
pub fn arm_from_report(
    label: &str,
    spec: &str,
    out_dir: &Path,
    started_at: Option<String>,
    wall_s: Option<u64>,
) -> Result<Arm> {
    let path = out_dir.join("report.json");
    let text = std::fs::read_to_string(&path)
        .with_context(|| format!("读不到 {}", path.display()))?;
    let r: ReportView = serde_json::from_str(&text)
        .with_context(|| format!("{} 解析失败", path.display()))?;
    let summary = r.proposer.clone().unwrap_or_default();
    let kept = r
        .archive
        .iter()
        .filter(|e| e.kept)
        .map(|e| KeptChange {
            id: e.id.clone(),
            layer: e.layer,
            change: e.change.summary(),
            why: e.why.clone(),
            p_value: e.verdict.p_value,
            effects: e
                .verdict
                .per_metric
                .iter()
                .flatten()
                .map(|(k, d)| {
                    format!(
                        "{k}: {} {}",
                        d.status.as_deref().unwrap_or("?"),
                        d.diff_pct
                            .map(|x| format!("{x:+.2}%"))
                            .unwrap_or_else(|| "未测量".into())
                    )
                })
                .collect(),
        })
        .collect();
    Ok(Arm {
        label: label.to_string(),
        spec: spec.to_string(),
        describe: if summary.describe.is_empty() {
            super::proposer::describe_spec(spec)
        } else {
            summary.describe.clone()
        },
        out_dir: out_dir.display().to_string(),
        report_json: path.display().to_string(),
        serial: r.serial,
        started_at: started_at.unwrap_or(r.generated_at),
        wall_s,
        proposer_calls: summary.calls,
        model_calls: summary.model_calls,
        proposed: r.layers.iter().map(|l| l.proposed).sum(),
        rejected_locally: r.layers.iter().map(|l| l.rejected_locally.len()).sum(),
        device_evals: r.layers.iter().map(|l| l.evaluated).sum(),
        aborted: r.layers.iter().map(|l| l.aborted).sum(),
        eval_errors: r.layers.iter().map(|l| l.eval_errors.len()).sum(),
        proposer_errors: r.layers.iter().filter_map(|l| l.llm_error.clone()).collect(),
        kept,
        headline: r.headline,
    })
}

/// 跑对比。`cfg.proposer.specs` 里有几个就跑几臂 (至少两个)。
pub fn run(cfg: &Config) -> Result<Comparison> {
    let specs = cfg.proposer.specs.clone();
    if specs.len() < 2 {
        bail!("对比模式要至少两个 --proposer");
    }
    if cfg.plan_only {
        bail!("对比模式不支持 --plan: 两臂的计划一模一样, 只看计划请去掉多余的 --proposer");
    }
    // spec 写错了在碰设备之前就停, 不要跑完第一臂才发现第二臂起不来。
    for s in &specs {
        let one = super::proposer::Options {
            specs: vec![s.clone()],
            ..cfg.proposer.clone()
        };
        one.spec(cfg.use_llm)?;
    }

    let mut arms: Vec<Arm> = Vec::new();
    let mut serial = cfg.serial.clone();
    for (i, spec) in specs.iter().enumerate() {
        let label = arm_label(i, spec);
        let mut c = cfg.clone();
        c.proposer.specs = vec![spec.clone()];
        c.out_dir = cfg.out_dir.join(&label);
        // 第一臂挑到哪台, 后面的就钉在哪台。
        c.serial = serial.clone();
        println!(
            "\n======== 对比 {}/{}: {} ({}) ========",
            i + 1,
            specs.len(),
            label,
            super::proposer::describe_spec(spec)
        );
        let started_at = Some(crate::engine::evolution::now());
        let t0 = Instant::now();
        super::run(&c).with_context(|| format!("对比臂 {label} 没跑完"))?;
        let arm = arm_from_report(&label, spec, &c.out_dir, started_at, Some(t0.elapsed().as_secs()))?;
        serial.get_or_insert_with(|| arm.serial.clone());
        arms.push(arm);
    }

    let cmp = build(cfg, arms);
    let (json, md) = write(&cmp, &cfg.out_dir)?;
    println!("\n对比报告: {json}\n          {md}");
    Ok(cmp)
}

/// 离线拼对比: 把分几段 (各自持锁) 跑出来的单臂输出目录拼成一份对比报告。
/// 每个目录是一次 `optimize --proposer <一个>` 的 `--out`; 臂名取目录名,
/// 出方案的一方取各自 report.json 里记的那个。用时与种子报告里没记, 如实写「未记录」。
pub fn from_arm_dirs(dirs: &[PathBuf], out: &Path) -> Result<Comparison> {
    if dirs.len() < 2 {
        bail!("拼对比要至少两个臂的输出目录");
    }
    let mut arms = Vec::new();
    let (mut game, mut budget_s) = (String::new(), 0u64);
    for d in dirs {
        let text = std::fs::read_to_string(d.join("report.json"))
            .with_context(|| format!("{} 下没有 report.json", d.display()))?;
        let r: ReportView = serde_json::from_str(&text)
            .with_context(|| format!("{}/report.json 解析失败", d.display()))?;
        let spec = r
            .proposer
            .as_ref()
            .map(|p| p.spec.clone())
            .with_context(|| format!("{}/report.json 没记出方案的一方 (是 --plan 的输出?)", d.display()))?;
        if game.is_empty() {
            game = r.game.clone();
            budget_s = r.plan.as_ref().map(|p| p.budget_s).unwrap_or(0);
        }
        let label = d
            .file_name()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_else(|| format!("arm{}", arms.len() + 1));
        arms.push(arm_from_report(&label, &spec, d, None, None)?);
    }
    let mut c = Comparison {
        schema: "game_opt_loop/compare@1".into(),
        generated_at: crate::engine::evolution::now(),
        game,
        serial: arms[0].serial.clone(),
        budget_s,
        seed: None,
        arms,
        notes: Vec::new(),
    };
    c.notes = notes_for(&c.arms);
    c.notes.push("这份对比由分段跑出的单臂输出离线拼成: 各臂预算/形状是否一致要看各自 report.json 的计划。".into());
    write(&c, out)?;
    Ok(c)
}

fn notes_for(arms: &[Arm]) -> Vec<String> {
    let mut notes: Vec<String> = vec![
        "各臂只换了出方案的一方, 游戏 / 设备 / 预算 / 种子 / 冻结的测试条件与判定口径一样。".into(),
        "各臂是先后跑的: 机器热态、电量、游戏版本可能在两臂之间漂, 先后顺序见各臂起始时间。".into(),
        "这里不判谁赢: 每个候选各自和基线 A/B, 两臂留下的改动之间没有直接对比过。".into(),
    ];
    let serials: std::collections::BTreeSet<_> = arms.iter().map(|a| a.serial.as_str()).collect();
    if serials.len() > 1 {
        notes.push(format!("注意: 各臂跑在不同设备上 ({serials:?}), 不可比。"));
    }
    notes
}

/// 拼对比报告。
pub fn build(cfg: &Config, arms: Vec<Arm>) -> Comparison {
    let notes = notes_for(&arms);
    Comparison {
        schema: "game_opt_loop/compare@1".into(),
        generated_at: crate::engine::evolution::now(),
        game: cfg.game.clone(),
        serial: arms.first().map(|a| a.serial.clone()).unwrap_or_default(),
        budget_s: cfg.budget.seconds,
        seed: Some(cfg.seed),
        arms,
        notes,
    }
}

/// 渲染 compare.md。
pub fn markdown(c: &Comparison) -> String {
    let mut o = String::new();
    o.push_str(&format!("# {} 出方案对比\n\n", c.game));
    o.push_str(&format!(
        "- 生成时间: {}\n- 设备: {}\n- 每臂预算: {} 秒, 种子 {}\n\n",
        c.generated_at,
        c.serial,
        c.budget_s,
        c.seed.map(|x| x.to_string()).unwrap_or_else(|| "未记录".into())
    ));
    o.push_str("## 账\n\n");
    o.push_str("| 臂 | 出方案的一方 | 起始 | 用时 | 问了几次 | 大模型调用 | 提出 | 本地打回 | 真机评测 | 其中 ABORT | 下发失败 | 留下 |\n");
    o.push_str("|---|---|---|---|---|---|---|---|---|---|---|---|\n");
    for a in &c.arms {
        o.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |\n",
            a.label,
            a.describe,
            a.started_at,
            a.wall_s.map(|x| format!("{x}s")).unwrap_or_else(|| "未记录".into()),
            a.proposer_calls,
            a.model_calls,
            a.proposed,
            a.rejected_locally,
            a.device_evals,
            a.aborted,
            a.eval_errors,
            a.kept.len()
        ));
    }
    for a in &c.arms {
        o.push_str(&format!("\n## {} — 留下的改动\n\n", a.label));
        o.push_str(&format!("{}\n\n", a.headline));
        if a.kept.is_empty() {
            o.push_str("没有留下任何改动。\n");
        } else {
            o.push_str("| 候选 | 层 | 改了什么 | p | 逐指标 |\n|---|---|---|---|---|\n");
            for k in &a.kept {
                o.push_str(&format!(
                    "| {} | {} | {} | {:.4} | {} |\n",
                    k.id,
                    k.layer.display(),
                    k.change,
                    k.p_value,
                    k.effects.join("; ")
                ));
            }
        }
        for e in &a.proposer_errors {
            o.push_str(&format!("\n- 出方案出过岔子: {e}\n"));
        }
        o.push_str(&format!("\n完整报告: `{}`\n", a.report_json));
    }
    o.push_str("\n## 读这份对比要注意\n\n");
    for n in &c.notes {
        o.push_str(&format!("- {n}\n"));
    }
    o
}

/// 写 compare.json + compare.md。
pub fn write(c: &Comparison, dir: &Path) -> Result<(String, String)> {
    std::fs::create_dir_all(dir)?;
    let json: PathBuf = dir.join("compare.json");
    let md: PathBuf = dir.join("compare.md");
    std::fs::write(&json, serde_json::to_string_pretty(c)?)
        .with_context(|| format!("写入 {} 失败", json.display()))?;
    std::fs::write(&md, markdown(c)).with_context(|| format!("写入 {} 失败", md.display()))?;
    Ok((json.display().to_string(), md.display().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arm_labels_are_file_name_safe_and_ordered() {
        assert_eq!(arm_label(0, "local"), "arm1_local");
        assert_eq!(arm_label(1, "llm"), "arm2_llm");
        assert_eq!(
            arm_label(1, "cmd:/x/y/my proposer.py --flag"),
            "arm2_cmd_my"
        );
        assert_eq!(arm_label(2, "cmd:examples/proposers/fixed_one.py"), "arm3_cmd_fixed_one.py");
    }
}
