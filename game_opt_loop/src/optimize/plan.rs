//! 按摸底结果排一份计划: 开哪几层、每层跑几个候选、预算够不够。
//!
//! `--plan` 到这里就停 —— 不碰设备。计划本身是纯函数 (`build`),
//! 输入是 caps + 预算, 输出是步骤表, 所以同一份 caps 排出来的计划一样,
//! 可以拿录下来的 caps 做回归。
//!
//! 预算不够时**不偷偷缩轮数**。轮数决定统计判定的分辨率上限:
//! 每臂 5 轮时精确置换检验的最小可达 p 是 0.0079, 每臂 3 轮只有 0.1 ——
//! 悄悄砍到 3 轮, 换回来的是一整轮"什么都不显著"的真机时间, 而报告里
//! 看不出那是分辨率不够而不是候选没用。所以宁可少跑一层, 也不降轮数。

use super::caps::Caps;
use super::eval::{CarrierKind, ChargingSuspend, Conditions, Kind, Protocol};
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

/// 每臂轮数的下限。
///
/// 这个数不是随手定的: n v n 精确置换检验的最小可达双侧 p = 2/C(2n,n),
/// n=5 时是 0.0079, 刚好够得着三指标 Bonferroni 收紧后的 0.0167;
/// n=4 时是 0.0286, 够不着。所以 5 是"判定还能有结论"的下限。
pub const MIN_ROUNDS: u32 = 5;

/// 等冷一次大概要多久 (秒)。用来估时间, 不是门槛。
const COOLDOWN_ESTIMATE_S: u64 = 90;
/// 每个候选的固定开销 (应用参数 / 还原 / 快照比对)。
const PER_CANDIDATE_OVERHEAD_S: u64 = 60;

/// 预算。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Budget {
    pub seconds: u64,
}

impl Budget {
    /// 默认预算刻意定小: 这条链路一跑就是几十分钟真机时间, 默认值应该是
    /// "先看看", 不是"跑通宵"。要跑长的必须显式说。
    pub const DEFAULT_SECONDS: u64 = 30 * 60;

    /// 解析 `90s` / `45m` / `2h` / 裸数字(按分钟)。
    pub fn parse(s: &str) -> Result<Self> {
        let t = s.trim();
        if t.is_empty() {
            bail!("空预算");
        }
        let (num, mult) = match t.chars().last().unwrap() {
            's' | 'S' => (&t[..t.len() - 1], 1),
            'm' | 'M' => (&t[..t.len() - 1], 60),
            'h' | 'H' => (&t[..t.len() - 1], 3600),
            _ => (t, 60),
        };
        let v: f64 = num
            .trim()
            .parse()
            .map_err(|_| anyhow::anyhow!("看不懂的预算 `{s}` (试试 45m / 2h / 900s)"))?;
        if !(v.is_finite() && v > 0.0) {
            bail!("预算必须是正数: `{s}`");
        }
        Ok(Self {
            seconds: (v * mult as f64).round() as u64,
        })
    }

    pub fn human(&self) -> String {
        let (h, m, s) = (
            self.seconds / 3600,
            (self.seconds % 3600) / 60,
            self.seconds % 60,
        );
        match (h, m, s) {
            (0, 0, s) => format!("{s}s"),
            (0, m, 0) => format!("{m}m"),
            (0, m, s) => format!("{m}m{s}s"),
            (h, 0, 0) => format!("{h}h"),
            (h, m, _) => format!("{h}h{m}m"),
        }
    }
}

impl Default for Budget {
    fn default() -> Self {
        Self {
            seconds: Self::DEFAULT_SECONDS,
        }
    }
}

/// 计划里的一步 = 一层。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Step {
    pub layer: Kind,
    pub layer_display: &'static str,
    pub route: String,
    /// 为什么开这一层。
    pub why: String,
    pub generations: u32,
    pub children: u32,
    /// 每臂轮数。
    pub rounds: u32,
    /// 单个候选估计耗时 (秒)。
    pub per_candidate_s: u64,
    /// 这一步估计耗时 (秒)。
    pub estimate_s: u64,
    /// 出主意的那一方: llm / local。
    pub proposer: String,
    /// 在什么载体上量。
    pub carrier: CarrierKind,
}

/// 没开的一层。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Skipped {
    pub layer: Kind,
    pub layer_display: &'static str,
    pub reason: String,
    pub blockers: Vec<String>,
}

/// 一份计划。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Plan {
    pub schema: &'static str,
    pub generated_at: String,
    pub tool_version: &'static str,
    pub game: String,
    pub package: Option<String>,
    pub serial: String,
    pub budget_s: u64,
    pub steps: Vec<Step>,
    pub skipped: Vec<Skipped>,
    /// 估计总耗时 (秒)。
    pub estimate_s: u64,
    /// 冻结的测试条件, 三层共用。
    pub conditions: Conditions,
    /// 只测量不改动 —— 三层全关时为 true。
    pub measure_only: bool,
    /// 已知的坑, 跟着计划一起打印。
    pub pitfalls: Vec<super::games::Pitfall>,
}

/// 温度安全线 (农场规矩: SoC 不超过 70C)。
pub const TEMP_CAP_C: f32 = 70.0;

/// 从 caps 推冻结的测试条件。
///
/// 等冷目标与温度上限都从**这次摸到的实际温度**推, 不用绝对常数:
/// 连跑几十轮之后设备根本降不到一个固定低温, 拿常数当门槛会把每组候选
/// 都卡死在等冷超时上。
pub fn conditions_from(caps: &Caps) -> Conditions {
    let base = caps.device.soc_temp_c.unwrap_or(40.0);
    // 温度上限: 安全线, 超了这一组作废。用农场的固定安全线, 不从摸底温度推:
    // 摸底时游戏多半没在跑, 待机温度 +3C 在真机上一跑原神就超 (10-11 真机两组
    // 候选都在第 1 轮报 52.3C / 60.5C 超过 51.8C 作废, 一个数都没量到)。
    // 它是安全线, 不是可比性条件 —— 可比性由等冷目标和交替 ABBA 管。
    let temp_cap = TEMP_CAP_C;
    // 等冷目标: "回到基线是在什么热态下量的"。超时不作废, 安全由上限单独把关。
    let cool = (base + 1.0).max(40.0).min(temp_cap - 2.0);

    let suspend = caps.device.charging_suspend.is_true();
    Conditions {
        cool_c: Some(cool),
        temp_cap_c: Some(temp_cap),
        require_root: caps.device.root.is_true(),
        // 功耗只有在放电态才是整机功耗。停不了充也要求放电态, 是为了让接收端
        // 把 power_watt 如实留 null, 而不是报一个看着正常的假数字。
        require_discharging: true,
        charging_suspend: Some(ChargingSuspend {
            enabled: suspend,
            node: caps.device.charge_ctl_nodes.first().cloned(),
        }),
        // 风扇状态量不到。写 unknown 而不是猜一个 —— 猜错会让两批不可比的
        // 数据看起来可比。
        fan: "unknown".into(),
        // 游戏内帧率设置同理: 这一版还读不到, 如实留空, 由人补。
        ingame_fps_setting: None,
        screen_brightness: None,
        // 画质真值由调用方在排完计划之后填 (命令行 --quality-reference)。
        quality_reference: None,
        notes: None,
    }
}

/// 一层在这台设备上该在什么载体上量。
fn carrier_for(kind: Kind, caps: &Caps) -> CarrierKind {
    match kind {
        // 系统参数与画面流程改写都必须在**真实负载**里量 —— 它们改的是
        // 整机行为, 孤立工装量不出来。
        Kind::Sysparam | Kind::Gray => match caps.carrier.box_kind {
            super::games::BoxKind::WhiteBox => CarrierKind::Refbench,
            super::games::BoxKind::BlackBox => CarrierKind::GameReplay,
        },
        Kind::Shader => match caps.carrier.box_kind {
            super::games::BoxKind::WhiteBox => CarrierKind::Refbench,
            super::games::BoxKind::BlackBox => CarrierKind::GameReplay,
        },
    }
}

/// 单轮负载时长 (秒)。
fn workload_seconds(caps: &Caps) -> u64 {
    match caps.carrier.box_kind {
        super::games::BoxKind::WhiteBox => 40,
        super::games::BoxKind::BlackBox => caps.target.workload.seconds.max(20) as u64,
    }
}

/// 排计划。纯函数。
///
/// `use_llm` 只影响计划里"谁来出主意"那一行 —— 写死成 llm 的话,
/// `--plan --no-llm` 会打印并落盘一句不成立的话。
pub fn build(caps: &Caps, budget: Budget, use_llm: bool) -> Plan {
    let seconds = workload_seconds(caps);
    // 一对 A/B = 两次「等冷 + 跑负载」。
    let per_pair_s = 2 * (COOLDOWN_ESTIMATE_S + seconds);
    let per_candidate_s = per_pair_s * MIN_ROUNDS as u64 + PER_CANDIDATE_OVERHEAD_S;

    // 开路顺序: ③ → ② → ①。不是随便排的 ——
    // 系统参数不动画面 (不必量画质, 最便宜也最安全), 画面流程改写次之,
    // 画面算子最贵 (要编译、要注入、要补测画质)。便宜的先跑, 预算见底时
    // 砍掉的是最贵的那一层, 而不是最有希望的那一层。
    let order: [(Kind, &super::caps::LayerVerdict); 3] = [
        (Kind::Sysparam, &caps.layers.sysparam),
        (Kind::Gray, &caps.layers.gray),
        (Kind::Shader, &caps.layers.shader),
    ];

    let mut steps = Vec::new();
    let mut skipped = Vec::new();
    // 预算怎么发: **按还剩几层均分, 但至少给得起一个候选**。
    //
    // 一开始就三层平分是错的: 三层平分缺省的 30m 是每层 10m, 而一个候选要
    // 22m —— 于是三层一起被砍, 跟"砍掉最贵的那一层"正好相反, 而且这条命令的
    // 缺省行为变成了"什么都不做"。
    //
    // 纯贪心 (排在前面的层先吃饱) 也是错的: 第一层会把预算吃光, 后面的层
    // 一个候选都轮不到, 于是"三层自动开路"实际上永远只开第一层。
    //
    // 所以按**剩下几层**均分: 轮到某一层时把剩余预算除以还没排的层数,
    // 除不出一个候选但剩余预算够一个的时候给一个。预算涨上去时各层大致
    // 均分, 预算紧的时候按顺序一层一个地给, 给不出就砍后面的。
    let driven_open = order
        .iter()
        .filter(|(k, v)| v.open && drives(*k))
        .count();
    let mut remaining = driven_open;
    let mut left = budget.seconds;

    for (kind, verdict) in order.iter() {
        if !verdict.open {
            skipped.push(Skipped {
                layer: *kind,
                layer_display: kind.display(),
                reason: verdict.reason.clone(),
                blockers: verdict.blockers.clone(),
            });
            continue;
        }

        // ① 有自己的入口 (`game_opt_loop --track sr ...`), optimize 不代跑,
        // 所以它也不该从预算里拿钱 —— 拿了就等于替一件不会发生的事占着时间。
        if !drives(*kind) {
            // 独立入口只打得到**白盒载体** —— 它要把算子编进载体自己的管线。
            // 黑盒游戏上的那条注入路线归 optimize, 等接收端。不说清这一条的话,
            // 读的人会拿着 `--track sr --carrier refbench` 去"优化原神",
            // 实际上量的是 refbench。
            let handoff = match caps.carrier.box_kind {
                super::games::BoxKind::WhiteBox => format!(
                    "用 `game_opt_loop --track sr --carrier refbench --generations 3 --children 4` 单独跑 ({})",
                    caps.target.display
                ),
                super::games::BoxKind::BlackBox => format!(
                    "独立入口只打得到白盒载体 (refbench), 打不到 {} 本身;                      要在这个黑盒游戏上挂算子, 走的是这里排的灰档注入那条路, 等接收端",
                    caps.target.display
                ),
            };
            skipped.push(Skipped {
                layer: *kind,
                layer_display: kind.display(),
                reason: format!(
                    "{} —— 但画面算子的自主进化闭环在本仓库另有入口, optimize 只把它排进计划, 不代跑",
                    verdict.reason
                ),
                blockers: vec![handoff],
            });
            continue;
        }

        // 这一层塞得下几个候选。轮数不降 —— 降了判定就出不了结论。
        let share = left / remaining.max(1) as u64;
        let mut fits = share / per_candidate_s.max(1);
        if fits == 0 && left >= per_candidate_s {
            // 均分下来不够一个, 但剩余预算够一个: 给一个。少跑一层不如
            // 每层都至少试一个方向 —— 一个候选也能判出显著性。
            fits = 1;
        }
        remaining = remaining.saturating_sub(1);
        if fits == 0 {
            skipped.push(Skipped {
                layer: *kind,
                layer_display: kind.display(),
                reason: format!(
                    "预算不够: 轮到这一层时只剩 {}, 而一个候选要 {} (每臂 {} 轮不能再少, 否则判定够不着显著性)",
                    Budget { seconds: left }.human(),
                    Budget {
                        seconds: per_candidate_s
                    }
                    .human(),
                    MIN_ROUNDS
                ),
                blockers: vec![format!(
                    "想多跑这一层, 预算每加 {} 多一个候选",
                    Budget {
                        seconds: per_candidate_s
                    }
                    .human()
                )],
            });
            continue;
        }
        // 候选先铺宽再迭代深: 第一代什么都不知道, 多试几个方向比在一个方向上
        // 多迭代一代更划算。
        let children = fits.clamp(1, 3) as u32;
        let generations = (fits / children as u64).max(1) as u32;
        let estimate_s = per_candidate_s * (children as u64) * (generations as u64);
        left = left.saturating_sub(estimate_s);
        steps.push(Step {
            layer: *kind,
            layer_display: kind.display(),
            route: verdict.route.clone().unwrap_or_default(),
            why: verdict.reason.clone(),
            generations,
            children,
            rounds: MIN_ROUNDS,
            per_candidate_s,
            estimate_s,
            proposer: if use_llm {
                "大模型 (挂了退回本地变异器)".into()
            } else {
                "本地确定性变异器 (--no-llm)".into()
            },
            carrier: carrier_for(*kind, caps),
        });
    }

    let estimate_s = steps.iter().map(|s| s.estimate_s).sum();
    Plan {
        schema: "game_opt_loop/plan@1",
        generated_at: crate::engine::evolution::now(),
        tool_version: crate::VERSION,
        game: caps.target.display.clone(),
        package: caps.target.package.clone(),
        serial: caps.device.serial.clone(),
        budget_s: budget.seconds,
        steps,
        skipped,
        estimate_s,
        conditions: conditions_from(caps),
        measure_only: !caps.any_layer_open(),
        pitfalls: caps.target.pitfalls.clone(),
    }
}

/// 人工指定的计划形状: 只跑哪几层、每层几代几个。
///
/// 预算分配给出的是"缺省怎么排"; 真机验证要的往往是"这一层跑 2 代、每代 1 个",
/// 而且长实验要拆成每段 ≤45 分钟持锁 —— 那就得能一次只跑一层。给了的字段压过
/// 预算算出来的值, 没给的照旧。估计耗时按新形状重算, 超出预算时计划里写明。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Shape {
    /// None = 按摸底能开的都排。
    pub layers: Option<Vec<Kind>>,
    pub generations: Option<u32>,
    pub children: Option<u32>,
}

impl Shape {
    /// 解析 `--layers sysparam,gray` (也认 `3`/`2`/`1` 与 `③②①`)。
    pub fn parse_layers(s: &str) -> Result<Vec<Kind>> {
        let mut out = Vec::new();
        for raw in s.split([',', ' ']).map(str::trim).filter(|x| !x.is_empty()) {
            let k = match raw {
                "sysparam" | "3" | "③" => Kind::Sysparam,
                "gray" | "2" | "②" => Kind::Gray,
                "shader" | "1" | "①" => Kind::Shader,
                _ => bail!("--layers 看不懂 {raw:?} (要 sysparam / gray / shader, 逗号分隔)"),
            };
            if !out.contains(&k) {
                out.push(k);
            }
        }
        if out.is_empty() {
            bail!("--layers 是空的");
        }
        Ok(out)
    }

    pub fn is_default(&self) -> bool {
        *self == Shape::default()
    }
}

/// 按 [`Shape`] 排计划: 没选的层当作"没开"(理由写明)。给了代数/个数时, 选中且能开的
/// 层一律按这个形状排 (只给一个时另一个取 1), 预算不再砍层; 没给时照常按预算排。纯函数。
pub fn build_shaped(caps: &Caps, budget: Budget, use_llm: bool, shape: &Shape) -> Plan {
    let mut c = caps.clone();
    if let Some(want) = &shape.layers {
        for (k, v) in [
            (Kind::Sysparam, &mut c.layers.sysparam),
            (Kind::Gray, &mut c.layers.gray),
            (Kind::Shader, &mut c.layers.shader),
        ] {
            if v.open && !want.contains(&k) {
                v.open = false;
                v.reason = format!("这一层能开, 但这次按 --layers 没选 ({})", v.reason);
                v.route = None;
                v.blockers = vec![];
            }
        }
    }
    let overridden = shape.generations.is_some() || shape.children.is_some();
    // 人工给了代数/个数: 选中的层都按这个形状排, 预算不再砍层 —— 否则排在
    // 前面的层按新形状吃掉预算, 后面选中的层被当成"预算不够"砍掉, 跟人要的正相反。
    // 计划里照旧写原预算, 估计超出时 render 会说明。
    let mut plan = if overridden {
        let mut p = build(&c, Budget { seconds: u64::MAX / 4 }, use_llm);
        p.budget_s = budget.seconds;
        p
    } else {
        build(&c, budget, use_llm)
    };
    if overridden {
        // 只给了其中一个时, 另一个取 1 (排计划时用的是放开的预算, 那边算出来的值没有意义)。
        for st in plan.steps.iter_mut() {
            st.generations = shape.generations.unwrap_or(1).max(1);
            st.children = shape.children.unwrap_or(1).max(1);
            st.estimate_s = st.per_candidate_s * st.generations as u64 * st.children as u64;
        }
        plan.estimate_s = plan.steps.iter().map(|s| s.estimate_s).sum();
    }
    plan
}

/// optimize 这条命令**自己**驱动哪几层。
///
/// ① 画面算子不在内: 它的自主进化闭环 (模板 → 变异 → 防爆门禁 → Pareto 归档)
/// 在本仓库另有入口, optimize 只负责把它排进计划并说清楚怎么单独跑。
/// 把它算进预算分配等于替一件不会发生的事占着真机时间。
pub fn drives(kind: Kind) -> bool {
    matches!(kind, Kind::Sysparam | Kind::Gray)
}

/// 一层的冻结统计协议。
pub fn protocol_for(step: &Step, caps: &Caps) -> Protocol {
    Protocol::for_kind(
        step.layer,
        step.rounds,
        workload_seconds(caps) as u32,
        conditions_from(caps).cool_c.unwrap_or(40.0),
    )
}

/// 把计划渲染成给人看的文本。
///
/// `receiver` 是接收端探活的结论。`--plan` 也要把它打出来: 计划上写着
/// "要跑 ③ 系统参数", 而接收端还没有 `eval --request` 的话, 真跑那一刻这一层
/// 会当场停下 —— 计划与真跑对不上, 读的人会以为说明书过期了。
pub fn render(plan: &Plan, caps: &Caps, receiver: Option<&str>) -> String {
    let mut o = String::new();
    o.push_str(&format!(
        "目标: {} ({})\n",
        plan.game,
        plan.package.as_deref().unwrap_or("包名未知")
    ));
    o.push_str(&format!(
        "设备: {} {} / {}\n",
        plan.serial,
        caps.device.model.as_deref().unwrap_or("型号未知"),
        match caps.device.platform {
            super::caps::Platform::Adb => "Android (adb)",
            super::caps::Platform::Hdc => "OpenHarmony (hdc)",
            super::caps::Platform::Unknown => "平台未知",
        }
    ));
    o.push_str(&format!(
        "  root {} | 停充 {} | 挂层 {} | 目标已装 {}\n",
        yn(&caps.device.root),
        yn(&caps.device.charging_suspend),
        yn(&caps.device.vulkan_layer),
        yn(&caps.device.game_installed),
    ));
    o.push_str(&format!(
        "  电池 {} {}% | SoC {}\n",
        caps.device.battery.status.as_deref().unwrap_or("未知"),
        caps.device
            .battery
            .capacity_pct
            .map(|c| c.to_string())
            .unwrap_or_else(|| "?".into()),
        // 温度要带着热区名一起说 —— 这个数最容易出的错就是"取错了热区",
        // 而一个光秃秃的 105.0C 看起来跟一个对的数没有任何区别。
        match (caps.device.soc_temp_c, caps.device.soc_temp_zone.as_deref()) {
            (Some(t), Some(z)) => format!("{t:.1}C ({z})"),
            (Some(t), None) => format!("{t:.1}C"),
            (None, _) => "未测到 (没有 cpu-/cpullc/gpuss 热区)".into(),
        },
    ));
    o.push_str(&format!(
        "  载体: {}\n",
        match caps.carrier.box_kind {
            super::games::BoxKind::WhiteBox => format!(
                "白盒 — {}",
                caps.carrier
                    .carrier
                    .as_ref()
                    .map(|c| c.dir.clone())
                    .unwrap_or_default()
            ),
            super::games::BoxKind::BlackBox => "黑盒 (第三方游戏)".to_string(),
        }
    ));

    o.push_str(&format!(
        "\n预算 {} | 估计耗时 {}\n",
        Budget {
            seconds: plan.budget_s
        }
        .human(),
        Budget {
            seconds: plan.estimate_s
        }
        .human()
    ));
    if plan.estimate_s > plan.budget_s {
        o.push_str("  (按 --generations/--children 指定的形状排, 估计耗时超出预算)\n");
    }

    if plan.measure_only {
        o.push_str("\n三层全都开不了 —— 这一轮只测量, 不改动设备。原因见下。\n");
    }

    if let Some(r) = receiver {
        o.push_str(&format!("\n统一测评接口: {r}\n"));
    }

    o.push_str("\n要跑的层:\n");
    if plan.steps.is_empty() {
        o.push_str("  (没有)\n");
    }
    for s in &plan.steps {
        o.push_str(&format!(
            "  {} — {}\n    路线: {}\n    {} 代 x {} 候选, 每臂 {} 轮, 载体 {:?}\n    单候选约 {}, 小计约 {}\n",
            s.layer_display,
            s.why,
            s.route,
            s.generations,
            s.children,
            s.rounds,
            s.carrier,
            Budget {
                seconds: s.per_candidate_s
            }
            .human(),
            Budget {
                seconds: s.estimate_s
            }
            .human(),
        ));
    }

    if !plan.skipped.is_empty() {
        o.push_str("\n不跑的层:\n");
        for s in &plan.skipped {
            o.push_str(&format!("  {} — {}\n", s.layer_display, s.reason));
            for b in &s.blockers {
                o.push_str(&format!("    还差: {b}\n"));
            }
        }
    }

    let c = &plan.conditions;
    o.push_str("\n冻结的测试条件 (看到任何候选数据之前就定下, 事后不改):\n");
    o.push_str(&format!(
        "  等冷目标 {} | 温度上限 {} | 要 root {} | 要放电态 {}\n",
        c.cool_c.map(|v| format!("{v:.1}C")).unwrap_or("不等冷".into()),
        c.temp_cap_c
            .map(|v| format!("{v:.1}C"))
            .unwrap_or("无".into()),
        if c.require_root { "是" } else { "否" },
        if c.require_discharging { "是" } else { "否" },
    ));
    o.push_str(&format!(
        "  停充 {} | 风扇 {} | 游戏内帧率设置 {}\n",
        match &c.charging_suspend {
            Some(s) if s.enabled => format!(
                "会尝试 ({})",
                s.node.as_deref().unwrap_or("节点由接收端自己探")
            ),
            _ => "停不了 —— 充电态下功耗如实留空, 不拿假数字比".to_string(),
        },
        c.fan,
        c.ingame_fps_setting.as_deref().unwrap_or("未记录 (要人补)"),
    ));

    if !plan.pitfalls.is_empty() {
        o.push_str("\n这台机器 / 这个目标上的已知坑:\n");
        for p in &plan.pitfalls {
            o.push_str(&format!("  - {}\n    → {}\n", p.symptom, p.workaround));
        }
    }
    o
}

fn yn(p: &super::caps::Probe<bool>) -> &'static str {
    match p.value {
        Some(true) => "有",
        Some(false) => "无",
        None => "没探出来",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize::caps;
    use crate::optimize::games::{self, CarrierRoots};
    use std::path::PathBuf;

    fn caps_for(raw: &str) -> Caps {
        let t = games::resolve(
            "原神",
            None,
            &CarrierRoots {
                carriers_dir: PathBuf::from("/nonexistent"),
                siblings_dir: PathBuf::from("/nonexistent"),
            },
        );
        caps::assemble(&t, "S1", raw)
    }

    const ROOTED: &str = "model=NX809J\nroot_uid=0\nvklayers=\nbattery_status=Discharging\n\
battery_capacity=84\nbattery_current_now=-1200000\n\
charge_node=/sys/class/qcom-battery/charging_enabled\nzone=cpu-0-0 41300\nzone=gpuss-0 38900\nzone=skin-therm 33000\nzone=pm8550b-bcl 105000\n\
pkg_path=package:/data/app/x/base.apk\n";

    const UNROOTED: &str = "model=Pixel\nroot_uid=NA\nbattery_status=Charging\nzone=cpu-0-0 38000\n\
pkg_path=package:/data/app/x/base.apk\n";

    #[test]
    fn budget_parsing() {
        assert_eq!(Budget::parse("90s").unwrap().seconds, 90);
        assert_eq!(Budget::parse("45m").unwrap().seconds, 2700);
        assert_eq!(Budget::parse("2h").unwrap().seconds, 7200);
        assert_eq!(Budget::parse("30").unwrap().seconds, 1800, "裸数字按分钟");
        assert_eq!(Budget::parse("1.5h").unwrap().seconds, 5400);
        for bad in ["", "abc", "-5m", "0"] {
            assert!(Budget::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn layers_parse_names_and_numbers() {
        assert_eq!(
            Shape::parse_layers("sysparam,gray").unwrap(),
            vec![Kind::Sysparam, Kind::Gray]
        );
        assert_eq!(Shape::parse_layers("2").unwrap(), vec![Kind::Gray]);
        assert!(Shape::parse_layers("cpu").is_err());
        assert!(Shape::parse_layers(",").is_err());
    }

    /// 只选一层时另一层出现在"不跑的层"里并写明是没选, 选中那层按人工代数/个数排,
    /// 估计耗时随之重算。
    #[test]
    fn a_shape_selects_layers_and_overrides_generations() {
        let c = caps_for(ROOTED);
        let shape = Shape {
            layers: Some(vec![Kind::Gray]),
            generations: Some(2),
            children: Some(1),
        };
        // 两层都选、30m 预算: 形状是人定的, 两层都要排上, 不能被预算砍掉一层
        let both = Shape {
            layers: None,
            generations: Some(2),
            children: Some(1),
        };
        let p2 = build_shaped(&c, Budget::parse("30m").unwrap(), true, &both);
        assert_eq!(p2.steps.len(), 2, "{:?}", p2.skipped);
        assert!(p2.steps.iter().all(|s| (s.generations, s.children) == (2, 1)));
        // 只给代数: 每代 1 个
        let g3 = Shape {
            generations: Some(3),
            ..Default::default()
        };
        let p3 = build_shaped(&c, Budget::parse("30m").unwrap(), true, &g3);
        assert!(p3.steps.iter().all(|s| (s.generations, s.children) == (3, 1)));
        assert_eq!(p2.budget_s, 1800);
        assert!(p2.estimate_s > p2.budget_s);

        let p = build_shaped(&c, Budget::parse("30m").unwrap(), true, &shape);
        assert_eq!(p.steps.len(), 1);
        assert_eq!(p.steps[0].layer, Kind::Gray);
        assert_eq!((p.steps[0].generations, p.steps[0].children), (2, 1));
        assert_eq!(p.steps[0].estimate_s, 2 * p.steps[0].per_candidate_s);
        assert_eq!(p.estimate_s, p.steps[0].estimate_s);
        let sys = p.skipped.iter().find(|s| s.layer == Kind::Sysparam).unwrap();
        assert!(sys.reason.contains("--layers"), "{}", sys.reason);
        // 不给形状 = 原来的计划
        let a = build_shaped(&c, Budget::parse("3h").unwrap(), true, &Shape::default());
        let b = build(&c, Budget::parse("3h").unwrap(), true);
        assert_eq!(
            serde_json::to_value(&a.steps).unwrap(),
            serde_json::to_value(&b.steps).unwrap()
        );
    }

    #[test]
    fn budget_is_small_by_default() {
        // 默认值应该是"先看看", 不是"跑通宵"。
        assert!(Budget::default().seconds <= 60 * 60);
    }

    /// 计划是 caps 的纯函数 —— 同一份 caps 排两次, 除时间戳外逐字段相同。
    #[test]
    fn the_plan_is_a_pure_function_of_caps() {
        let c = caps_for(ROOTED);
        let a = build(&c, Budget::parse("3h").unwrap(), true);
        let b = build(&c, Budget::parse("3h").unwrap(), true);
        assert_eq!(
            serde_json::to_value(&a.steps).unwrap(),
            serde_json::to_value(&b.steps).unwrap()
        );
    }

    #[test]
    fn a_rooted_device_plans_the_layers_optimize_actually_drives() {
        let plan = build(&caps_for(ROOTED), Budget::parse("6h").unwrap(), true);
        assert_eq!(plan.steps.len(), 2, "{:?}", plan.skipped);
        // 顺序: 便宜且不动画面的先跑。
        assert_eq!(plan.steps[0].layer, Kind::Sysparam);
        assert_eq!(plan.steps[1].layer, Kind::Gray);
        assert!(!plan.measure_only);

        // ① 能开, 但 optimize 不代跑 —— 它要出现在"不跑的层"里并说清去哪跑,
        // 而不是悄悄消失。
        let shader = plan
            .skipped
            .iter()
            .find(|s| s.layer == Kind::Shader)
            .expect("① 不该从计划里消失");
        assert!(shader.reason.contains("不代跑"), "{}", shader.reason);
        // 黑盒目标上, 交接话术说的是"独立入口打不到它" —— 具体措辞见
        // the_handoff_for_the_image_operator_layer_tells_the_truth_about_its_target
        assert!(!shader.blockers.is_empty());
    }

    /// ① 不从预算里拿钱 —— 拿了就等于替一件不会发生的事占着真机时间,
    /// 而那会让默认预算下三层一起被砍。
    #[test]
    fn the_layer_optimize_does_not_drive_takes_no_budget() {
        assert!(drives(Kind::Sysparam));
        assert!(drives(Kind::Gray));
        assert!(!drives(Kind::Shader));

        // 缺省预算下至少要排得出一层 —— 排不出的话这条命令的默认行为就是
        // "什么都不做", 而用户看不出那是预算问题还是设备问题。
        let plan = build(&caps_for(ROOTED), Budget::default(), true);
        assert!(
            !plan.steps.is_empty(),
            "缺省预算 {} 连一层都排不出: {:?}",
            Budget::default().human(),
            plan.skipped
        );
        assert_eq!(plan.steps[0].layer, Kind::Sysparam, "先跑最便宜的那层");
    }

    /// 预算**顺序发放**不平分: 排在前面的层先吃饱, 吃不下的留给后面。
    /// 平分会让三层一起被砍 —— 跟"砍掉最贵的那一层"正好相反。
    #[test]
    fn budget_is_handed_out_in_order_not_split_evenly() {
        let caps = caps_for(ROOTED);
        let one = build(&caps, Budget::default(), true);
        assert_eq!(one.steps.len(), 1, "缺省预算只够一层: {:?}", one.skipped);
        assert!(one
            .skipped
            .iter()
            .any(|s| s.layer == Kind::Gray && s.reason.contains("只剩")));

        // 预算加倍就该多排一层, 而不是两层各减一半。
        let two = build(&caps, Budget::parse("60m").unwrap(), true);
        assert_eq!(two.steps.len(), 2, "{:?}", two.skipped);
        assert_eq!(two.steps[0].rounds, MIN_ROUNDS);
    }

    /// 计划里"谁来出主意"要跟着 --no-llm 走 —— 写死成大模型的话,
    /// `--plan --no-llm` 会打印并落盘一句不成立的话。
    #[test]
    fn the_plan_says_who_actually_proposes() {
        let caps = caps_for(ROOTED);
        let with = build(&caps, Budget::parse("6h").unwrap(), true);
        assert!(with.steps[0].proposer.contains("大模型"));
        let without = build(&caps, Budget::parse("6h").unwrap(), false);
        assert!(without.steps[0].proposer.contains("本地"));
        assert!(!without.steps[0].proposer.contains("大模型"));
    }

    /// 预算不够时砍层, **不降轮数** —— 降了判定就够不着显著性,
    /// 换回来的是一整轮"什么都不显著"。
    #[test]
    fn a_tight_budget_drops_layers_instead_of_rounds() {
        let plan = build(&caps_for(ROOTED), Budget::parse("10m").unwrap(), true);
        for s in &plan.steps {
            assert_eq!(s.rounds, MIN_ROUNDS);
        }
        assert!(plan.steps.is_empty(), "10 分钟连一个候选都装不下");
        assert!(!plan.skipped.is_empty(), "预算这么小不该三层都跑");
        // 被砍掉的层必须说清楚差多少。
        for s in &plan.skipped {
            assert!(!s.reason.is_empty());
        }
    }

    #[test]
    fn nothing_open_means_measure_only() {
        let plan = build(&caps_for(UNROOTED), Budget::parse("6h").unwrap(), true);
        assert!(plan.steps.is_empty());
        assert!(plan.measure_only);
        assert_eq!(plan.skipped.len(), 3);
    }

    /// 等冷目标从实测温度推, 不是常数; 温度上限是固定安全线。
    #[test]
    fn thermal_targets_follow_the_measured_temperature() {
        let hot = caps_for(&ROOTED.replace("zone=cpu-0-0 41300", "zone=cpu-0-0 52000"));
        let c = conditions_from(&hot);
        assert_eq!(c.temp_cap_c, Some(TEMP_CAP_C));
        assert_eq!(c.cool_c, Some(53.0));

        // 凉机上有下限兜底, 不会把目标定到一个永远等不到的低温。
        let cool = caps_for(&ROOTED.replace("zone=cpu-0-0 41300", "zone=cpu-0-0 25000"));
        let c = conditions_from(&cool);
        assert_eq!(c.temp_cap_c, Some(TEMP_CAP_C));
        assert_eq!(c.cool_c, Some(40.0));
        assert!(c.cool_c.unwrap() < c.temp_cap_c.unwrap());
    }

    /// 停不了充也要求放电态 —— 为的是让接收端把功耗留 null, 而不是报假数字。
    #[test]
    fn power_is_always_required_to_be_measured_on_battery() {
        for raw in [ROOTED, UNROOTED] {
            assert!(conditions_from(&caps_for(raw)).require_discharging);
        }
        // 没有 root 就停不了充, 条件里要如实反映。
        let c = conditions_from(&caps_for(UNROOTED));
        assert!(!c.charging_suspend.unwrap().enabled);
    }

    /// 风扇状态量不到就写 unknown, 不猜。
    #[test]
    fn the_fan_state_is_never_guessed() {
        assert_eq!(conditions_from(&caps_for(ROOTED)).fan, "unknown");
    }

    /// 计划要把接收端的状态一起打出来。
    ///
    /// 计划上写着"要跑 ③ 系统参数", 而接收端还没有 `eval --request` 的话,
    /// 真跑那一刻这一层会当场停下。两边对不上时, 读的人会以为说明书过期了,
    /// 而不是"这条链路还差最后一段"。
    #[test]
    fn the_rendered_plan_states_the_receiver_status() {
        let c = caps_for(ROOTED);
        let p = build(&c, Budget::parse("6h").unwrap(), true);
        let text = render(&p, &c, Some("phonefarm 还不认 `eval --request`"));
        assert!(text.contains("统一测评接口"), "{text}");
        assert!(text.contains("还不认"), "{text}");
        // 不给状态时也不该凭空编一个。
        assert!(!render(&p, &c, None).contains("统一测评接口"));
    }

    /// ① 的交接话术要看目标是不是白盒。
    ///
    /// 独立入口只打得到白盒载体, 打不到黑盒游戏本身。不说清这一条, 读的人会
    /// 拿着 `--track sr --carrier refbench` 去"优化原神", 实际上量的是 refbench。
    #[test]
    fn the_handoff_for_the_image_operator_layer_tells_the_truth_about_its_target() {
        let black = build(&caps_for(ROOTED), Budget::parse("6h").unwrap(), true);
        let shader = black
            .skipped
            .iter()
            .find(|s| s.layer == Kind::Shader)
            .unwrap();
        let b = shader.blockers.join(" ");
        assert!(b.contains("打不到"), "{b}");
        assert!(b.contains("原神"), "{b}");
    }

    /// 渲染出来的计划必须把"为什么不跑这一层"讲出来 ——
    /// 那是用户唯一能拿到的交代。
    #[test]
    fn the_rendered_plan_explains_every_closed_layer() {
        let c = caps_for(UNROOTED);
        let text = render(&build(&c, Budget::default(), true), &c, None);
        assert!(text.contains("不跑的层"));
        assert!(text.contains("root"));
        assert!(text.contains("冻结的测试条件"));
        // 已知的坑也要跟着出现。
        assert!(text.contains("手柄"), "{text}");
    }
}
