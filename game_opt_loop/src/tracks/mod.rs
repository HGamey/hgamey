//! 赛道契约 (Track Contracts)
//!
//! 一个赛道 = 一个「合法介入插槽」的完整硬约束描述:
//! 它固定了算子能看见的**显存资源**(零拷贝共享纹理槽位)、单帧耗时预算、
//! 画质基线、以及 LLM 可以改写的变异插槽标记。
//!
//! 这里的 binding 契约是 §2 红线「画面绝对不能离开 GPU 显存」的静态可执行形式:
//! 算子只允许声明这张表里的 storage image, 任何额外的 SSBO / 采样器 / 未登记
//! binding 都意味着帧数据要经过一块我们没有授权的内存 —— 直接打回。

use crate::TrackMode;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

/// 槽位访问权限。对应 GLSL 的 `readonly` / `writeonly` 限定符。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SlotAccess {
    ReadOnly,
    WriteOnly,
}

/// 一个零拷贝共享纹理槽位。
#[derive(Debug, Clone, Copy)]
pub struct BindingSlot {
    /// `layout(binding = N)` 的 N。
    pub binding: u32,
    /// GLSL 中必须使用的变量名 (合成层按名字挂载, 改名即挂不上)。
    pub name: &'static str,
    pub access: SlotAccess,
    /// 这个槽位在合成管线里的语义。
    pub role: &'static str,
    /// 是否必须出现。可选槽位允许算子不用 (例如只用当前帧的插帧算子)。
    pub required: bool,
}

/// 赛道的完整实现契约。
#[derive(Debug, Clone, Copy)]
pub struct TrackSpec {
    /// 契约 id, 同时是 candidate.schema.json 里 `track` 字段的取值。
    pub id: &'static str,
    pub display: &'static str,
    /// 单帧 GPU compute pass 耗时硬预算 (ms)。超过一票否决。
    pub budget_ms: f32,
    /// 画质硬底线 (dB)。低于基线一票否决 —— PSNR 是约束不是目标。
    pub quality_baseline_db: f32,
    /// 整机功耗参考基线 (W), 用于换算相对收益。
    pub power_baseline_watt: f32,
    /// 允许的零拷贝槽位白名单。
    pub bindings: &'static [BindingSlot],
    /// LLM 变异插槽标记 —— 模板里这行注释会被整体替换。
    pub mutation_slot: &'static str,
    /// 赛道目标的一句话描述, 直接进 LLM prompt。
    pub objective: &'static str,
    /// 交给 LLM 的赛道专属提示 (算法方向, 不含任何具体论文/网络名)。
    pub hints: &'static [&'static str],
}

impl TrackSpec {
    /// 契约要求必须出现的槽位。
    pub fn required_bindings(&self) -> impl Iterator<Item = &BindingSlot> {
        self.bindings.iter().filter(|b| b.required)
    }

    pub fn slot_for(&self, binding: u32) -> Option<&BindingSlot> {
        self.bindings.iter().find(|b| b.binding == binding)
    }

    /// 人类/LLM 可读的 binding 契约表。
    pub fn binding_table(&self) -> String {
        let mut s = String::new();
        for b in self.bindings {
            s.push_str(&format!(
                "  layout(binding = {}, rgba8) uniform {} image2D {};  // {}{}\n",
                b.binding,
                match b.access {
                    SlotAccess::ReadOnly => "readonly",
                    SlotAccess::WriteOnly => "writeonly",
                },
                b.name,
                b.role,
                if b.required { "" } else { " [可选]" }
            ));
        }
        s
    }
}

/// 超分辨率赛道: 720p 颜色缓冲进, 1080p 出。
pub const SR: TrackSpec = TrackSpec {
    id: "sr",
    display: "超分辨率 (Super Resolution)",
    budget_ms: 1.5,
    // 来源: phonefarm docs/SPEC_SR_LOOP.md §7, NX809J 数据集 A 锁定的 Bicubic 基线。
    quality_baseline_db: 37.286,
    power_baseline_watt: 5.60,
    bindings: &[
        BindingSlot {
            binding: 0,
            name: "u_InputTexture",
            access: SlotAccess::ReadOnly,
            role: "游戏低分辨率颜色缓冲 (常驻显存)",
            required: true,
        },
        BindingSlot {
            binding: 1,
            name: "u_OutputTexture",
            access: SlotAccess::WriteOnly,
            role: "送显高分辨率目标 (常驻显存)",
            required: true,
        },
    ],
    mutation_slot: "// [LLM 变异插槽: 高频重构与边缘自适应锐化]",
    objective: "从低分辨率颜色缓冲重建锐利、干净的高分辨率画面; 禁止产生摩尔纹、振铃或边缘破碎。",
    hints: &[
        "输出像素中心与输入像素中心的映射必须半像素对齐 ((p + 0.5) / outSize * inSize - 0.5), 错半像素会系统性掉 PSNR。",
        "邻域采样半径直接决定 imageLoad 次数; 移动端瓶颈通常是访存而不是 ALU, 优先用更少的 tap 配更好的权重。",
        "锐化项应受局部对比度/梯度调制, 平坦区不要加锐化, 否则噪点被一起放大。",
        "所有写回值必须 clamp 到 [0,1], 越界会在 rgba8 上环绕成色斑。",
    ],
};

/// 双倍插帧赛道: 前后两帧进, 中间帧出。
pub const FRAME_GEN: TrackSpec = TrackSpec {
    id: "frame_gen",
    display: "双倍插帧 (Frame Generation)",
    budget_ms: 2.5,
    quality_baseline_db: 34.0,
    power_baseline_watt: 5.60,
    bindings: &[
        BindingSlot {
            binding: 0,
            name: "u_PrevFrame",
            access: SlotAccess::ReadOnly,
            role: "上一帧颜色缓冲 (常驻显存)",
            required: true,
        },
        BindingSlot {
            binding: 1,
            name: "u_CurrFrame",
            access: SlotAccess::ReadOnly,
            role: "当前帧颜色缓冲 (常驻显存)",
            required: true,
        },
        BindingSlot {
            binding: 2,
            name: "u_InterpFrame",
            access: SlotAccess::WriteOnly,
            role: "插入的中间帧 (常驻显存)",
            required: true,
        },
    ],
    mutation_slot: "// [LLM 变异插槽: 块匹配运动估计与双向混帧]",
    objective: "在相邻两帧之间合成中间帧; 禁止大面积拉扯果冻效应, HUD 区域必须稳定不闪烁。",
    hints: &[
        "运动搜索半径与 imageLoad 次数是平方关系, 预算极易爆掉; 优先用粗到细的少量候选而不是全搜索。",
        "遮挡区域 (前后帧都匹配不上) 应回退到加权混合, 强行位移会产生撕裂。",
        "静止区域 (前后帧差值极小) 应直接走直通路径, 省掉全部搜索开销。",
    ],
};

pub fn spec(track: TrackMode) -> &'static TrackSpec {
    match track {
        TrackMode::Sr => &SR,
        TrackMode::FrameGen => &FRAME_GEN,
    }
}

pub fn spec_by_id(id: &str) -> Option<&'static TrackSpec> {
    match id {
        "sr" => Some(&SR),
        "frame_gen" => Some(&FRAME_GEN),
        _ => None,
    }
}

const SR_TEMPLATE: &str = include_str!("../../templates/sr/template.comp");
const FRAME_GEN_TEMPLATE: &str = include_str!("../../templates/frame_gen/template.comp");

/// 读取赛道基线模板。
///
/// 优先读工作目录下的文件 (方便迭代模板而不用重编译), 读不到就回退到编译期内嵌的副本,
/// 保证单二进制在任何工作目录下都能自洽运行。
pub fn load_template(track: TrackMode) -> Result<String> {
    let rel_path = match track {
        TrackMode::Sr => "templates/sr/template.comp",
        TrackMode::FrameGen => "templates/frame_gen/template.comp",
    };

    if Path::new(rel_path).exists() {
        fs::read_to_string(rel_path).with_context(|| format!("读取模板失败: {rel_path}"))
    } else {
        Ok(match track {
            TrackMode::Sr => SR_TEMPLATE.to_string(),
            TrackMode::FrameGen => FRAME_GEN_TEMPLATE.to_string(),
        })
    }
}

/// 编译期内嵌的模板副本 (不读盘)。
pub fn embedded_template(track: TrackMode) -> &'static str {
    match track {
        TrackMode::Sr => SR_TEMPLATE,
        TrackMode::FrameGen => FRAME_GEN_TEMPLATE,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_templates_carry_their_mutation_slot() {
        assert!(embedded_template(TrackMode::Sr).contains(SR.mutation_slot));
        assert!(embedded_template(TrackMode::FrameGen).contains(FRAME_GEN.mutation_slot));
    }

    #[test]
    fn embedded_templates_declare_every_required_binding() {
        for (mode, spec) in [(TrackMode::Sr, &SR), (TrackMode::FrameGen, &FRAME_GEN)] {
            let src = embedded_template(mode);
            for slot in spec.required_bindings() {
                assert!(
                    src.contains(slot.name),
                    "{} 模板缺少契约槽位 {}",
                    spec.id,
                    slot.name
                );
            }
        }
    }

    #[test]
    fn track_ids_round_trip() {
        assert_eq!(spec_by_id("sr").unwrap().id, "sr");
        assert_eq!(spec_by_id("frame_gen").unwrap().id, "frame_gen");
        assert!(spec_by_id("nope").is_none());
    }

    #[test]
    fn budgets_match_the_design_red_lines() {
        assert_eq!(SR.budget_ms, 1.5);
        assert_eq!(FRAME_GEN.budget_ms, 2.5);
    }
}
