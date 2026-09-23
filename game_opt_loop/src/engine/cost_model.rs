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
//! **2026-09-22 用真机实测回标过一次。** 标定数据来自 phonefarm `gpu-op` 在
//! 红魔 NX809J (SM8850 / Adreno 840) 上对 5 个真实候选算子的 VkQueryPool 计时
//! (1280x720 → 1920x1080, 每个 60 次采样, 轮内离散 1.2%~2.6%):
//!
//! | 算子 | tap | ALU | 超越 | 实测 ms |
//! |---|---|---|---|---|
//! | gen0_baseline | 1 | 13 | 0 | 0.510 |
//! | gen1_loc3 | 4 | 102 | 0 | 0.872 |
//! | gen2_loc3 | 4 | 86 | 4 | 0.937 |
//! | gen1_loc1 | 4 | 111 | 0 | 0.945 |
//! | gen2_loc2 | 9 | 200 | 0 | 1.412 |
//!
//! 回标改了两件事:
//!
//! 1. **各路径改为相加, 不再取 max**。原先是「慢路径 + 快路径漏出一部分」,
//!    那是从"访存与计算重叠"推出来的。实测数据否掉了它: 一旦把 ALU 的真实
//!    单价代进去, `gen2_loc2` 的计算路径会超过访存路径, max 翻边后模型给出
//!    2.61 ms, 而真机是 1.412 ms。数据说的是在实测区间内两条路径就是相加的。
//! 2. **ALU 单价上调约 7 倍**。原模型认为算力几乎被访存完全遮住, 实测不是。
//!    纹理发射吞吐原本估得很准 (45 vs 实测 47.3 M texel/ms), 说明 tap 那条对了,
//!    错的是"ALU 可以忽略"这个判断。
//!
//! 标定覆盖 tap ∈ [1,9]、ALU ∈ [13,200]。**区间外是外推, 不是测量**。
//! 极端 ALU 重而 tap 轻的算子没有数据支撑, 那种形态下相加会高估。
//!
//! 仍然成立的结构性事实: 邻域多 tap 绝大部分命中纹理 cache, 不重复付 DRAM 的钱,
//! 所以 **DRAM 必付流量**(读一遍输入图 + 写一遍输出图) 与 **纹理单元发射**分开算;
//! 实测区间内访存项仍然大于计算项, 移动端"搬数据比算更贵"的次序没有被推翻。
//!
//! # PSNR: 已按同一参考图重新锚定, 但**换参考帧后必须重标**
//!
//! 回标延迟之后前沿一度变成空集。原因不是延迟模型错了, 而是两个尺度被混在一起:
//! 延迟已经是真机标定的, 而画质基线 `quality_baseline_db` 还是 37.286 ——
//! 那个数来自 sr_loop 在**原神数据集 A** 上的 Bicubic 基线, 和本模型的画质估计
//! 不在同一把尺子上。混用的结果是"可行域"变成一个没有物理意义的交集。
//!
//! 现在两边都锚在同一张参考图上 (2026-09-22 真机, 程序化参考图):
//!
//! | 算子 | 实测 PSNR | 相对基线 |
//! |---|---|---|
//! | 赛道模板 (1 tap 直通) | 38.666 dB | 0 (基线) |
//! | 4 tap bilinear | 42.826 dB | +4.16 |
//! | 9 tap bilinear | 42.826 dB | +4.16 (与 4 tap **逐位相同**) |
//! | 4 tap gaussian | 40.861 dB | +2.20 |
//!
//! 两条被实测推翻的建模假设:
//!
//! 1. **tap 数不等于重建支撑**。bilinear 权重 `max(0,1-|d|)` 支撑半径为 1,
//!    3x3 邻域外圈 5 个 tap 权重恒为 0 —— 多付 5 次访存, 输出逐位不变。
//!    原模型按 `image_loads` 给画质加分, 等于奖励纯浪费。现在增益按**有效支撑**
//!    算, 并在饱和后不再给分。
//! 2. **平滑核不是加分项**。原模型给用超越函数算权重的核 +0.06 dB,
//!    实测 gaussian 比同 tap 数的 bilinear **低 1.97 dB** (sigma=0.7 在 2x2
//!    邻域上糊过头了)。符号反了, 已改为惩罚。
//!
//! **这把尺子只对当前那张程序化参考图成立。** 参考帧换成真实大世界帧之后,
//! 基线与增益曲线都必须用同一批真机数据重新导出 —— 绝对 dB 不可跨参考图比较。

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
    /// ALU 吞吐 (IR 指令 / 毫秒)。
    ///
    /// 口径是 **naga IR 指令**, 不是硬件 FLOP: 一条 IR 指令可能是 vec4 运算,
    /// 展开成多条硬件指令。所以这个数字不该拿去和厂商标称的 TFLOPS 比。
    pub alu_per_ms: f64,
    /// 超越函数吞吐 (IR 指令 / 毫秒)。口径同上。
    pub transcendental_per_ms: f64,
    /// 一次 dispatch 的固定开销 (ms)。
    pub dispatch_overhead_ms: f64,
    /// GPU 满载时在整机功耗里占的瓦数。
    pub gpu_active_watt: f64,
    /// 一帧的时间预算 (ms), 用于算算子的占空比。
    pub frame_budget_ms: f64,
    /// 游戏本体从原生分辨率降载到低分辨率省下的 GPU 功耗比例。
    pub render_downscale_power_saving: f64,
}

/// 红魔 NX809J: 骁龙 SM8850 / Adreno 840 @ 902 MHz, 1080p 送显。
///
/// 这台机器是 phonefarm 已完成 Gate 0 物理标尺标定的设备。
pub const NX809J_1080P: DeviceModel = DeviceModel {
    name: "NX809J (SM8850 / Adreno 840 @ 902MHz, 1080p)",
    out_pixels: 1920.0 * 1080.0,
    render_scale_area: 1.0 / 2.25,
    // 以下四项由 2026-09-22 真机实测最小二乘拟合而来 (见模块头)
    dram_bytes_per_ms: 32.2e6,
    tex_issue_per_ms: 47.3e6,
    alu_per_ms: 7.00e8,
    transcendental_per_ms: 9.76e7,
    dispatch_overhead_ms: 0.050,
    gpu_active_watt: 2.60,
    frame_budget_ms: 16.667,
    render_downscale_power_saving: 0.18,
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

        // 两条路径相加。2026-09-22 真机回标的结论: 在实测区间内它们就是相加的,
        // 不是「慢的那条 + 快的那条漏一点」。见模块头的反例。
        let latency_ms = self.dispatch_overhead_ms + memory_path_ms + compute_path_ms;

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

    // 重建支撑带来的增益, 锚在赛道模板 (1 tap 直通) = 0。
    // 饱和值 4.35 dB / 时间常数 1.16 由 2026-09-22 真机两点拟合
    // (1 tap → 0, 4 tap bilinear → +4.16)。
    //
    // 上限 kSaturateTaps: 再多的 tap 也不再加分。实测 9 tap bilinear 与 4 tap
    // 输出逐位相同 —— 支撑半径之外的 tap 权重为零, 只费访存不产生画质。
    // 不设这个上限, 模型就会奖励"多采样但什么也没多看见"的算子。
    const SATURATE_TAPS: f32 = 4.0;
    let effective = taps.min(SATURATE_TAPS);
    let tap_gain = 4.35 * (1.0 - (-(effective - 1.0) / 1.16).exp());

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

    // 平滑核 (用超越函数算权重, 即 gaussian) 是**减分**不是加分:
    // 实测 4 tap gaussian 40.861 dB vs 同 tap 数 bilinear 42.826 dB, 低 1.97。
    // 在这么小的邻域上, 高斯权重把本该锐利的边也一起糊掉了。
    let smooth_gain = if f.transcendental_ops > 0 { -1.97 } else { 0.0 };

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
            psnr_db: Some(psnr),
        },
        verdict: Verdict {
            is_pareto_improvement: false,
            // 估计值没有统计显著性可言。用 1.0 这个哨兵明确表示「未做检验」,
            // 而不是伪造一个漂亮的小 p 值。
            p_value: 1.0,
        },
        source: MetricSource::StaticCostModel,
        // 成本模型根本没看过任何图像, 它的 psnr 是从 tap 数推出来的估计值,
        // 没有"参考图"这回事。留 None, 不编一个来源出来。
        quality_reference: None,
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

    /// 模型必须能复现真机。这是整个成本模型唯一真正的验收判据 ——
    /// 其余那些单调性、量级断言都只是自洽性检查, 自洽但系统性偏一倍照样没用
    /// (回标前就正好如此: 全部自洽, 全线低估 1.8 倍)。
    ///
    /// 数据: 2026-09-22 phonefarm gpu-op 在 NX809J 上的 VkQueryPool 计时,
    /// 每个算子 60 次采样, 轮内离散 1.2%~2.6%。容差取 8%: 比测量离散度宽,
    /// 但足以抓住"整体偏一倍"这类系统性错误。
    #[test]
    fn the_model_reproduces_real_hardware_measurements() {
        // (taps, alu, transcendental, 实测 ms)
        const MEASURED: [(u32, u32, u32, f64); 5] = [
            (1, 13, 0, 0.5097),
            (4, 102, 0, 0.8715),
            (4, 86, 4, 0.9368),
            (4, 111, 0, 0.9446),
            (9, 200, 0, 1.4119),
        ];

        let mut worst = 0.0f64;
        for (taps, alu, trans, real) in MEASURED {
            let f = ShaderFeatures {
                image_loads: taps,
                alu_ops: alu,
                transcendental_ops: trans,
                ..Default::default()
            };
            let est = NX809J_1080P
                .estimate_latency(&f, TrackMode::Sr)
                .latency_ms;
            let rel = (est - real).abs() / real;
            worst = worst.max(rel);
            assert!(
                rel < 0.08,
                "tap={taps} alu={alu} trans={trans}: 估计 {est:.3} ms vs 实测 {real:.3} ms, 偏差 {:.1}%",
                rel * 100.0
            );
        }
        assert!(worst < 0.08, "最大偏差 {:.1}%", worst * 100.0);
    }

    /// 基线 1-tap 直通算子必须舒服地落在 1.5ms 预算内 —— 否则整个可行域是空集,
    /// 演化根本无从开始 (这正是 sr_loop 当初 invoke 口径踩的坑)。
    #[test]
    fn baseline_operator_fits_the_budget_with_room_to_spare() {
        let src = crate::tracks::embedded_template(TrackMode::Sr);
        let f = features_of(src, &SR);
        let cost = NX809J_1080P.estimate_latency(&f, TrackMode::Sr);
        assert!(
            cost.latency_ms < 0.7,
            "赛道基线模板估计耗时 {:.3} ms, 与实测 0.510 ms 量级不符",
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

    /// 画质模型必须复现真机在同一张参考图上量到的那几个点。
    /// 与延迟那条同理: 自洽不等于对。
    #[test]
    fn the_psnr_model_reproduces_real_hardware_measurements() {
        let at = |taps: u32, trans: u32| {
            let f = ShaderFeatures {
                image_loads: taps,
                transcendental_ops: trans,
                ..Default::default()
            };
            estimate_psnr_db(&f, &SR, 0.0, false, false)
        };
        // (taps, transcendental, 实测 dB)
        for (taps, trans, real) in [(1u32, 0u32, 38.666f32), (4, 0, 42.826), (4, 4, 40.861)] {
            let est = at(taps, trans);
            assert!(
                (est - real).abs() < 0.15,
                "tap={taps} trans={trans}: 估计 {est:.3} dB vs 实测 {real:.3} dB"
            );
        }
    }

    /// 支撑半径之外的 tap 只费访存不产生画质 —— 实测 9 tap bilinear 与 4 tap
    /// 输出逐位相同。模型不能奖励这种"多采样但什么也没多看见"的算子。
    #[test]
    fn taps_beyond_the_kernel_support_earn_no_quality() {
        let at = |taps: u32| {
            let f = ShaderFeatures { image_loads: taps, ..Default::default() };
            estimate_psnr_db(&f, &SR, 0.0, false, false)
        };
        assert!((at(4) - at(9)).abs() < 1e-4, "9 tap 不该比 4 tap 多拿分");
        assert!((at(9) - at(36)).abs() < 1e-4, "36 tap 同样不该多拿分");
        // 但支撑半径以内仍然要单调上升
        assert!(at(1) < at(2) && at(2) < at(4), "支撑内的 tap 增益必须单调");
    }

    /// 平滑核在小邻域上是减分项, 不是加分项 (实测 gaussian 比 bilinear 低 1.97 dB)。
    #[test]
    fn a_smoothing_kernel_is_penalised_not_rewarded() {
        let f = |trans: u32| ShaderFeatures {
            image_loads: 4,
            transcendental_ops: trans,
            ..Default::default()
        };
        let bilinear = estimate_psnr_db(&f(0), &SR, 0.0, false, false);
        let gaussian = estimate_psnr_db(&f(4), &SR, 0.0, false, false);
        assert!(gaussian < bilinear, "gaussian 不该比 bilinear 高");
        assert!((bilinear - gaussian - 1.97).abs() < 0.05);
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
