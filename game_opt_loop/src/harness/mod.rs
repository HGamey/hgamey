//! phonefarm 物理底座驱动
//!
//! 本仓库**不碰硬件**。冷机控温、60s 确定性回放、A/B/A/B 交替、SmartPerf 采样、
//! 冻结规则 t 检验、原子还原 —— 全部是 `../phonefarm/phonefarm` 的职责。
//! 这里只做三件事: 组评测请求、调进程、把回包反序列化成 `EvalReport`。
//!
//! # 关于 probe 的诚实性
//!
//! 旧实现用 `which ../phonefarm/phonefarm` 探活。`which` 只查 PATH, 对相对路径
//! 永远失败, 于是那段代码在任何情况下都走到「就绪模式」分支, 无论底座在不在都报成功。
//! 探活探出来的必须是真状态, 否则整条链路的第一个环节就在骗人。

use crate::engine::{Candidate, EvalReport, MetricSource};
use crate::tracks::TrackSpec;
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

/// 评测协议参数 —— 防硬件噪声欺骗的那一套 (DESIGN.md §2 红线 4)。
///
/// 这些数字在**看到候选数据之前**就落盘冻结, 事后不得调整。
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct EvalProtocol {
    /// 冷机门槛: SoC 结温低于此值才放行 (摄氏度)。
    pub cool_c: f32,
    /// 单次回放时长 (秒)。
    pub replay_seconds: u32,
    /// A/B 交替组数。
    pub rounds: u32,
    /// 是否严格 A/B/A/B 交替 (而不是跑完 A 再跑 B)。
    pub alternating: bool,
    /// 两样本 t 检验的显著性门槛。
    pub p_threshold: f64,
}

impl Default for EvalProtocol {
    fn default() -> Self {
        Self {
            cool_c: 32.0,
            replay_seconds: 60,
            rounds: 4,
            alternating: true,
            p_threshold: 0.01,
        }
    }
}

/// 下发给 phonefarm 的评测请求。落盘格式即 `contracts/eval_request.schema.json`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalRequest {
    pub version: u32,
    pub candidate_id: String,
    pub track: String,
    /// 目标设备序列号。None = 交给 phonefarm 自己选唯一在线设备。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub serial: Option<String>,
    /// 被测游戏包名。
    pub package: String,
    /// 确定性回放脚本 (phonefarm script 模式的脚本 id 或路径)。
    pub replay_script: String,
    /// 算子 GLSL 源码路径。
    pub shader_path: String,
    /// 预编译好的 SPIR-V 路径 —— 本地门禁已经出过一次, 不让真机再编一遍。
    pub spirv_path: String,
    pub budget_ms: f32,
    pub quality_baseline_db: f32,
    /// 在位冠军算子的实测耗时 (ms)。None = 还没有冠军 (第 0 代)。
    ///
    /// 为什么要带这个: refbench 载体的 A 臂是**不挂任何算子**的场景
    /// (`knob.postfx off`), 所以 A 臂的"算子耗时"按定义是 0。接收端原本拿
    /// `候选 < 基线` 判"变快没有", 在这个载体下等价于 `正数 < 0` —— 恒假。
    /// 于是 `is_pareto_improvement` 在 refbench 上**永远是 false**, 哪怕候选
    /// 确实比上一代冠军便宜一半。
    ///
    /// 把冠军耗时显式带下去, 那个判断才问得出真问题:
    /// "它在真实场景里比现任冠军便宜吗"。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub incumbent_latency_ms: Option<f32>,
    pub protocol: EvalProtocol,
}

/// 底座探活结果。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ProbeStatus {
    /// 二进制在, 且报告了至少一台在线设备。
    Ready { devices: Vec<String> },
    /// 二进制在, 但没有在线设备。
    NoDevice,
    /// 二进制在, 但执行探活命令失败。
    BinaryUnusable { detail: String },
    /// 压根找不到 phonefarm。
    Missing { looked_at: String },
}

impl ProbeStatus {
    pub fn can_evaluate(&self) -> bool {
        matches!(self, ProbeStatus::Ready { .. })
    }

    pub fn describe(&self) -> String {
        match self {
            ProbeStatus::Ready { devices } => {
                format!("就绪 — {} 台在线设备: {}", devices.len(), devices.join(", "))
            }
            ProbeStatus::NoDevice => "phonefarm 可用, 但没有在线设备".to_string(),
            ProbeStatus::BinaryUnusable { detail } => format!("phonefarm 无法执行: {detail}"),
            ProbeStatus::Missing { looked_at } => format!("找不到 phonefarm ({looked_at})"),
        }
    }
}

/// phonefarm 认不认算子评测子命令。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "state")]
pub enum SubcommandStatus {
    /// 认。
    Supported,
    /// 明确回了「未知子命令」—— 接收端还没实现。
    Unsupported { subcommand: String },
    /// 探不出来 (二进制不在, 或起不来)。
    Unknown { detail: String },
}

impl SubcommandStatus {
    pub fn is_supported(&self) -> bool {
        matches!(self, SubcommandStatus::Supported)
    }

    pub fn describe(&self) -> String {
        match self {
            SubcommandStatus::Supported => "算子评测子命令可用".to_string(),
            SubcommandStatus::Unsupported { subcommand } => format!(
                "phonefarm 不认 `{subcommand}` 子命令 —— 真机算子评测的接收端尚未实现"
            ),
            SubcommandStatus::Unknown { detail } => format!("无法探测算子评测子命令: {detail}"),
        }
    }
}

/// 一次子进程调用的结果。
///
/// 把它从 `std::process::Output` 里抽出来, 是为了让「下发 → 收包 → 解析」
/// 这条链路能在没有真机、也没有 phonefarm 接收端的情况下被完整测到。
/// 目前 phonefarm 还不认算子评测子命令 (见 `probe_subcommand`),
/// 没有这层抽象的话, 本仓库这一侧的发送逻辑就只能靠肉眼看。
#[derive(Debug, Clone)]
pub struct RunOutcome {
    pub exit_code: Option<i32>,
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
}

/// 替身执行器: 收到请求文件路径, 回一份进程结果。
type Runner = Box<dyn Fn(&Path) -> Result<RunOutcome> + Send + Sync>;

/// phonefarm 驱动。
pub struct PhonefarmDriver {
    binary: PathBuf,
    /// 评测载体: headless 孤立工装, 或 refbench 真实渲染场景。
    ///
    /// refbench 载体下 archive 里存的是**场景内**的代价 —— 真实帧时 p95、
    /// 场景内整机瓦数、算子挂进管线后的边际 GPU 开销。孤立跑出来的微秒数
    /// 不等于它在真实渲染上下文里的代价 (实测同一算子 0.942 vs 0.477 ms)。
    carrier: String,
    /// refbench 每臂渲染帧数。
    frames: u32,
    /// refbench 场景的 fragment 负载强度。
    intensity: u32,
    /// 下发评测用的子命令。phonefarm 侧新增算子评测能力后从这里对接。
    subcommand: String,
    package: String,
    replay_script: String,
    serial: Option<String>,
    protocol: EvalProtocol,
    /// 测试用替身。None = 真的去 fork phonefarm 进程。
    runner: Option<Runner>,
}

impl Default for PhonefarmDriver {
    fn default() -> Self {
        Self::new()
    }
}

impl PhonefarmDriver {
    pub fn new() -> Self {
        Self {
            binary: PathBuf::from("../phonefarm/phonefarm"),
            subcommand: "gpu-op".to_string(),
            package: "com.miHoYo.Yuanshen".to_string(),
            replay_script: "sr_replay_60s".to_string(),
            serial: None,
            protocol: EvalProtocol::default(),
            runner: None,
            carrier: "refbench".to_string(),
            frames: 900,
            intensity: 5,
        }
    }

    pub fn with_carrier(mut self, carrier: impl Into<String>) -> Self {
        self.carrier = carrier.into();
        self
    }

    /// 覆盖冷机门槛。
    ///
    /// 冻结协议的 <32C 假定挂了外置风扇; 没有风扇的台子上那个门槛等不到。
    /// 放宽它就是在改冻结判据, 调用方有义务在报告里说明。
    pub fn with_cool_c(mut self, cool_c: f32) -> Self {
        self.protocol.cool_c = cool_c;
        self
    }

    pub fn with_scene(mut self, frames: u32, intensity: u32) -> Self {
        self.frames = frames;
        self.intensity = intensity;
        self
    }

    pub fn carrier(&self) -> &str {
        &self.carrier
    }

    /// 注入一个替身执行器 (只供测试用)。
    pub fn with_runner(
        mut self,
        runner: impl Fn(&Path) -> Result<RunOutcome> + Send + Sync + 'static,
    ) -> Self {
        self.runner = Some(Box::new(runner));
        self
    }

    pub fn with_binary(mut self, path: impl Into<PathBuf>) -> Self {
        self.binary = path.into();
        self
    }

    pub fn with_subcommand(mut self, sub: impl Into<String>) -> Self {
        self.subcommand = sub.into();
        self
    }

    pub fn with_serial(mut self, serial: Option<String>) -> Self {
        self.serial = serial;
        self
    }

    pub fn with_package(mut self, package: impl Into<String>) -> Self {
        self.package = package.into();
        self
    }

    pub fn protocol(&self) -> &EvalProtocol {
        &self.protocol
    }

    pub fn binary(&self) -> &Path {
        &self.binary
    }

    /// 真探活: 文件在不在、能不能执行、有没有在线设备。
    pub fn probe(&self) -> ProbeStatus {
        if !self.binary.is_file() {
            return ProbeStatus::Missing {
                looked_at: self.binary.display().to_string(),
            };
        }

        let output = match Command::new(&self.binary).arg("devices").output() {
            Ok(o) => o,
            Err(e) => {
                return ProbeStatus::BinaryUnusable {
                    detail: e.to_string(),
                }
            }
        };

        if !output.status.success() {
            return ProbeStatus::BinaryUnusable {
                detail: format!(
                    "`{} devices` 退出码 {:?}: {}",
                    self.binary.display(),
                    output.status.code(),
                    String::from_utf8_lossy(&output.stderr)
                        .lines()
                        .next()
                        .unwrap_or("")
                ),
            };
        }

        let devices = parse_device_list(&String::from_utf8_lossy(&output.stdout));
        if devices.is_empty() {
            ProbeStatus::NoDevice
        } else {
            ProbeStatus::Ready { devices }
        }
    }

    /// phonefarm 认不认我们要用的算子评测子命令。
    ///
    /// 底座在线 ≠ 能评测算子。phonefarm 目前只有 `bench`(TFLite 模型) 与
    /// `capture` 两个图形相关能力, 还没有「挂载 Compute Shader 到合成层插槽
    /// 并做 A/B 回放」的接收端。不把这件事单独探出来的话, 报告里的
    /// 「底座就绪」会被读成「真机链路已通」, 那是两回事。
    pub fn probe_subcommand(&self) -> SubcommandStatus {
        if !self.binary.is_file() {
            return SubcommandStatus::Unknown {
                detail: format!("找不到 {}", self.binary.display()),
            };
        }
        let output = match Command::new(&self.binary).arg(&self.subcommand).output() {
            Ok(o) => o,
            Err(e) => {
                return SubcommandStatus::Unknown {
                    detail: e.to_string(),
                }
            }
        };
        let merged = format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        if merged.contains("未知子命令") || merged.contains("unknown subcommand") {
            SubcommandStatus::Unsupported {
                subcommand: self.subcommand.clone(),
            }
        } else {
            SubcommandStatus::Supported
        }
    }

    /// 组一份评测请求。
    pub fn build_request(
        &self,
        candidate: &Candidate,
        track: &TrackSpec,
        shader_path: &Path,
        spirv_path: &Path,
    ) -> EvalRequest {
        self.build_request_against(candidate, track, shader_path, spirv_path, None)
    }

    /// 带在位冠军耗时的请求。见 `EvalRequest::incumbent_latency_ms`。
    pub fn build_request_against(
        &self,
        candidate: &Candidate,
        track: &TrackSpec,
        shader_path: &Path,
        spirv_path: &Path,
        incumbent_latency_ms: Option<f32>,
    ) -> EvalRequest {
        EvalRequest {
            version: 1,
            candidate_id: candidate.id.clone(),
            track: track.id.to_string(),
            serial: self.serial.clone(),
            package: self.package.clone(),
            replay_script: self.replay_script.clone(),
            shader_path: shader_path.display().to_string(),
            spirv_path: spirv_path.display().to_string(),
            budget_ms: track.budget_ms,
            quality_baseline_db: track.quality_baseline_db,
            incumbent_latency_ms,
            protocol: self.protocol,
        }
    }

    /// 下发一次真机评测并解析回包。
    ///
    /// 请求先落盘再把路径传给 phonefarm: 既避开了 argv 长度限制,
    /// 也让每一次真机下发都留下可回溯的证据。
    pub fn evaluate(&self, request: &EvalRequest, request_path: &Path) -> Result<EvalReport> {
        if let Some(dir) = request_path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(request_path, serde_json::to_string_pretty(request)?)
            .with_context(|| format!("写入评测请求失败: {}", request_path.display()))?;

        let outcome = match self.runner.as_ref() {
            Some(runner) => runner(request_path)?,
            None => {
                let output = Command::new(&self.binary)
                    .arg(&self.subcommand)
                    .arg("--request")
                    .arg(request_path)
                    .arg("--carrier")
                    .arg(&self.carrier)
                    .arg("--frames")
                    .arg(self.frames.to_string())
                    .arg("--intensity")
                    .arg(self.intensity.to_string())
                    .arg("--json")
                    .output()
                    .with_context(|| {
                        format!("调起 `{} {}` 失败", self.binary.display(), self.subcommand)
                    })?;
                RunOutcome {
                    exit_code: output.status.code(),
                    success: output.status.success(),
                    stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
                    stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
                }
            }
        };

        // phonefarm 的退出码契约 (docs/SPEC_GPU_OP.md):
        //   0 = PASS
        //   1 = **测量有效但判定失败** (SLOW / POOR_QUALITY / CRASH) —— 回包是完整报告
        //   2 = 用法/设备/测量本身出错 —— 没有可信数字
        // 把 1 当成错误会把"这个算子确实变慢了"误报成"评测坏了",
        // 演化就永远学不到负样本。只有 2 (及其它非契约码) 才是真的出错。
        if !matches!(outcome.exit_code, Some(0) | Some(1)) {
            bail!(
                "phonefarm {} 退出码 {:?}\nstderr: {}",
                self.subcommand,
                outcome.exit_code,
                outcome.stderr.lines().take(10).collect::<Vec<_>>().join("\n")
            );
        }

        parse_eval_report(&outcome.stdout).with_context(|| {
            format!(
                "phonefarm {} 退出码 {:?}, 回包无法解析\nstderr: {}",
                self.subcommand,
                outcome.exit_code,
                outcome.stderr.lines().take(10).collect::<Vec<_>>().join("\n")
            )
        })
    }
}

/// 解析 phonefarm 回传的评测报告。
///
/// 契约规定 `--json` 时 stdout 只有一个 JSON 对象, 进度一律走 stderr。
/// 但实现难免混进一行半行, 所以从后往前找第一个能解析成报告的 JSON 对象。
pub fn parse_eval_report(stdout: &str) -> Result<EvalReport> {
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        bail!("phonefarm 回包为空");
    }

    if let Ok(report) = serde_json::from_str::<EvalReport>(trimmed) {
        return Ok(validate_measured(report));
    }

    // 回包里混进了进度行。从最后一个 `{` 往前逐个试:
    // 按行扫是不够的 —— pretty-print 的 JSON 对象本身就横跨多行。
    let bytes = trimmed.as_bytes();
    let starts: Vec<usize> = (0..bytes.len()).filter(|&i| bytes[i] == b'{').collect();
    for &start in starts.iter().rev() {
        let mut stream =
            serde_json::Deserializer::from_str(&trimmed[start..]).into_iter::<EvalReport>();
        if let Some(Ok(report)) = stream.next() {
            return Ok(validate_measured(report));
        }
    }

    bail!(
        "phonefarm 回包解析失败, 不符合 eval_report 契约: {}",
        trimmed.chars().take(300).collect::<String>()
    )
}

/// 真机回包一律打上 `Phonefarm` 来源。
///
/// 来源由**接收端**判定而不是信发送端自称: 一份从真机链路回来的报告就是实测数据,
/// 一份成本模型算出来的就不是, 这个区分不能交给被解析的 JSON 自己声明。
fn validate_measured(mut report: EvalReport) -> EvalReport {
    report.source = MetricSource::Phonefarm;
    report
}

/// 从 `phonefarm devices` 的输出里抠设备序列号。
pub fn parse_device_list(stdout: &str) -> Vec<String> {
    let mut out = Vec::new();
    for line in stdout.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let lower = line.to_lowercase();
        // 表头 / 提示行不是设备。
        if lower.starts_with("list of")
            || lower.starts_with("无设备")
            || lower.starts_with("设备")
            || lower.contains("没有")
        {
            continue;
        }
        let Some(token) = line.split_whitespace().next() else {
            continue;
        };
        // 序列号至少要像个序列号。
        if token.len() < 4
            || !token
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == ':' || c == '.')
        {
            continue;
        }
        out.push(token.to_string());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::{EvalStatus, Metrics, Verdict};

    #[test]
    fn probe_reports_missing_binary_instead_of_pretending() {
        let driver = PhonefarmDriver::new().with_binary("/nonexistent/phonefarm");
        let status = driver.probe();
        assert!(matches!(status, ProbeStatus::Missing { .. }));
        assert!(!status.can_evaluate());
        assert!(status.describe().contains("找不到"));
    }

    /// 旧实现用 `which <相对路径>` 探活, 那永远失败, 于是无论如何都报「就绪」。
    /// 这条用例把那个行为钉死为不可接受。
    #[test]
    fn probe_never_reports_ready_without_a_real_device() {
        for path in ["/nonexistent/phonefarm", "../phonefarm/phonefarm"] {
            let status = PhonefarmDriver::new().with_binary(path).probe();
            if let ProbeStatus::Ready { devices } = &status {
                assert!(!devices.is_empty(), "报了就绪却没有任何设备");
            }
        }
    }

    /// 「设备在线」和「接收端已实现」是两件独立的事, 必须分别探。
    /// 混成一个布尔值会让报告里的"底座就绪"被读成"真机链路已通"。
    #[test]
    fn subcommand_probe_is_independent_of_device_probe() {
        let driver = PhonefarmDriver::new().with_binary("/nonexistent/phonefarm");
        assert!(matches!(
            driver.probe_subcommand(),
            SubcommandStatus::Unknown { .. }
        ));
        assert!(!driver.probe_subcommand().is_supported());

        // 真实的 phonefarm 若在, 必须如实报告它认不认这个子命令。
        let real = PhonefarmDriver::new();
        if real.binary().is_file() {
            let status = real.probe_subcommand();
            match &status {
                SubcommandStatus::Supported => {}
                SubcommandStatus::Unsupported { subcommand } => {
                    assert_eq!(subcommand, "gpu-op");
                    assert!(status.describe().contains("尚未实现"));
                }
                SubcommandStatus::Unknown { .. } => {}
            }
        }
    }

    #[test]
    fn subcommand_status_round_trips_as_json() {
        let s = SubcommandStatus::Unsupported {
            subcommand: "gpu-op".into(),
        };
        let json = serde_json::to_string(&s).unwrap();
        assert!(json.contains("unsupported"), "{json}");
        let back: SubcommandStatus = serde_json::from_str(&json).unwrap();
        assert_eq!(back, s);
    }

    #[test]
    fn parses_device_serials_and_skips_headers() {
        let out = "List of devices attached\nR5CT30ABCDE\tdevice\n127.0.0.1:5555 device\n\n";
        assert_eq!(
            parse_device_list(out),
            vec!["R5CT30ABCDE".to_string(), "127.0.0.1:5555".to_string()]
        );
    }

    #[test]
    fn empty_device_list_yields_nothing() {
        assert!(parse_device_list("List of devices attached\n\n").is_empty());
        assert!(parse_device_list("").is_empty());
    }

    fn sample_report_json(source_claim: &str) -> String {
        format!(
            r#"{{"candidate_id":"gen1_c1","status":"PASS",
                 "metrics":{{"operator_latency_ms":1.12,"fps_p95_ms":16.9,"power_watt":4.71,"psnr_db":38.62}},
                 "verdict":{{"is_pareto_improvement":true,"p_value":0.0031}},
                 "source":"{source_claim}"}}"#
        )
    }

    #[test]
    fn parses_a_well_formed_report() {
        let r = parse_eval_report(&sample_report_json("phonefarm")).unwrap();
        assert_eq!(r.candidate_id, "gen1_c1");
        assert_eq!(r.status, EvalStatus::Pass);
        assert!((r.metrics.psnr_db.unwrap() - 38.62).abs() < 1e-4);
        assert!((r.verdict.p_value - 0.0031).abs() < 1e-9);
    }

    /// 画质**没量到**时接收端如实回 null, 不拿默认值填补。
    ///
    /// 这是第二次踩同一个坑: `fps_p95_ms` 先前也声明成 `f32`, 直到真机上
    /// 头一次回 null 才暴露 —— 于是整批候选全被打成 `eval_error`,
    /// 明明 0.29ms / 7.28ms / 6.34W 的真实数字就躺在回包里。
    #[test]
    fn an_unmeasured_psnr_comes_back_as_null_not_as_a_parse_error() {
        let raw = r#"{"candidate_id":"gen1_llm1","status":"PASS",
            "metrics":{"operator_latency_ms":0.29027405,"fps_p95_ms":7.2842,
                       "power_watt":6.3387156,"psnr_db":null},
            "verdict":{"is_pareto_improvement":true,"p_value":0.00064},
            "source":"phonefarm"}"#;
        let r = parse_eval_report(raw).unwrap();
        assert_eq!(r.metrics.psnr_db, None);
        assert!((r.metrics.fps_p95_ms.unwrap() - 7.2842).abs() < 1e-4);
        assert!((r.metrics.operator_latency_ms - 0.29027405).abs() < 1e-6);
    }

    /// 来源由接收端判定。真机链路回来的东西就是实测数据,
    /// 哪怕回包自称是模拟值也不作数 —— 反过来也一样。
    #[test]
    fn report_source_is_decided_by_the_receiving_side() {
        let r = parse_eval_report(&sample_report_json("static_cost_model")).unwrap();
        assert_eq!(r.source, MetricSource::Phonefarm);
        assert!(r.source.is_measured());
    }

    #[test]
    fn picks_the_report_out_of_noisy_stdout() {
        let noisy = format!("等冷中 33.4C\n准备回放\n{}\n", sample_report_json("phonefarm"));
        let r = parse_eval_report(&noisy).unwrap();
        assert_eq!(r.candidate_id, "gen1_c1");
    }

    #[test]
    fn rejects_empty_or_unparseable_output() {
        assert!(parse_eval_report("").is_err());
        assert!(parse_eval_report("  \n ").is_err());
        assert!(parse_eval_report("phonefarm: 未知子命令").is_err());
        // 形状不对的 JSON 也不能蒙混过关。
        assert!(parse_eval_report(r#"{"ok":true}"#).is_err());
    }

    // ---------- 下发 → 收包 → 解析 的整条链路 ----------
    //
    // phonefarm 侧的接收端还没实现 (probe_subcommand 会如实报 Unsupported),
    // 所以这条链路用替身执行器测: 真机那一端是别人的职责,
    // 但**我们这一端**发什么、收到什么怎么解释, 必须现在就钉死。

    fn scratch(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("gol_harness_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn a_candidate() -> Candidate {
        use crate::engine::CandidateOrigin;
        Candidate::new(
            "gen2_c3",
            2,
            "sr",
            None,
            1.5,
            "#version 450\nvoid main() {}".into(),
            CandidateOrigin::Llm,
            None,
        )
    }

    #[test]
    fn a_successful_round_trip_writes_the_request_and_parses_the_report() {
        use crate::tracks::SR;
        let dir = scratch("roundtrip");
        let request_path = dir.join("gen2_c3.request.json");

        // 替身: 读走请求, 回一份合契约的报告。
        let seen = std::sync::Arc::new(std::sync::Mutex::new(None::<EvalRequest>));
        let seen_clone = seen.clone();
        let driver = PhonefarmDriver::new()
            .with_serial(Some("R5CT30ABCDE".into()))
            .with_runner(move |path: &Path| {
                let raw = std::fs::read_to_string(path)?;
                let parsed: EvalRequest = serde_json::from_str(&raw)?;
                *seen_clone.lock().unwrap() = Some(parsed);
                Ok(RunOutcome {
                    exit_code: Some(0),
                    success: true,
                    stdout: sample_report_json("phonefarm"),
                    stderr: "等冷 31.8C\n回放 60s\n".into(),
                })
            });

        let candidate = a_candidate();
        let request = driver.build_request(
            &candidate,
            &SR,
            Path::new("runs/evo/candidates/gen2_c3.comp"),
            Path::new("runs/evo/candidates/gen2_c3.spv"),
        );
        let report = driver.evaluate(&request, &request_path).unwrap();

        // 请求真的落了盘 —— 每次下发都要留可回溯的证据。
        assert!(request_path.is_file(), "评测请求没有落盘");

        // phonefarm 收到的就是我们组的那一份, 且冻结协议原样送达。
        let got = seen.lock().unwrap().clone().expect("替身没收到请求");
        assert_eq!(got.candidate_id, "gen2_c3");
        assert_eq!(got.serial.as_deref(), Some("R5CT30ABCDE"));
        assert_eq!(got.protocol.cool_c, 32.0);
        assert_eq!(got.protocol.replay_seconds, 60);
        assert!(got.protocol.alternating);
        assert_eq!(got.protocol.p_threshold, 0.01);

        assert_eq!(report.candidate_id, "gen1_c1");
        assert_eq!(report.source, MetricSource::Phonefarm);
        assert!(report.source.is_measured());

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_non_zero_exit_surfaces_phonefarm_stderr() {
        use crate::tracks::SR;
        let dir = scratch("failexit");
        let driver = PhonefarmDriver::new().with_runner(|_: &Path| {
            Ok(RunOutcome {
                exit_code: Some(2),
                success: false,
                stdout: String::new(),
                stderr: "ERROR: 设备无心跳\n等冷超时".into(),
            })
        });
        let request = driver.build_request(
            &a_candidate(),
            &SR,
            Path::new("x.comp"),
            Path::new("x.spv"),
        );
        let err = driver
            .evaluate(&request, &dir.join("r.json"))
            .unwrap_err()
            .to_string();
        assert!(err.contains("退出码 Some(2)"), "{err}");
        assert!(err.contains("设备无心跳"), "真机的失败原因被吞了: {err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 退出码 1 = 量到了、但判定没过。这是**有效负样本**, 不是评测故障。
    ///
    /// 把它当错误的后果: 演化只看得见"变快了"的候选, 看不见"变慢了"的,
    /// 相当于把 Pareto 前沿的另一半直接蒙上眼睛。
    #[test]
    fn exit_code_one_is_a_measured_rejection_not_a_harness_failure() {
        use crate::tracks::SR;
        let dir = scratch("verdictfail");
        let driver = PhonefarmDriver::new().with_runner(|_: &Path| {
            Ok(RunOutcome {
                exit_code: Some(1),
                success: false,
                stdout: r#"{"candidate_id":"gen2_llm2","status":"SLOW",
                    "metrics":{"operator_latency_ms":2.81,"fps_p95_ms":9.44,
                               "power_watt":7.02,"psnr_db":null},
                    "verdict":{"is_pareto_improvement":false,"p_value":0.0004},
                    "source":"phonefarm"}"#
                    .into(),
                stderr: "载体 refbench: 场景内帧时显著变慢\n".into(),
            })
        });
        let request =
            driver.build_request(&a_candidate(), &SR, Path::new("x.comp"), Path::new("x.spv"));
        let report = driver.evaluate(&request, &dir.join("r.json")).unwrap();
        assert_eq!(report.status, EvalStatus::Slow);
        assert!(!report.verdict.is_pareto_improvement);
        assert!((report.metrics.operator_latency_ms - 2.81).abs() < 1e-5);
        assert!(report.source.is_measured(), "负样本同样是实测数据");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 退出码 1 但回包也是垃圾 —— 这就真是坏了, 而且要能看见 stderr。
    #[test]
    fn exit_code_one_with_an_unparseable_body_still_surfaces_stderr() {
        use crate::tracks::SR;
        let dir = scratch("verdictgarbage");
        let driver = PhonefarmDriver::new().with_runner(|_: &Path| {
            Ok(RunOutcome {
                exit_code: Some(1),
                success: false,
                stdout: "判定失败".into(),
                stderr: "ERROR: runner 段错误".into(),
            })
        });
        let request =
            driver.build_request(&a_candidate(), &SR, Path::new("x.comp"), Path::new("x.spv"));
        let err = driver
            .evaluate(&request, &dir.join("r.json"))
            .unwrap_err()
            .to_string();
        assert!(err.contains("退出码 Some(1)"), "{err}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 退出码 0 但回包是垃圾 —— 不能当成成功。
    #[test]
    fn a_zero_exit_with_an_unparseable_body_is_still_an_error() {
        use crate::tracks::SR;
        let dir = scratch("garbage");
        let driver = PhonefarmDriver::new().with_runner(|_: &Path| {
            Ok(RunOutcome {
                exit_code: Some(0),
                success: true,
                stdout: "一切正常".into(),
                stderr: String::new(),
            })
        });
        let request = driver.build_request(
            &a_candidate(),
            &SR,
            Path::new("x.comp"),
            Path::new("x.spv"),
        );
        assert!(driver.evaluate(&request, &dir.join("r.json")).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn eval_request_freezes_the_anti_noise_protocol() {
        let p = EvalProtocol::default();
        assert_eq!(p.cool_c, 32.0, "冷机门槛必须是 <32C");
        assert_eq!(p.replay_seconds, 60, "确定性回放必须是 60s");
        assert!(p.alternating, "必须 A/B/A/B 严格交替");
        assert_eq!(p.p_threshold, 0.01, "显著性门槛必须是 p < 0.01");
    }

    #[test]
    fn build_request_carries_the_contract() {
        use crate::engine::CandidateOrigin;
        use crate::tracks::SR;

        let cand = Candidate::new(
            "gen2_c3",
            2,
            "sr",
            Some("gen1_c1".into()),
            1.5,
            "#version 450\nvoid main() {}".into(),
            CandidateOrigin::Llm,
            None,
        );
        let driver = PhonefarmDriver::new().with_serial(Some("R5CT30ABCDE".into()));
        let req = driver.build_request(
            &cand,
            &SR,
            Path::new("runs/evo/gen2_c3.comp"),
            Path::new("runs/evo/gen2_c3.spv"),
        );

        assert_eq!(req.candidate_id, "gen2_c3");
        assert_eq!(req.track, "sr");
        assert_eq!(req.serial.as_deref(), Some("R5CT30ABCDE"));
        assert_eq!(req.budget_ms, 1.5);
        assert!((req.quality_baseline_db - 38.666).abs() < 1e-4);
        assert!(req.spirv_path.ends_with(".spv"));

        // 请求必须能原样序列化成契约 JSON。
        let json: serde_json::Value = serde_json::to_value(&req).unwrap();
        for key in [
            "version",
            "candidate_id",
            "track",
            "package",
            "replay_script",
            "shader_path",
            "spirv_path",
            "budget_ms",
            "quality_baseline_db",
            "protocol",
        ] {
            assert!(json.get(key).is_some(), "请求缺字段 {key}");
        }
    }

    #[test]
    fn report_round_trips_through_the_contract_shape() {
        let r = EvalReport {
            candidate_id: "x".into(),
            status: EvalStatus::Slow,
            metrics: Metrics {
                operator_latency_ms: 2.2,
                fps_p95_ms: Some(17.1),
                power_watt: 5.0,
                psnr_db: Some(38.0),
            },
            verdict: Verdict {
                is_pareto_improvement: false,
                p_value: 0.4,
            },
            source: MetricSource::Phonefarm,
        };
        let json = serde_json::to_string(&r).unwrap();
        assert!(json.contains("\"SLOW\""), "status 必须序列化成契约枚举值");
        let back: EvalReport = serde_json::from_str(&json).unwrap();
        assert_eq!(back.status, EvalStatus::Slow);
    }
}
