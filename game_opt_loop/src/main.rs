//! game_opt_loop CLI

use anyhow::{Context, Result};
use clap::Parser;
use game_opt_loop::engine::evolution::{self, Evolution, EvolutionConfig, EvolutionSummary};
use game_opt_loop::engine::MetricSource;
use game_opt_loop::harness::PhonefarmDriver;
use game_opt_loop::{tracks, TrackMode, VERSION};
use serde::Serialize;
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(name = "game_opt_loop", version = VERSION)]
#[command(about = "移动游戏自主优化与画质跃升闭环 (Rust 原生)", long_about = None)]
pub struct Cli {
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
    power_watt: f32,
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
    for e in front {
        println!(
            "{:<18} {:>10.3} {:>9.3} {:>9}  {:<18} {}",
            e.id,
            e.report.metrics.operator_latency_ms,
            e.report.metrics.power_watt,
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
