//! game_opt_loop CLI

use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};
use game_opt_loop::engine::evolution::{self, Evolution, EvolutionConfig, EvolutionSummary};
use game_opt_loop::engine::MetricSource;
use game_opt_loop::harness::PhonefarmDriver;
use game_opt_loop::optimize;
use game_opt_loop::{tracks, TrackMode, VERSION};
use serde::Serialize;
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(name = "game_opt_loop", version = VERSION)]
#[command(about = "移动游戏自主优化与画质跃升闭环 (Rust 原生)", long_about = None)]
pub struct Cli {
    #[command(subcommand)]
    command: Option<Command>,

    #[command(flatten)]
    legacy: LegacyArgs,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// 一条命令跑完整条优化链路: 摸底 → 三层自动开路 → 真机评测 → 统一档案 + 总报告。
    ///
    /// 三层是: ③ 系统参数 (要 root)、② 画面流程改写 (要能挂 Vulkan 层)、
    /// ① 画面算子 (要白盒载体, 或能挂层的黑盒)。能开哪层由摸底结果决定,
    /// 一层都开不了就只测量并写清原因。
    Optimize(OptimizeArgs),

    /// 起一个本机只读可视化面板, 把三层闭环的产物摊在浏览器里。
    ///
    /// 只读: 不碰设备, 不碰设备锁, 没有任何写接口。
    /// 前端资源内嵌进二进制, 不引外网资源, 断网可用。
    Serve {
        /// 监听端口 (只绑 127.0.0.1)
        #[arg(long, default_value_t = 8765)]
        port: u16,

        /// game_opt_loop 仓库根 (层① 数据与 evidence)
        #[arg(long, default_value = ".")]
        root: PathBuf,

        /// phonefarm 仓库根 (层②③ 数据)。缺省取 `<root>/../phonefarm`
        #[arg(long)]
        phonefarm_root: Option<PathBuf>,

        /// devlock.d / devlock.log 所在目录
        #[arg(long, default_value = "/private/tmp/claude-501")]
        lock_root: PathBuf,

        /// phonefarm 可执行文件。缺省取 `<phonefarm-root>/phonefarm`。
        ///
        /// 「手机」那一页靠它: 面板调 `phonefarm fleet --json` 拿每台手机的现状,
        /// 自己不碰设备。
        #[arg(long)]
        phonefarm_bin: Option<PathBuf>,

        /// 手机状态的缓存时长 (秒)。页面 30 秒自刷新, 这个值略小于它即可。
        #[arg(long, default_value_t = game_opt_loop::viz::fleet::DEFAULT_TTL_S)]
        fleet_ttl_s: u64,

        /// 完全不去读手机 (只看历史产物时用)。
        #[arg(long, default_value_t = false)]
        no_fleet: bool,

        /// 在手机卡片上显示屏幕缩略图。
        ///
        /// 默认关。开了也受两道闸: 别人持有设备锁时不截, 以及最小间隔
        /// (`--phone-screenshot-every`)。图落在 `<phonefarm-root>/tasks/_fleet/`。
        #[arg(long, default_value_t = false)]
        phone_screenshots: bool,

        /// 截图最小间隔 (秒)。
        #[arg(long, default_value_t = 300)]
        phone_screenshot_every: u64,
    },
}

#[derive(Args, Debug)]
pub struct OptimizeArgs {
    /// 游戏名 ("原神" / "genshin"), 白盒载体名 ("refbench"), 或直接给包名。
    #[arg(long)]
    game: String,

    /// 目标设备序列号。不给则要求恰好只有一台在线 —— 多台时不猜,
    /// 挑错设备的代价是在别人的机器上写 sysfs。
    #[arg(long)]
    device: Option<String>,

    /// 包名。名字认不出来时用它指定; 给了就压过内置表的推断。
    #[arg(long)]
    package: Option<String>,

    /// 真机时长预算: `45m` / `2h` / `900s` / 裸数字(按分钟)。
    ///
    /// 默认刻意定小 —— 这条链路一跑就是几十分钟真机时间, 默认值应该是
    /// "先看看"。预算不够时砍掉整层, 而不是降轮数: 降轮数会让统计判定
    /// 够不着显著性, 换回一整轮"什么都不显著"。
    #[arg(long)]
    budget: Option<String>,

    /// 只摸底 + 打印计划, 一个字节都不往设备上写。
    #[arg(long, default_value_t = false)]
    plan: bool,

    /// 产物目录 (caps.json / report.json / report.md / requests/)。
    #[arg(long, default_value = "runs/optimize")]
    out: PathBuf,

    /// phonefarm 二进制路径。
    #[arg(long, default_value = "../phonefarm/phonefarm")]
    phonefarm: PathBuf,

    /// secrets.env 路径 (出主意用的大模型密钥)。
    #[arg(long, default_value = "../phonefarm/secrets.env")]
    secrets: PathBuf,

    /// 完全不走大模型, 只用本地确定性变异器。
    #[arg(long, default_value_t = false)]
    no_llm: bool,

    /// 指定大模型服务名。
    #[arg(long)]
    llm_provider: Option<String>,

    /// 大模型单次请求超时 (秒)。
    #[arg(long)]
    llm_timeout_s: Option<u64>,

    /// 本地变异器的随机种子 (固定种子 → 可复现的降级产出)。
    #[arg(long, default_value_t = 20260923)]
    seed: u64,

    /// 画质真值: 真机截帧目录或单张 PNG。
    #[arg(long)]
    quality_reference: Option<String>,

    /// 已采好的系统参数探测结果 (`probe.txt`)。
    ///
    /// 白名单是真机探测结果的函数, 而探测要写节点再还原 —— 那归接收端。
    /// 接收端补上之前, 用这个参数喂一份已有的探测结果, 先把出主意那一半跑通。
    #[arg(long)]
    sysparam_probe: Option<PathBuf>,

    /// 出方案的一方: `llm` (缺省: 大模型, 挂了退回本地变异器) / `local` (本地确定性
    /// 变异器, 同 --no-llm) / `cmd:<可执行文件>` (外部进程: stdin 收一份 proposer@1 输入,
    /// stdout 回一份候选; 契约见 contracts/proposer_*.schema.json, 接入说明见 docs/INTEGRATION.md)。
    /// 给两个以上就是对比模式: 同一游戏 / 设备 / 预算依次各跑一遍, 输出目录下多一份 compare.md。
    #[arg(long = "proposer", value_name = "SPEC")]
    proposer: Vec<String>,

    /// 外部出方案进程单次超时 (秒)。
    #[arg(long, default_value_t = game_opt_loop::optimize::proposer::DEFAULT_TIMEOUT_S)]
    proposer_timeout_s: u64,

    /// 设备锁 label。缺省取环境 `DEVLOCK_LABEL`, 再缺省 `game_opt_loop`。
    /// lead 预排了队列时要用队列里那个名字。
    ///
    /// optimize (含 `--plan`) 自己拿锁、心跳、释放; 外面不要再用 `devlock run`
    /// 包另一个 label —— 包同一个 label 没问题 (认得出是自己这一侧的, 原地执行)。
    #[arg(long)]
    lock_label: Option<String>,

    /// 最多排多久队: `45m` / `2h` / `900s`。等满还没轮到就退出, 不碰设备。
    #[arg(long, default_value = "2h")]
    lock_wait: String,

    /// 不拿设备锁 (只在确定没人共用这台手机时用)。
    #[arg(long, default_value_t = false)]
    no_devlock: bool,
}

#[derive(Args, Debug)]
pub struct LegacyArgs {
    /// 目标优化赛道: sr (超分辨率) 或 frame_gen (双倍插帧)
    #[arg(short, long, value_enum, default_value_t = TrackMode::Sr)]
    track: TrackMode,

    /// 演化代际总数
    #[arg(short, long, default_value_t = 5)]
    generations: u32,

    /// 单代变异候选算子数
    #[arg(short, long, default_value_t = 4)]
    children: u32,

    /// 真机耗时预算 (毫秒)。默认取赛道契约值
    #[arg(short, long)]
    budget_ms: Option<f32>,

    /// Dry run: 不连真机, 用静态成本模型估分跑通完整闭环
    #[arg(long, default_value_t = false)]
    dry_run: bool,

    /// 只跑防爆门禁红队语料, 输出拦截率后退出
    #[arg(long, default_value_t = false)]
    self_check: bool,

    /// 只把指定文件送进防爆门禁并打印结论后退出。
    /// 文件可以是裸 GLSL, 也可以是含 ```glsl 代码块的 LLM 回包 (逐块校验)
    #[arg(long)]
    gate_file: Option<PathBuf>,

    /// 归档产物输出目录
    #[arg(short, long, default_value = "runs/evo")]
    out: PathBuf,

    /// 演化随机种子 (固定种子 → 可复现的 dry-run 结果)
    #[arg(long, default_value_t = 20260921)]
    seed: u64,

    /// 指定 LLM 服务名 (不指定则按密钥可用性自动挑)
    #[arg(long)]
    llm_provider: Option<String>,

    /// LLM 单次请求超时 (秒)。不给则用服务表里的默认值
    #[arg(long)]
    llm_timeout_s: Option<u64>,

    /// 完全不走 LLM, 只用本地确定性变异器。
    /// 用于离线冒烟: 一次真实的多算子变异请求要几分钟, 冒烟不该被外部服务卡住
    #[arg(long, default_value_t = false)]
    no_llm: bool,

    /// 冷机门槛 (摄氏度)。
    ///
    /// DESIGN 的防噪声协议写的是 <32C, 那个数**假定挂了外置风扇强吹**。
    /// 没有风扇时, 屏幕亮着加上渲染场景循环跑, 机器稳定在 38~47C,
    /// 32C 永远等不到 —— 会直接耗尽等冷超时。
    /// 这个参数把该前提显式化: 改了它就是在放宽冻结协议, 必须在报告里说明。
    #[arg(long, default_value_t = 32.0)]
    cool_c: f32,

    /// 真机评测载体: refbench (真实渲染场景, 缺省) 或 headless (孤立工装)。
    /// refbench 下档案里存的是场景内的真实代价 —— 帧时 p95 与整机瓦数
    #[arg(long, default_value = "refbench")]
    carrier: String,

    /// refbench 每臂渲染帧数
    #[arg(long, default_value_t = 900)]
    scene_frames: u32,

    /// refbench 场景 fragment 负载强度
    #[arg(long, default_value_t = 5)]
    scene_intensity: u32,

    /// 画质真值: 真机截帧目录 (或单张 PNG)。
    ///
    /// 不给就退回 runner 内置的程序化图案 —— 那是没有真实帧时的确定性替代品,
    /// 高频细节远少于真实游戏画面。实测同一算子在两者上差约 11 dB
    /// (程序化 42.826 dB / 真机截帧 31.333 dB), 用合成图案挑出来的
    /// "画质更好的算子", 未必在真实画面上也更好。
    /// 帧集用 `../phonefarm/phonefarm capture` 采。
    #[arg(long)]
    quality_reference: Option<String>,

    /// secrets.env 路径
    #[arg(long, default_value = "../phonefarm/secrets.env")]
    secrets: PathBuf,

    /// 验收报告输出路径
    #[arg(long, default_value = "evidence/M0_report.json")]
    evidence: PathBuf,

    /// 不写验收报告
    #[arg(long, default_value_t = false)]
    no_evidence: bool,
}

#[allow(clippy::too_many_arguments)]
fn run_serve(
    port: u16,
    root: PathBuf,
    phonefarm_root: Option<PathBuf>,
    lock_root: PathBuf,
    phonefarm_bin: Option<PathBuf>,
    fleet_ttl_s: u64,
    no_fleet: bool,
    phone_screenshots: bool,
    phone_screenshot_every: u64,
) -> Result<()> {
    let root = root
        .canonicalize()
        .with_context(|| format!("--root {} 不存在", root.display()))?;
    let mut paths = game_opt_loop::viz::VizPaths::from_gol_root(root);
    if let Some(p) = phonefarm_root {
        paths.phonefarm_root = p;
    }
    paths.lock_root = lock_root.clone();
    // 路径是上面几个参数算出来的, 所以 fleet 配置要在它们定完之后重建一遍。
    paths.fleet = if no_fleet {
        None
    } else {
        Some(game_opt_loop::viz::fleet::FleetConfig {
            bin: phonefarm_bin.unwrap_or_else(|| paths.phonefarm_root.join("phonefarm")),
            cwd: paths.phonefarm_root.clone(),
            ttl: std::time::Duration::from_secs(fleet_ttl_s),
            timeout: std::time::Duration::from_secs(
                game_opt_loop::viz::fleet::DEFAULT_TIMEOUT_S,
            ),
            lock_dir: Some(lock_root),
            // 图必须落在 phonefarm 仓库之内 —— /api/file 的白名单只认两个仓库根,
            // 落在别处的话卡片上那个 <img> 会 403。
            screenshot_dir: phone_screenshots
                .then(|| paths.phonefarm_root.join("tasks/_fleet")),
            screenshot_every_s: phone_screenshot_every,
            enabled: true,
        })
    };
    game_opt_loop::viz::serve::run(paths, port)
}

/// M0 验收报告。
#[derive(Debug, Serialize)]
struct M0Report {
    schema: &'static str,
    tool: &'static str,
    version: &'static str,
    generated_at: String,
    track: String,
    /// 本次运行的数据来源。simulated 的数字**不是**性能结论。
    metric_source: MetricSource,
    data_disclaimer: &'static str,
    host: HostInfo,
    harness: HarnessInfo,
    gate_self_check: evolution::SelfCheckResult,
    evolution: EvolutionSummary,
    pareto_front: Vec<FrontRow>,
}

#[derive(Debug, Serialize)]
struct HostInfo {
    os: &'static str,
    arch: &'static str,
    rustc_target: &'static str,
}

#[derive(Debug, Serialize)]
struct HarnessInfo {
    phonefarm_binary: String,
    probe: String,
    /// 设备是否在线且 phonefarm 可执行。
    device_online: bool,
    /// phonefarm 是否已实现算子评测的接收端。
    /// 设备在线 ≠ 真机链路已通, 这两件事必须分开记。
    operator_eval_subcommand: game_opt_loop::harness::SubcommandStatus,
    /// 只有上面两条同时成立, 才拿得到真机数字。
    can_evaluate_on_device: bool,
    llm_provider: Option<String>,
    llm_error: Option<String>,
}

#[derive(Debug, Serialize)]
struct FrontRow {
    id: String,
    generation: u32,
    origin: game_opt_loop::engine::CandidateOrigin,
    notes: Option<String>,
    operator_latency_ms: f32,
    power_watt: Option<f32>,
    /// 功耗为什么没参与支配排序; None = 参与了。
    #[serde(skip_serializing_if = "Option::is_none")]
    power_excluded_reason: Option<String>,
    psnr_db: Option<f32>,
    source: MetricSource,
}

fn main() -> Result<()> {
    // naga 在校验失败时会自己往 stderr 打一条 ERROR 日志。那条信息我们已经
    // 作为 IR_VALIDATION_FAILED 违规原样收进门禁报告了, 再让它裸奔到终端上
    // 只会让「门禁正常拦截了一个非法算子」看起来像「工具自己炸了」。
    // 默认关掉它; 需要排查时仍可用 RUST_LOG=naga=debug 打开。
    env_logger::Builder::from_env(
        env_logger::Env::default().default_filter_or("info,naga=off"),
    )
    .init();

    let cli = Cli::parse();

    println!("==================================================");
    println!("  game_opt_loop v{VERSION} — 移动游戏画质跃升闭环");
    println!("==================================================");

    match cli.command {
        Some(Command::Optimize(args)) => return run_optimize(args),
        Some(Command::Serve {
            port,
            root,
            phonefarm_root,
            lock_root,
            phonefarm_bin,
            fleet_ttl_s,
            no_fleet,
            phone_screenshots,
            phone_screenshot_every,
        }) => {
            return run_serve(
                port,
                root,
                phonefarm_root,
                lock_root,
                phonefarm_bin,
                fleet_ttl_s,
                no_fleet,
                phone_screenshots,
                phone_screenshot_every,
            )
        }
        None => {}
    }
    // 没给子命令 = 老的单赛道演化入口, 原样保留。
    let cli = cli.legacy;

    if cli.self_check {
        return run_self_check();
    }

    if let Some(path) = cli.gate_file.as_ref() {
        return run_gate_file(path, cli.track);
    }

    let spec = tracks::spec(cli.track);
    let budget = cli.budget_ms.unwrap_or(spec.budget_ms);

    println!(
        "赛道: {} | 代数: {} | 每代候选: {} | 预算: {:.2} ms | 画质基线: {:.3} dB",
        spec.display, cli.generations, cli.children, budget, spec.quality_baseline_db
    );

    // ---- 1. 基线模板自检 ----
    println!("\n[1/4] 加载赛道模板并做门禁自检...");
    let template = tracks::load_template(cli.track)?;
    let gate = game_opt_loop::engine::Validator::new().gate(&template, spec);
    if !gate.passed {
        anyhow::bail!("赛道基线模板自身不合规: {}", gate.feedback_line());
    }
    println!(
        "      模板 {} 字节 → SPIR-V {} 字节, 门禁耗时 {} us",
        template.len(),
        gate.spirv_bytes.unwrap_or(0),
        gate.elapsed_us
    );

    // ---- 2. 红队语料 ----
    println!("\n[2/4] 防爆门禁红队语料...");
    let check = evolution::self_check();
    println!(
        "      恶意算子 {}/{} 全部拦下 (拦对原因 {}), 合规算子 {}/{} 全部放行, 最慢 {} us",
        check.intercepted,
        check.total,
        check.exact_code_matches,
        check.benign_passed,
        check.benign_total,
        check.max_gate_us
    );
    if check.intercepted < check.total {
        anyhow::bail!(
            "有 {} 条恶意算子漏网, 拒绝继续",
            check.total - check.intercepted
        );
    }

    // ---- 3. 底座探活 ----
    println!("\n[3/4] phonefarm 物理底座探活...");
    let driver = PhonefarmDriver::new();
    let probe = driver.probe();
    println!("      {}", probe.describe());
    let subcommand = driver.probe_subcommand();
    println!("      {}", subcommand.describe());
    let device_path_ready = probe.can_evaluate() && subcommand.is_supported();

    if !cli.dry_run && !device_path_ready {
        anyhow::bail!(
            "真机模式拿不到实测数据。\n  底座: {}\n  接收端: {}\n\
             如果只想验证本地闭环, 加 --dry-run。",
            probe.describe(),
            subcommand.describe()
        );
    }

    // 画质真值在本地先看一眼再开跑。这条路径要等到真机跑起来、画质补测那一步
    // 才会被接收端读到 —— 打错一个字母的代价是几小时之后拿到一批对着程序化图案
    // 量出来的 dB, 而终端上那行配置照样写着漂亮的目录名。
    if let Some(r) = cli.quality_reference.as_ref() {
        let n = count_reference_frames(r)
            .with_context(|| format!("画质真值 --quality-reference {r} 不可用"))?;
        println!("      画质真值: {r} ({n} 张参考帧)");
    }

    // ---- 4. 演化 ----
    println!("\n[4/4] 启动自主进化闭环...");
    if cli.dry_run {
        println!(
            "      ⚠ DRY-RUN: 指标由静态成本模型估计, **不是真机测量值**, 不可作为性能结论。"
        );
    }

    let config = EvolutionConfig {
        track: cli.track,
        generations: cli.generations,
        children: cli.children,
        budget_ms: budget,
        out_dir: cli.out.clone(),
        dry_run: cli.dry_run,
        seed: cli.seed,
        prefer_provider: cli.llm_provider.clone(),
        secrets_path: cli.secrets.clone(),
        use_llm: !cli.no_llm,
        llm_timeout_s: cli.llm_timeout_s,
        device: evolution::default_device(),
        carrier: cli.carrier.clone(),
        scene_frames: cli.scene_frames,
        scene_intensity: cli.scene_intensity,
        cool_c: cli.cool_c,
        quality_reference: cli.quality_reference.clone(),
    };

    let mut evo = Evolution::new(config)?;
    match (evo.llm_available(), evo.llm_error()) {
        (true, _) => println!("      LLM 变异通道: 就绪 (单次多算子变异约需数分钟)"),
        (false, Some(e)) => println!("      LLM 变异通道: 不可用 ({e}) → 全程走本地确定性变异器"),
        (false, None) => println!("      LLM 变异通道: 已按 --no-llm 关闭 → 全程走本地确定性变异器"),
    }

    let summary = evo.run()?;
    print_summary(&summary, &evo);

    if !cli.no_evidence {
        let report = M0Report {
            schema: "game_opt_loop/M0_report@1",
            tool: "game_opt_loop",
            version: VERSION,
            generated_at: evolution::now(),
            track: spec.id.to_string(),
            metric_source: summary.metric_source,
            data_disclaimer:
                "metric_source = static_cost_model 时, latency/power/psnr 全部是静态估计值, \
                 不是真机测量, 不带统计显著性, 不可用于任何对外性能结论; \
                 只有 metric_source = phonefarm 的记录才是实测数据。",
            host: HostInfo {
                os: std::env::consts::OS,
                arch: std::env::consts::ARCH,
                rustc_target: env!("CARGO_PKG_NAME"),
            },
            harness: HarnessInfo {
                phonefarm_binary: driver.binary().display().to_string(),
                probe: probe.describe(),
                device_online: probe.can_evaluate(),
                operator_eval_subcommand: subcommand.clone(),
                can_evaluate_on_device: device_path_ready,
                llm_provider: if evo.llm_available() {
                    Some("configured".to_string())
                } else {
                    None
                },
                llm_error: evo.llm_error().map(|s| s.to_string()),
            },
            gate_self_check: check,
            pareto_front: evo
                .archive()
                .front()
                .iter()
                .map(|e| FrontRow {
                    id: e.id.clone(),
                    generation: e.generation,
                    origin: e.origin,
                    notes: e.notes.clone(),
                    operator_latency_ms: e.report.metrics.operator_latency_ms,
                    power_watt: e.report.metrics.power_watt,
                    power_excluded_reason: e
                        .report
                        .power_exclusion_reason()
                        .map(str::to_string),
                    psnr_db: e.report.metrics.psnr_db,
                    source: e.report.source,
                })
                .collect(),
            evolution: summary,
        };

        if let Some(dir) = cli.evidence.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(&cli.evidence, serde_json::to_string_pretty(&report)?)
            .with_context(|| format!("写入验收报告失败: {}", cli.evidence.display()))?;
        println!("\n验收报告: {}", cli.evidence.display());
    }

    Ok(())
}

/// `optimize` 子命令。
fn run_optimize(args: OptimizeArgs) -> Result<()> {
    let budget = match args.budget.as_deref() {
        Some(s) => optimize::plan::Budget::parse(s)?,
        None => optimize::plan::Budget::default(),
    };

    let cfg = optimize::Config {
        game: args.game,
        package: args.package,
        serial: args.device,
        budget,
        plan_only: args.plan,
        out_dir: args.out,
        phonefarm: args.phonefarm,
        secrets: args.secrets,
        use_llm: !args.no_llm,
        llm_provider: args.llm_provider,
        llm_timeout_s: args.llm_timeout_s,
        seed: args.seed,
        quality_reference: args.quality_reference,
        sysparam_probe: args.sysparam_probe,
        proposer: optimize::proposer::Options {
            specs: args.proposer,
            timeout_s: args.proposer_timeout_s,
        },
    };

    // 摸底 (含 --plan) 就要问设备, 所以锁在最前面拿。守卫活到 run 返回 ——
    // 正常结束、`?` 提前返回、panic 展开都会释放。
    let _lock = if args.no_devlock {
        game_opt_loop::devlock::Guard::disabled()
    } else {
        let wait = game_opt_loop::devlock::parse_wait(&args.lock_wait)?;
        let label = game_opt_loop::devlock::resolve_label(args.lock_label.as_deref());
        let Some(script) = game_opt_loop::devlock::script_path(&cfg.phonefarm) else {
            anyhow::bail!(
                "找不到设备锁脚本 (DEVLOCK_BIN 未设, {} 旁边没有 tools/devlock, PATH 上也没有 devlock)。\
                 没拿到锁不碰设备; 确实没人共用这台手机时可加 --no-devlock",
                cfg.phonefarm.display()
            );
        };
        game_opt_loop::devlock::Guard::acquire(script, label, wait)?
    };

    // 给了两个以上 --proposer 就是对比模式: 同一游戏 / 设备 / 预算依次跑, 出 compare.md。
    if cfg.proposer.specs.len() >= 2 {
        optimize::compare::run(&cfg)?;
        return Ok(());
    }
    let outcome = optimize::run(&cfg)?;
    if outcome.plan_only {
        println!("\n(--plan: 只摸底 + 排计划, 没有对设备做任何写入)");
    }
    Ok(())
}

/// 把一个文件送进防爆门禁。
///
/// 既吃裸 GLSL, 也吃 LLM 的原始回包 (自动按 ```glsl 围栏拆块逐个校验) ——
/// 这样线上遇到可疑产物时, 不用先手工剪裁就能复现门禁判定。
fn run_gate_file(path: &std::path::Path, track: TrackMode) -> Result<()> {
    let spec = tracks::spec(track);
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("读取 {} 失败", path.display()))?;

    let blocks = game_opt_loop::engine::generator::extract_shaders(&raw);
    let shaders: Vec<String> = if blocks.is_empty() {
        vec![raw]
    } else {
        blocks
    };

    let validator = game_opt_loop::engine::Validator::new();
    let mut passed = 0usize;
    println!("\n门禁: {} (赛道 {})", path.display(), spec.id);

    for (i, src) in shaders.iter().enumerate() {
        let r = validator.gate(src, spec);
        if r.passed {
            passed += 1;
            let f = &r.features;
            println!(
                "  #{} [PASS] SPIR-V {} 字节 | {} tap / {} ALU / {} 超越 / 循环上界 {} | 工作组 {:?} | {} us",
                i + 1,
                r.spirv_bytes.unwrap_or(0),
                f.image_loads,
                f.alu_ops,
                f.transcendental_ops,
                f.max_loop_trip,
                f.workgroup_size,
                r.elapsed_us
            );
        } else {
            println!("  #{} [FAIL] {} us", i + 1, r.elapsed_us);
            for v in &r.violations {
                println!("        {v}");
            }
        }
    }

    println!("\n{}/{} 通过。", passed, shaders.len());
    Ok(())
}

fn run_self_check() -> Result<()> {
    let r = evolution::self_check();
    println!("\n防爆门禁红队语料:");
    for c in &r.cases {
        println!(
            "  [{}] {:<34} 期望 {:<26} 实际 {}",
            if c.intercepted { "拦下" } else { "漏网" },
            c.name,
            c.expected,
            if c.actual_codes.is_empty() {
                "(放行)".to_string()
            } else {
                c.actual_codes.join(",")
            }
        );
    }
    println!(
        "\n拦截率 {:.1}% ({}/{}) | 拦对原因 {}/{} | 合规放行 {}/{} | 最慢门禁 {} us",
        r.interception_rate * 100.0,
        r.intercepted,
        r.total,
        r.exact_code_matches,
        r.total,
        r.benign_passed,
        r.benign_total,
        r.max_gate_us
    );

    if r.intercepted < r.total || r.benign_passed < r.benign_total {
        anyhow::bail!("门禁自检未通过");
    }
    Ok(())
}

/// 数一眼 `--quality-reference` 指到的地方有几张图。
///
/// 只做本地存在性检查, 不解码 —— 尺寸合不合格 (必须 >= 1920x1080) 由接收端
/// 在裁剪时判, 这里的目的只是别让一个打错的路径拖到几小时之后才暴露。
fn count_reference_frames(path: &str) -> Result<usize> {
    let p = std::path::Path::new(path);
    if p.is_file() {
        return Ok(1);
    }
    if !p.is_dir() {
        anyhow::bail!("既不是文件也不是目录 (当前工作目录 {:?})", std::env::current_dir()?);
    }
    let n = std::fs::read_dir(p)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|f| {
            f.is_file()
                && matches!(
                    f.extension()
                        .and_then(|e| e.to_str())
                        .map(|e| e.to_ascii_lowercase())
                        .as_deref(),
                    Some("png" | "jpg" | "jpeg")
                )
        })
        .count();
    if n == 0 {
        anyhow::bail!("目录里没有 png/jpg 图片");
    }
    Ok(n)
}

fn print_summary(s: &EvolutionSummary, evo: &Evolution) {
    println!("\n---------- 演化汇总 ----------");
    println!(
        "候选 {} 个: 已评测 {} / 门禁打回 {} / 指纹去重 {} / 评测出错 {}",
        s.total_candidates, s.evaluated, s.gate_rejected, s.duplicates_skipped, s.eval_errors
    );
    println!("门禁拦截率: {:.1}%", s.gate_rejection_rate * 100.0);
    if !s.gate_rejections_by_code.is_empty() {
        for (code, n) in &s.gate_rejections_by_code {
            println!("    {code}: {n}");
        }
    }
    println!("档案 {} 条, Pareto 前沿 {} 条", s.archive_size, s.front_size);

    let series: Vec<String> = s
        .best_psnr_series
        .iter()
        .map(|v| v.map_or("-".to_string(), |x| format!("{x:.3}")))
        .collect();
    println!("累计最优画质序列 (dB): {}", series.join(" → "));

    // 画质真值是什么必须跟着 dB 一起说: 绝对 dB 不可跨参考图比较,
    // 同一算子在程序化图案上 42.8 dB, 换成真机截帧只剩 31.3 dB。
    let q = &s.quality_reference;
    if q.observed.is_empty() {
        println!(
            "画质真值: 接收端未报 (成本模型估计值, 或旧版 phonefarm){}",
            q.requested
                .as_deref()
                .map(|r| format!("; 本次请求的是 {r}"))
                .unwrap_or_default()
        );
    } else {
        println!(
            "画质真值: {} (真实帧报告 {} 份 / 程序化图案 {} 份{})",
            q.observed.join(" + "),
            q.real_frame_reports,
            q.procedural_reports,
            if q.unknown_kind_reports > 0 {
                format!(" / 不认识的来源 {} 份", q.unknown_kind_reports)
            } else {
                String::new()
            }
        );
        // 要了真实帧却一份都没用上 = 那些 dB 不是你以为的那把尺子量的。
        if q.requested.is_some() && q.real_frame_reports == 0 {
            println!(
                "      ⚠ 请求了真机参考帧 {}, 但没有一份报告用上它 —— \
                 上面的 dB 仍然来自程序化图案, 不是真实画面",
                q.requested.as_deref().unwrap_or("?")
            );
        }
    }

    println!("\n---------- Pareto 前沿 ----------");
    println!(
        "{:<18} {:>10} {:>9} {:>9}  {:<18} 形态",
        "id", "延迟(ms)", "功耗(W)", "画质(dB)", "来源"
    );
    let mut front = evo.archive().front();
    front.sort_by(|a, b| {
        a.report
            .metrics
            .operator_latency_ms
            .partial_cmp(&b.report.metrics.operator_latency_ms)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    // 前沿里只要有一条功耗没参与比较, 就把原因讲清楚 —— 否则表里那一列
    // 看着像是也在被优化, 而它其实没有。
    let excluded: Vec<String> = front
        .iter()
        .filter_map(|e| e.report.power_exclusion_reason().map(str::to_string))
        .fold(Vec::new(), |mut acc, r| {
            if !acc.contains(&r) {
                acc.push(r);
            }
            acc
        });
    if !excluded.is_empty() {
        println!("  * = 该功耗未参与支配排序: {}", excluded.join("; "));
    }
    for e in front {
        println!(
            "{:<18} {:>10.3} {:>9} {:>9}  {:<18} {}",
            e.id,
            e.report.metrics.operator_latency_ms,
            // 没参与支配排序的功耗后面缀个 *, 免得表格看着像它也在被比较。
            match (e.report.metrics.power_watt, e.report.power_exclusion_reason()) {
                (Some(w), None) => format!("{w:.3}"),
                (Some(w), Some(_)) => format!("{w:.3}*"),
                (None, _) => "未测量".to_string(),
            },
            e.report
                .metrics
                .psnr_db
                .map(|q| format!("{q:.3}"))
                .unwrap_or_else(|| "未测量".into()),
            match e.report.source {
                MetricSource::Phonefarm => "phonefarm(实测)",
                MetricSource::StaticCostModel => "cost_model(估计)",
            },
            e.notes.as_deref().unwrap_or("-")
        );
    }

    if s.metric_source == MetricSource::StaticCostModel {
        println!(
            "\n⚠ 以上全部为静态成本模型的**估计值**, 不是真机测量, 不带统计显著性。\n\
             真机数据需要 phonefarm 在冷机 <32C 下做 60s 确定性回放 + A/B/A/B 交替 + t 检验 (p<0.01)。"
        );
    }

    println!("\n归档: {}", s.archive_path);
}
