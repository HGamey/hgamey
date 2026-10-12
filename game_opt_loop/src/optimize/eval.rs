//! 统一测评接口: `phonefarm eval --request <json> --json`。
//!
//! 三类改动面 (shader / sysparam / gray) 走**同一个**信封、回**同一个**形状的报告,
//! 契约见 `contracts/eval_request.schema.json` 与 `contracts/eval_report.schema.json`。
//! 本仓库出主意, phonefarm 应用/测量/判定/还原 —— 这条分工线在这里是硬的:
//! 下面没有任何一处在判断"快了算不算改进", 那是回包里 `verdict` 的事。
//!
//! # 接收端还没实现的时候
//!
//! `phonefarm eval --request` 是新接口。它上线之前, 本模块的适配层把
//! **shader 类**请求降级成 v1 扁平格式发给已有的 `phonefarm gpu-op`,
//! 让算子那条路先跑起来; sysparam 与 gray 类则如实报"接收端还没有",
//! 不假装跑过。降级发生在哪里、为什么降级, 都会记进报告。

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const REQUEST_VERSION: u32 = 2;

/// 改的是哪一层。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// ① 画面算子。
    Shader,
    /// ② 画面流程改写 (灰档 Vulkan layer)。
    Gray,
    /// ③ 系统参数 (黑档)。
    Sysparam,
    /// ④ 引擎设置 (白盒: 有源码的游戏把渲染设置做成启动参数)。
    Whitebox,
}

impl Kind {
    pub fn id(self) -> &'static str {
        match self {
            Kind::Shader => "shader",
            Kind::Gray => "gray",
            Kind::Sysparam => "sysparam",
            Kind::Whitebox => "whitebox",
        }
    }

    /// 给人看的层名。
    pub fn display(self) -> &'static str {
        match self {
            Kind::Shader => "① 画面算子",
            Kind::Gray => "② 画面流程改写",
            Kind::Sysparam => "③ 系统参数",
            Kind::Whitebox => "④ 引擎设置 (白盒)",
        }
    }
}

/// 在什么东西上量。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CarrierKind {
    /// vkop_runner 孤立跑 dispatch。量不到帧时。
    Headless,
    /// 第一方白盒靶场, 算子挂进它的管线。
    Refbench,
    /// 真实游戏 + 确定性回放脚本。黑盒唯一的路。
    GameReplay,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkloadSpec {
    pub carrier: CarrierKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub script: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seconds: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub frames: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub intensity: Option<u32>,
    /// 渲染线程名 (ftrace comm), 来自游戏档案。缺省时接收端按 Unity 的 UnityGfxDeviceW 解析。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub render_comm: Option<String>,
    /// 每臂重启游戏之后怎么回到游戏场景。`onboard` = 接收端跑 `phonefarm onboard
    /// --enter-only` 的白名单状态机 (有游戏档案的目标才这么填); 缺省 = 不管 (旧行为)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub enter: Option<String>,
    /// 自驱动负载 (应用带 extras 启动后自己跑确定性路线), 有它就不看 script。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub launch: Option<super::games::Launch>,
}

/// 停充意图。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChargingSuspend {
    pub enabled: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node: Option<String>,
}

/// 冻结的测试条件。
///
/// 这些字段的价值不在"跑的时候照着做", 而在**原样回到报告里**:
/// 一份不写清风扇状态与游戏内帧率设置的数字, 换个环境就没法比。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Conditions {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cool_c: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub temp_cap_c: Option<f32>,
    pub require_root: bool,
    pub require_discharging: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub charging_suspend: Option<ChargingSuspend>,
    /// 外置风扇状态。量不到写 "unknown", 不猜。
    pub fan: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ingame_fps_setting: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub screen_brightness: Option<i64>,
    /// 画质真值: 真机截帧目录或单张 PNG。
    ///
    /// 放在信封上而不是 shader 的 payload 里, 是因为**不止 shader 会动画面**:
    /// 灰档的每一个改写 (LoadOp / 精度 / 渲染分辨率) 都改画面, 那一层的
    /// psnr 约束没有真值就判不了。payload 里的同名字段仍然优先, 那是
    /// 单个候选的一次性覆盖。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
}

/// 一个指标看哪个方向。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MetricSpec {
    pub id: String,
    /// "lower" | "higher"
    pub better: String,
    /// true = 这一项是约束不是目标 (掉下去一票否决, 不改善不算输)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub constraint: Option<bool>,
}

impl MetricSpec {
    fn lower(id: &str) -> Self {
        Self {
            id: id.into(),
            better: "lower".into(),
            constraint: None,
        }
    }
    fn higher(id: &str) -> Self {
        Self {
            id: id.into(),
            better: "higher".into(),
            constraint: None,
        }
    }
    fn constraint_higher(id: &str) -> Self {
        Self {
            id: id.into(),
            better: "higher".into(),
            constraint: Some(true),
        }
    }
}

/// 冻结的统计协议。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Protocol {
    /// v1 遗留字段, 留着让降级适配层直接用。
    pub cool_c: f32,
    /// v1 遗留字段。
    pub replay_seconds: u32,
    /// **每臂**轮数。
    pub rounds: u32,
    pub alternating: bool,
    pub p_threshold: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stat: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub multiple_comparison: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub metrics: Option<Vec<MetricSpec>>,
}

impl Protocol {
    /// 某一层的缺省判定口径。
    ///
    /// 三层看的东西不一样, 这不是随便选的:
    ///
    /// - sysparam 不动画面, 画质天然不变, 量它是浪费真机时间;
    /// - shader 动画面, 画质是**约束**不是目标 —— 哪怕显著更快, 掉了画质也是不合格品;
    /// - gray 改的是渲染流程 (LoadOp / 精度 / 分辨率), 每一项都可能动画面, 所以也要量画质。
    pub fn for_kind(kind: Kind, rounds: u32, seconds: u32, cool_c: f32) -> Self {
        let metrics = match kind {
            Kind::Sysparam => vec![
                MetricSpec::lower("fps_p95_ms"),
                MetricSpec::higher("fps_mean"),
                MetricSpec::lower("power_watt"),
            ],
            Kind::Shader => vec![
                MetricSpec::lower("operator_latency_ms"),
                MetricSpec::lower("power_watt"),
                MetricSpec::constraint_higher("psnr_db"),
            ],
            Kind::Gray | Kind::Whitebox => vec![
                MetricSpec::lower("fps_p95_ms"),
                MetricSpec::higher("fps_mean"),
                MetricSpec::lower("power_watt"),
                MetricSpec::constraint_higher("psnr_db"),
            ],
        };
        Self {
            cool_c,
            replay_seconds: seconds.max(1),
            rounds,
            alternating: true,
            p_threshold: 0.05,
            // 每臂轮数少的时候精确置换检验比 t 检验稳: 它不假设分布,
            // 而且能自己交代分辨率上限 (n v n 的最小可达 p = 2/C(2n,n))。
            // shader 那条路沿用接收端已有的 Welch, 换检验等于换了历史可比性。
            stat: Some(match kind {
                Kind::Shader => "welch_t".into(),
                _ => "exact_permutation".into(),
            }),
            // 三层都收紧。画质是约束不是目标, 不参与「改善」那一侧的计数,
            // 所以 shader 那边收不收紧对结论影响小; 但统一收紧没有坏处,
            // 而且省掉一处"这一层为什么不一样"的解释。
            multiple_comparison: Some("bonferroni_on_wins".into()),
            metrics: Some(metrics),
        }
    }
}

/// 各层特有的部分。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Payload {
    Shader(ShaderPayload),
    Sysparam(SysparamPayload),
    Gray(GrayPayload),
    Whitebox(WhiteboxPayload),
}

/// ④ 白盒引擎设置。对照臂 = workload.launch.extras; 改写臂 = 后面追加 `--es <旋钮> <值>`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WhiteboxPayload {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub why: Option<String>,
    pub knobs: BTreeMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub measure_only: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_check: Option<ImageCheck>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShaderPayload {
    pub track: String,
    pub shader_path: String,
    pub spirv_path: String,
    pub budget_ms: f32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_baseline_db: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub incumbent_latency_ms: Option<f32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality_reference: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inject: Option<Inject>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Inject {
    /// "carrier_native" | "vulkan_layer"
    pub via: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub layer: Option<String>,
}

/// 一条落到设备上的写入。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PlanEntry {
    /// "sysfs" | "setting"
    pub kind: String,
    pub path: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SysparamPayload {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub why: Option<String>,
    pub params: BTreeMap<String, String>,
    pub plan: Vec<PlanEntry>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub whitelist: Option<BTreeMap<String, super::sysparam::ParamSpec>>,
    pub deny_keywords: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Rewrite {
    pub knob: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scope: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub args: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GrayPayload {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub why: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub layer: Option<String>,
    pub rewrites: Vec<Rewrite>,
    /// 候选臂必须真的命中过改写, 否则整轮作废。
    ///
    /// 恒为 true 不是保守: 改写没生效却当成有效样本, 等于拿 A 去和 A 比,
    /// 会稳定产出"无显著差异"的假阴性 —— 而那正好长得像"这个改写没用"。
    pub require_hit: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub side_effect_ack: Option<String>,
    /// 画面比对: 每轮改写臂与对照臂同位置截图的 PSNR 低于门槛 → ABORT「画面出错」。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_check: Option<ImageCheck>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImageCheck {
    pub min_psnr_db: f64,
}

/// v2 评测请求。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Request {
    pub version: u32,
    pub kind: Kind,
    pub candidate_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub serial: Option<String>,
    pub package: String,
    pub workload: WorkloadSpec,
    pub conditions: Conditions,
    pub protocol: Protocol,
    pub payload: Payload,
}

/// 回包的逐指标明细。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MetricDetail {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub a_mean: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub b_mean: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diff_mean: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diff_pct: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub p: Option<f64>,
}

/// 量不到的字段 + 原因。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Unavailable {
    pub field: String,
    pub reason: String,
}

/// v2 指标表。所有层共用; 量不到的项是 `None`, **不是 0**。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct MetricsV2 {
    #[serde(default)]
    pub operator_latency_ms: Option<f64>,
    #[serde(default)]
    pub fps_p95_ms: Option<f64>,
    #[serde(default)]
    pub frame_p95_ms: Option<f64>,
    #[serde(default)]
    pub fps_mean: Option<f64>,
    #[serde(default)]
    pub power_watt: Option<f64>,
    #[serde(default)]
    pub psnr_db: Option<f64>,
    #[serde(default)]
    pub hud_ghost_db: Option<f64>,
    #[serde(default)]
    pub stretch_pct: Option<f64>,
    #[serde(default)]
    pub soc_temp_max_c: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unavailable: Option<Vec<Unavailable>>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct VerdictV2 {
    #[serde(default)]
    pub is_pareto_improvement: bool,
    #[serde(default = "one")]
    pub p_value: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stat: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alpha_win: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub alpha_regression: Option<f64>,
    /// 本轮轮数下 `alpha_win` 够不够得着。false 时 REJECT 读不出"这组没用"。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reachable: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wins: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub regressions: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub per_metric: Option<BTreeMap<String, MetricDetail>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

fn one() -> f64 {
    1.0
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ConditionsObserved {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cool_reached_c: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cool_timeout: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub soc_temp_max_c: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fan: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ingame_fps_setting: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub apply_ok: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hits: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_identical: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub restored: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_diff: Option<Vec<String>>,
    /// ③: 改写臂里有几臂被厂商性能服务抢回 (施加后回读对不上), 这几臂没进统计。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contested_arms: Option<u32>,
    /// ③: 快照里只有厂商服务会动的频率边界变了 (不判留痕), 逐项 `键: 前 → 后`。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_vendor_drift: Option<Vec<String>>,
    /// ②: 画面比对的逐轮 PSNR 与门槛 (原样)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_check: Option<serde_json::Value>,
}

/// v2 评测报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Report {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<Kind>,
    pub candidate_id: String,
    pub status: String,
    #[serde(default)]
    pub metrics: MetricsV2,
    #[serde(default)]
    pub verdict: VerdictV2,
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub power_source: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quality_reference: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conditions_observed: Option<ConditionsObserved>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence_dir: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phonefarm_version: Option<String>,
    /// 这份报告是怎么拿到的 —— 直接调 eval, 还是经适配层降级调了 gpu-op。
    /// 不是契约字段, 只进本仓库的证据。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub obtained_via: Option<String>,
}

impl Report {
    /// 这条记录能不能进档案 / 能不能当负样本。
    ///
    /// ABORT 是"这次没量准", 不是"量到了, 不好"。把两者混为一谈, 演化就会把
    /// 工装故障学成算法结论。
    pub fn is_measurement_valid(&self) -> bool {
        self.status != "ABORT"
    }

    /// 值不值得保留。
    pub fn kept(&self) -> bool {
        self.is_measurement_valid() && self.verdict.is_pareto_improvement
    }
}

/// 接收端认不认 `eval --request`。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "state")]
pub enum EvalSupport {
    /// 认。
    Supported,
    /// 不认 —— 接收端还没实现, 或还是只吃 `--set` 的旧版。
    Unsupported { detail: String },
    /// 探不出来 (二进制不在, 或起不来)。
    Unknown { detail: String },
}

impl EvalSupport {
    pub fn is_supported(&self) -> bool {
        matches!(self, EvalSupport::Supported)
    }

    pub fn describe(&self) -> String {
        match self {
            EvalSupport::Supported => "phonefarm eval --request 可用".to_string(),
            EvalSupport::Unsupported { detail } => {
                format!("phonefarm 还不认 `eval --request`: {detail}")
            }
            EvalSupport::Unknown { detail } => format!("探不出 `eval --request`: {detail}"),
        }
    }
}

/// 统一测评接口的驱动。
pub struct EvalDriver {
    binary: PathBuf,
    #[allow(clippy::type_complexity)]
    runner: Option<Box<dyn Fn(&str, &[String]) -> Result<(Option<i32>, String, String)> + Send + Sync>>,
}

/// 进门的结果。
#[derive(Debug, Clone, PartialEq)]
pub enum EnterOutcome {
    InGame,
    /// 要人动手 (登录/实名/验证码/更新)。带接收端最后几行输出。
    NeedsHuman(String),
    Failed(String),
    /// 接收端没有 `onboard` 子命令。
    Unsupported,
}

impl EvalDriver {
    pub fn new(binary: impl Into<PathBuf>) -> Self {
        Self {
            binary: binary.into(),
            runner: None,
        }
    }

    /// 注入替身 (只供测试用): 给 (子命令, 参数) 回 (退出码, stdout, stderr)。
    pub fn with_runner(
        mut self,
        f: impl Fn(&str, &[String]) -> Result<(Option<i32>, String, String)> + Send + Sync + 'static,
    ) -> Self {
        self.runner = Some(Box::new(f));
        self
    }

    pub fn binary(&self) -> &Path {
        &self.binary
    }

    fn call(&self, sub: &str, args: &[String]) -> Result<(Option<i32>, String, String)> {
        if let Some(f) = self.runner.as_ref() {
            return f(sub, args);
        }
        // stderr 一边收集一边实时转进自己的日志: 接收端的进度 (等冷、轮次) 只往 stderr 打,
        // 不转的话一次几十分钟的评测在节点日志里一行都看不到, 分不清是在等冷还是卡死了。
        use std::io::{BufRead, Read};
        let mut child = Command::new(&self.binary)
            .arg(sub)
            .args(args)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .with_context(|| format!("调起 `{} {sub}` 失败", self.binary.display()))?;
        let pid = child.id();
        super::children::register(pid);
        let echo = matches!(sub, "eval" | "onboard");
        let err = child.stderr.take();
        let reader = std::thread::spawn(move || {
            let mut all = String::new();
            if let Some(e) = err {
                for line in std::io::BufReader::new(e).lines().map_while(|l| l.ok()) {
                    if echo && !line.trim().is_empty() {
                        println!("        │ {line}");
                    }
                    all.push_str(&line);
                    all.push('\n');
                }
            }
            all
        });
        let mut stdout = String::new();
        if let Some(mut o) = child.stdout.take() {
            let mut buf = Vec::new();
            let _ = o.read_to_end(&mut buf);
            stdout = String::from_utf8_lossy(&buf).into_owned();
        }
        let status = child.wait();
        super::children::unregister(pid);
        let stderr = reader.join().unwrap_or_default();
        let status = status.with_context(|| format!("等 `{} {sub}` 退出失败", self.binary.display()))?;
        Ok((status.code(), stdout, stderr))
    }

    /// 探一下接收端认不认 `eval --request`。
    ///
    /// 用一个**不存在**的请求路径探: 认这个参数的接收端会报"读不到文件",
    /// 不认的会报"未知参数"或继续要 `--set`。这样探不会真的跑一轮测试,
    /// 也不碰设备。
    pub fn probe(&self) -> EvalSupport {
        if !self.binary.is_file() {
            return EvalSupport::Unknown {
                detail: format!("找不到 {}", self.binary.display()),
            };
        }
        let probe_path = "/nonexistent/game_opt_loop_eval_probe.json".to_string();
        let args = vec!["--request".to_string(), probe_path, "--json".to_string()];
        let (_, stdout, stderr) = match self.call("eval", &args) {
            Ok(v) => v,
            Err(e) => {
                return EvalSupport::Unknown {
                    detail: e.to_string(),
                }
            }
        };
        let merged = format!("{stdout}{stderr}");
        if merged.contains("未知子命令") || merged.contains("unknown subcommand") {
            return EvalSupport::Unsupported {
                detail: "phonefarm 没有 eval 子命令".into(),
            };
        }
        // 旧版 eval 只吃 --set, 会直接抱怨 --set 必填。
        if merged.contains("--set") {
            return EvalSupport::Unsupported {
                detail: "当前 eval 仍然只吃 `--set <evalset.toml>`, 没有 `--request`".into(),
            };
        }
        if merged.contains("未知参数") || merged.contains("unknown option") {
            return EvalSupport::Unsupported {
                detail: "eval 不认 `--request`".into(),
            };
        }
        EvalSupport::Supported
    }

    /// 下发一次评测。请求先落盘再把路径传下去 —— 既避开 argv 长度限制,
    /// 也让每一次下发都留下可回溯的证据。
    /// 把黑盒游戏带进游戏场景: `phonefarm onboard --package P --serial S --enter-only`。
    ///
    /// 系统参数那一层的评测不重启游戏, 量的是**屏幕上现在是什么**: 游戏被别人
    /// force-stop 了、停在登录页, 量到的就是桌面/登录页。所以每个候选上机前先进门。
    /// 接收端的退出码: 0 = 在游戏里, 3 = 需要人 (登录/实名/验证码), 其余 = 失败。
    /// 接收端不认这个子命令 (旧版 phonefarm) 时回 `Unsupported`, 调用方照旧行为继续。
    pub fn enter_scene(&self, package: &str, serial: &str, out: &Path) -> EnterOutcome {
        let args = vec![
            "--package".to_string(),
            package.to_string(),
            "--serial".to_string(),
            serial.to_string(),
            "--enter-only".to_string(),
            "--out".to_string(),
            out.display().to_string(),
        ];
        match self.call("onboard", &args) {
            Err(e) => EnterOutcome::Failed(e.to_string()),
            Ok((code, stdout, stderr)) => {
                let tail = |s: &str| s.lines().rev().take(3).collect::<Vec<_>>().join(" | ");
                if stderr.contains("未知子命令") || stderr.contains("unknown subcommand") {
                    return EnterOutcome::Unsupported;
                }
                match code {
                    Some(0) => EnterOutcome::InGame,
                    Some(3) => EnterOutcome::NeedsHuman(tail(&stdout)),
                    c => EnterOutcome::Failed(format!(
                        "退出码 {c:?}: {} {}",
                        tail(&stdout),
                        tail(&stderr)
                    )),
                }
            }
        }
    }

    pub fn evaluate(&self, request: &Request, request_path: &Path) -> Result<Report> {
        if let Some(dir) = request_path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(request_path, serde_json::to_string_pretty(request)?)
            .with_context(|| format!("写入评测请求失败: {}", request_path.display()))?;

        let mut args = vec![
            "--request".to_string(),
            request_path.display().to_string(),
            "--json".to_string(),
        ];
        // 证据目录放进 optimize 的输出目录 (<out>/evidence/<候选>), 不放接收端的工作目录:
        // 真机跑在远端节点上时只有输出目录会被拉回来, 落在别处的 apply 日志、快照、截图
        // 就再也看不到了 (10-11 两次 ③ 的 ABORT 就是这样查不了原因)。
        if let (Some(dir), Some(stem)) = (
            request_path.parent().and_then(|p| p.parent()),
            request_path.file_stem(),
        ) {
            args.push("--out".to_string());
            args.push(dir.join("evidence").join(stem).display().to_string());
        }
        let (code, stdout, stderr) = self.call("eval", &args)?;

        // 退出码契约与 gpu-op 同源 (phonefarm docs/SPEC_GPU_OP.md §1):
        //   0 = 判定通过; 1 = **测量有效但判定失败**, 回包是完整报告;
        //   2 = 用法/设备/测量本身出错, 没有可信数字。
        // 把 1 当成错误会把"这个候选确实更差"误报成"评测坏了", 上层就永远学不到负样本。
        if !matches!(code, Some(0) | Some(1)) {
            bail!(
                "phonefarm eval 退出码 {code:?}\nstderr: {}",
                stderr.lines().take(10).collect::<Vec<_>>().join("\n")
            );
        }

        let mut report = parse_report(&stdout).with_context(|| {
            format!(
                "phonefarm eval 退出码 {code:?}, 回包无法解析\nstderr: {}",
                stderr.lines().take(10).collect::<Vec<_>>().join("\n")
            )
        })?;
        report.obtained_via.get_or_insert("phonefarm eval".into());

        // 回包的 kind 与请求对不上 = 发错了或认错了。照收会让证据错位,
        // 所以整份作废而不是将就。
        if let Some(k) = report.kind {
            if k != request.kind {
                bail!(
                    "回包的 kind 是 {}, 请求的是 {} —— 对不上, 整份作废",
                    k.id(),
                    request.kind.id()
                );
            }
        }
        Ok(report)
    }
}

/// 从 stdout 里抠出报告对象。
///
/// 从**最前面**的 `{` 往后找第一个带 `candidate_id` 的对象。从尾部倒着找是错的:
/// 嵌套的 `metrics` / `verdict` 自己也是合法 JSON, 会先命中它们。
pub fn parse_report(stdout: &str) -> Result<Report> {
    let bytes = stdout.as_bytes();
    for (i, _) in bytes.iter().enumerate().filter(|(_, b)| **b == b'{') {
        let mut de = serde_json::Deserializer::from_str(&stdout[i..]);
        if let Ok(v) = serde_json::Value::deserialize(&mut de) {
            if v.get("candidate_id").is_some() {
                return serde_json::from_value(v).context("回包字段与契约不符");
            }
        }
    }
    bail!("stdout 里没有 eval_report 对象")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_request(kind: Kind) -> Request {
        Request {
            version: REQUEST_VERSION,
            kind,
            candidate_id: "gen1_c1".into(),
            serial: Some("S1".into()),
            package: "com.miHoYo.Yuanshen".into(),
            workload: WorkloadSpec {
                carrier: CarrierKind::GameReplay,
                script: Some("w.json".into()),
                seconds: Some(36),
                frames: None,
                intensity: None,
                render_comm: None,
                enter: None,
                launch: None,
            },
            conditions: Conditions {
                cool_c: Some(40.0),
                temp_cap_c: Some(48.0),
                require_root: true,
                require_discharging: true,
                charging_suspend: Some(ChargingSuspend {
                    enabled: true,
                    node: None,
                }),
                fan: "unknown".into(),
                ingame_fps_setting: None,
                screen_brightness: None,
                quality_reference: None,
                notes: None,
            },
            protocol: Protocol::for_kind(kind, 5, 36, 40.0),
            payload: match kind {
                Kind::Whitebox => Payload::Whitebox(WhiteboxPayload {
                    why: Some("雾挪到透明物之后".into()),
                    knobs: BTreeMap::from([("fog_event".to_string(), "AfterRenderingTransparents".to_string())]),
                    measure_only: None,
                    image_check: Some(ImageCheck { min_psnr_db: 30.0 }),
                }),
                Kind::Sysparam => Payload::Sysparam(SysparamPayload {
                    why: Some("试试总线下限".into()),
                    params: BTreeMap::from([("bus.DDR.boost_freq".to_string(), "5333000".to_string())]),
                    plan: vec![PlanEntry {
                        kind: "sysfs".into(),
                        path: "/sys/devices/system/cpu/bus_dcvs/DDR/boost_freq".into(),
                        value: "5333000".into(),
                    }],
                    whitelist: None,
                    deny_keywords: super::super::sysparam::DENY_KEYWORDS
                        .iter()
                        .map(|s| s.to_string())
                        .collect(),
                }),
                Kind::Gray => Payload::Gray(GrayPayload {
                    why: None,
                    layer: None,
                    rewrites: vec![Rewrite {
                        knob: "storeop_dont_care".into(),
                        scope: Some("15a51a879df99c32:2".into()),
                        args: None,
                    }],
                    require_hit: true,
                    side_effect_ack: None,
                    image_check: Some(ImageCheck { min_psnr_db: 30.0 }),
                }),
                Kind::Shader => Payload::Shader(ShaderPayload {
                    track: "sr".into(),
                    shader_path: "a.comp".into(),
                    spirv_path: "a.spv".into(),
                    budget_ms: 1.5,
                    quality_baseline_db: Some(30.0),
                    incumbent_latency_ms: None,
                    quality_reference: None,
                    inject: None,
                }),
            },
        }
    }

    #[test]
    fn requests_round_trip_for_every_kind() {
        for kind in [Kind::Shader, Kind::Gray, Kind::Sysparam, Kind::Whitebox] {
            let r = sample_request(kind);
            let v = serde_json::to_value(&r).unwrap();
            assert_eq!(v["version"], 2);
            assert_eq!(v["kind"], kind.id());
            assert!(v["payload"].is_object());
            let back: Request = serde_json::from_value(v).unwrap();
            assert_eq!(back.kind, kind);
        }
    }

    /// 系统参数不动画面, 判定口径里就不该有画质 —— 量一个恒定不变的东西
    /// 是在浪费真机时间, 还会把多重比较的 K 无谓抬高。
    #[test]
    fn sysparam_does_not_ask_for_image_quality() {
        let p = Protocol::for_kind(Kind::Sysparam, 5, 36, 40.0);
        let ids: Vec<&str> = p
            .metrics
            .as_ref()
            .unwrap()
            .iter()
            .map(|m| m.id.as_str())
            .collect();
        assert!(!ids.contains(&"psnr_db"), "{ids:?}");

        // 改画面的两层必须量画质, 而且是当约束量。
        for kind in [Kind::Shader, Kind::Gray, Kind::Whitebox] {
            let p = Protocol::for_kind(kind, 5, 36, 40.0);
            let psnr = p
                .metrics
                .as_ref()
                .unwrap()
                .iter()
                .find(|m| m.id == "psnr_db")
                .unwrap_or_else(|| panic!("{kind:?} 少了画质"));
            assert_eq!(psnr.constraint, Some(true), "画质是约束不是目标");
        }
    }

    #[test]
    fn report_parsing_takes_the_outer_object_not_a_nested_one() {
        let out = "进度行\n{\"candidate_id\":\"c1\",\"status\":\"PASS\",\
            \"metrics\":{\"fps_p95_ms\":16.7,\"power_watt\":null},\
            \"verdict\":{\"is_pareto_improvement\":true,\"p_value\":0.004},\
            \"source\":\"phonefarm\",\"kind\":\"sysparam\"}\n";
        let r = parse_report(out).unwrap();
        assert_eq!(r.candidate_id, "c1");
        assert_eq!(r.kind, Some(Kind::Sysparam));
        assert_eq!(r.metrics.fps_p95_ms, Some(16.7));
        assert!(r.metrics.power_watt.is_none(), "null 必须留成 None, 不能变 0");
    }

    /// 旧版 gpu-op 的扁平回包 (没有 version / kind) 照样解析得动。
    #[test]
    fn a_v1_report_still_parses() {
        let legacy = r#"{"candidate_id":"c","status":"PASS",
            "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":7.3,"power_watt":6.5,"psnr_db":31.3},
            "verdict":{"is_pareto_improvement":false,"p_value":0.004},"source":"phonefarm"}"#;
        let r = parse_report(legacy).unwrap();
        assert!(r.version.is_none());
        assert!(r.kind.is_none());
        assert_eq!(r.metrics.operator_latency_ms, Some(0.5));
    }

    /// ABORT 与 REJECT 必须分得开。
    #[test]
    fn abort_is_not_a_negative_sample() {
        let mk = |status: &str| Report {
            version: Some(2),
            kind: Some(Kind::Sysparam),
            candidate_id: "c".into(),
            status: status.into(),
            metrics: MetricsV2::default(),
            verdict: VerdictV2::default(),
            source: "phonefarm".into(),
            power_source: None,
            quality_reference: None,
            conditions_observed: None,
            evidence_dir: None,
            phonefarm_version: None,
            obtained_via: None,
        };
        assert!(!mk("ABORT").is_measurement_valid());
        assert!(mk("REJECT").is_measurement_valid());
        assert!(mk("PASS").is_measurement_valid());
        assert!(!mk("ABORT").kept());
    }

    #[test]
    fn a_mismatched_kind_in_the_reply_is_refused() {
        let dir = std::env::temp_dir().join(format!("gol_eval_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let d = EvalDriver::new("/nonexistent/phonefarm").with_runner(|_, _| {
            Ok((
                Some(0),
                r#"{"candidate_id":"gen1_c1","status":"PASS","kind":"gray",
                    "metrics":{},"verdict":{"is_pareto_improvement":true,"p_value":0.01},
                    "source":"phonefarm"}"#
                    .to_string(),
                String::new(),
            ))
        });
        let err = d
            .evaluate(&sample_request(Kind::Sysparam), &dir.join("req.json"))
            .unwrap_err();
        assert!(err.to_string().contains("对不上"), "{err}");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// 退出码 1 是"测量有效但判定失败", 不是错误。
    #[test]
    fn exit_code_one_is_a_verdict_not_a_failure() {
        let dir = std::env::temp_dir().join(format!("gol_eval1_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let d = EvalDriver::new("/nonexistent/phonefarm").with_runner(|_, _| {
            Ok((
                Some(1),
                r#"{"candidate_id":"gen1_c1","status":"REJECT","kind":"sysparam",
                    "metrics":{},"verdict":{"is_pareto_improvement":false,"p_value":0.7},
                    "source":"phonefarm"}"#
                    .to_string(),
                String::new(),
            ))
        });
        let r = d
            .evaluate(&sample_request(Kind::Sysparam), &dir.join("req.json"))
            .unwrap();
        assert_eq!(r.status, "REJECT");
        assert!(r.is_measurement_valid());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn exit_code_two_is_a_failure() {
        let dir = std::env::temp_dir().join(format!("gol_eval2_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let d = EvalDriver::new("/nonexistent/phonefarm")
            .with_runner(|_, _| Ok((Some(2), String::new(), "设备掉了".to_string())));
        assert!(d
            .evaluate(&sample_request(Kind::Shader), &dir.join("req.json"))
            .is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    /// 只吃 `--set` 的旧版 eval 必须被认出来 —— 否则会把"参数没生效"
    /// 当成"跑过了", 拿一份空报告继续往下走。
    #[test]
    fn an_old_eval_that_only_takes_set_is_detected() {
        let dir = std::env::temp_dir().join(format!("gol_probe_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fake = dir.join("phonefarm");
        std::fs::write(&fake, "#!/bin/sh\n").unwrap();

        let d = EvalDriver::new(&fake)
            .with_runner(|_, _| Ok((Some(2), String::new(), "--set <evalset.toml> 必填".into())));
        assert!(matches!(d.probe(), EvalSupport::Unsupported { .. }));

        let d = EvalDriver::new(&fake)
            .with_runner(|_, _| Ok((Some(2), String::new(), "读取请求失败: 文件不存在".into())));
        assert!(d.probe().is_supported());

        std::fs::remove_dir_all(&dir).ok();
    }

    /// 进门的退出码契约: 0 在游戏里, 3 要人, 其余失败; 旧接收端不认子命令时照旧继续。
    #[test]
    fn enter_scene_maps_receiver_exit_codes() {
        let p = std::path::Path::new("/tmp/x");
        let run = |code: Option<i32>, err: &'static str| {
            EvalDriver::new("/nonexistent/phonefarm").with_runner(move |sub, args| {
                assert_eq!(sub, "onboard");
                assert!(args.iter().any(|a| a == "--enter-only"));
                Ok((code, "{\"status\":\"X\"}".into(), err.into()))
            })
        };
        assert_eq!(run(Some(0), "").enter_scene("pkg", "S", p), EnterOutcome::InGame);
        assert!(matches!(run(Some(3), "").enter_scene("pkg", "S", p), EnterOutcome::NeedsHuman(_)));
        assert!(matches!(run(Some(1), "").enter_scene("pkg", "S", p), EnterOutcome::Failed(_)));
        assert_eq!(
            run(Some(2), "未知子命令 onboard").enter_scene("pkg", "S", p),
            EnterOutcome::Unsupported
        );
    }
}
