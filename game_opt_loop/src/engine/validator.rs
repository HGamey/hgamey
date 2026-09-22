//! 本地防爆编译门禁 (Validator)
//!
//! 四道闸, 任何一道不过就地打回, 绝不推入真机:
//!
//! | 闸 | 做什么 | 由谁执行 |
//! |---|---|---|
//! | Source | 源码层契约: `#version`、工作组声明、源码规模 | 本模块 |
//! | Parse   | GLSL 450 前端: 语法、类型、readonly/writeonly 违规、递归 | naga `glsl-in` |
//! | Policy  | 零拷贝 binding 契约、循环有界性、barrier 一致性、写回越界守卫 | 本模块 + `analysis` |
//! | Codegen | 真正吐出 SPIR-V 二进制 —— 能落地成设备字节码才算数 | naga `spv-out` |
//!
//! 全程纯 Rust, 不依赖 glslang / shaderc / 任何 C++ 工具链, 单二进制即可运行。

use crate::engine::analysis::{self, IrAnalysis, LoopBound, ShaderFeatures};
use crate::tracks::{SlotAccess, TrackSpec};
use naga::front::glsl::{Frontend, Options as GlslOptions};
use naga::valid::{Capabilities, ValidationFlags};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::time::Instant;

/// 门禁的四个阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GateStage {
    Source,
    Parse,
    Policy,
    Codegen,
}

/// 违规代码。稳定字符串, 会进档案、进统计、也进 LLM 的下一轮反馈。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ViolationCode {
    /// 缺 `#version 450` / `#version 460` 标头。
    MissingVersionDirective,
    /// 缺 `layout(local_size_x = ...) in;` 工作组声明。
    MissingWorkgroupDecl,
    /// 源码体积超限 (防 prompt 注入式超长产物)。
    SourceTooLarge,
    /// GLSL 语法 / 类型错误 (含递归、读写限定符违规)。
    SyntaxError,
    /// naga IR 校验失败。
    IrValidationFailed,
    /// 缺少契约要求的零拷贝槽位。
    MissingRequiredBinding,
    /// 声明了契约外的 binding。
    UnknownBinding,
    /// 槽位的读写限定符与契约不符。
    BindingAccessMismatch,
    /// 槽位变量名与契约不符 (合成层按名字挂载)。
    BindingNameMismatch,
    /// 声明了 SSBO / 未登记 uniform 等非零拷贝资源 —— 帧数据会离开显存。
    NonZeroCopyResource,
    /// 片上 shared 内存超限。
    WorkgroupMemoryTooLarge,
    /// 工作组总调用数超过硬件上限。
    WorkgroupTooLarge,
    /// 工作组尺寸为 0 或未被前端识别。
    WorkgroupInvalid,
    /// 存在无法静态定界的循环 —— 真机挂死风险。
    UnboundedLoop,
    /// 循环可定界但迭代上界过大。
    LoopTripTooLarge,
    /// barrier 处于非一致控制流。
    DivergentBarrier,
    /// 存在没有被 `imageSize()` 守卫保护的写回。
    UnguardedImageStore,
    /// 一次写回都没有 —— 算子没产出。
    NoImageStore,
    /// SPIR-V 生成失败。
    SpirvEmitFailed,
}

impl ViolationCode {
    pub fn as_str(&self) -> &'static str {
        use ViolationCode::*;
        match self {
            MissingVersionDirective => "MISSING_VERSION_DIRECTIVE",
            MissingWorkgroupDecl => "MISSING_WORKGROUP_DECL",
            SourceTooLarge => "SOURCE_TOO_LARGE",
            SyntaxError => "SYNTAX_ERROR",
            IrValidationFailed => "IR_VALIDATION_FAILED",
            MissingRequiredBinding => "MISSING_REQUIRED_BINDING",
            UnknownBinding => "UNKNOWN_BINDING",
            BindingAccessMismatch => "BINDING_ACCESS_MISMATCH",
            BindingNameMismatch => "BINDING_NAME_MISMATCH",
            NonZeroCopyResource => "NON_ZERO_COPY_RESOURCE",
            WorkgroupMemoryTooLarge => "WORKGROUP_MEMORY_TOO_LARGE",
            WorkgroupTooLarge => "WORKGROUP_TOO_LARGE",
            WorkgroupInvalid => "WORKGROUP_INVALID",
            UnboundedLoop => "UNBOUNDED_LOOP",
            LoopTripTooLarge => "LOOP_TRIP_TOO_LARGE",
            DivergentBarrier => "DIVERGENT_BARRIER",
            UnguardedImageStore => "UNGUARDED_IMAGE_STORE",
            NoImageStore => "NO_IMAGE_STORE",
            SpirvEmitFailed => "SPIRV_EMIT_FAILED",
        }
    }
}

impl fmt::Display for ViolationCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Violation {
    pub code: ViolationCode,
    pub stage: GateStage,
    pub message: String,
}

impl fmt::Display for Violation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.code, self.message)
    }
}

/// 一次门禁的完整结论。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GateReport {
    pub passed: bool,
    pub violations: Vec<Violation>,
    pub features: ShaderFeatures,
    /// 生成的 SPIR-V 字节码 (仅在全部通过时非空)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spirv_bytes: Option<usize>,
    /// 门禁耗时 (微秒) —— 「秒级打回」的证据。
    pub elapsed_us: u128,
}

impl GateReport {
    pub fn codes(&self) -> Vec<ViolationCode> {
        self.violations.iter().map(|v| v.code).collect()
    }

    /// 给 LLM 看的一行反馈。
    pub fn feedback_line(&self) -> String {
        if self.passed {
            return "PASS".to_string();
        }
        self.violations
            .iter()
            .map(|v| format!("{}: {}", v.code, v.message))
            .collect::<Vec<_>>()
            .join("; ")
    }
}

/// 硬件与安全上限。
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// 工作组总调用数上限 (Vulkan 规范下限即 128; Adreno/Mali 实际为 1024)。
    pub max_workgroup_invocations: u32,
    /// 片上 shared 内存上限 (字节)。Adreno 840 为 32 KiB。
    pub max_workgroup_bytes: u32,
    /// 单个循环允许的静态迭代上界。
    pub max_loop_trip: u64,
    /// 源码字节上限。
    pub max_source_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_workgroup_invocations: 1024,
            max_workgroup_bytes: 32 * 1024,
            max_loop_trip: 256,
            max_source_bytes: 64 * 1024,
        }
    }
}

/// 防爆编译门禁。
pub struct Validator {
    limits: Limits,
}

impl Default for Validator {
    fn default() -> Self {
        Self::new()
    }
}

impl Validator {
    pub fn new() -> Self {
        Self {
            limits: Limits::default(),
        }
    }

    pub fn with_limits(limits: Limits) -> Self {
        Self { limits }
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// 兼容旧接口: 只关心「过 / 不过」。
    pub fn verify_syntax(&self, source: &str, track: &TrackSpec) -> anyhow::Result<()> {
        let report = self.gate(source, track);
        if report.passed {
            Ok(())
        } else {
            anyhow::bail!("{}", report.feedback_line())
        }
    }

    /// 跑完整的四道闸。
    pub fn gate(&self, source: &str, track: &TrackSpec) -> GateReport {
        let started = Instant::now();
        let mut violations = Vec::new();

        // ---- 闸 1: 源码层契约 ----
        self.check_source(source, &mut violations);
        if !violations.is_empty() {
            return self.finish(violations, ShaderFeatures::default(), None, started);
        }

        // ---- 闸 2: GLSL 前端 ----
        let mut frontend = Frontend::default();
        let options = GlslOptions::from(naga::ShaderStage::Compute);
        let module = match frontend.parse(&options, source) {
            Ok(m) => m,
            Err(errs) => {
                let detail = errs
                    .errors
                    .iter()
                    .map(|e| e.to_string())
                    .collect::<Vec<_>>()
                    .join(" | ");
                violations.push(Violation {
                    code: ViolationCode::SyntaxError,
                    stage: GateStage::Parse,
                    message: detail,
                });
                return self.finish(violations, ShaderFeatures::default(), None, started);
            }
        };

        let mut naga_validator =
            naga::valid::Validator::new(ValidationFlags::all(), Capabilities::all());
        let info = match naga_validator.validate(&module) {
            Ok(i) => i,
            Err(e) => {
                violations.push(Violation {
                    code: ViolationCode::IrValidationFailed,
                    stage: GateStage::Parse,
                    message: e.to_string(),
                });
                return self.finish(violations, ShaderFeatures::default(), None, started);
            }
        };

        // ---- 闸 3: 策略层 (零拷贝契约 + 静态安全) ----
        let ir = analysis::analyse(&module);
        self.check_bindings(&module, track, &mut violations);
        self.check_static_safety(&ir, &mut violations);

        if !violations.is_empty() {
            return self.finish(violations, ir.features, None, started);
        }

        // ---- 闸 4: 真出 SPIR-V ----
        let spv_options = naga::back::spv::Options::default();
        let mut features = ir.features;
        let spirv_bytes = match naga::back::spv::write_vec(&module, &info, &spv_options, None) {
            Ok(words) => {
                features.spirv_words = words.len() as u32;
                Some(words.len() * 4)
            }
            Err(e) => {
                violations.push(Violation {
                    code: ViolationCode::SpirvEmitFailed,
                    stage: GateStage::Codegen,
                    message: e.to_string(),
                });
                None
            }
        };

        self.finish(violations, features, spirv_bytes, started)
    }

    fn finish(
        &self,
        violations: Vec<Violation>,
        features: ShaderFeatures,
        spirv_bytes: Option<usize>,
        started: Instant,
    ) -> GateReport {
        GateReport {
            passed: violations.is_empty(),
            violations,
            features,
            spirv_bytes,
            elapsed_us: started.elapsed().as_micros(),
        }
    }

    // ---------- 闸 1 ----------

    fn check_source(&self, source: &str, out: &mut Vec<Violation>) {
        if source.len() > self.limits.max_source_bytes {
            out.push(Violation {
                code: ViolationCode::SourceTooLarge,
                stage: GateStage::Source,
                message: format!(
                    "源码 {} 字节, 超过上限 {} 字节",
                    source.len(),
                    self.limits.max_source_bytes
                ),
            });
            return;
        }

        let has_version = source.lines().any(|l| {
            let t = l.trim();
            t.starts_with("#version") && (t.contains("450") || t.contains("460"))
        });
        if !has_version {
            out.push(Violation {
                code: ViolationCode::MissingVersionDirective,
                stage: GateStage::Source,
                message: "缺少 `#version 450` (或 460) 标头; Vulkan GLSL 必须显式声明版本"
                    .to_string(),
            });
        }

        // naga 在没有 local_size 声明时会静默取 [1,1,1], 那是 1/256 的吞吐,
        // 属于必须显式发现的错误而不是可接受的默认值。
        if !source.contains("local_size_x") {
            out.push(Violation {
                code: ViolationCode::MissingWorkgroupDecl,
                stage: GateStage::Source,
                message: "缺少 `layout(local_size_x = .., local_size_y = ..) in;` 工作组声明"
                    .to_string(),
            });
        }
    }

    // ---------- 闸 3a: 零拷贝 binding 契约 ----------

    fn check_bindings(&self, module: &naga::Module, track: &TrackSpec, out: &mut Vec<Violation>) {
        use naga::AddressSpace;

        let mut seen: Vec<u32> = Vec::new();

        for (_, var) in module.global_variables.iter() {
            let name = var.name.clone().unwrap_or_default();

            match var.space {
                // 内建变量 (gl_GlobalInvocationID 等) 会落在 Private, 不参与契约。
                AddressSpace::Private | AddressSpace::Function => continue,
                // 片上 shared 不离开 GPU, 允许, 容量另行校验。
                AddressSpace::WorkGroup => continue,
                // push constant: 只能传参数标量, 尺寸受限, 不可能夹带帧数据。
                AddressSpace::Immediate => continue,
                AddressSpace::Storage { .. } => {
                    out.push(Violation {
                        code: ViolationCode::NonZeroCopyResource,
                        stage: GateStage::Policy,
                        message: format!(
                            "声明了 storage buffer `{name}`: 帧数据一旦落到 SSBO 就要经过一次显存搬运, \
                             违反零拷贝红线 (前车之鉴: 2.9ms 算力 + 7ms 拷贝)"
                        ),
                    });
                    continue;
                }
                AddressSpace::Uniform => {
                    out.push(Violation {
                        code: ViolationCode::NonZeroCopyResource,
                        stage: GateStage::Policy,
                        message: format!(
                            "声明了契约外的 uniform block `{name}`; 参数请走 push_constant"
                        ),
                    });
                    continue;
                }
                AddressSpace::Handle => {}
                _ => {
                    out.push(Violation {
                        code: ViolationCode::NonZeroCopyResource,
                        stage: GateStage::Policy,
                        message: format!("`{name}` 使用了合成层插槽不支持的地址空间 {:?}", var.space),
                    });
                    continue;
                }
            }

            let Some(binding) = var.binding.as_ref() else {
                continue;
            };
            seen.push(binding.binding);

            let Some(slot) = track.slot_for(binding.binding) else {
                out.push(Violation {
                    code: ViolationCode::UnknownBinding,
                    stage: GateStage::Policy,
                    message: format!(
                        "`{name}` 占用了契约外的 binding = {}; {} 赛道只开放 {:?}",
                        binding.binding,
                        track.id,
                        track.bindings.iter().map(|b| b.binding).collect::<Vec<_>>()
                    ),
                });
                continue;
            };

            if name != slot.name {
                out.push(Violation {
                    code: ViolationCode::BindingNameMismatch,
                    stage: GateStage::Policy,
                    message: format!(
                        "binding = {} 的变量名必须是 `{}` (合成层按名字挂载), 实际是 `{name}`",
                        slot.binding, slot.name
                    ),
                });
            }

            // 必须是 storage image, 且读写权限与契约一致。
            match &module.types[var.ty].inner {
                naga::TypeInner::Image {
                    class: naga::ImageClass::Storage { access, .. },
                    ..
                } => {
                    let wants_read = slot.access == SlotAccess::ReadOnly;
                    let has_load = access.contains(naga::StorageAccess::LOAD);
                    let has_store = access.contains(naga::StorageAccess::STORE);
                    let ok = if wants_read {
                        has_load && !has_store
                    } else {
                        has_store && !has_load
                    };
                    if !ok {
                        out.push(Violation {
                            code: ViolationCode::BindingAccessMismatch,
                            stage: GateStage::Policy,
                            message: format!(
                                "`{name}` (binding = {}) 契约要求 {}, 实际权限 load={has_load} store={has_store}",
                                slot.binding,
                                match slot.access {
                                    SlotAccess::ReadOnly => "readonly",
                                    SlotAccess::WriteOnly => "writeonly",
                                }
                            ),
                        });
                    }
                }
                other => {
                    out.push(Violation {
                        code: ViolationCode::NonZeroCopyResource,
                        stage: GateStage::Policy,
                        message: format!(
                            "`{name}` (binding = {}) 必须是 storage image2D, 实际类型 {other:?}",
                            slot.binding
                        ),
                    });
                }
            }
        }

        for slot in track.required_bindings() {
            if !seen.contains(&slot.binding) {
                out.push(Violation {
                    code: ViolationCode::MissingRequiredBinding,
                    stage: GateStage::Policy,
                    message: format!(
                        "缺少契约必需槽位 binding = {} (`{}`, {})",
                        slot.binding, slot.name, slot.role
                    ),
                });
            }
        }
    }

    // ---------- 闸 3b: 静态安全 ----------

    fn check_static_safety(&self, ir: &IrAnalysis, out: &mut Vec<Violation>) {
        let f = &ir.features;

        let invocations = f.workgroup_invocations();
        if invocations == 0 {
            out.push(Violation {
                code: ViolationCode::WorkgroupInvalid,
                stage: GateStage::Policy,
                message: "工作组尺寸含 0 维".to_string(),
            });
        } else if invocations > self.limits.max_workgroup_invocations {
            out.push(Violation {
                code: ViolationCode::WorkgroupTooLarge,
                stage: GateStage::Policy,
                message: format!(
                    "工作组 {:?} = {invocations} 个调用, 超过硬件上限 {}",
                    f.workgroup_size, self.limits.max_workgroup_invocations
                ),
            });
        }

        if f.workgroup_bytes > self.limits.max_workgroup_bytes {
            out.push(Violation {
                code: ViolationCode::WorkgroupMemoryTooLarge,
                stage: GateStage::Policy,
                message: format!(
                    "片上 shared 内存 {} 字节, 超过上限 {} 字节",
                    f.workgroup_bytes, self.limits.max_workgroup_bytes
                ),
            });
        }

        for bound in &ir.loop_bounds {
            match bound {
                LoopBound::Unproven(why) => out.push(Violation {
                    code: ViolationCode::UnboundedLoop,
                    stage: GateStage::Policy,
                    message: format!("{why} —— 该算子会把合成线程挂死, 沙盒直杀"),
                }),
                LoopBound::Proven(n) if *n > self.limits.max_loop_trip => out.push(Violation {
                    code: ViolationCode::LoopTripTooLarge,
                    stage: GateStage::Policy,
                    message: format!(
                        "循环静态迭代上界 {n} 次, 超过上限 {} 次",
                        self.limits.max_loop_trip
                    ),
                }),
                _ => {}
            }
        }

        if ir.divergent_barriers > 0 {
            out.push(Violation {
                code: ViolationCode::DivergentBarrier,
                stage: GateStage::Policy,
                message: format!(
                    "{} 处 barrier 位于非一致控制流 (if/loop 内部); Vulkan 下为未定义行为, 真机表现为整组挂死",
                    ir.divergent_barriers
                ),
            });
        }

        if f.image_stores == 0 {
            out.push(Violation {
                code: ViolationCode::NoImageStore,
                stage: GateStage::Policy,
                message: "算子没有任何 imageStore, 不产出画面".to_string(),
            });
        } else if !ir.all_stores_guarded {
            out.push(Violation {
                code: ViolationCode::UnguardedImageStore,
                stage: GateStage::Policy,
                message: format!(
                    "{} 处 imageStore 没有被 imageSize() 派生的边界守卫保护, 存在写越界风险",
                    ir.unguarded_stores
                ),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tracks::{FRAME_GEN, SR};

    const HDR: &str = "#version 450\nlayout(local_size_x = 16, local_size_y = 16) in;\n\
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;\n\
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;\n";

    const GUARD: &str = "ivec2 p = ivec2(gl_GlobalInvocationID.xy);\n\
    ivec2 o = imageSize(u_OutputTexture);\n\
    if (p.x >= o.x || p.y >= o.y) return;\n";

    fn sr(body: &str) -> String {
        format!("{HDR}void main() {{\n{GUARD}{body}\n}}\n")
    }

    fn gate_sr(src: &str) -> GateReport {
        Validator::new().gate(src, &SR)
    }

    fn assert_rejected(report: &GateReport, code: ViolationCode) {
        assert!(!report.passed, "期望被拦截, 实际通过了");
        assert!(
            report.codes().contains(&code),
            "期望违规码 {code}, 实际是 {:?}",
            report.codes()
        );
    }

    // ---------- 合规样本必须通过 ----------

    #[test]
    fn baseline_templates_pass_the_gate() {
        for (spec, src) in [
            (&SR, crate::tracks::embedded_template(crate::TrackMode::Sr)),
            (
                &FRAME_GEN,
                crate::tracks::embedded_template(crate::TrackMode::FrameGen),
            ),
        ] {
            let report = Validator::new().gate(src, spec);
            assert!(
                report.passed,
                "{} 基线模板未通过门禁: {:?}",
                spec.id, report.violations
            );
            assert!(report.spirv_bytes.unwrap_or(0) > 0, "未产出 SPIR-V 字节码");
        }
    }

    #[test]
    fn realistic_multi_tap_operator_passes() {
        let src = sr(
            "ivec2 isz = imageSize(u_InputTexture);
    vec2 uv = (vec2(p) + 0.5) / vec2(o) * vec2(isz) - 0.5;
    ivec2 b = ivec2(floor(uv));
    vec2 fr = uv - vec2(b);
    vec4 acc = vec4(0.0);
    float wsum = 0.0;
    for (int dy = -1; dy <= 2; dy++) {
        for (int dx = -1; dx <= 2; dx++) {
            ivec2 q = clamp(b + ivec2(dx, dy), ivec2(0), isz - 1);
            vec2 d = vec2(float(dx), float(dy)) - fr;
            float w = exp(-0.5 * dot(d, d) * 1.7);
            acc += imageLoad(u_InputTexture, q) * w;
            wsum += w;
        }
    }
    imageStore(u_OutputTexture, p, clamp(acc / max(wsum, 1e-5), 0.0, 1.0));",
        );
        let r = gate_sr(&src);
        assert!(r.passed, "真实多 tap 算子被误杀: {:?}", r.violations);
        // 4x4 邻域 = 16 次采样, 由静态迭代上界展开得到。
        assert_eq!(r.features.image_loads, 16);
        assert_eq!(r.features.max_loop_trip, 4);
        assert!(r.features.transcendental_ops >= 16);
    }

    /// 大模型很爱把采样抽成 helper 再在循环里调。开销统计必须跟着调用走,
    /// 否则这种算子的访存会被记成 1 次, 成本模型低估一个数量级。
    ///
    /// 这段结构取自 glm-4.7 真实产出的候选算子: 4x4 循环里调 `fetchTexel`,
    /// 外加 main 里 5 次直调 = 21 次访存。
    #[test]
    fn tap_counting_follows_function_calls() {
        let src = format!(
            "{HDR}
vec4 fetchTexel(ivec2 p, ivec2 sz) {{
    return imageLoad(u_InputTexture, clamp(p, ivec2(0), sz - ivec2(1)));
}}

vec3 sampleNeighbourhood(vec2 pos, ivec2 sz) {{
    vec2 base = floor(pos - 0.5);
    vec3 sum = vec3(0.0);
    for (int m = -1; m <= 2; ++m) {{
        for (int n = -1; n <= 2; ++n) {{
            sum += fetchTexel(ivec2(base) + ivec2(n, m), sz).rgb;
        }}
    }}
    return sum;
}}

void main() {{
{GUARD}    ivec2 inSize = imageSize(u_InputTexture);
    vec2 inCoord = (vec2(p) + 0.5) * vec2(inSize) / vec2(o);
    ivec2 ip = clamp(ivec2(floor(inCoord)), ivec2(0), inSize - ivec2(1));
    vec3 base = sampleNeighbourhood(inCoord, inSize);
    vec3 c0 = fetchTexel(ip, inSize).rgb;
    vec3 cl = fetchTexel(ip + ivec2(-1, 0), inSize).rgb;
    vec3 cr = fetchTexel(ip + ivec2( 1, 0), inSize).rgb;
    vec3 cu = fetchTexel(ip + ivec2( 0,-1), inSize).rgb;
    vec3 cd = fetchTexel(ip + ivec2( 0, 1), inSize).rgb;
    vec3 detail = 4.0 * c0 - (cl + cr + cu + cd);
    imageStore(u_OutputTexture, p, vec4(clamp(base / 16.0 + detail * 0.1, 0.0, 1.0), 1.0));
}}"
        );
        let r = gate_sr(&src);
        assert!(r.passed, "{:?}", r.violations);
        assert_eq!(
            r.features.image_loads, 21,
            "跨函数的访存统计不对: 4x4 循环里的 16 次 + main 里的 5 次 = 21"
        );
    }

    /// 守卫在 main 里、写回在被调函数里 —— 守卫状态必须能穿过调用点,
    /// 否则合规算子会被误杀。
    #[test]
    fn guard_state_propagates_into_called_functions() {
        let src = format!(
            "{HDR}
void emit(ivec2 p, vec4 c) {{
    imageStore(u_OutputTexture, p, c);
}}

void main() {{
{GUARD}    emit(p, imageLoad(u_InputTexture, p));
}}"
        );
        let r = gate_sr(&src);
        assert!(r.passed, "守卫没有穿过调用点: {:?}", r.violations);
        assert_eq!(r.features.image_stores, 1);
    }

    /// 反过来: main 里没有守卫, 被调函数里也没有 —— 照样要拦。
    #[test]
    fn an_unguarded_store_inside_a_helper_is_still_caught() {
        let src = format!(
            "{HDR}
void emit(ivec2 p, vec4 c) {{
    imageStore(u_OutputTexture, p, c);
}}

void main() {{
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    emit(p, imageLoad(u_InputTexture, p));
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::UnguardedImageStore);
    }

    /// 死代码里的死循环今天不跑, 接上就挂机 —— 未被调用的函数也要查。
    #[test]
    fn unbounded_loop_in_an_uncalled_helper_is_still_rejected() {
        let src = format!(
            "{HDR}
vec4 unusedHelper() {{
    vec4 c = vec4(0.0);
    while (true) {{ c += vec4(0.01); }}
    return c;
}}

void main() {{
{GUARD}    imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    #[test]
    fn guard_written_as_a_positive_condition_is_accepted() {
        let src = format!(
            "{HDR}void main() {{
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    ivec2 o = imageSize(u_OutputTexture);
    if (p.x < o.x && p.y < o.y) {{
        imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));
    }}
}}"
        );
        let r = gate_sr(&src);
        assert!(r.passed, "正条件守卫被误杀: {:?}", r.violations);
    }

    #[test]
    fn shared_memory_and_uniform_barrier_are_allowed() {
        let src = "#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
shared vec4 tile[16][16];
void main() {
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    ivec2 o = imageSize(u_OutputTexture);
    uvec2 l = gl_LocalInvocationID.xy;
    tile[l.y][l.x] = imageLoad(u_InputTexture, p);
    barrier();
    if (p.x >= o.x || p.y >= o.y) return;
    imageStore(u_OutputTexture, p, tile[l.y][l.x]);
}".to_string();
        let r = gate_sr(&src);
        assert!(r.passed, "片上 shared + 一致 barrier 被误杀: {:?}", r.violations);
        assert_eq!(r.features.workgroup_bytes, 16 * 16 * 16);
    }

    // ---------- 死循环: 门禁的头号职责 ----------

    #[test]
    fn rejects_while_true() {
        let src = sr("vec4 c = vec4(0.0); while (true) { c += vec4(0.01); } imageStore(u_OutputTexture, p, c);");
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    #[test]
    fn rejects_for_ever() {
        let src = sr("vec4 c = vec4(0.0); for (;;) { c += vec4(0.01); } imageStore(u_OutputTexture, p, c);");
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    #[test]
    fn rejects_do_while_true() {
        let src = sr("vec4 c = vec4(0.0); do { c += vec4(0.01); } while (true); imageStore(u_OutputTexture, p, c);");
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    /// 字符串匹配的门禁在这里一定漏: 源码里没有 `while(true)` 字样,
    /// 但条件恒真, 依然是死循环。必须靠 IR 上的常量求值抓到。
    #[test]
    fn rejects_obfuscated_infinite_loop() {
        let src = sr("vec4 c = vec4(0.0); while (1 > 0) { c += vec4(0.01); } imageStore(u_OutputTexture, p, c);");
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    /// 计数器往反方向走 —— 永不触碰上界。
    #[test]
    fn rejects_counter_moving_away_from_bound() {
        let src = sr("vec4 c = vec4(0.0); for (int i = 0; i < 8; i--) { c += vec4(0.01); } imageStore(u_OutputTexture, p, c);");
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    /// 上界来自运行期的 imageSize(): 静态定不了界, 保守打回。
    #[test]
    fn rejects_runtime_bounded_loop() {
        let src = sr(
            "vec4 c = vec4(0.0); int n = imageSize(u_InputTexture).x;
    for (int i = 0; i < n; i++) { c += imageLoad(u_InputTexture, ivec2(i, 0)); }
    imageStore(u_OutputTexture, p, c);",
        );
        assert_rejected(&gate_sr(&src), ViolationCode::UnboundedLoop);
    }

    #[test]
    fn rejects_loop_trip_over_limit() {
        let src = sr(
            "vec4 c = vec4(0.0);
    for (int i = 0; i < 4096; i++) { c += vec4(0.0001); }
    imageStore(u_OutputTexture, p, c);",
        );
        assert_rejected(&gate_sr(&src), ViolationCode::LoopTripTooLarge);
    }

    // ---------- 零拷贝契约 ----------

    #[test]
    fn rejects_storage_buffer() {
        let src = format!(
            "#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
layout(binding = 5, std430) buffer Scratch {{ vec4 staging[]; }} scratch;
void main() {{
{GUARD}    scratch.staging[0] = imageLoad(u_InputTexture, p);
    imageStore(u_OutputTexture, p, scratch.staging[0]);
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::NonZeroCopyResource);
    }

    #[test]
    fn rejects_binding_outside_the_contract() {
        let src = format!(
            "{HDR}layout(binding = 7, rgba8) uniform readonly image2D u_Rogue;
void main() {{
{GUARD}    imageStore(u_OutputTexture, p, imageLoad(u_Rogue, p));
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::UnknownBinding);
    }

    #[test]
    fn rejects_renamed_contract_slot() {
        let src = "#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(binding = 0, rgba8) uniform readonly image2D u_MyInput;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
void main() {
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    ivec2 o = imageSize(u_OutputTexture);
    if (p.x >= o.x || p.y >= o.y) return;
    imageStore(u_OutputTexture, p, imageLoad(u_MyInput, p));
}".to_string();
        assert_rejected(&gate_sr(&src), ViolationCode::BindingNameMismatch);
    }

    #[test]
    fn rejects_missing_required_binding() {
        let src = "#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
void main() {
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    ivec2 o = imageSize(u_OutputTexture);
    if (p.x >= o.x || p.y >= o.y) return;
    imageStore(u_OutputTexture, p, vec4(1.0));
}";
        assert_rejected(&gate_sr(src), ViolationCode::MissingRequiredBinding);
    }

    /// SR 的 binding 布局套到 frame_gen 赛道上必然缺槽位 —— 赛道契约不是摆设。
    #[test]
    fn sr_operator_is_rejected_on_the_frame_gen_track() {
        let src = crate::tracks::embedded_template(crate::TrackMode::Sr);
        let r = Validator::new().gate(src, &FRAME_GEN);
        assert_rejected(&r, ViolationCode::MissingRequiredBinding);
    }

    // ---------- 其它静态安全 ----------

    #[test]
    fn rejects_divergent_barrier() {
        let src = format!(
            "#version 450
layout(local_size_x = 16, local_size_y = 16) in;
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
shared vec4 tile[16][16];
void main() {{
{GUARD}    uvec2 l = gl_LocalInvocationID.xy;
    tile[l.y][l.x] = imageLoad(u_InputTexture, p);
    if (l.x > 3u) {{ barrier(); }}
    imageStore(u_OutputTexture, p, tile[l.y][l.x]);
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::DivergentBarrier);
    }

    #[test]
    fn rejects_unguarded_store() {
        let src = format!(
            "{HDR}void main() {{
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::UnguardedImageStore);
    }

    #[test]
    fn rejects_oversized_workgroup() {
        let src = "#version 450
layout(local_size_x = 64, local_size_y = 64) in;
layout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;
layout(binding = 1, rgba8) uniform writeonly image2D u_OutputTexture;
void main() {
    ivec2 p = ivec2(gl_GlobalInvocationID.xy);
    ivec2 o = imageSize(u_OutputTexture);
    if (p.x >= o.x || p.y >= o.y) return;
    imageStore(u_OutputTexture, p, imageLoad(u_InputTexture, p));
}";
        assert_rejected(&gate_sr(src), ViolationCode::WorkgroupTooLarge);
    }

    #[test]
    fn rejects_syntax_error() {
        let src = sr("imageStore(u_OutputTexture, p imageLoad(u_InputTexture, p));");
        assert_rejected(&gate_sr(&src), ViolationCode::SyntaxError);
    }

    #[test]
    fn rejects_missing_version_directive() {
        let src = "layout(local_size_x = 16, local_size_y = 16) in;\nvoid main() {}";
        assert_rejected(&gate_sr(src), ViolationCode::MissingVersionDirective);
    }

    #[test]
    fn rejects_missing_workgroup_declaration() {
        let src = "#version 450\nlayout(binding = 0, rgba8) uniform readonly image2D u_InputTexture;\nvoid main() {}";
        assert_rejected(&gate_sr(src), ViolationCode::MissingWorkgroupDecl);
    }

    #[test]
    fn rejects_operator_that_writes_nothing() {
        let src = format!(
            "{HDR}void main() {{
    ivec2 o = imageSize(u_OutputTexture);
    float x = float(o.x);
}}"
        );
        assert_rejected(&gate_sr(&src), ViolationCode::NoImageStore);
    }

    /// 门禁必须足够快才配叫「秒级打回」。
    #[test]
    fn gate_is_fast_enough_to_be_a_local_tripwire() {
        let src = crate::tracks::embedded_template(crate::TrackMode::Sr);
        let r = gate_sr(src);
        assert!(
            r.elapsed_us < 500_000,
            "门禁耗时 {} us, 慢到不配做本地熔断",
            r.elapsed_us
        );
    }
}
