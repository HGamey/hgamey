//! 本地确定性变异器 (离线算子族)
//!
//! 两个职责:
//! 1. **LLM 的兜底**。SPEC 的底线是「LLM 全链失败, 整代仍要靠本地变异推进」——
//!    闭环零人工介入不能建立在某个外部服务一定可用之上。
//! 2. **`--dry-run` 的算子来源**。不联网、不连真机, 也要能跑出一条真实的演化轨迹,
//!    每个候选都必须是能过防爆门禁、能出 SPIR-V 的真 GLSL。
//!
//! 变异对象是一个**参数化基因组**而不是文本 diff: 基因组 → GLSL 是确定性的,
//! 同一个种子两次运行逐字节相同, 归档因此可复现。

use crate::tracks::{TrackSpec, FRAME_GEN, SR};
use crate::TrackMode;
use serde::{Deserialize, Serialize};

/// 一个可复现的小 PRNG (xorshift64*)。
///
/// 不引入 `rand` 依赖: 演化的可复现性要求算法本身固定, 而不是依赖某个 crate
/// 的版本间行为一致。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        // 0 是 xorshift 的不动点, 必须避开。
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }

    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// [0, 1) 均匀分布。
    pub fn next_f32(&mut self) -> f32 {
        (self.next_u64() >> 40) as f32 / (1u32 << 24) as f32
    }

    /// [lo, hi] 闭区间整数。
    pub fn range_i32(&mut self, lo: i32, hi: i32) -> i32 {
        if hi <= lo {
            return lo;
        }
        lo + (self.next_u64() % ((hi - lo + 1) as u64)) as i32
    }

    pub fn chance(&mut self, p: f32) -> bool {
        self.next_f32() < p
    }

    /// 从切片里等概率取一个。
    pub fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[(self.next_u64() % items.len() as u64) as usize]
    }
}

/// 重建核的形状。决定权重曲线, 直接影响锐度与振铃。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Kernel {
    /// 双线性: 最省, 也最糊。
    Bilinear,
    /// 高斯权重: 平滑, 无振铃, 但细节保留一般。
    Gaussian,
    /// Catmull-Rom 型: 带负瓣, 锐利, 过强会振铃。
    CatmullRom,
    /// 分段多项式近似的窗口 sinc: 锐利度介于两者之间, 且不用超越函数。
    PolyWindow,
}

impl Kernel {
    fn id(self) -> &'static str {
        match self {
            Kernel::Bilinear => "bilinear",
            Kernel::Gaussian => "gaussian",
            Kernel::CatmullRom => "catmullrom",
            Kernel::PolyWindow => "polywindow",
        }
    }
}

/// 超分算子基因组。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SrGenome {
    /// 邻域**边长** (每个轴上的 tap 数), 取值 2..=5 → tap 数 4 / 9 / 16 / 25。
    ///
    /// 早先这里是「半径」, 只能生成 4 / 16 / 36 三档: 4 tap 画质不到基线,
    /// 36 tap 爆预算, 可行域里实际只剩 16 tap 一个点, 前沿必然塌成一条。
    /// 按边长取值才有 9 和 25 这两档, 预算边缘附近才有东西可权衡。
    pub side: i32,
    pub kernel: Kernel,
    /// 高斯核的 sigma (仅 Gaussian 用)。
    pub sigma: f32,
    /// Catmull-Rom 的形状参数 (仅 CatmullRom 用)。
    pub cr_b: f32,
    /// 锐化强度。0 = 不锐化。
    pub sharpen: f32,
    /// 锐化是否受局部梯度调制 (平坦区不锐化, 避免放大噪点)。
    pub adaptive: bool,
    /// 用邻域 min/max 夹紧输出, 抑制振铃与过冲。
    pub clamp_ringing: bool,
}

impl Default for SrGenome {
    /// 基线: 2x2 双线性, 不锐化。等价于模板的行为, 作为 Gen 0 的起点。
    fn default() -> Self {
        Self {
            side: 2,
            kernel: Kernel::Bilinear,
            sigma: 0.70,
            cr_b: 0.0,
            sharpen: 0.0,
            adaptive: false,
            clamp_ringing: false,
        }
    }
}

impl SrGenome {
    pub fn taps(&self) -> i32 {
        self.side * self.side
    }

    /// 人类可读的一行描述, 进档案和日志。
    pub fn describe(&self) -> String {
        format!(
            "{n}x{n} tap / {} / sharpen={:.2}{}{}",
            self.kernel.id(),
            self.sharpen,
            if self.adaptive { " / adaptive" } else { "" },
            if self.clamp_ringing { " / clamped" } else { "" },
            n = self.side,
        )
    }

    pub fn mutate(&self, rng: &mut Rng) -> Self {
        let mut g = self.clone();
        // 每次变异动 1~2 个基因位: 全改等于随机重启, 丢掉了父代的积累。
        let n = rng.range_i32(1, 2);
        for _ in 0..n {
            match rng.range_i32(0, 5) {
                0 => g.side = (g.side + if rng.chance(0.5) { 1 } else { -1 }).clamp(2, 5),
                1 => {
                    g.kernel = *rng.pick(&[
                        Kernel::Bilinear,
                        Kernel::Gaussian,
                        Kernel::CatmullRom,
                        Kernel::PolyWindow,
                    ])
                }
                2 => g.sigma = (g.sigma + (rng.next_f32() - 0.5) * 0.5).clamp(0.35, 1.60),
                3 => g.sharpen = (g.sharpen + (rng.next_f32() - 0.4) * 0.40).clamp(0.0, 0.85),
                4 => g.adaptive = !g.adaptive,
                _ => g.clamp_ringing = !g.clamp_ringing,
            }
        }
        g.cr_b = (g.cr_b + (rng.next_f32() - 0.5) * 0.2).clamp(0.0, 0.5);
        g
    }

    /// 基因组 → GLSL 450 Compute Shader。
    ///
    /// 展开成定长循环而不是运行期可变边界 —— 门禁要求循环静态可定界,
    /// 这个约束反过来也是对的: 变长邻域在 GPU 上会让整组等最慢的那条 lane。
    pub fn to_glsl(&self) -> String {
        let s = self.side;
        // 邻域以 base 为中心向两侧铺开; 偶数边长时偏向正方向 (与 frac 的定义一致)。
        let lo = -((s - 1) / 2);
        let hi = lo + s - 1;
        let support = s as f32 * 0.5;

        let weight_expr = match self.kernel {
            Kernel::Bilinear => {
                // 可分离三角权重, 半径外为 0。
                "        float w = max(0.0, 1.0 - abs(d.x)) * max(0.0, 1.0 - abs(d.y));"
                    .to_string()
            }
            Kernel::Gaussian => format!(
                "        float w = exp(-0.5 * dot(d, d) / ({:.4} * {:.4}));",
                self.sigma, self.sigma
            ),
            Kernel::CatmullRom => format!(
                "        float w = crWeight(abs(d.x), {:.4}) * crWeight(abs(d.y), {:.4});",
                self.cr_b, self.cr_b
            ),
            Kernel::PolyWindow => {
                // (1 - t^2)^2 窗: 光滑、有限支撑、纯乘加, 不用超越函数。
                "        float t2 = min(dot(d, d) / (SUPPORT * SUPPORT), 1.0);\n\
                 \x20       float w = (1.0 - t2) * (1.0 - t2);"
                    .to_string()
            }
        };

        let mut helpers = String::new();
        if self.kernel == Kernel::CatmullRom {
            helpers.push_str(
                "
// Catmull-Rom 型三次核。b 调负瓣深度: 0 最锐, 越大越平滑。
float crWeight(float x, float b) {
    float c = 0.5 - b * 0.5;
    if (x < 1.0) {
        return ((12.0 - 9.0 * b - 6.0 * c) * x * x * x
              + (-18.0 + 12.0 * b + 6.0 * c) * x * x
              + (6.0 - 2.0 * b)) / 6.0;
    } else if (x < 2.0) {
        return ((-b - 6.0 * c) * x * x * x
              + (6.0 * b + 30.0 * c) * x * x
              + (-12.0 * b - 48.0 * c) * x
              + (8.0 * b + 24.0 * c)) / 6.0;
    }
    return 0.0;
}
",
            );
        }

        let mut track_minmax = String::new();
        let mut apply_clamp = String::new();
        if self.clamp_ringing {
            track_minmax.push_str(
                "        nmin = min(nmin, s);\n        nmax = max(nmax, s);\n",
            );
            apply_clamp.push_str(
                "    // 邻域包络夹紧: 负瓣核与锐化都会过冲, 夹回去才不产生振铃亮边。\n\
                 \x20   col = clamp(col, nmin, nmax);\n",
            );
        }

        let sharpen_block = if self.sharpen > 0.001 {
            let gate = if self.adaptive {
                // 用局部对比度调制: 平坦区 (contrast≈0) 不锐化, 免得把噪点一起抬起来。
                "    float contrast = dot(nmax.rgb - nmin.rgb, vec3(0.3333));\n\
                 \x20   float gain = SHARPEN * smoothstep(0.015, 0.16, contrast);\n"
            } else {
                "    float gain = SHARPEN;\n"
            };
            format!(
                "{gate}    vec4 blur = acc2 / max(wsum2, 1e-5);\n\
                 \x20   col.rgb += (col.rgb - blur.rgb) * gain;\n"
            )
        } else {
            String::new()
        };

        // 自适应锐化要看邻域包络, 所以必须一起开 min/max 追踪。
        let need_minmax = self.clamp_ringing || (self.sharpen > 0.001 && self.adaptive);
        let (minmax_decl, minmax_track) = if need_minmax {
            (
                "    vec4 nmin = vec4(1.0);\n    vec4 nmax = vec4(0.0);\n",
                "        nmin = min(nmin, s);\n        nmax = max(nmax, s);\n",
            )
        } else {
            ("", "")
        };
        let _ = track_minmax;

        // 锐化需要一份更宽的低通做 unsharp mask, 复用同一批采样, 不额外访存。
        let (blur_decl, blur_track) = if self.sharpen > 0.001 {
            (
                "    vec4 acc2 = vec4(0.0);\n    float wsum2 = 0.0;\n",
                "        float w2 = 1.0 / (1.0 + dot(d, d));\n        acc2 += s * w2;\n        wsum2 += w2;\n",
            )
        } else {
            ("", "")
        };

        format!(
            r#"#version 450
layout(local_size_x = 16, local_size_y = 16) in;

layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;

// 算子族: {desc}
// 邻域 {n}x{n} = {taps} tap; 全部循环静态定界, 零显存往返。
#define SUPPORT {support:.4}
#define SHARPEN {sharpen:.4}
{helpers}
void main() {{
    ivec2 outPos = ivec2(gl_GlobalInvocationID.xy);
    ivec2 outSize = imageSize(u_OutputTexture);
    if (outPos.x >= outSize.x || outPos.y >= outSize.y) return;

    ivec2 inSize = imageSize(u_InputTexture);
    // 半像素对齐: 输出像素中心映回输入像素中心。差半像素就是系统性掉 PSNR。
    vec2 uv = (vec2(outPos) + 0.5) / vec2(outSize) * vec2(inSize) - 0.5;
    ivec2 base = ivec2(floor(uv));
    vec2 frac = uv - vec2(base);

    vec4 acc = vec4(0.0);
    float wsum = 0.0;
{blur_decl}{minmax_decl}
    for (int dy = {lo}; dy <= {hi}; dy++) {{
        for (int dx = {lo}; dx <= {hi}; dx++) {{
            ivec2 q = clamp(base + ivec2(dx, dy), ivec2(0), inSize - ivec2(1));
            vec4 s = imageLoad(u_InputTexture, q);
            vec2 d = vec2(float(dx), float(dy)) - frac;
{weight_expr}
            acc += s * w;
            wsum += w;
{minmax_track}{blur_track}        }}
    }}

    vec4 col = acc / max(wsum, 1e-5);
{sharpen_block}{apply_clamp}    imageStore(u_OutputTexture, outPos, vec4(clamp(col.rgb, 0.0, 1.0), 1.0));
}}
"#,
            desc = self.describe(),
            n = s,
            taps = self.taps(),
            support = support,
            sharpen = self.sharpen,
            helpers = helpers,
            lo = lo,
            hi = hi,
            weight_expr = weight_expr,
            blur_decl = blur_decl,
            minmax_decl = minmax_decl,
            minmax_track = minmax_track,
            blur_track = blur_track,
            sharpen_block = sharpen_block,
            apply_clamp = apply_clamp,
        )
    }
}

/// 插帧算子基因组。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FgGenome {
    /// 运动搜索半径 (像素)。搜索代价与半径平方成正比。
    pub search_radius: i32,
    /// 静止区直通: 前后帧差极小就跳过全部搜索。
    pub static_fast_path: bool,
    /// 遮挡区回退到加权混合而不是强行位移 (避免撕裂)。
    pub occlusion_fallback: bool,
    /// 时间位置 (0.5 = 正中间)。
    pub phase: f32,
    /// 判定静止的阈值。
    pub still_threshold: f32,
}

impl Default for FgGenome {
    fn default() -> Self {
        Self {
            search_radius: 1,
            static_fast_path: false,
            occlusion_fallback: false,
            phase: 0.5,
            still_threshold: 0.02,
        }
    }
}

impl FgGenome {
    pub fn taps(&self) -> i32 {
        let side = 2 * self.search_radius + 1;
        side * side * 2
    }

    pub fn describe(&self) -> String {
        format!(
            "search r={} / phase={:.2}{}{}",
            self.search_radius,
            self.phase,
            if self.static_fast_path { " / fastpath" } else { "" },
            if self.occlusion_fallback { " / occl-fallback" } else { "" },
        )
    }

    pub fn mutate(&self, rng: &mut Rng) -> Self {
        let mut g = self.clone();
        for _ in 0..rng.range_i32(1, 2) {
            match rng.range_i32(0, 3) {
                0 => {
                    g.search_radius =
                        (g.search_radius + if rng.chance(0.5) { 1 } else { -1 }).clamp(1, 3)
                }
                1 => g.static_fast_path = !g.static_fast_path,
                2 => g.occlusion_fallback = !g.occlusion_fallback,
                _ => g.phase = (g.phase + (rng.next_f32() - 0.5) * 0.2).clamp(0.35, 0.65),
            }
        }
        g.still_threshold = (g.still_threshold + (rng.next_f32() - 0.5) * 0.01).clamp(0.004, 0.06);
        g
    }

    pub fn to_glsl(&self) -> String {
        let r = self.search_radius;

        let fast_path = if self.static_fast_path {
            format!(
                "    // 静止区直通: 前后帧几乎一样就没必要搜索, 直接混合省掉全部访存。\n\
                 \x20   if (dot(abs(c.rgb - p.rgb), vec3(0.3333)) < {:.4}) {{\n\
                 \x20       imageStore(u_InterpFrame, pos, vec4(clamp(mix(p.rgb, c.rgb, PHASE), 0.0, 1.0), 1.0));\n\
                 \x20       return;\n\
                 \x20   }}\n",
                self.still_threshold
            )
        } else {
            String::new()
        };

        let occlusion = if self.occlusion_fallback {
            "    // 遮挡: 最优匹配依然很差 → 前后帧互相看不见这块内容, 位移只会撕裂, 退回直混。\n\
             \x20   if (bestCost > 0.22) {\n\
             \x20       imageStore(u_InterpFrame, pos, vec4(clamp(mix(p.rgb, c.rgb, PHASE), 0.0, 1.0), 1.0));\n\
             \x20       return;\n\
             \x20   }\n"
        } else {
            ""
        };

        format!(
            r#"#version 450
layout(local_size_x = 16, local_size_y = 16) in;

layout(binding = 0, rgba8) uniform readonly image2D u_PrevFrame;
layout(binding = 1, rgba8) uniform readonly image2D u_CurrFrame;
layout(binding = 2, rgba8) uniform writeonly image2D u_InterpFrame;

// 算子族: {desc}
// 搜索窗 {side}x{side}, 静态定界; 全程只读写显存内共享纹理。
#define SR {r}
#define PHASE {phase:.4}

void main() {{
    ivec2 pos = ivec2(gl_GlobalInvocationID.xy);
    ivec2 size = imageSize(u_InterpFrame);
    if (pos.x >= size.x || pos.y >= size.y) return;

    vec4 p = imageLoad(u_PrevFrame, pos);
    vec4 c = imageLoad(u_CurrFrame, pos);
{fast_path}
    ivec2 bestMv = ivec2(0);
    float bestCost = 1e9;
    for (int dy = -SR; dy <= SR; dy++) {{
        for (int dx = -SR; dx <= SR; dx++) {{
            ivec2 mv = ivec2(dx, dy);
            // 对称双向匹配: 前帧往前看, 后帧往后看, 偏移相反。
            vec4 a = imageLoad(u_PrevFrame, clamp(pos - mv, ivec2(0), size - ivec2(1)));
            vec4 b = imageLoad(u_CurrFrame, clamp(pos + mv, ivec2(0), size - ivec2(1)));
            float cost = dot(abs(a.rgb - b.rgb), vec3(0.3333))
                       + float(abs(dx) + abs(dy)) * 0.004;   // 平滑项: 同等匹配下偏好小位移
            if (cost < bestCost) {{ bestCost = cost; bestMv = mv; }}
        }}
    }}
{occlusion}
    vec4 a = imageLoad(u_PrevFrame, clamp(pos - bestMv, ivec2(0), size - ivec2(1)));
    vec4 b = imageLoad(u_CurrFrame, clamp(pos + bestMv, ivec2(0), size - ivec2(1)));
    vec3 interp = mix(a.rgb, b.rgb, PHASE);
    imageStore(u_InterpFrame, pos, vec4(clamp(interp, 0.0, 1.0), 1.0));
}}
"#,
            desc = self.describe(),
            side = 2 * r + 1,
            r = r,
            phase = self.phase,
            fast_path = fast_path,
            occlusion = occlusion,
        )
    }
}

/// 一个赛道无关的基因组句柄。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum Genome {
    Sr(SrGenome),
    FrameGen(FgGenome),
}

impl Genome {
    pub fn baseline(track: TrackMode) -> Self {
        match track {
            TrackMode::Sr => Genome::Sr(SrGenome::default()),
            TrackMode::FrameGen => Genome::FrameGen(FgGenome::default()),
        }
    }

    pub fn mutate(&self, rng: &mut Rng) -> Self {
        match self {
            Genome::Sr(g) => Genome::Sr(g.mutate(rng)),
            Genome::FrameGen(g) => Genome::FrameGen(g.mutate(rng)),
        }
    }

    pub fn to_glsl(&self) -> String {
        match self {
            Genome::Sr(g) => g.to_glsl(),
            Genome::FrameGen(g) => g.to_glsl(),
        }
    }

    pub fn describe(&self) -> String {
        match self {
            Genome::Sr(g) => g.describe(),
            Genome::FrameGen(g) => g.describe(),
        }
    }

    pub fn spec(&self) -> &'static TrackSpec {
        match self {
            Genome::Sr(_) => &SR,
            Genome::FrameGen(_) => &FRAME_GEN,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::validator::Validator;

    /// 本地变异器吐出的每一个算子都必须能过自家的防爆门禁。
    /// 兜底路径要是自己就过不了门禁, 「LLM 挂了也能推进」就是空话。
    #[test]
    fn every_locally_mutated_operator_survives_the_gate() {
        let validator = Validator::new();
        for track in [TrackMode::Sr, TrackMode::FrameGen] {
            let mut rng = Rng::new(0xC0FFEE);
            let mut genome = Genome::baseline(track);
            for step in 0..120 {
                let src = genome.to_glsl();
                let report = validator.gate(&src, genome.spec());
                assert!(
                    report.passed,
                    "第 {step} 步变异 ({:?}) 生成的算子过不了门禁: {:?}\n--- 源码 ---\n{src}",
                    genome.describe(),
                    report.violations
                );
                assert!(report.spirv_bytes.unwrap_or(0) > 0);
                genome = genome.mutate(&mut rng);
            }
        }
    }

    #[test]
    fn genome_to_glsl_is_deterministic() {
        let g = SrGenome {
            side: 4,
            kernel: Kernel::Gaussian,
            sigma: 0.8,
            cr_b: 0.1,
            sharpen: 0.3,
            adaptive: true,
            clamp_ringing: true,
        };
        assert_eq!(g.to_glsl(), g.to_glsl());
    }

    #[test]
    fn mutation_is_reproducible_from_a_seed() {
        let run = || {
            let mut rng = Rng::new(42);
            let mut g = Genome::baseline(TrackMode::Sr);
            for _ in 0..25 {
                g = g.mutate(&mut rng);
            }
            g
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn mutation_actually_explores() {
        let mut rng = Rng::new(7);
        let base = Genome::baseline(TrackMode::Sr);
        let mut seen = std::collections::HashSet::new();
        let mut g = base.clone();
        for _ in 0..60 {
            g = g.mutate(&mut rng);
            seen.insert(g.describe());
        }
        assert!(seen.len() > 8, "变异多样性不足: 只见到 {} 种形态", seen.len());
    }

    #[test]
    fn tap_count_tracks_side_length() {
        for (side, taps) in [(2, 4), (3, 9), (4, 16), (5, 25)] {
            assert_eq!(SrGenome { side, ..Default::default() }.taps(), taps);
        }
    }

    /// 静态分析数出来的 tap 数必须和基因组声明的一致 —— 两条独立路径对账。
    #[test]
    fn static_analysis_agrees_with_the_declared_tap_count() {
        let validator = Validator::new();
        for side in 2..=5 {
            let g = SrGenome {
                side,
                kernel: Kernel::Gaussian,
                ..Default::default()
            };
            let report = validator.gate(&g.to_glsl(), &SR);
            assert!(report.passed, "{:?}", report.violations);
            assert_eq!(
                report.features.image_loads as i32,
                g.taps(),
                "side={side} 的静态 tap 计数对不上"
            );
        }
    }

    #[test]
    fn rng_is_stable_across_runs() {
        let mut a = Rng::new(1234);
        let mut b = Rng::new(1234);
        for _ in 0..100 {
            assert_eq!(a.next_u64(), b.next_u64());
        }
    }
}
