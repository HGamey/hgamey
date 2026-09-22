//! 静态成本模型 —— **估计器, 不是测量器**
//!
//! # 这个模块产出的数字不是性能结论
//!
//! 它从算子的静态特征 (tap 数、ALU 条数、超越函数条数) 推出一个**量级估计**,
//! 只有两个用途:
//! 1. `--dry-run` 下不连真机也能跑通完整演化闭环, 验证管线本身;
//! 2. 真机排队前的粗筛排序, 少浪费 60s 回放的名额。
//!
//! 任何对外结论都必须来自 `phonefarm` 的物理采样 + 冻结规则 t 检验。
//! 所有本模块产出的 `EvalReport` 一律带 `MetricSource::StaticCostModel`,
//! 归档与报告里都能一眼分辨。
//!
//! # 模型形状与标定依据
//!
//! 参数按 phonefarm 在红魔 NX809J (SM8850 / Adreno 840 @ 902 MHz) 上的实测量级标定
//! (见 `phonefarm/docs/SPEC_SR_LOOP.md` §2 与 `MOBILE_GPU_OPT_ROUTES.md` §1):
//!
//! - 访存与计算在 GPU 上部分重叠, 所以总耗时是「慢路径 + 快路径漏出的一部分」
//!   而不是简单相加, 也不是纯 max (纯 max 会让同 tap 数下的 ALU 差异彻底消失);
//! - 邻域多 tap 绝大部分命中纹理 cache, 不重复付 DRAM 的钱, 因此
//!   **DRAM 必付流量**(读一遍输入图 + 写一遍输出图) 与 **纹理单元发射**分开算;
//! - 移动端的瓶颈通常是搬数据而不是算力, 模型必须复现这个次序 —— 否则演化会
//!   朝着「多算少存」的 PC 直觉跑偏。

use crate::engine::analysis::ShaderFeatures;
use crate::engine::{EvalReport, EvalStatus, MetricSource, Metrics, Verdict};
use crate::tracks::TrackSpec;
use crate::TrackMode;

/// 目标分辨率与设备侧的标定常数。
#[derive(Debug, Clone, Copy)]
pub struct DeviceModel {
    pub name: &'static str,
    /// 送显分辨率的像素数。
    pub out_pixels: f64,
    /// 游戏内部渲染分辨率相对送显的面积比 (720p → 1080p 为 1/2.25)。
    pub render_scale_area: f64,
    /// GPU 可用 DRAM 带宽 (字节 / 毫秒)。
    pub dram_bytes_per_ms: f64,
    /// 纹理单元发射吞吐 (texel / 毫秒)。
    pub tex_issue_per_ms: f64,
    /// 标量 ALU 吞吐 (op / 毫秒)。
    pub alu_per_ms: f64,
    /// 超越函数吞吐 (op / 毫秒)。
    pub transcendental_per_ms: f64,
    /// 一次 dispatch 的固定开销 (ms)。
    pub dispatch_overhead_ms: f64,
    /// GPU 满载时在整机功耗里占的瓦数。
    pub gpu_active_watt: f64,
    /// 一帧的时间预算 (ms), 用于算算子的占空比。
    pub frame_budget_ms: f64,
    /// 游戏本体从原生分辨率降载到低分辨率省下的 GPU 功耗比例。
    pub render_downscale_power_saving: f64,
    /// 访存与计算的**不完全重叠**系数。
    ///
    /// 纯 `max(访存, 计算)` 假设两条路径完美重叠, 那会让同 tap 数下的 ALU 差异
    /// 彻底消失 —— 演化因此看不到「把 exp() 换成多项式」这类真实收益的梯度。
    /// 实际 GPU 上两者只是部分重叠, 较短的那条路径仍会漏出一部分到关键路径上。
    pub overlap_leak: f64,
}

/// 红魔 NX809J: 骁龙 SM8850 / Adreno 840 @ 902 MHz, 1080p 送显。
///
/// 这台机器是 phonefarm 已完成 Gate 0 物理标尺标定的设备。
pub const NX809J_1080P: DeviceModel = DeviceModel {
    name: "NX809J (SM8850 / Adreno 840 @ 902MHz, 1080p)",
    out_pixels: 1920.0 * 1080.0,
    render_scale_area: 1.0 / 2.25,
    dram_bytes_per_ms: 55.0e6,
    tex_issue_per_ms: 45.0e6,
    alu_per_ms: 1.20e9,
    transcendental_per_ms: 1.50e8,
    dispatch_overhead_ms: 0.050,
    gpu_active_watt: 2.60,
    frame_budget_ms: 16.667,
    render_downscale_power_saving: 0.18,
    overlap_leak: 0.25,
};

/// 成本模型的中间量, 全部暴露出来便于核对而不是当黑箱。
#[derive(Debug, Clone, Copy)]
pub struct CostBreakdown {
    pub dram_ms: f64,
    pub tex_ms: f64,
    pub alu_ms: f64,
    pub transcendental_ms: f64,
    /// 访存路径总计。
    pub memory_path_ms: f64,
    /// 计算路径总计。
    pub compute_path_ms: f64,
    pub latency_ms: f64,
}

impl DeviceModel {
    /// 由静态特征估计单帧算子耗时。
    pub fn estimate_latency(&self, f: &ShaderFeatures, track: TrackMode) -> CostBreakdown {
        let out_px = self.out_pixels;

        // DRAM 必付流量。
        // SR: 读一遍低分辨率输入 (面积比 render_scale_area) + 写一遍全分辨率输出。
        // 插帧: 前后两帧都是全分辨率, 各读一遍, 写一遍中间帧。
        let read_bytes = match track {
            TrackMode::Sr => out_px * self.render_scale_area * 4.0,
            TrackMode::FrameGen => out_px * 2.0 * 4.0,
        };
        let write_bytes = out_px * 4.0;
        let dram_ms = (read_bytes + write_bytes) / self.dram_bytes_per_ms;

        // 纹理单元发射: 每个 tap 都要发一次, 即便命中 cache。
        let tex_ms = out_px * f.image_loads as f64 / self.tex_issue_per_ms;

        let alu_ms = out_px * f.alu_ops as f64 / self.alu_per_ms;
        let transcendental_ms =
            out_px * f.transcendental_ops as f64 / self.transcendental_per_ms;

        let memory_path_ms = dram_ms + tex_ms;
        let compute_path_ms = alu_ms + transcendental_ms;

        // 访存与计算部分重叠: 慢的那条走关键路径, 快的那条漏一部分出来。
        let slow = memory_path_ms.max(compute_path_ms);
        let fast = memory_path_ms.min(compute_path_ms);
        let latency_ms = self.dispatch_overhead_ms + slow + fast * self.overlap_leak;

        CostBreakdown {
            dram_ms,
            tex_ms,
            alu_ms,
            transcendental_ms,
            memory_path_ms,
            compute_path_ms,
            latency_ms,
        }
    }

    /// 由算子耗时估计整机功耗。
    ///
    /// 账本: 基线 − (游戏降载省下的) + (算子自己烧掉的)。
    /// 算子的功耗按它在一帧里占的 GPU 时间比例折算。
    pub fn estimate_power(&self, latency_ms: f64, track: &TrackSpec) -> f64 {
        let baseline = track.power_baseline_watt as f64;
        let saved = baseline * self.render_downscale_power_saving;
        let operator = (latency_ms / self.frame_budget_ms) * self.gpu_active_watt;
        baseline - saved + operator
    }
}

/// 由静态特征估计画质。
///
/// 结构性事实, 不是拟合出来的魔法数:
/// - **重建支撑**: tap 越多, 可用的频域信息越完整, 收益递减;
/// - **锐化**: 适度的高频补偿提 PSNR, 过量会过冲/振铃反而掉;
/// - **自适应门控**: 平坦区不锐化, 避免把噪点一起抬起来;
/// - **包络夹紧**: 抑制负瓣核与锐化的过冲。
///
/// 返回的是**估计值**, 只用于本地排序。真 PSNR 必须由真机截帧算。
pub fn estimate_psnr_db(
    f: &ShaderFeatures,
    track: &TrackSpec,
    sharpen_strength: f32,
    adaptive: bool,
    clamped: bool,
) -> f32 {
    let taps = f.image_loads.max(1) as f32;

    // 重建支撑带来的增益, 收益递减。
    let tap_gain = 3.10 * (1.0 - (-(taps - 1.0) / 5.0).exp()) - 1.90;

    // 锐化: 先升后降, 峰值在 ~0.35 附近。
    let s = sharpen_strength;
    let sharpen_gain = if s <= 0.001 {
        0.0
    } else {
        1.45 * s - 2.10 * s * s
    };

    // 自适应门控在有锐化时才有意义。
    let adaptive_gain = if adaptive && s > 0.001 { 0.22 } else { 0.0 };
    let clamp_gain = if clamped { 0.14 } else { 0.0 };

    // 平滑核 (用超越函数算权重) 比纯多项式略好一点点, 但差距很小。
    let smooth_gain = if f.transcendental_ops > 0 { 0.06 } else { 0.0 };

    track.quality_baseline_db + tap_gain + sharpen_gain + adaptive_gain + clamp_gain + smooth_gain
}

/// 由静态特征估计 p95 帧时间。
///
/// 注意 `MOBILE_GPU_OPT_ROUTES.md` §4 的实测结论: 该机型该负载下 **p50 被软件限帧钉死**,
/// 降低 GPU 工作量换不出帧率, 只能换功耗与热预算。模型必须复现这一点 ——
/// 否则演化会朝着一个真机上根本不存在的收益跑。
pub fn estimate_fps_p95_ms(latency_ms: f64, device: &DeviceModel) -> f32 {
    // 限帧器封顶: 帧周期贴着上限, 算子只影响余量里的抖动分量。
    let capped_period = device.frame_budget_ms;
    (capped_period + latency_ms * 0.18) as f32
}

/// 用成本模型给一个候选打分, 产出一份**带来源标记**的评测报告。
#[allow(clippy::too_many_arguments)]
pub fn evaluate(
    candidate_id: &str,
    features: &ShaderFeatures,
    track_mode: TrackMode,
    track: &TrackSpec,
    device: &DeviceModel,
    sharpen_strength: f32,
    adaptive: bool,
    clamped: bool,
) -> (EvalReport, CostBreakdown) {
    let cost = device.estimate_latency(features, track_mode);
    let power = device.estimate_power(cost.latency_ms, track);
    let psnr = estimate_psnr_db(features, track, sharpen_strength, adaptive, clamped);
    let fps_p95 = estimate_fps_p95_ms(cost.latency_ms, device);

    let latency_ms = cost.latency_ms as f32;
    let status = if latency_ms > track.budget_ms {
        EvalStatus::Slow
    } else if psnr < track.quality_baseline_db {
        EvalStatus::PoorQuality
    } else {
        EvalStatus::Pass
    };

    let report = EvalReport {
        candidate_id: candidate_id.to_string(),
        status,
        metrics: Metrics {
            operator_latency_ms: latency_ms,
            fps_p95_ms: Some(fps_p95),
            power_watt: power as f32,
            psnr_db: psnr,
        },
        verdict: Verdict {
            is_pareto_improvement: false,
            // 估计值没有统计显著性可言。用 1.0 这个哨兵明确表示「未做检验」,
            // 而不是伪造一个漂亮的小 p 值。
            p_value: 1.0,
        },
        source: MetricSource::StaticCostModel,
    };

    (report, cost)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::mutate::{Kernel, SrGenome};
    use crate::engine::validator::Validator;
    use crate::tracks::SR;

    fn features_of(src: &str, spec: &crate::tracks::TrackSpec) -> ShaderFeatures {
        let r = Validator::new().gate(src, spec);
        assert!(r.passed, "{:?}", r.violations);
        r.features
    }

    /// 基线 4-tap 双线性必须舒服地落在 1.5ms 预算内 —— 否则整个可行域是空集,
    /// 演化根本无从开始 (这正是 sr_loop 当初 invoke 口径踩的坑)。
    #[test]
    fn baseline_operator_fits_the_budget_with_room_to_spare() {
        let g = SrGenome::default();
        let f = features_of(&g.to_glsl(), &SR);
        let cost = NX809J_1080P.estimate_latency(&f, TrackMode::Sr);
        assert!(
            cost.latency_ms < 0.6,
            "4-tap 基线估计耗时 {:.3} ms, 与实测量级不符",
            cost.latency_ms
        );
    }

    /// 模型必须复现移动端的次序: 访存主导, 不是算力主导。
    #[test]
    fn memory_path_dominates_for_a_typical_operator() {
        let g = SrGenome {
            side: 4,
            kernel: Kernel::PolyWindow,
            ..Default::default()
        };
        let f = features_of(&g.to_glsl(), &SR);
        let cost = NX809J_1080P.estimate_latency(&f, TrackMode::Sr);
        assert!(
            cost.memory_path_ms > cost.compute_path_ms,
            "16-tap 算子的瓶颈算成了计算 (mem {:.3} vs alu {:.3}); 模型跑偏成 PC 直觉了",
            cost.memory_path_ms,
            cost.compute_path_ms
        );
    }

    /// tap 数一涨耗时必须单调上升 —— 这是演化赖以工作的梯度。
    #[test]
    fn latency_grows_monotonically_with_tap_count() {
        let mut prev = 0.0;
        for side in 2..=5 {
            let g = SrGenome {
                side,
                kernel: Kernel::PolyWindow,
                ..Default::default()
            };
            let f = features_of(&g.to_glsl(), &SR);
            let cost = NX809J_1080P.estimate_latency(&f, TrackMode::Sr);
            assert!(
                cost.latency_ms > prev,
                "side={side} 没有让耗时上升 ({:.3} <= {prev:.3})",
                cost.latency_ms
            );
            prev = cost.latency_ms;
        }
    }

    /// 6x6 = 36 tap 必须爆预算: 没有上限的搜索空间等于没有约束。
    #[test]
    fn an_oversized_neighbourhood_blows_the_budget() {
        let g = SrGenome {
            side: 7,
            kernel: Kernel::Gaussian,
            ..Default::default()
        };
        let f = features_of(&g.to_glsl(), &SR);
        let (report, _) = evaluate(
            "x", &f, TrackMode::Sr, &SR, &NX809J_1080P, 0.0, false, false,
        );
        assert_eq!(report.status, EvalStatus::Slow);
    }

    #[test]
    fn power_drops_below_the_native_baseline() {
        let g = SrGenome::default();
        let f = features_of(&g.to_glsl(), &SR);
        let cost = NX809J_1080P.estimate_latency(&f, TrackMode::Sr);
        let w = NX809J_1080P.estimate_power(cost.latency_ms, &SR);
        let drop = (SR.power_baseline_watt as f64 - w) / SR.power_baseline_watt as f64;
        assert!(
            (0.10..=0.20).contains(&drop),
            "功耗下降 {:.1}% 落在 -10%~-20% 目标区间之外",
            drop * 100.0
        );
    }

    /// 超越函数要真的更贵, 否则演化学不会「换成多项式权重」这条真实的优化。
    #[test]
    fn transcendental_weighting_costs_more_than_polynomial() {
        let gauss = SrGenome {
            side: 4,
            kernel: Kernel::Gaussian,
            ..Default::default()
        };
        let poly = SrGenome {
            side: 4,
            kernel: Kernel::PolyWindow,
            ..Default::default()
        };
        let fg = features_of(&gauss.to_glsl(), &SR);
        let fp = features_of(&poly.to_glsl(), &SR);
        assert!(fg.transcendental_ops > fp.transcendental_ops);
        assert!(
            NX809J_1080P
                .estimate_latency(&fg, TrackMode::Sr)
                .compute_path_ms
                > NX809J_1080P
                    .estimate_latency(&fp, TrackMode::Sr)
                    .compute_path_ms
        );
    }

    #[test]
    fn psnr_rewards_more_taps_with_diminishing_returns() {
        let at = |taps: u32| {
            let f = ShaderFeatures {
                image_loads: taps,
                ..Default::default()
            };
            estimate_psnr_db(&f, &SR, 0.0, false, false)
        };
        let (a, b, c) = (at(4), at(16), at(36));
        assert!(a < b && b < c, "PSNR 没有随 tap 单调上升");
        assert!(b - a > c - b, "收益递减没有体现出来");
    }

    #[test]
    fn over_sharpening_is_punished() {
        let f = ShaderFeatures {
            image_loads: 16,
            ..Default::default()
        };
        let moderate = estimate_psnr_db(&f, &SR, 0.35, false, false);
        let excessive = estimate_psnr_db(&f, &SR, 0.85, false, false);
        assert!(
            excessive < moderate,
            "过度锐化没有被惩罚 ({excessive:.3} >= {moderate:.3}); 模型会诱导出振铃算子"
        );
    }

    /// 估计值必须永远带着「这不是测量」的标记。
    #[test]
    fn simulated_reports_are_labelled_and_carry_no_fake_p_value() {
        let f = ShaderFeatures {
            image_loads: 16,
            ..Default::default()
        };
        let (r, _) = evaluate("c1", &f, TrackMode::Sr, &SR, &NX809J_1080P, 0.3, true, true);
        assert_eq!(r.source, MetricSource::StaticCostModel);
        assert!(!r.source.is_measured());
        assert_eq!(r.verdict.p_value, 1.0);
    }

    /// 限帧钉死的事实必须被模型保留: 算子再快也换不出成比例的帧率。
    #[test]
    fn frame_time_stays_pinned_by_the_frame_limiter() {
        let fast = estimate_fps_p95_ms(0.2, &NX809J_1080P);
        let slow = estimate_fps_p95_ms(1.4, &NX809J_1080P);
        assert!(fast >= NX809J_1080P.frame_budget_ms as f32);
        assert!((slow - fast) < 0.5, "帧时间对算子耗时过度敏感, 与实测不符");
    }
}
