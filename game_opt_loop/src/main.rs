//! game_opt_loop -- 移动游戏自主优化与画质跃升闭环 (Rust)

use clap::{Parser, ValueEnum};
use std::path::PathBuf;

mod engine;
mod harness;
mod tracks;

#[derive(Parser, Debug)]
#[command(name = "game_opt_loop")]
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

    /// 真机耗时预算 (毫秒)
    #[arg(short, long)]
    budget_ms: Option<f32>,

    /// Dry run 模式 (不连真机，仅验证本地变异与防爆编译)
    #[arg(long, default_value_t = false)]
    dry_run: bool,

    /// 归档产物输出目录
    #[arg(short, long, default_value = "runs/evo")]
    out: PathBuf,
}

#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub enum TrackMode {
    Sr,
    FrameGen,
}

fn main() -> anyhow::Result<()> {
    env_logger::init();
    let cli = Cli::parse();

    println!("==================================================");
    println!("  game_opt_loop v0.1.0 -- 移动游戏画质跃升闭环");
    println!("  赛道: {:?} | 计划代数: {} | DryRun: {}", cli.track, cli.generations, cli.dry_run);
    println!("==================================================");

    let budget = cli.budget_ms.unwrap_or(match cli.track {
        TrackMode::Sr => 1.5,
        TrackMode::FrameGen => 2.5,
    });

    println!("[1/4] 加载赛道模板: {:?} (预算: {:.2} ms)", cli.track, budget);
    let template = tracks::load_template(cli.track)?;
    println!("      模板字节数: {} bytes", template.len());

    println!("[2/4] 初始化防爆编译验证器 (Shader Validator)...");
    let validator = engine::Validator::new();
    validator.verify_syntax(&template)?;
    println!("      基线模板验证通过 (零语法警告)");

    if cli.dry_run {
        println!("[3/4] DryRun 模式: 模拟生成 1 代候选并做本地编译门禁拦截测试...");
        let candidates = engine::mock_generate_candidates(cli.track, &template, cli.children)?;
        for (i, cand) in candidates.iter().enumerate() {
            match validator.verify_syntax(&cand.shader_source) {
                Ok(_) => println!("      候选 #{} [PASS]: 语法防爆合规", i + 1),
                Err(e) => println!("      候选 #{} [FAIL]: 已秒级拦截非法代码: {}", i + 1, e),
            }
        }
        println!("[4/4] 冒烟验证完成。无需连接真机即可证明 Rust 编译闭环就绪。");
        return Ok(());
    }

    println!("[3/4] 连接 phonefarm 物理底座...");
    let driver = harness::PhonefarmDriver::new();
    let status = driver.probe()?;
    println!("      底座状态: {}", status);

    println!("[4/4] 启动自主进化闭环...");
    engine::run_evolution_loop(cli.track, cli.generations, cli.children, budget, &cli.out)?;

    Ok(())
}
