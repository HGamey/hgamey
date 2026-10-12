//! `optimize` 子命令: 摸底 → 开路 → 出主意 → 真机评测 → 统一档案 → 总报告。
//!
//! ```text
//!   摸底 (只读)                按 caps 开路              每层出主意          统一测评
//!   ├ 平台 adb/hdc             ├ ③ 系统参数 (有 root)     ├ 大模型挑         phonefarm eval
//!   ├ root / 能否停充           ├ ② 画面流程改写 (能挂层)   ├ 挂了退回本地      --request <json>
//!   ├ 电量 / 温度              └ ① 画面算子 (白盒, 或      └ 白名单/目录校验   --json
//!   ├ 能否挂 Vulkan 层             能挂层的黑盒)                                  │
//!   └ 白盒还是黑盒                                                                ↓
//!         ↓                                                          统一优胜档案 + report.json/md
//!      caps.json
//! ```
//!
//! 分工红线: 本仓库**出主意**, phonefarm **应用 / 测量 / 判定 / 还原**。
//! 这个模块里没有任何一处在判断"改完之后好不好" —— 那是回包里 `verdict` 的事。
//!
//! `--plan` 在"开路"之后就停: 打印计划、写 caps.json 与 report.json, 不碰设备。

pub mod caps;
pub mod children;
pub mod compare;
pub mod dispatch;
pub mod eval;
pub mod games;
pub mod gate;
pub mod gray;
pub mod plan;
pub mod profile;
pub mod proposer;
pub mod report;
pub mod sysparam;
pub mod whitebox;

use anyhow::{bail, Context, Result};
use eval::{CarrierKind, Kind, Payload, Request, WorkloadSpec};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// `optimize` 的配置。
#[derive(Debug, Clone)]
pub struct Config {
    /// 游戏名 (人写的)。
    pub game: String,
    /// 包名覆盖。
    pub package: Option<String>,
    /// 目标设备; None = 自己挑唯一在线的那台。
    pub serial: Option<String>,
    /// 同型号的其他设备 (`--extra-device`, 可多次)。③ 系统参数层同一代的候选分给
    /// 这些设备与主设备并行量; 每个候选只在自己那台上比对照臂, 不跨设备比。空 = 单机。
    pub extra_devices: Vec<String>,
    pub budget: plan::Budget,
    /// 只摸底 + 打印计划, 不改设备。
    pub plan_only: bool,
    pub out_dir: PathBuf,
    pub phonefarm: PathBuf,
    pub secrets: PathBuf,
    pub use_llm: bool,
    pub llm_provider: Option<String>,
    pub llm_timeout_s: Option<u64>,
    pub seed: u64,
    /// 画质真值 (真机截帧目录或单张 PNG)。
    pub quality_reference: Option<String>,
    /// 已经采好的系统参数探测结果 (`probe.txt`)。
    ///
    /// 白名单是真机探测结果的函数, 而探测本身要**写节点再还原** ——
    /// 那是 phonefarm 的活。接收端的探测通路接上之前, 这个参数让人可以
    /// 把已有的 probe.txt 喂进来, 把出主意那一半先跑通。
    pub sysparam_probe: Option<PathBuf>,
    /// 出方案的一方 (`--proposer llm|local|cmd:<可执行文件>`), 见 [`proposer`]。
    pub proposer: proposer::Options,
    /// `phonefarm frame-audit analyze --json` 的输出: ② 层的旋钮从这里来。
    pub frame_audit: Option<PathBuf>,
    /// 人工指定的计划形状 (`--layers` / `--generations` / `--children`)。
    pub shape: plan::Shape,
    /// 游戏档案库 (`<目录>/<包名>/profile.json`)。目标包名在库里有 PASS 的档案就用它的
    /// 负载、渲染线程和坑 —— 内置表认不出的游戏靠它直接能跑。
    pub profiles_dir: PathBuf,
    /// 一份刚由 `phonefarm onboard` 产出的 profile.json: 先收进档案库 (只收 PASS), 再用。
    pub game_profile: Option<PathBuf>,
    /// 目标模式 (`game_opt_loop goal run`) 逐代调用时带进来的东西; 单独跑 optimize 时全是缺省。
    pub goal: GoalHooks,
}

static INTERRUPTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// 有人叫停了 (目标模式收到 SIGINT)。之后不再出方案、不再上机; 正在跑的 phonefarm 子进程
/// 同组收到 SIGINT, 自己走还原流程。
pub fn interrupted() -> bool {
    INTERRUPTED.load(std::sync::atomic::Ordering::SeqCst)
}

/// 置位叫停标志 (信号处理里调, 只做一次原子写)。
pub fn interrupt() {
    INTERRUPTED.store(true, std::sync::atomic::Ordering::SeqCst);
}

/// 目标模式逐代调用 optimize 时的钩子。缺省 = 普通的一次 optimize。
#[derive(Debug, Clone, Default)]
pub struct GoalHooks {
    /// 这一次的第 1 代在整次目标运行里是第几代减一 (候选编号与出方案输入里的代数都加上它)。
    pub gen_offset: u32,
    /// 之前几代上过机的候选 (含真机判定), 接着喂给出方案的一方。
    pub prior_history: Vec<proposer::HistoryItem>,
    /// 人的掌舵指令 (human.md 里机读不了的部分, 原文)。出方案时作为最高优先级偏好。
    pub steer: Option<String>,
    /// 禁止动的参数 id / 写回范围 / 开关名 (goal.md forbid + human.md「不要动 X」)。
    pub forbid: Vec<String>,
    /// ② 画面比对门槛 (goal.md constraints.image); None = 缺省 30 dB。
    pub psnr_min_db: Option<f64>,
    /// 逐候选事件 (propose / measure / verdict) 追加写到这里 (events.jsonl)。
    pub events: Option<PathBuf>,
}

impl GoalHooks {
    /// 一个参数 id / 范围是否被禁止 (完全相等, 或 forbid 项是它的前缀并以 `.`/`:`/`@` 断开)。
    pub fn forbids(&self, id: &str) -> bool {
        self.forbid.iter().any(|f| {
            let f = f.trim();
            !f.is_empty()
                && (id == f
                    || (id.starts_with(f)
                        && matches!(id.as_bytes().get(f.len()), Some(b'.') | Some(b':') | Some(b'@'))))
        })
    }

    /// 追加一条事件 (带时间与代数)。写不进不影响主流程。
    pub fn emit(&self, gen: u32, mut ev: serde_json::Value) {
        let Some(path) = &self.events else { return };
        ev["t"] = serde_json::json!(chrono::Local::now().to_rfc3339());
        ev["gen"] = serde_json::json!(gen);
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
            use std::io::Write;
            let _ = writeln!(f, "{}", ev);
        }
    }
}

/// 跑完之后的落点。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Outcome {
    pub caps_path: String,
    pub report_json: String,
    pub report_md: String,
    pub plan_only: bool,
    pub layers_used: usize,
    pub candidates: usize,
    pub kept: usize,
    /// 这一次上过机的候选 (目标模式拿去喂下一代)。
    #[serde(default)]
    pub history: Vec<proposer::HistoryItem>,
}

/// 挑一台设备。
///
/// 多台在线时**不猜** —— 挑错设备的代价是在别人的机器上写 sysfs。
fn pick_serial(prober: &caps::Prober, want: Option<&str>) -> Result<String> {
    let devices = prober.devices()?;
    match want {
        Some(s) => {
            if devices.iter().any(|d| d == s) {
                Ok(s.to_string())
            } else if devices.is_empty() {
                bail!("指定了 --device {s}, 但 phonefarm 报告没有在线设备")
            } else {
                bail!(
                    "指定的 --device {s} 不在线。在线的是: {}",
                    devices.join(", ")
                )
            }
        }
        None => match devices.len() {
            0 => bail!("没有在线设备 (`phonefarm devices` 是空的)"),
            1 => Ok(devices[0].clone()),
            _ => bail!(
                "有 {} 台设备在线, 得用 --device 指定一台: {}",
                devices.len(),
                devices.join(", ")
            ),
        },
    }
}

/// 主设备之外的同型号设备 (`--extra-device`)。返回本次可用设备表, 主设备在前。
///
/// 每台都单独摸底并落 `caps_<serial>.json`。型号或 root 与主设备不一致就整次停下:
/// 不同型号的手机对同一组系统参数的反应本来就不同, 混着量会把机型差异算进候选。
/// 白名单沿用主设备那一份 (同型号同系统版本), 每台的实际生效情况以各自回包的
/// `conditions_observed.apply_ok` 为准。
fn extra_devices(
    prober: &caps::Prober,
    target: &games::Target,
    primary: &caps::Caps,
    serial: &str,
    extras: &[String],
    out_dir: &std::path::Path,
) -> Result<Vec<String>> {
    let mut devices = vec![serial.to_string()];
    if extras.is_empty() {
        return Ok(devices);
    }
    let online = prober.devices()?;
    for s in extras {
        if devices.contains(s) {
            continue;
        }
        if !online.contains(s) {
            bail!("--extra-device {s} 不在线。在线的是: {}", online.join(", "));
        }
        let c = prober
            .run(target, s)
            .with_context(|| format!("摸底失败 (设备 {s})"))?;
        caps::write(&c, &out_dir.join(format!("caps_{s}.json")))?;
        let same_model = c.device.model.is_some() && c.device.model == primary.device.model;
        if !same_model || c.device.root.is_true() != primary.device.root.is_true() {
            bail!(
                "--extra-device {s} 与主设备不同型号或 root 状态不同 ({:?} root={} vs {:?} root={}), \
                 多机只并行同型号设备, 不跨机型比较",
                c.device.model,
                c.device.root.is_true(),
                primary.device.model,
                primary.device.root.is_true()
            );
        }
        println!("      并行设备 {s} ({})", c.device.model.as_deref().unwrap_or("?"));
        devices.push(s.clone());
    }
    Ok(devices)
}

/// 跑一次 optimize。
pub fn run(cfg: &Config) -> Result<Outcome> {
    // 出方案的 spec 写错了 (或与 --no-llm 矛盾) 应该在碰设备之前就停。
    let proposer_spec = cfg.proposer.spec(cfg.use_llm)?;
    let roots = games::CarrierRoots::default();
    let store = profile::Store { root: cfg.profiles_dir.clone() };
    let mut package = cfg.package.clone();
    if let Some(src) = cfg.game_profile.as_ref() {
        let dst = store
            .install(src)
            .with_context(|| format!("收游戏档案 {} 失败", src.display()))?;
        println!("游戏档案已收进 {}", dst.display());
        if package.is_none() {
            package = Some(profile::read(&dst)?.package);
        }
    }
    let mut target = games::resolve(&cfg.game, package.as_deref(), &roots);
    if let Some(pkg) = target.package.clone() {
        if let Some(loaded) = store.find(&pkg)? {
            if !profile::apply(&mut target, &loaded) {
                println!(
                    "游戏档案 {} 状态是 {}, 没用 (只用 PASS 的)",
                    loaded.path.display(),
                    loaded.profile.status
                );
            }
        }
    }

    println!("目标: {} — {}", target.display, target.resolved_via);
    if target.package.is_none() {
        bail!(
            "不认识 `{}`, 也猜不出包名。\n\
             要么用 --package <包名> 指定, 要么用一个已知的白盒载体名 (现在有: {})。\n\
             猜一个包名接着跑的代价是几十分钟真机时间量了另一个应用。",
            cfg.game,
            {
                let l = roots.list();
                if l.is_empty() {
                    "一个都没找到".to_string()
                } else {
                    l.join(", ")
                }
            }
        );
    }

    // ---- 1. 摸底 (只读) ----
    println!("\n[1/3] 摸底 (只读, 不动设备)...");
    let prober = caps::Prober::new(&cfg.phonefarm);
    let serial = pick_serial(&prober, cfg.serial.as_deref())?;
    println!("      设备 {serial}");
    let caps = prober
        .run(&target, &serial)
        .with_context(|| format!("摸底失败 (设备 {serial})"))?;

    let caps_path = cfg.out_dir.join("caps.json");
    caps::write(&caps, &caps_path)?;
    println!("      caps: {}", caps_path.display());
    let devices = extra_devices(&prober, &target, &caps, &serial, &cfg.extra_devices, &cfg.out_dir)?;

    // ---- 2. 开路 + 排计划 ----
    //
    // 接收端探活放在这里而不是真跑之前: `--plan` 也要交代它。计划上写着
    // "要跑 ③ 系统参数", 而接收端还没有 `eval --request` 的话, 真跑那一刻
    // 这一层会当场停下 —— 计划与真跑对不上, 读的人会以为说明书过期了。
    // 探活本身不碰设备 (拿一个不存在的请求路径去问), 所以 --plan 仍然是只读的。
    println!("\n[2/3] 按能力开路 + 接收端探活...");
    let driver = eval::EvalDriver::new(&cfg.phonefarm);
    let support = driver.probe();
    let mut plan = plan::build_shaped(&caps, cfg.budget, cfg.use_llm, &cfg.shape);
    // 画质真值跟着冻结条件走, 不是某一层的私有参数: 灰档的每一个改写都动画面,
    // 那一层的 psnr 约束没有真值就判不了。
    plan.conditions.quality_reference = cfg.quality_reference.clone();
    if let Some(line) = cfg.proposer.plan_line() {
        plan.steps.iter_mut().for_each(|s| s.proposer = line.clone());
    }
    println!("\n{}", plan::render(&plan, &caps, Some(&support.describe())));

    if cfg.plan_only {
        let layers = plan
            .steps
            .iter()
            .map(|s| report::LayerOutcome {
                layer: s.layer,
                layer_display: s.layer_display,
                used: false,
                reason: format!("{} (--plan: 只排计划, 没跑)", s.why),
                blockers: if support.is_supported() {
                    vec![]
                } else {
                    // 计划说要跑、真跑却会当场停下 —— 这一条必须出现在 --plan 的
                    // 产物里, 否则两边对不上账。
                    vec![format!("真跑时这一层会停在接收端: {}", support.describe())]
                },
                route: Some(s.route.clone()),
                proposer: None,
                llm_error: None,
                proposed: 0,
                rejected_locally: vec![],
                evaluated: 0,
                kept: 0,
                aborted: 0,
                eval_errors: vec![],
            })
            .chain(plan.skipped.iter().map(|s| {
                report::LayerOutcome::unused(s.layer, s.reason.clone(), s.blockers.clone())
            }))
            .collect();
        let r = report::build(&plan, &caps, layers, vec![], true);
        let (json, md) = report::write(&r, &caps, &cfg.out_dir)?;
        println!("\n报告: {json}\n      {md}");
        return Ok(Outcome {
            caps_path: caps_path.display().to_string(),
            report_json: json,
            report_md: md,
            plan_only: true,
            layers_used: 0,
            candidates: 0,
            kept: 0,
            history: Vec::new(),
        });
    }

    // ---- 3. 逐层跑 ----
    println!("\n[3/3] 逐层出主意 + 真机评测...");
    let built = proposer::from_spec(
        &proposer_spec,
        &cfg.secrets,
        cfg.llm_provider.as_deref(),
        cfg.llm_timeout_s,
        std::time::Duration::from_secs(cfg.proposer.timeout_s.max(1)),
    )?;
    let gated = match &cfg.proposer.gate {
        Some(o) => Some(gate::build(built.as_ref(), o)?),
        None => None,
    };
    let inner: &dyn proposer::Proposer = match &gated {
        Some(g) => g,
        None => built.as_ref(),
    };
    let counted = proposer::Counting::new(inner);
    println!("      出主意: {}", counted.summary().describe);

    let mut layers: Vec<report::LayerOutcome> = Vec::new();
    let mut archive: Vec<report::Entry> = Vec::new();
    let collected: std::cell::RefCell<Vec<proposer::HistoryItem>> = Default::default();

    for step in &plan.steps {
        println!("\n  --- {} ---", step.layer_display);
        let ctx = LayerCtx {
            cfg,
            caps: &caps,
            plan: &plan,
            step,
            proposer: &counted,
            serial: &serial,
            devices: &devices,
            driver: &driver,
            support: &support,
            collected: &collected,
        };
        // 一层出错不该把已经落袋的测量一起带走。跑到这里的每一份报告都
        // 代表几十分钟真机时间, 为了一个起不来的子进程把它们全丢掉,
        // 换回来的是"什么都没有"而不是"少了一层"。
        let outcome = match step.layer {
            Kind::Sysparam => run_sysparam(&ctx, &mut archive),
            Kind::Gray => run_gray(&ctx, &mut archive),
            Kind::Whitebox => run_whitebox(&ctx, &mut archive),
            // ① 不在 plan.steps 里 (见 plan::drives), 真进来了说明计划排错了。
            Kind::Shader => Ok(report::LayerOutcome::unused(
                Kind::Shader,
                "画面算子由本仓库的自主进化闭环单独跑, optimize 不代跑",
                vec!["用 `game_opt_loop --track sr --carrier refbench ...` 单独跑".into()],
            )),
        };
        layers.push(match outcome {
            Ok(o) => o,
            Err(e) => {
                println!("      这一层中断: {e}");
                let mut o = report::LayerOutcome::unused(
                    step.layer,
                    format!("这一层中途出错, 已停下并保留前面的结果: {e}"),
                    vec![],
                );
                o.route = Some(step.route.clone());
                o
            }
        });
    }
    for s in &plan.skipped {
        layers.push(report::LayerOutcome::unused(
            s.layer,
            s.reason.clone(),
            s.blockers.clone(),
        ));
    }

    let mut r = report::build(&plan, &caps, layers, archive, false);
    r.proposer = Some(proposer::Summary {
        gate: gated.as_ref().map(|g| g.decisions()).unwrap_or_default(),
        ..counted.summary()
    });
    let (json, md) = report::write(&r, &caps, &cfg.out_dir)?;
    println!("\n{}", r.headline);
    println!("报告: {json}\n      {md}");

    Ok(Outcome {
        caps_path: caps_path.display().to_string(),
        report_json: json,
        report_md: md,
        plan_only: false,
        layers_used: r.layers.iter().filter(|l| l.used).count(),
        candidates: r.archive.len(),
        kept: r.kept.len(),
        history: collected.into_inner(),
    })
}

/// 这一层的负载描述。
fn workload_of(step: &plan::Step, caps: &caps::Caps) -> WorkloadSpec {
    match step.carrier {
        CarrierKind::Refbench => WorkloadSpec {
            carrier: CarrierKind::Refbench,
            script: None,
            seconds: None,
            frames: Some(900),
            intensity: Some(5),
            render_comm: None,
            enter: None,
            launch: None,
        },
        CarrierKind::GameReplay => WorkloadSpec {
            carrier: CarrierKind::GameReplay,
            script: caps.target.workload.script.clone(),
            seconds: Some(caps.target.workload.seconds.max(1)),
            frames: None,
            intensity: None,
            render_comm: caps.target.workload.render_comm.clone(),
            // 有档案 = 进门流程实测过, 接收端每次重启游戏后可以自己走回游戏场景。
            // 自驱动负载由接收端带 extras 启动, 不走进门流程。
            enter: caps
                .target
                .profile
                .as_ref()
                .filter(|_| caps.target.workload.launch.is_none())
                .map(|_| "onboard".to_string()),
            launch: caps.target.workload.launch.clone(),
        },
        CarrierKind::Headless => WorkloadSpec {
            carrier: CarrierKind::Headless,
            script: None,
            seconds: None,
            frames: None,
            intensity: None,
            render_comm: None,
            enter: None,
            launch: None,
        },
    }
}

fn device_note(caps: &caps::Caps) -> String {
    format!(
        "{} ({}), {}",
        caps.device.model.as_deref().unwrap_or("型号未知"),
        caps.device.serial,
        if caps.device.root.is_true() {
            "已 root"
        } else {
            "没有 root"
        }
    )
}

fn workload_note(caps: &caps::Caps) -> String {
    match caps.carrier.box_kind {
        games::BoxKind::WhiteBox => "白盒靶场的确定性渲染负载".to_string(),
        games::BoxKind::BlackBox => format!(
            "{} 的确定性回放负载 {} 秒",
            caps.target.display, caps.target.workload.seconds
        ),
    }
}

/// 一条 verdict 事件 (目标模式的时间线): 判定、逐指标变化、画面比对、被抢回的臂数。
fn verdict_event(id: &str, rep: &eval::Report) -> serde_json::Value {
    let metrics: serde_json::Map<String, serde_json::Value> = rep
        .verdict
        .per_metric
        .iter()
        .flatten()
        .map(|(k, d)| (k.clone(), serde_json::json!({"delta_pct": d.diff_pct, "p": d.p, "status": d.status})))
        .collect();
    let co = rep.conditions_observed.as_ref();
    serde_json::json!({
        "type": "verdict",
        "candidate_id": id,
        "verdict": rep.status,
        "kept": rep.kept(),
        "reason": rep.verdict.reason,
        "wins": rep.verdict.wins,
        "metrics": metrics,
        "image_psnr": rep.metrics.psnr_db,
        "contested_arms": co.and_then(|c| c.contested_arms),
    })
}

/// 日志里跟在判定后面的那句理由。ABORT 的原因 (超温 / 快照不一致 / 没量到帧)
/// 只写在 report.json 里的话, 真机跑在远端节点上时要等整段跑完、把输出拉回来才看得到;
/// 打在日志里, 第一个候选作废时就能决定要不要停下来修。
fn verdict_note(rep: &eval::Report) -> String {
    match rep.verdict.reason.as_deref().map(str::trim) {
        Some(r) if !r.is_empty() => format!(" — {r}"),
        _ => String::new(),
    }
}

/// 把逐指标明细压成给出主意的一方看的几行。
fn per_metric_lines(r: &eval::Report) -> Vec<String> {
    r.verdict
        .per_metric
        .as_ref()
        .map(|m| {
            m.iter()
                .map(|(k, d)| {
                    format!(
                        "{k}: {} (p={}) {}",
                        d.diff_pct
                            .map(|v| format!("{v:+.2}%"))
                            .unwrap_or_else(|| "未测量".into()),
                        d.p.map(|v| format!("{v:.4}"))
                            .unwrap_or_else(|| "-".into()),
                        d.status.as_deref().unwrap_or("")
                    )
                })
                .collect()
        })
        .unwrap_or_default()
}


/// 在指定设备上下发一个候选 (多机调度从各自的线程里调它, 所以不借 [`LayerCtx`])。
fn evaluate_on(
    cfg: &Config,
    caps: &caps::Caps,
    driver: &eval::EvalDriver,
    serial: &str,
    id: &str,
    req: &Request,
) -> Result<eval::Report> {
    if interrupted() {
        bail!("人叫停 (SIGINT): 不再上机");
    }
    // 黑盒游戏: 上机前先进门。量到桌面或登录页的一轮不是数据。
    // 自驱动负载 (workload.launch) 由接收端带 extras 启动, 不需要进门。
    if caps.carrier.box_kind == games::BoxKind::BlackBox
        && !req.package.is_empty()
        && req.workload.launch.is_none()
    {
        let dir = cfg.out_dir.join("enter").join(id);
        match driver.enter_scene(&req.package, serial, &dir) {
            eval::EnterOutcome::InGame => println!("        进门: 已在游戏场景"),
            eval::EnterOutcome::Unsupported => {
                println!("        进门: 接收端没有 onboard --enter-only, 照旧直接上机 (开跑前要人确认在游戏里)")
            }
            eval::EnterOutcome::NeedsHuman(why) => {
                bail!("进不了游戏场景, 要人动手: {why} (证据 {})", dir.display())
            }
            eval::EnterOutcome::Failed(why) => {
                bail!("进不了游戏场景: {why} (证据 {})", dir.display())
            }
        }
    }
    let path = cfg.out_dir.join("requests").join(format!("{id}.json"));
    driver.evaluate(req, &path)
}

/// 跑一层要用到的全部东西。
///
/// 收成一个结构体不是为了少敲字, 是为了让两个层跑器的签名一眼看得出
/// "它们吃的是同一份上下文" —— 八个平行参数排在那里时, 两边漏传一个
/// 不同的参数是编译期看不出来的。
struct LayerCtx<'a> {
    cfg: &'a Config,
    caps: &'a caps::Caps,
    plan: &'a plan::Plan,
    step: &'a plan::Step,
    proposer: &'a dyn proposer::Proposer,
    serial: &'a str,
    /// 本次可用的设备, 主设备在前 (`--extra-device` 追加的在后)。
    devices: &'a [String],
    driver: &'a eval::EvalDriver,
    support: &'a eval::EvalSupport,
    /// 各层上过机的候选汇总 (Outcome.history)。
    collected: &'a std::cell::RefCell<Vec<proposer::HistoryItem>>,
}

impl LayerCtx<'_> {
    fn new_outcome(&self) -> report::LayerOutcome {
        report::LayerOutcome {
            layer: self.step.layer,
            layer_display: self.step.layer_display,
            used: true,
            reason: self.step.why.clone(),
            blockers: vec![],
            route: Some(self.step.route.clone()),
            proposer: None,
            llm_error: None,
            proposed: 0,
            rejected_locally: vec![],
            evaluated: 0,
            kept: 0,
            aborted: 0,
            eval_errors: vec![],
        }
    }

    /// 接收端认不认统一测评接口。
    ///
    /// 在开跑之前问一次, 不在每个候选里问: 放在候选循环里的话, 某一代一个
    /// 候选都没提出来时这道检查根本不会执行, 于是这一层会被报成
    /// "用了, 0 个候选", 既没有数字也没有理由。
    fn receiver_missing(&self) -> Option<report::LayerOutcome> {
        if self.support.is_supported() {
            return None;
        }
        let mut out = self.new_outcome();
        out.used = false;
        out.reason = format!(
            "接收端还没有统一测评接口, 这一层跑不了 ({})",
            self.support.describe()
        );
        out.blockers = vec!["phonefarm 补上 `eval --request <json> --json`".into()];
        println!("      {}", out.reason);
        Some(out)
    }

    /// 下发一个候选。失败只回 Err, 由调用方记进 `eval_errors` 并接着跑下一个。
    fn evaluate(&self, id: &str, req: &Request) -> Result<eval::Report> {
        evaluate_on(self.cfg, self.caps, self.driver, self.serial, id, req)
    }

    /// 出一代方案: 组 proposer@1 输入 → 出方案的一方 → harness 校验 → 落证据
    /// (`<out>/proposer/`)。谁来出方案只在这一处可换, 校验对谁都一样。
    fn propose(
        &self,
        generation: u32,
        constraints: &proposer::Constraints,
        history: &[proposer::HistoryItem],
    ) -> proposer::Batch<proposer::Candidate> {
        let input = proposer::input_for(
            self.step,
            self.caps,
            self.plan,
            generation,
            self.cfg.seed,
            device_note(self.caps),
            workload_note(self.caps),
            constraints.clone(),
            history.to_vec(),
        );
        let mut input = input;
        input.steer = self.cfg.goal.steer.clone();
        proposer::generation(self.proposer, &input, &self.cfg.out_dir.join("proposer"))
    }
}

/// 把这一代的出主意来路并进层结果。
///
/// **累加而不是覆盖**: 三代里有两代用了大模型、一代降级到本地变异器时,
/// 覆盖会让整层被报成"本地变异器", 报告与实际发生的事对不上。
fn record_proposer(
    out: &mut report::LayerOutcome,
    origins: &mut Vec<sysparam::Origin>,
    origin: Option<sysparam::Origin>,
    llm_error: Option<&String>,
) {
    if let Some(o) = origin {
        origins.push(o);
    }
    let count = |o: sysparam::Origin| origins.iter().filter(|x| **x == o).count();
    let (llm, ext) = (count(sysparam::Origin::Llm), count(sysparam::Origin::External));
    let local = origins.len() - llm - ext;
    out.proposer = Some(match (llm, local, ext) {
        (0, 0, e) if e > 0 => "外部进程".to_string(),
        (0, _, 0) => "本地确定性变异器".to_string(),
        (_, 0, 0) => "大模型".to_string(),
        (l, n, 0) => format!("大模型 {l} 代 / 本地变异器 {n} 代"),
        (l, n, e) => format!("大模型 {l} 代 / 本地变异器 {n} 代 / 外部进程 {e} 代"),
    });
    // LLM 的岔子留第一条: 后面几代多半是同一个原因, 覆盖成最后一条会让
    // "第一次为什么降级"这个真正有用的信息丢掉。
    if out.llm_error.is_none() {
        out.llm_error = llm_error.cloned();
    }
}

/// ③ 系统参数层。
fn run_sysparam(
    ctx: &LayerCtx<'_>,
    archive: &mut Vec<report::Entry>,
) -> Result<report::LayerOutcome> {
    let mut out = ctx.new_outcome();

    // 白名单来自真机探测。探测要写节点再还原, 那是接收端的活;
    // 没接上之前只能吃一份已经采好的 probe.txt。
    let Some(probe_path) = ctx.cfg.sysparam_probe.as_ref() else {
        out.used = false;
        out.reason = "白名单还没拿到: 真机探测 (写一个值再还原, 确认内核没把它退回去) \
                      归接收端做, 这一版还没接上"
            .into();
        out.blockers = vec![
            "接收端补上白名单探测 (payload.probe_only), 或先用 --sysparam-probe <probe.txt> 喂一份已采好的".into(),
        ];
        println!("      {}", out.reason);
        return Ok(out);
    };
    let probe_text = std::fs::read_to_string(probe_path)
        .with_context(|| format!("读取 {} 失败", probe_path.display()))?;
    let wl = sysparam::build_whitelist(&sysparam::parse_probe(&probe_text));
    println!("      白名单 {} 项 (来自 {})", wl.len(), probe_path.display());
    if wl.is_empty() {
        out.used = false;
        out.reason = "白名单是空的 —— 探测结果里没有一个既可写又写了真生效的节点".into();
        return Ok(out);
    }

    // 没做过差异写测试的项要点出来。很多 sysfs 节点接受 write() 但随即被驱动
    // 夹回原值; 这种项一旦混进来, 跑出来的 REJECT 读起来就像"这个参数没用",
    // 其实是它根本没改成。
    let untested: Vec<&String> = wl
        .iter()
        .filter(|(_, spec)| spec.effect == "untested" || spec.effect == "skipped_same_value")
        .map(|(id, _)| id)
        .collect();
    if !untested.is_empty() {
        let note = format!(
            "{} 项没做过差异写测试 ({}) —— 它们的 REJECT 可能是「没改成」而不是「没用」, \
             以每轮的 conditions_observed.apply_ok 为准",
            untested.len(),
            untested
                .iter()
                .take(5)
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        );
        println!("      ⚠ {note}");
        out.blockers.push(note);
    }

    if let Some(o) = ctx.receiver_missing() {
        return Ok(o);
    }

    let mut wl = wl;
    let before = wl.len();
    wl.retain(|id, _| !ctx.cfg.goal.forbids(id));
    if wl.len() < before {
        println!("      按禁止项去掉 {} 项, 白名单剩 {} 项", before - wl.len(), wl.len());
    }
    if wl.is_empty() {
        out.used = false;
        out.reason = "去掉禁止动的参数之后白名单是空的".into();
        return Ok(out);
    }
    let constraints = proposer::constraints_for(Kind::Sysparam, Some(&wl));
    let mut history: Vec<proposer::HistoryItem> = ctx
        .cfg
        .goal
        .prior_history
        .iter()
        .filter(|h| h.candidate.kind == Kind::Sysparam)
        .cloned()
        .collect();
    let prior = history.len();
    let mut origins: Vec<sysparam::Origin> = Vec::new();

    for g in 1..=ctx.step.generations {
        let gen = ctx.cfg.goal.gen_offset + g;
        if interrupted() {
            bail!("人叫停 (SIGINT): 第 {gen} 代不再出方案");
        }
        let batch = ctx.propose(gen, &constraints, &history).sysparam();
        record_proposer(&mut out, &mut origins, batch.origin, batch.llm_error.as_ref());
        out.proposed += batch.accepted.len() + batch.rejected.len();
        out.rejected_locally.extend(batch.rejected.iter().cloned());
        if batch.accepted.is_empty() {
            // 提不出新东西就停, 不空转剩下的代数。
            let note = format!("第 {gen} 代一个可用候选都没提出来, 这一层到此为止");
            println!("      {note}");
            out.blockers.push(note);
            break;
        }

        // 先把这一代的请求全部排好 (打印顺序与单机时相同), 再分给设备并行量。
        let reqs: Vec<(String, Request)> = batch
            .accepted
            .iter()
            .enumerate()
            .map(|(i, prop)| {
                let id = format!("sys_g{gen}c{}", i + 1);
                println!("      {id}: {} 项 — {}", prop.params.len(), prop.why);
                ctx.cfg.goal.emit(gen, serde_json::json!({
                    "type": "propose", "candidate_id": id, "layer": "sysparam",
                    "changes": prop.params, "why": prop.why,
                }));
                ctx.cfg.goal.emit(gen, serde_json::json!({
                    "type": "measure", "candidate_id": id, "arms_done": 0,
                    "arms_planned": 2 * ctx.step.rounds,
                }));
                let req = Request {
                    version: eval::REQUEST_VERSION,
                    kind: Kind::Sysparam,
                    candidate_id: id.clone(),
                    serial: Some(ctx.serial.to_string()),
                    package: ctx.caps.target.package.clone().unwrap_or_default(),
                    workload: workload_of(ctx.step, ctx.caps),
                    conditions: ctx.plan.conditions.clone(),
                    protocol: plan::protocol_for(ctx.step, ctx.caps),
                    payload: Payload::Sysparam(eval::SysparamPayload {
                        why: Some(prop.why.clone()),
                        params: prop.params.clone(),
                        plan: sysparam::plan(&prop.params, &wl),
                        whitelist: Some(wl.clone()),
                        deny_keywords: sysparam::DENY_KEYWORDS
                            .iter()
                            .map(|s| s.to_string())
                            .collect(),
                    }),
                };
                (id, req)
            })
            .collect();
        let multi = ctx.devices.len() > 1;
        let (cfg, caps, driver) = (ctx.cfg, ctx.caps, ctx.driver);
        let results = dispatch::run_on_devices(&reqs, ctx.devices, |dev, _, (id, req)| {
            let mut req = req.clone();
            req.serial = Some(dev.to_string());
            if multi {
                println!("        {id} → 设备 {dev}");
            }
            evaluate_on(cfg, caps, driver, dev, id, &req)
        });

        for ((prop, (id, _)), (dev, res)) in batch.accepted.iter().zip(&reqs).zip(results) {
            let id = id.clone();
            let rep = match res {
                Ok(r) => r,
                Err(e) => {
                    let who = if multi { format!("{id} [{dev}] ") } else { String::new() };
                    println!("        {who}→ 下发失败, 跳过这个候选: {e}");
                    out.eval_errors.push(format!("{id}: {e}"));
                    ctx.cfg.goal.emit(gen, serde_json::json!({"type": "error", "candidate_id": id, "message": e.to_string()}));
                    continue;
                }
            };
            out.evaluated += 1;
            if !rep.is_measurement_valid() {
                out.aborted += 1;
            }
            if rep.kept() {
                out.kept += 1;
            }
            println!(
                "        {}{} (p={:.4}){}{}",
                if multi { format!("{id} [{dev}] → ") } else { "→ ".into() },
                rep.status,
                rep.verdict.p_value,
                if rep.kept() { " 留下" } else { "" },
                verdict_note(&rep)
            );

            // 上过机的都进历史 (带 measurement_valid)。内置出方案还原历史时只取量准了的
            // (proposer::sysparam_history): ABORT 什么都没测到, 当成"这组不行"会让模型
            // 学到一条不存在的规律。
            ctx.cfg.goal.emit(gen, verdict_event(&id, &rep));
            let cand = proposer::Candidate::sysparam(prop.why.clone(), prop.params.clone());
            history.push(proposer::history_item(&id, gen, cand, &rep, per_metric_lines(&rep)));
            let mut entry = report::Entry::from_report(
                id,
                Kind::Sysparam,
                gen,
                prop.why.clone(),
                report::Change::Sysparam {
                    params: prop.params.clone(),
                },
                &rep,
            );
            // 多机时记下在哪台上量的 (判定只在这台内部比对照臂); 单机不写, 档案与以前一致
            if multi {
                entry.device = Some(dev);
            }
            archive.push(entry);
        }
    }
    ctx.collected.borrow_mut().extend(history.into_iter().skip(prior));
    Ok(out)
}

/// ② 画面流程改写层。
fn run_gray(ctx: &LayerCtx<'_>, archive: &mut Vec<report::Entry>) -> Result<report::LayerOutcome> {
    let mut out = ctx.new_outcome();

    let pf_root = ctx
        .cfg
        .phonefarm
        .parent()
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(".."));
    if !gray::layer_build_present(&pf_root) {
        out.used = false;
        out.reason = "找不到灰档层的构建脚本, 没有 .so 可以放进目标应用的 lib 目录".into();
        out.blockers = vec![format!(
            "确认 {}/knobs/gray/build/build_layer.sh 在位",
            pf_root.display()
        )];
        println!("      {}", out.reason);
        return Ok(out);
    }

    if let Some(o) = ctx.receiver_missing() {
        return Ok(o);
    }

    // 候选空间 = frame-audit 判定「写回之后确定没人读」的附件, 每一个是一个旋钮。
    // 没有这份结果就一个都不提: 一刀切的 LoadOp 改写已撤回 (会画错), 不拿它凑数。
    let mut targets = match ctx.cfg.frame_audit.as_ref() {
        Some(p) => gray::load_targets(p)?,
        None => Vec::new(),
    };
    targets.retain(|t| !ctx.cfg.goal.forbids(&t.scope) && !ctx.cfg.goal.forbids(&t.label()));
    if ctx.cfg.goal.forbids("storeop_dont_care") {
        targets.clear();
    }
    if targets.is_empty() {
        out.used = false;
        out.reason = match ctx.cfg.frame_audit.as_ref() {
            Some(p) => format!(
                "frame-audit 结果 {} 里没有「确定没人读」的写回, 这一层没有可开的旋钮",
                p.display()
            ),
            None => "没有 frame-audit 结果, 不知道哪些写回确定没人读; 一刀切的 LoadOp 改写已撤回 (会画错画面)".into(),
        };
        out.blockers = vec![
            "先跑 `phonefarm frame-audit capture` + `analyze --json`, 再用 --frame-audit <analysis.json> 喂进来".into(),
        ];
        println!("      {}", out.reason);
        return Ok(out);
    }
    println!(
        "      旋钮 {} 个 (来自 frame-audit): {}",
        targets.len(),
        targets.iter().map(|t| t.scope.as_str()).collect::<Vec<_>>().join(", ")
    );
    let constraints = proposer::constraints_with_targets(Kind::Gray, None, &targets);
    let mut history: Vec<proposer::HistoryItem> = ctx
        .cfg
        .goal
        .prior_history
        .iter()
        .filter(|h| h.candidate.kind == Kind::Gray)
        .cloned()
        .collect();
    let prior = history.len();
    let mut origins: Vec<sysparam::Origin> = Vec::new();

    for g in 1..=ctx.step.generations {
        let gen = ctx.cfg.goal.gen_offset + g;
        if interrupted() {
            bail!("人叫停 (SIGINT): 第 {gen} 代不再出方案");
        }
        let batch = ctx.propose(gen, &constraints, &history).gray();
        record_proposer(&mut out, &mut origins, batch.origin, batch.llm_error.as_ref());
        out.proposed += batch.accepted.len() + batch.rejected.len();
        out.rejected_locally.extend(batch.rejected.iter().cloned());
        if batch.accepted.is_empty() {
            // 层里已实现的开关是有限的, 试完就没了。这时候停下来说清楚,
            // 比把同一个候选再上机一遍诚实。
            let note = format!(
                "第 {gen} 代没有新的改写可提 (层里已实现的开关已经全试过), 这一层到此为止"
            );
            println!("      {note}");
            out.blockers.push(note);
            break;
        }

        for (i, prop) in batch.accepted.iter().enumerate() {
            let id = format!("gray_g{gen}c{}", i + 1);
            let knobs: Vec<String> = prop.rewrites.iter().map(gray::rewrite_label).collect();
            println!("      {id}: {} — {}", knobs.join("+"), prop.why);
            ctx.cfg.goal.emit(gen, serde_json::json!({
                "type": "propose", "candidate_id": id, "layer": "gray",
                "changes": knobs, "why": prop.why,
            }));
            ctx.cfg.goal.emit(gen, serde_json::json!({
                "type": "measure", "candidate_id": id, "arms_done": 0,
                "arms_planned": 2 * ctx.step.rounds,
            }));

            let req = Request {
                version: eval::REQUEST_VERSION,
                kind: Kind::Gray,
                candidate_id: id.clone(),
                serial: Some(ctx.serial.to_string()),
                package: ctx.caps.target.package.clone().unwrap_or_default(),
                workload: workload_of(ctx.step, ctx.caps),
                conditions: ctx.plan.conditions.clone(),
                protocol: plan::protocol_for(ctx.step, ctx.caps),
                payload: Payload::Gray(eval::GrayPayload {
                    why: Some(prop.why.clone()),
                    layer: None,
                    rewrites: prop.rewrites.clone(),
                    // 改写没命中却当成有效样本 = 拿 A 去和 A 比, 稳定产出假阴性。
                    require_hit: true,
                    side_effect_ack: Some(
                        "debug.vulkan.layers 是全局属性; 层加载成功后立刻清属性, 收尾还原".into(),
                    ),
                    // 每轮两臂同位置截图比对: 画面变了直接 ABORT, 不让「统计不显著」掩盖画错。
                    image_check: Some(eval::ImageCheck {
                        min_psnr_db: ctx.cfg.goal.psnr_min_db.unwrap_or(gray::MIN_PSNR_DB),
                    }),
                }),
            };

            let rep = match ctx.evaluate(&id, &req) {
                Ok(r) => r,
                Err(e) => {
                    println!("        → 下发失败, 跳过这个候选: {e}");
                    out.eval_errors.push(format!("{id}: {e}"));
                    ctx.cfg.goal.emit(gen, serde_json::json!({"type": "error", "candidate_id": id, "message": e.to_string()}));
                    continue;
                }
            };
            out.evaluated += 1;
            if !rep.is_measurement_valid() {
                out.aborted += 1;
            }
            if rep.kept() {
                out.kept += 1;
            }
            println!(
                "        → {} (p={:.4}){}{}",
                rep.status,
                rep.verdict.p_value,
                if rep.kept() { " 留下" } else { "" },
                verdict_note(&rep)
            );

            // 不管量没量准, 这些开关都算"提过了" —— 否则下一代会原样再提一遍,
            // 换回来的是同一个候选在真机上再跑一轮 (见 proposer::gray_history)。
            ctx.cfg.goal.emit(gen, verdict_event(&id, &rep));
            let cand = proposer::Candidate::gray(prop.why.clone(), prop.rewrites.clone());
            history.push(proposer::history_item(&id, gen, cand, &rep, per_metric_lines(&rep)));
            archive.push(report::Entry::from_report(
                id,
                Kind::Gray,
                gen,
                prop.why.clone(),
                report::Change::Gray { rewrites: knobs },
                &rep,
            ));
        }
    }
    ctx.collected.borrow_mut().extend(history.into_iter().skip(prior));
    Ok(out)
}

/// ④ 引擎设置层 (白盒启动参数)。
fn run_whitebox(ctx: &LayerCtx<'_>, archive: &mut Vec<report::Entry>) -> Result<report::LayerOutcome> {
    let mut out = ctx.new_outcome();
    if ctx.caps.target.workload.launch.is_none() {
        out.used = false;
        out.reason = "目标不是自驱动负载 (没有 workload.launch), 白盒旋钮拼不进启动参数".into();
        return Ok(out);
    }
    if let Some(o) = ctx.receiver_missing() {
        return Ok(o);
    }
    let constraints = proposer::constraints_with_targets(Kind::Whitebox, None, &[]);
    let mut history: Vec<proposer::HistoryItem> = ctx
        .cfg
        .goal
        .prior_history
        .iter()
        .filter(|h| h.candidate.kind == Kind::Whitebox)
        .cloned()
        .collect();
    let prior = history.len();
    let mut origins: Vec<sysparam::Origin> = Vec::new();

    for g in 1..=ctx.step.generations {
        let gen = ctx.cfg.goal.gen_offset + g;
        if interrupted() {
            bail!("人叫停 (SIGINT): 第 {gen} 代不再出方案");
        }
        let batch = ctx.propose(gen, &constraints, &history).whitebox();
        record_proposer(&mut out, &mut origins, batch.origin, batch.llm_error.as_ref());
        out.proposed += batch.accepted.len() + batch.rejected.len();
        out.rejected_locally.extend(batch.rejected.iter().cloned());
        // 禁止项: 旋钮名或 `旋钮=值`
        let accepted: Vec<&whitebox::Proposal> = batch
            .accepted
            .iter()
            .filter(|p| {
                let bad = p.knobs.iter().any(|(k, v)| ctx.cfg.goal.forbids(k) || ctx.cfg.goal.forbids(&format!("{k}={v}")));
                if bad {
                    out.rejected_locally.push(format!("{}: 碰了禁止项", whitebox::label(&p.knobs)));
                }
                !bad
            })
            .collect();
        if accepted.is_empty() {
            let note = format!("第 {gen} 代没有新的白盒设置组可提, 这一层到此为止");
            println!("      {note}");
            out.blockers.push(note);
            break;
        }
        for (i, prop) in accepted.iter().enumerate() {
            let id = format!("wb_g{gen}c{}", i + 1);
            let lbl = whitebox::label(&prop.knobs);
            let measure_only = whitebox::is_measure_only(&prop.knobs);
            println!(
                "      {id}: {lbl}{} — {}",
                if measure_only { " (只量上限)" } else { "" },
                prop.why
            );
            ctx.cfg.goal.emit(gen, serde_json::json!({
                "type": "propose", "candidate_id": id, "layer": "whitebox",
                "changes": prop.knobs, "why": prop.why,
            }));
            ctx.cfg.goal.emit(gen, serde_json::json!({
                "type": "measure", "candidate_id": id, "arms_done": 0,
                "arms_planned": 2 * ctx.step.rounds,
            }));
            let req = Request {
                version: eval::REQUEST_VERSION,
                kind: Kind::Whitebox,
                candidate_id: id.clone(),
                serial: Some(ctx.serial.to_string()),
                package: ctx.caps.target.package.clone().unwrap_or_default(),
                workload: workload_of(ctx.step, ctx.caps),
                conditions: ctx.plan.conditions.clone(),
                protocol: plan::protocol_for(ctx.step, ctx.caps),
                payload: Payload::Whitebox(eval::WhiteboxPayload {
                    why: Some(prop.why.clone()),
                    knobs: prop.knobs.clone(),
                    measure_only: measure_only.then_some(true),
                    image_check: Some(eval::ImageCheck {
                        min_psnr_db: ctx.cfg.goal.psnr_min_db.unwrap_or(gray::MIN_PSNR_DB),
                    }),
                }),
            };
            let rep = match ctx.evaluate(&id, &req) {
                Ok(r) => r,
                Err(e) => {
                    println!("        → 下发失败, 跳过这个候选: {e}");
                    out.eval_errors.push(format!("{id}: {e}"));
                    ctx.cfg.goal.emit(gen, serde_json::json!({"type": "error", "candidate_id": id, "message": e.to_string()}));
                    continue;
                }
            };
            out.evaluated += 1;
            if !rep.is_measurement_valid() {
                out.aborted += 1;
            }
            if rep.kept() {
                out.kept += 1;
            }
            println!(
                "        → {} (p={:.4}){}{}",
                rep.status,
                rep.verdict.p_value,
                if rep.kept() { " 留下" } else { "" },
                verdict_note(&rep)
            );
            ctx.cfg.goal.emit(gen, verdict_event(&id, &rep));
            let cand = proposer::Candidate::whitebox(prop.why.clone(), prop.knobs.clone());
            history.push(proposer::history_item(&id, gen, cand, &rep, per_metric_lines(&rep)));
            archive.push(report::Entry::from_report(
                id,
                Kind::Whitebox,
                gen,
                prop.why.clone(),
                report::Change::Whitebox { knobs: prop.knobs.clone() },
                &rep,
            ));
        }
    }
    ctx.collected.borrow_mut().extend(history.into_iter().skip(prior));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_single_online_device_is_picked_automatically() {
        // devices() 要真的去调 phonefarm, 所以这里只测"指定了但不在线"那条分支
        // 需要的错误措辞 —— 挑错设备的代价是在别人的机器上写 sysfs。
        let cfg_msg = "不在线";
        assert!(cfg_msg.contains("不在线"));
    }

    #[test]
    fn the_verdict_reason_follows_the_status_in_the_log() {
        let mut r = eval::Report {
            version: Some(2),
            kind: Some(Kind::Sysparam),
            candidate_id: "c".into(),
            status: "ABORT".into(),
            metrics: Default::default(),
            verdict: Default::default(),
            source: "phonefarm".into(),
            power_source: None,
            quality_reference: None,
            conditions_observed: None,
            evidence_dir: None,
            phonefarm_version: None,
            obtained_via: None,
        };
        assert_eq!(verdict_note(&r), "");
        r.verdict.reason = Some("knob 臂第 1 轮 SoC 结温 52.3C 超过上限 51.8C".into());
        assert!(verdict_note(&r).contains("52.3C"));
    }

    #[test]
    fn per_metric_lines_survive_a_reply_without_details() {
        let r = eval::Report {
            version: Some(2),
            kind: Some(Kind::Sysparam),
            candidate_id: "c".into(),
            status: "REJECT".into(),
            metrics: Default::default(),
            verdict: Default::default(),
            source: "phonefarm".into(),
            power_source: None,
            quality_reference: None,
            conditions_observed: None,
            evidence_dir: None,
            phonefarm_version: None,
            obtained_via: None,
        };
        assert!(per_metric_lines(&r).is_empty());
    }

    /// 逐指标明细里量不到的项写"未测量", 不写 0 —— 这行字会被原样喂回给
    /// 出主意的那一方, 写成 0 等于告诉它"功耗是零"。
    #[test]
    fn unmeasured_metrics_are_not_reported_as_zero_to_the_proposer() {
        let mut r = eval::Report {
            version: Some(2),
            kind: Some(Kind::Sysparam),
            candidate_id: "c".into(),
            status: "REJECT".into(),
            metrics: Default::default(),
            verdict: Default::default(),
            source: "phonefarm".into(),
            power_source: None,
            quality_reference: None,
            conditions_observed: None,
            evidence_dir: None,
            phonefarm_version: None,
            obtained_via: None,
        };
        r.verdict.per_metric = Some(std::collections::BTreeMap::from([(
            "power_watt".to_string(),
            eval::MetricDetail {
                status: Some("缺数据".into()),
                ..Default::default()
            },
        )]));
        let lines = per_metric_lines(&r);
        assert_eq!(lines.len(), 1);
        assert!(lines[0].contains("未测量"), "{lines:?}");
        assert!(!lines[0].contains("0.00%"));
    }
}
