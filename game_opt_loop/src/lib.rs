//! game_opt_loop — 移动游戏自主优化与画质跃升闭环 (Rust 原生)
//!
//! 本 crate 负责**算法进化与管线编排**; 所有真机硬件操作归 `../phonefarm`。
//!
//! - [`tracks`] — 赛道契约: 零拷贝槽位、耗时预算、画质基线
//! - [`engine::validator`] — 本地防爆编译门禁 (GLSL → naga IR → 策略检查 → SPIR-V)
//! - [`engine::analysis`] — IR 静态分析: 循环有界性证明、开销特征提取
//! - [`engine::mutate`] — 确定性本地变异器 (LLM 的兜底 / dry-run 的算子来源)
//! - [`engine::generator`] — LLM 变异编排
//! - [`engine::cost_model`] — 静态成本模型 (**估计器, 不是测量器**)
//! - [`engine::pareto`] / [`engine::archive`] — 三目标支配排序与优胜档案
//! - [`engine::evolution`] — 多代闭环
//! - [`harness`] — phonefarm 物理底座驱动
//! - [`optimize`] — `optimize` 子命令: 摸底 → 三层自动开路 → 统一测评 → 总报告
//! - [`viz`] — 本地只读可视化面板 (`serve` 子命令)

use clap::ValueEnum;
use serde::{Deserialize, Serialize};

pub mod devlock;
pub mod engine;
pub mod goal;
pub mod harness;
pub mod optimize;
pub mod tracks;
pub mod viz;

/// 优化赛道。
#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TrackMode {
    /// 超分辨率: 低分辨率颜色缓冲 → 高分辨率送显。
    Sr,
    /// 双倍插帧: 相邻两帧 → 中间帧。
    ///
    /// 同时接受 `frame_gen`: clap 缺省把枚举名 kebab 化成 `frame-gen`,
    /// 而契约 (`candidate.schema.json` / `eval_request.schema.json`) 里的
    /// 赛道 id 是 `frame_gen`。两种写法都收, 免得命令行与契约对不上。
    #[value(alias = "frame_gen")]
    FrameGen,
}

impl TrackMode {
    pub fn id(self) -> &'static str {
        tracks::spec(self).id
    }
}

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
