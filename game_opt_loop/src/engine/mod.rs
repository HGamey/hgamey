//! 进化中枢: 候选算子的数据模型、防爆门禁、变异、评测与 Pareto 归档。

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub mod analysis;
pub mod archive;
pub mod cost_model;
pub mod evolution;
pub mod generator;
pub mod mutate;
pub mod pareto;
pub mod validator;

pub use archive::{ArchiveEntry, ParetoArchive};
pub use validator::{GateReport, GateStage, Validator, Violation, ViolationCode};

/// 候选算子从哪来的。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateOrigin {
    /// 赛道基线模板。
    Baseline,
    /// 大模型变异产物。
    Llm,
    /// 本地确定性变异器产物 (LLM 不可用时的兜底, 也是 dry-run 的来源)。
    LocalMutation,
}

/// 一个待评测的算子。字段与 `contracts/candidate.schema.json` 对齐。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Candidate {
    pub id: String,
    pub generation: u32,
    pub track: String,
    pub parent_id: Option<String>,
    pub budget_ms: f32,
    pub shader_source: String,
    /// 规范化源码的 sha256 前 16 位, 用于跨代去重 (同构算子不重复上真机)。
    pub fingerprint: String,
    pub origin: CandidateOrigin,
    /// 一行人类可读的形态描述。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub notes: Option<String>,
}

impl Candidate {
    // 参数多是因为它就是契约字段的逐项映射; 拆成 builder 只会让调用点更啰嗦。
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: impl Into<String>,
        generation: u32,
        track: &str,
        parent_id: Option<String>,
        budget_ms: f32,
        shader_source: String,
        origin: CandidateOrigin,
        notes: Option<String>,
    ) -> Self {
        let fingerprint = fingerprint(&shader_source);
        Self {
            id: id.into(),
            generation,
            track: track.to_string(),
            parent_id,
            budget_ms,
            shader_source,
            fingerprint,
            origin,
            notes,
        }
    }
}

/// 源码指纹。先折叠空白与注释再哈希: 只改了排版或注释的算子是同一个算子,
/// 不值得再占一次真机 60s 回放。
pub fn fingerprint(source: &str) -> String {
    let normalized: String = source
        .lines()
        .map(|l| {
            let l = match l.find("//") {
                Some(i) => &l[..i],
                None => l,
            };
            l.split_whitespace().collect::<Vec<_>>().join(" ")
        })
        .filter(|l| !l.is_empty())
        .collect::<Vec<_>>()
        .join("\n");

    let digest = Sha256::digest(normalized.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// 真机 (或成本模型) 给出的多维指标。字段与 `contracts/eval_report.schema.json` 对齐。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Metrics {
    /// 算子自身的 GPU compute pass 耗时。硬预算就卡这个数。
    pub operator_latency_ms: f32,
    /// 整帧时间 p95 (ms)。
    ///
    /// `None` = **本次没有量到**, 不是 0。headless 算子标尺量的是算子自身的
    /// GPU 耗时, 帧时 p95 需要真实渲染上下文才有意义; 真机 2026-09-22 回的
    /// 第一份报告这一项就是 null, 而当时它是 `f32`, 上游直接解析失败。
    /// 契约必须容得下「未测量」这个状态。
    pub fps_p95_ms: Option<f32>,
    /// 整机功耗 (W)。
    ///
    /// `None` = **本次没有量到**, 不是 0。接收端在非放电态下这一项回 null ——
    /// 充电时两条轨量到的都不是整机功耗 (电池轨是充放相抵的余量, USB 轨含着
    /// 灌进电池的那一份), 与其回一个看着正常的假数字, 不如如实留空。
    ///
    /// 这是第三次踩同一个坑 (`fps_p95_ms` / `psnr_db` 之后)。声明成 `f32` 的话,
    /// 一份充电态回包会让**整份报告**解析失败, 而不是只丢掉功耗这一项。
    pub power_watt: Option<f32>,
    /// 画质 (dB)。必须 >= 赛道基线。
    ///
    /// `None` = **本次没有量到**, 不是 0。refbench 载体只跑真实渲染场景,
    /// 画质由 headless 载体负责 (refbench 的边界不允许它自带测量逻辑)。
    ///
    /// 这是第二次踩同一个坑: `fps_p95_ms` 已经因为同样的原因改成 Option 了,
    /// 当时只改了那一个字段, 没有把"指标可能量不到"这件事推广到整个 Metrics。
    pub psnr_db: Option<f32>,
}

/// 指标从哪来 —— 这一条**绝不能**丢。
///
/// 成本模型产出的数字是**静态估计**, 不是测量; 它只能用于本地排序与冒烟,
/// 不能拿来下任何性能结论。真机数据才带 p 值。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MetricSource {
    /// phonefarm 真机物理采样 + 冻结规则 t 检验。
    Phonefarm,
    /// 本地静态成本模型的估计值 (dry-run 专用, 非测量)。
    StaticCostModel,
}

impl MetricSource {
    pub fn is_measured(&self) -> bool {
        matches!(self, MetricSource::Phonefarm)
    }
}

/// 评测结论。取值与 `contracts/eval_report.schema.json` 的 status 枚举一致。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EvalStatus {
    Pass,
    /// 超出耗时硬预算。
    Slow,
    /// PSNR 低于基线 —— 画质一票否决。
    PoorQuality,
    Crash,
    Reverted,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Verdict {
    pub is_pareto_improvement: bool,
    /// 两样本 t 检验的 p 值。成本模型不产出 p 值, 记 NaN 之外的哨兵 1.0。
    pub p_value: f64,
}

/// 一份完整评测报告。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalReport {
    pub candidate_id: String,
    pub status: EvalStatus,
    pub metrics: Metrics,
    pub verdict: Verdict,
    /// 数据来源。序列化后依然带着, 归档里一眼能分辨真机数与估计值。
    pub source: MetricSource,
    /// `metrics.psnr_db` 是对着哪张 (哪组) 参考图量出来的。
    ///
    /// 绝对 dB 不可跨参考图比较: 同一个算子在 runner 内置程序化图案上是 42.8 dB,
    /// 换成真机截帧只剩 31.3 dB —— 真实画面的高频细节远多于合成图案。
    /// 档案里存了一堆 dB 而不记它们各自的真值出处, 那些数字之间就没法比。
    ///
    /// None = 接收端没报 (旧版 phonefarm, 或成本模型估计值)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quality_reference: Option<QualityReference>,
    /// `metrics.power_watt` 是在什么供电状态下量的。
    ///
    /// None = 接收端没报 (旧版 phonefarm, 或成本模型估计值)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub power_source: Option<PowerSource>,
}

/// 供电状态, 由 phonefarm 随报告回传。字段与
/// `contracts/eval_report.schema.json` 的 `power_source` 一致。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PowerSource {
    /// `usb` | `battery`
    pub rail: String,
    /// `/sys/class/power_supply/battery/status` 原值
    pub battery_status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub battery_current_now_ua: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub battery_capacity_pct: Option<i64>,
    /// 整机是不是真由电池供电。只有 true 时 `power_watt` 才是整机功耗。
    pub on_battery: bool,
    /// 这一轮有没有用 root 停充, 停的是哪个节点。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub charging_suspended_via: Option<String>,
}

/// 画质真值的出处, 由 phonefarm 随报告回传。字段与
/// `contracts/eval_report.schema.json` 的 `quality_reference` 一致。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QualityReference {
    /// `real_frames` = 真机截帧; `procedural` = runner 内置程序化图案。
    pub kind: String,
    /// 真值的本地来源路径 (单张图或整个目录)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    /// 参与平均的帧数。
    pub frames: u32,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub frame_files: Vec<String>,
    /// 候选臂逐帧 PSNR, 与 `frame_files` **逐位对齐**。
    ///
    /// `None` = 那一张没量到。必须是 `Option` 而不是 `f64`: 接收端用 null
    /// 占位来保持下标对齐 (塌缩会让"第 3 张最差"指到另一张图上),
    /// 声明成 `f64` 的话一张量不到就让**整份报告**解析失败 ——
    /// 那是 §6.5 那个坑的第三次重演。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub candidate_psnr_db: Vec<Option<f64>>,
    /// 基线臂逐帧 PSNR, 与 `frame_files` 逐位对齐。
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub baseline_psnr_db: Vec<Option<f64>>,
}

impl QualityReference {
    /// 画质真值是不是真实游戏画面。
    pub fn is_real_frames(&self) -> bool {
        self.kind == "real_frames"
    }

    /// 画质真值是不是 runner 内置的程序化图案。
    ///
    /// 注意它**不是** `!is_real_frames()`: `kind` 是接收端传过来的字符串,
    /// 写错了或者将来多一种真值, 两个判断都该是 false, 而不是把一个不认识的
    /// 来源当成程序化图案报出去。
    pub fn is_procedural(&self) -> bool {
        self.kind == "procedural"
    }

    /// 一句话的来源描述, 给报告与终端输出用。
    pub fn describe(&self) -> String {
        if self.is_real_frames() {
            format!(
                "真机参考帧 x{} ({})",
                self.frames,
                self.source.as_deref().unwrap_or("来源未标注")
            )
        } else if self.is_procedural() {
            "runner 内置程序化图案 (非真实游戏帧)".to_string()
        } else {
            format!("未知真值类型 {:?} (接收端报了本仓库不认识的来源)", self.kind)
        }
    }

    /// 本场基线臂的画质 (逐帧均值)。`None` = 接收端没报。
    ///
    /// 这是画质地板该用的那个数: A/B 本来就同场同参考图跑, 用 A 臂的实测值
    /// 当地板是自洽的, 且换参考图不会让判据错位。赛道契约里的常数是在**另一张**
    /// 参考图上得到的, 只能兜底。
    pub fn baseline_floor_db(&self) -> Option<f32> {
        let v: Vec<f64> = self.baseline_psnr_db.iter().flatten().copied().collect();
        if v.is_empty() {
            return None;
        }
        Some((v.iter().sum::<f64>() / v.len() as f64) as f32)
    }
}

impl EvalReport {
    /// 这一份的功耗能不能参与支配排序 —— 能就给值, 不能就说明为什么。
    ///
    /// 只有**确实由电池供电**时 `power_watt` 才是整机功耗。充电态下电池轨量到的是
    /// 充放相抵的余量、USB 轨含着灌进电池的那一份, 两个读数都落在可信区间内,
    /// 光看数字分辨不出来。
    ///
    /// 危害不止于"误差大一点": 充电电流随电量上升自己衰减 (CC→CV), 是一条**单调漂移**。
    /// 把这样一个量放进支配排序, **测量顺序**就变成了选择压力 —— 先跑的候选系统性地
    /// 占便宜, 而每一条记录单看都是"实测值"。M1 那轮 6.26W→5.94W 就是这么来的。
    ///
    /// 所以判据是 `on_battery == true`, 不是"有没有数字"。接收端没报 `power_source`
    /// 时也不参与: 旧版接收端不记供电状态, 分辨不出它是哪种情况, 默认排除是安全的一侧
    /// (少比一维只会让前沿更宽, 不会造出假的改进)。
    pub fn comparable_power_watt(&self) -> Result<f32, &'static str> {
        let Some(w) = self.metrics.power_watt else {
            return Err("本次未测量");
        };
        if !w.is_finite() {
            return Err("本次未测量");
        }
        match self.power_source.as_ref() {
            None => Err("接收端未报供电状态"),
            Some(p) if !p.on_battery => Err("非放电态 (充电中, 瓦数含充电分量且随电量漂移)"),
            Some(_) => Ok(w),
        }
    }

    /// 功耗被排除在支配排序之外的原因; 参与比较时为 None。
    pub fn power_exclusion_reason(&self) -> Option<&'static str> {
        self.comparable_power_watt().err()
    }

    /// 这份报告该用哪个画质地板。
    ///
    /// 优先用**本场基线臂的实测值** (随报告带回的 `quality_reference`),
    /// 赛道契约里的 `quality_baseline_db` 只作兜底。
    ///
    /// 为什么必须这样: 绝对 dB 不可跨参考图比较。赛道常数 38.666 dB 是在
    /// runner 内置程序化图案上得到的; 换成真机截帧, 同一个算子只有 31.3 dB。
    /// 拿前者去卡后者, **每一个**候选都会被判成画质不合格 —— 前沿清空,
    /// 亲本池清空, 演化退化成反复生成基线。接收端 (SPEC §4.1) 早就改用
    /// 本场基线了, 上游这一侧原本还在用常数, 等于同一条判据两把尺子。
    ///
    /// 判据本身没有放松: 规则仍然是「候选画质不得低于基线」, 只是基线值
    /// 改为同场实测。
    pub fn quality_floor_db(&self, track_baseline_db: f32) -> f32 {
        self.quality_reference
            .as_ref()
            .and_then(|q| q.baseline_floor_db())
            .unwrap_or(track_baseline_db)
    }

    /// 是否有资格进入 Pareto 前沿。
    ///
    /// 三条硬约束同时成立才算数:
    /// - 耗时在预算内 (§3 赛道硬约束)
    /// - PSNR >= 基线 (§2 红线 3: 画质是约束, 不是目标)
    /// - 真机数据还要显著 (p < 0.01; §2 红线 4: 防硬件噪声欺骗)
    pub fn is_admissible(&self, budget_ms: f32, quality_baseline_db: f32) -> bool {
        if self.status != EvalStatus::Pass {
            return false;
        }
        if self.metrics.operator_latency_ms > budget_ms {
            return false;
        }
        // 量不到画质时不否决 —— "没量" 和 "不合格" 是两回事。
        // 该载体的画质由另一条载体负责, 在这里当成不合格会把好算子全毙掉。
        if let Some(q) = self.metrics.psnr_db {
            if q < self.quality_floor_db(quality_baseline_db) {
                return false;
            }
        }
        if self.source.is_measured() && self.verdict.p_value >= 0.01 {
            return false;
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_ignores_comments_and_whitespace() {
        let a = "#version 450\nvoid main() { int x = 1; }";
        let b = "#version 450\n\n  void  main()   {  int x = 1; }  // 换了注释\n";
        assert_eq!(fingerprint(a), fingerprint(b));
    }

    #[test]
    fn fingerprint_separates_real_changes() {
        let a = "#version 450\nvoid main() { int x = 1; }";
        let b = "#version 450\nvoid main() { int x = 2; }";
        assert_ne!(fingerprint(a), fingerprint(b));
    }

    fn report(status: EvalStatus, latency: f32, psnr: f32, p: f64, src: MetricSource) -> EvalReport {
        EvalReport {
            candidate_id: "t".into(),
            status,
            metrics: Metrics {
                operator_latency_ms: latency,
                fps_p95_ms: Some(16.0),
                power_watt: Some(4.8),
                psnr_db: Some(psnr),
            },
            verdict: Verdict {
                is_pareto_improvement: false,
                p_value: p,
            },
            source: src,
            quality_reference: None,
            power_source: None,
        }
    }

    fn power_source(on_battery: bool, status: &str) -> PowerSource {
        PowerSource {
            rail: if on_battery { "battery".into() } else { "usb".into() },
            battery_status: status.into(),
            battery_current_now_ua: None,
            battery_capacity_pct: None,
            on_battery,
            charging_suspended_via: None,
        }
    }

    /// 只有确实由电池供电时功耗才进支配排序。
    ///
    /// 判据是 `on_battery`, 不是"有没有数字": 充电态下两条轨的读数都落在
    /// 可信区间内, 光看数字分辨不出来, 而充电电流随电量单调漂移会让
    /// **测量顺序**变成选择压力。
    #[test]
    fn power_only_counts_when_the_device_is_actually_on_battery() {
        let mut r = report(EvalStatus::Pass, 1.0, 38.0, 0.001, MetricSource::Phonefarm);

        // 放电态: 参与。
        r.power_source = Some(power_source(true, "Discharging"));
        assert_eq!(r.comparable_power_watt(), Ok(4.8));
        assert_eq!(r.power_exclusion_reason(), None);

        // 充电态: 不参与, 且说得出为什么。数字本身照样留在 metrics 里。
        r.power_source = Some(power_source(false, "Charging"));
        assert!(r.comparable_power_watt().is_err());
        assert!(r.power_exclusion_reason().unwrap().contains("非放电态"));
        assert_eq!(r.metrics.power_watt, Some(4.8), "排除的是比较资格, 不是数字本身");

        // 接收端没报供电状态 (旧版): 默认排除 —— 分辨不出是哪种情况时,
        // 少比一维只会让前沿更宽, 不会造出假改进。
        r.power_source = None;
        assert!(r.power_exclusion_reason().unwrap().contains("未报供电状态"));

        // 压根没量到。
        r.power_source = Some(power_source(true, "Discharging"));
        r.metrics.power_watt = None;
        assert!(r.power_exclusion_reason().unwrap().contains("未测量"));
    }

    /// 非放电态的回包里 `power_watt` 是 null —— 整份报告必须照样解析得动。
    ///
    /// 声明成 `f32` 的话一份充电态回包会让整代候选打成 eval_error,
    /// 那是 §6.5 那个坑的第四次重演。
    #[test]
    fn a_charging_report_with_null_power_still_parses() {
        let raw = r#"{"candidate_id":"c","status":"PASS",
            "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":7.3,"power_watt":null,"psnr_db":31.3},
            "verdict":{"is_pareto_improvement":false,"p_value":0.004},"source":"phonefarm",
            "power_source":{"rail":"usb","battery_status":"Charging","on_battery":false,
                            "battery_capacity_pct":84}}"#;
        let r: EvalReport = serde_json::from_str(raw).unwrap();
        assert_eq!(r.metrics.power_watt, None);
        assert!(!r.power_source.as_ref().unwrap().on_battery);
        assert!(r.power_exclusion_reason().is_some());
        // 其余指标不受连坐。
        assert_eq!(r.metrics.psnr_db, Some(31.3));
        assert_eq!(r.metrics.fps_p95_ms, Some(7.3));
    }

    /// 画质地板优先用本场基线臂实测值, 赛道常数只兜底。
    ///
    /// 这条不成立的话, 换上真机参考帧之后**每一个**候选都会被判画质不合格:
    /// 赛道常数 38.666 dB 是在程序化图案上量的, 真机截帧上同一个算子只有 31 dB。
    /// 前沿清空 → 亲本池清空 → 演化退化成反复生成基线。
    #[test]
    fn the_quality_floor_comes_from_this_run_s_baseline_arm() {
        let real = QualityReference {
            kind: "real_frames".into(),
            source: Some("tasks/genshin_refframes".into()),
            frames: 2,
            frame_files: vec!["a.png".into(), "b.png".into()],
            candidate_psnr_db: vec![Some(31.2), Some(31.4)],
            baseline_psnr_db: vec![Some(29.0), Some(29.4)],
        };
        let mut r = report(EvalStatus::Pass, 1.0, 31.3, 0.001, MetricSource::Phonefarm);
        r.quality_reference = Some(real);

        assert!((r.quality_floor_db(38.666) - 29.2).abs() < 1e-4, "该用本场基线 29.2");
        assert!(
            r.is_admissible(1.5, 38.666),
            "31.3 dB 比本场基线 29.2 好 2.1 dB, 不该被程序化图案的 38.666 卡掉"
        );

        // 判据没有放松: 低于本场基线照样出局。
        let mut worse = r.clone();
        worse.metrics.psnr_db = Some(28.0);
        assert!(!worse.is_admissible(1.5, 38.666));

        // 接收端没报基线画质时退回赛道常数 (原语义)。
        let bare = report(EvalStatus::Pass, 1.0, 31.3, 0.001, MetricSource::Phonefarm);
        assert!((bare.quality_floor_db(38.666) - 38.666).abs() < 1e-4);
        assert!(!bare.is_admissible(1.5, 38.666));
    }

    /// 逐帧 dB 允许 null 占位 —— 一张量不到不该让**整份报告**解析失败。
    #[test]
    fn a_report_with_a_missing_per_frame_reading_still_parses() {
        let raw = r#"{"candidate_id":"c","status":"PASS",
            "metrics":{"operator_latency_ms":0.5,"fps_p95_ms":7.3,"power_watt":6.5,"psnr_db":31.3},
            "verdict":{"is_pareto_improvement":false,"p_value":0.004},"source":"phonefarm",
            "quality_reference":{"kind":"real_frames","source":"d","frames":3,
              "frame_files":["a.png","b.png","c.png"],
              "candidate_psnr_db":[31.0,null,31.6],
              "baseline_psnr_db":[29.0,null,29.4]}}"#;
        let r: EvalReport = serde_json::from_str(raw).unwrap();
        let q = r.quality_reference.as_ref().unwrap();
        // 位置留着, 下标仍与 frame_files 对得上。
        assert_eq!(q.candidate_psnr_db.len(), 3);
        assert_eq!(q.candidate_psnr_db[1], None);
        // 地板只用量到的那些。
        assert!((q.baseline_floor_db().unwrap() - 29.2).abs() < 1e-4);
    }

    #[test]
    fn admissibility_enforces_the_latency_budget() {
        let r = report(EvalStatus::Pass, 1.9, 38.0, 0.001, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
    }

    #[test]
    fn admissibility_enforces_the_quality_floor() {
        let r = report(EvalStatus::Pass, 0.9, 37.0, 0.001, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
    }

    /// 真机数据不显著 = 被硬件噪声忽悠, 不算数。
    #[test]
    fn admissibility_requires_significance_for_measured_data() {
        let r = report(EvalStatus::Pass, 0.9, 38.5, 0.20, MetricSource::Phonefarm);
        assert!(!r.is_admissible(1.5, 37.286));
        let ok = report(EvalStatus::Pass, 0.9, 38.5, 0.004, MetricSource::Phonefarm);
        assert!(ok.is_admissible(1.5, 37.286));
    }

    /// 成本模型的估计值没有 p 值可言, 不能拿显著性去卡它 (也永远不该被当成真机结论)。
    #[test]
    fn simulated_reports_are_not_judged_on_significance() {
        let r = report(EvalStatus::Pass, 0.9, 38.5, 1.0, MetricSource::StaticCostModel);
        assert!(r.is_admissible(1.5, 37.286));
        assert!(!r.source.is_measured());
    }

    #[test]
    fn non_pass_status_is_never_admissible() {
        for s in [EvalStatus::Slow, EvalStatus::PoorQuality, EvalStatus::Crash, EvalStatus::Reverted] {
            let r = report(s, 0.5, 40.0, 0.0001, MetricSource::Phonefarm);
            assert!(!r.is_admissible(1.5, 37.286), "{s:?} 不该被接纳");
        }
    }
}
