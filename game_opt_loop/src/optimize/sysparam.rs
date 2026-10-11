//! ③ 系统参数层: 白名单 + 出主意。
//!
//! 这一层的算法部分从 phonefarm `loop_v1/auto/{whitelist,llm}.py` 搬过来,
//! 改写成 Rust。搬的是**出主意**那一半 —— 准改什么、这一轮改成什么;
//! 留在 phonefarm 的是**应用/测量/判定/还原**。分界线很清楚:
//! 本文件里没有任何一处在判断"改完之后好不好"。
//!
//! 三条各自独立的判断 (与 Python 版同构):
//!
//! | 谁 | 决定什么 | 不决定什么 |
//! |---|---|---|
//! | [`build_whitelist`] | 准改哪些参数、准改成哪些值 | 不决定改成什么 |
//! | [`Proposer`] | 这一轮改成什么 | 不决定好不好 |
//! | phonefarm 的冻结判定 | 好不好 | 不决定改什么 |

use super::eval::PlanEntry;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// 温控保护关键词。命中即拒, **不看探测结果, 不看大模型怎么说**。
///
/// 关热保护、抬温控阈值能立刻换来漂亮数字, 但那是拿硬件安全换指标, 不是优化。
/// 主机端 (这里) 拦一道, 设备端再拦一道 —— 故意冗余, 设备端是最后一道。
pub const DENY_KEYWORDS: &[&str] = &[
    "thermal",
    "trip_point",
    "cooling",
    "fan",
    "tsens",
    "bcl",
    "throttl",
];

/// 命中温控保护关键词没有。
pub fn denied(s: &str) -> bool {
    let low = s.to_ascii_lowercase();
    DENY_KEYWORDS.iter().any(|k| low.contains(k))
}

/// 一个准改的参数: 准写哪些值, 现在是什么。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ParamSpec {
    /// "sysfs" | "setting"
    pub kind: String,
    /// sysfs 绝对路径, 或 `<ns>:<key>`。
    pub path: String,
    /// 允许的取值表。出主意的一方只能从里面挑。
    pub values: Vec<String>,
    /// 探测时的原值。
    pub current: String,
    /// 跨项约束分组 (同组的 min/max 要成对合法)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub group: Option<String>,
    /// 这一项是凭什么进来的 —— 证据里直接看得到, 不必回头翻 probe 原文。
    pub effect: String,
}

/// 白名单。
pub type Whitelist = BTreeMap<String, ParamSpec>;

/// 探测结果里"写了真生效"的结论。
const EFFECT_OK: &[&str] = &["live"];
/// 当前值已经是探测值, 做不了差异写测试 —— 放行, 但在 spec 里如实标注。
const EFFECT_SOFT: &[&str] = &["skipped_same_value"];

fn probe_get<'a>(p: &'a BTreeMap<String, String>, key: &str) -> Option<&'a str> {
    p.get(key).map(String::as_str).filter(|s| !s.is_empty())
}

/// 可写 + (生效测试通过 或 这台机器上没有可供差异测试的第二个值)。
///
/// 第三关 (写了真生效) 是关键: 很多 sysfs 节点接受 `write()` 但随即被驱动或
/// 热策略夹回原值, 只看 write 的返回码会得出"能改"的假结论。
fn usable(p: &BTreeMap<String, String>, key: &str) -> bool {
    if probe_get(p, &format!("{key}.writable")) != Some("yes") {
        return false;
    }
    match probe_get(p, &format!("{key}.effect")) {
        None => true,
        Some(e) => EFFECT_OK.contains(&e) || EFFECT_SOFT.contains(&e),
    }
}

fn effect_of(p: &BTreeMap<String, String>, key: &str) -> String {
    probe_get(p, &format!("{key}.effect"))
        .unwrap_or("untested")
        .to_string()
}

/// 把一串数字文本解析成去重排序的整数表。
fn ints(s: &str) -> Vec<i64> {
    let mut v: Vec<i64> = s
        .replace(',', " ")
        .split_whitespace()
        .filter_map(|t| t.parse().ok())
        .collect();
    v.sort_unstable();
    v.dedup();
    v
}

/// `key=value` 探测文本 → map。`#` 开头是注释。
pub fn parse_probe(text: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some((k, v)) = line.split_once('=') {
            out.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    out
}

struct Adder<'a> {
    wl: Whitelist,
    probe: &'a BTreeMap<String, String>,
}

impl<'a> Adder<'a> {
    /// 收一个参数进白名单。
    ///
    /// 生效结论 (`effect`) 不单独传: sysfs 类一律按参数 id 去探测结果里查
    /// (两者本来就同名), settings 类没有差异写测试, 靠每轮的快照复核实际生效。
    /// 多一个参数就多一处能传错的地方。
    fn add(
        &mut self,
        pid: &str,
        kind: &str,
        path: &str,
        values: Vec<String>,
        current: &str,
        group: Option<&str>,
    ) {
        if denied(path) || denied(pid) {
            return;
        }
        let values: Vec<String> = values.into_iter().filter(|v| !v.is_empty()).collect();
        // 只有一个合法取值 = 没得改, 放进白名单只会让出主意的一方浪费一格。
        if values.len() < 2 {
            return;
        }
        self.wl.insert(
            pid.to_string(),
            ParamSpec {
                kind: kind.to_string(),
                path: path.to_string(),
                values,
                current: current.to_string(),
                group: group.map(str::to_string),
                effect: if kind == "sysfs" {
                    effect_of(self.probe, pid)
                } else {
                    "n/a(setting)".to_string()
                },
            },
        );
    }
}

/// 探测结果 → 白名单。
///
/// 白名单不是写死的常量表, 而是真机探测结果的**函数**: 探不到的、写了被内核
/// 退回的, 一律不进 —— 宁可少改, 不可假改。
pub fn build_whitelist(probe: &BTreeMap<String, String>) -> Whitelist {
    let mut a = Adder {
        wl: Whitelist::new(),
        probe,
    };

    // ── CPU 各簇 ──
    for pol in probe_get(probe, "cpu.policies")
        .unwrap_or("")
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let k = format!("cpu.policy{pol}");
        let base = format!("/sys/devices/system/cpu/cpufreq/policy{pol}");
        let freqs = ints(probe_get(probe, &format!("{k}.avail_freqs")).unwrap_or(""));
        if !freqs.is_empty() {
            let strs: Vec<String> = freqs.iter().map(|v| v.to_string()).collect();
            for which in ["scaling_min_freq", "scaling_max_freq"] {
                let key = format!("{k}.{which}");
                if usable(probe, &key) {
                    a.add(
                        &key,
                        "sysfs",
                        &format!("{base}/{which}"),
                        strs.clone(),
                        probe_get(probe, &format!("{key}.cur")).unwrap_or(""),
                        Some(&format!("{k}.freq")),
                    );
                }
            }
        }
        let govs: Vec<String> = probe_get(probe, &format!("{k}.avail_governors"))
            .unwrap_or("")
            .split_whitespace()
            .map(str::to_string)
            .collect();
        let key = format!("{k}.scaling_governor");
        if !govs.is_empty() && usable(probe, &key) {
            a.add(
                &key,
                "sysfs",
                &format!("{base}/scaling_governor"),
                govs,
                probe_get(probe, &format!("{key}.cur")).unwrap_or(""),
                None,
            );
        }
    }

    // ── GPU ──
    const KGSL: &str = "/sys/class/kgsl/kgsl-3d0";
    if let Some(n) = probe_get(probe, "gpu.num_pwrlevels").and_then(|s| s.parse::<i64>().ok()) {
        if n > 1 {
            // kgsl 语义: 0 = 最快档, 数字越大越慢。min_pwrlevel 是"最慢准到哪",
            // max_pwrlevel 是"最快准到哪", 因此恒有 max_pwrlevel <= min_pwrlevel。
            let lv: Vec<String> = (0..n).map(|i| i.to_string()).collect();
            for which in ["min_pwrlevel", "max_pwrlevel"] {
                let key = format!("gpu.{which}");
                if usable(probe, &key) {
                    a.add(
                        &key,
                        "sysfs",
                        &format!("{KGSL}/{which}"),
                        lv.clone(),
                        probe_get(probe, &format!("{key}.cur")).unwrap_or(""),
                        Some("gpu.pwrlevel"),
                    );
                }
            }
        }
    }
    let gfreqs = ints(probe_get(probe, "gpu.devfreq.avail_freqs").unwrap_or(""));
    if !gfreqs.is_empty() {
        let strs: Vec<String> = gfreqs.iter().map(|v| v.to_string()).collect();
        for which in ["min_freq", "max_freq"] {
            let key = format!("gpu.devfreq.{which}");
            if usable(probe, &key) {
                a.add(
                    &key,
                    "sysfs",
                    &format!("{KGSL}/devfreq/{which}"),
                    strs.clone(),
                    probe_get(probe, &format!("{key}.cur")).unwrap_or(""),
                    Some("gpu.devfreq"),
                );
            }
        }
    }
    let ggov: Vec<String> = probe_get(probe, "gpu.devfreq.avail_governors")
        .unwrap_or("")
        .split_whitespace()
        .map(str::to_string)
        .collect();
    if !ggov.is_empty() && usable(probe, "gpu.devfreq.governor") {
        a.add(
            "gpu.devfreq.governor",
            "sysfs",
            &format!("{KGSL}/devfreq/governor"),
            ggov,
            probe_get(probe, "gpu.devfreq.governor.cur").unwrap_or(""),
            None,
        );
    }

    // ── 总线 DDR / LLCC 下限 (手工验证过的那一条就在这里) ──
    for n in ["DDR", "LLCC"] {
        let key = format!("bus.{n}.boost_freq");
        if !usable(probe, &key) {
            continue;
        }
        let mut vals = ints(probe_get(probe, &format!("bus.{n}.avail_freqs")).unwrap_or(""));
        if vals.is_empty() {
            let lo = probe_get(probe, &format!("bus.{n}.hw_min_freq")).unwrap_or("");
            let hi = probe_get(probe, &format!("bus.{n}.hw_max_freq")).unwrap_or("");
            vals = ints(&format!("{lo} {hi}"));
        }
        a.add(
            &key,
            "sysfs",
            &format!("/sys/devices/system/cpu/bus_dcvs/{n}/boost_freq"),
            vals.iter().map(|v| v.to_string()).collect(),
            probe_get(probe, &format!("{key}.cur")).unwrap_or(""),
            None,
        );
    }

    // ── 刷新率 ──
    let mut modes: Vec<i64> = probe_get(probe, "display.modes")
        .unwrap_or("")
        .split(',')
        .filter_map(|x| x.split_whitespace().next())
        .filter_map(|x| x.parse::<f64>().ok())
        .map(|f| f as i64)
        .collect();
    modes.sort_unstable();
    modes.dedup();
    if !modes.is_empty() {
        let strs: Vec<String> = modes.iter().map(|v| v.to_string()).collect();
        for (pid, skey) in [
            ("setting.system.peak_refresh_rate", "peak_refresh_rate"),
            ("setting.system.min_refresh_rate", "min_refresh_rate"),
        ] {
            let cur = probe_get(probe, &format!("setting.system.{skey}.cur")).unwrap_or("");
            if cur.is_empty() || cur == "null" {
                continue;
            }
            a.add(
                pid,
                "setting",
                &format!("system:{skey}"),
                strs.clone(),
                cur,
                Some("refresh"),
            );
        }
    }

    a.wl
}

/// 一组参数。
pub type Params = BTreeMap<String, String>;

/// 校验一组参数。三层检查, 任何一层不过**整组作废** ——
/// 不做"剔掉违规项后凑合跑", 因为那样跑出来的数据对应的不是提出的那组参数,
/// 证据会对不上账。
pub fn validate(params: &Params, wl: &Whitelist) -> Result<(), String> {
    if params.is_empty() {
        return Err("空参数组".into());
    }
    for (pid, val) in params {
        let Some(spec) = wl.get(pid) else {
            return Err(format!("{pid} 不在白名单"));
        };
        if denied(pid) || denied(&spec.path) {
            return Err(format!("{pid} 命中温控保护关键词"));
        }
        if !spec.values.iter().any(|v| v == val) {
            return Err(format!(
                "{pid}={val} 不在合法取值表 {:?}...",
                &spec.values[..spec.values.len().min(6)]
            ));
        }
    }

    // 跨项约束。缺省值一律取**最宽松**的那一端 (未知的上界当无穷大,
    // 未知的下界当 0), 否则同簇另一项没进白名单时会把一切合法值误判成倒置。
    let num = |k: &str, fallback: i64| -> i64 {
        params
            .get(k)
            .or_else(|| wl.get(k).map(|s| &s.current))
            .and_then(|v| v.trim().parse::<i64>().ok())
            .unwrap_or(fallback)
    };

    for pid in params.keys() {
        if let Some(stem) = pid.strip_suffix(".scaling_min_freq") {
            let mx_id = format!("{stem}.scaling_max_freq");
            let (mn, mx) = (num(pid, 0), num(&mx_id, i64::MAX));
            if mn > mx {
                return Err(format!("{pid}={mn} 超过同簇 max={mx}"));
            }
        }
    }
    if params.contains_key("gpu.max_pwrlevel") || params.contains_key("gpu.min_pwrlevel") {
        let mx = num("gpu.max_pwrlevel", 0);
        let mn = num("gpu.min_pwrlevel", i64::MAX);
        if mx > mn {
            return Err(format!(
                "gpu.max_pwrlevel={mx} 必须 <= gpu.min_pwrlevel={mn} (0 是最快档)"
            ));
        }
    }
    if params.contains_key("gpu.devfreq.min_freq") || params.contains_key("gpu.devfreq.max_freq") {
        let mn = num("gpu.devfreq.min_freq", 0);
        let mx = num("gpu.devfreq.max_freq", i64::MAX);
        if mn > mx {
            return Err(format!("gpu.devfreq.min_freq={mn} 超过 max={mx}"));
        }
    }
    if params.contains_key("setting.system.min_refresh_rate")
        || params.contains_key("setting.system.peak_refresh_rate")
    {
        let mn = num("setting.system.min_refresh_rate", 0);
        let pk = num("setting.system.peak_refresh_rate", i64::MAX);
        if mn > pk {
            return Err(format!("min_refresh_rate={mn} 超过 peak={pk}"));
        }
    }
    Ok(())
}

/// 一组参数 → 下发到设备的写入计划。
///
/// 排序固定, 所以同一组参数每次生成逐字节相同 (证据可比对)。写入顺序里把
/// "放宽上限"排在"抬高下限"之前, 少踩一次内核的 `min<=max` 夹取。
pub fn plan(params: &Params, wl: &Whitelist) -> Vec<PlanEntry> {
    let rank = |pid: &str| -> u8 {
        if pid.ends_with("max_freq") || pid.ends_with("max_pwrlevel") || pid.contains("peak_refresh")
        {
            0
        } else {
            1
        }
    };
    let mut ids: Vec<&String> = params.keys().collect();
    ids.sort_by(|a, b| rank(a).cmp(&rank(b)).then_with(|| a.cmp(b)));
    ids.into_iter()
        .filter_map(|pid| {
            let spec = wl.get(pid)?;
            Some(PlanEntry {
                kind: spec.kind.clone(),
                path: spec.path.clone(),
                value: params[pid].clone(),
            })
        })
        .collect()
}

/// 给出主意的一方看的白名单说明 (紧凑; 取值表超过 8 个时只给首尾与档数)。
pub fn describe(wl: &Whitelist) -> String {
    let mut out = Vec::new();
    for (pid, s) in wl {
        let v = &s.values;
        let vs = if v.len() <= 8 {
            v.join(", ")
        } else {
            format!(
                "{} .. {} (共 {} 档: {}, ...)",
                v[0],
                v[v.len() - 1],
                v.len(),
                v[..3].join(", ")
            )
        };
        out.push(format!("- {pid} (当前 {}) 可选: {vs}", s.current));
    }
    out.join("\n")
}

/// 一条历史: 这组参数试过了, 真机给的结论是什么。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct History {
    pub params: Params,
    pub verdict: String,
    pub reason: String,
    /// 逐指标的实测数字 (指标名 → 一句话)。模型看得到这些数字与 p 值,
    /// 但没有任何改写判定的途径。
    pub per_metric: Vec<String>,
}

/// 一个候选主意。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Proposal {
    pub why: String,
    pub params: Params,
    /// 这个主意是谁出的。
    pub origin: Origin,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Origin {
    Llm,
    LocalMutation,
    /// 外部进程 (`--proposer cmd:...`) 出的。
    External,
}

/// 系统提示词。与 Python 版同一套硬规矩。
pub const SYSTEM_PROMPT: &str = "你是移动端系统性能调优专家。给你一台已 root 的手机的可写系统参数白名单, \
以及历史上每组参数在真机游戏负载下的实测结果。你的任务是挑下一批候选参数组。\n\
硬规矩:\n\
1. 只能用白名单里的参数名, 值只能从给出的取值表里选, 一个字都不能改。\n\
2. 绝不提出任何与温控保护相关的改动 (关热保护、抬温控阈值), 提了会被直接拒绝。\n\
3. 每组 1-4 个参数。参数少一点容易归因, 别一次全改。\n\
4. 各组之间要有明显差异, 不要提交几乎一样的组。\n\
5. 只输出 JSON, 不要解释、不要 markdown 代码块以外的任何文字。\n\
输出格式 (顶层是数组):\n\
[{\"why\":\"一句话说明这组想验证什么\",\"params\":{\"参数名\":\"值\"}}, ...]";

/// 组用户提示词。
pub fn user_prompt(
    device_note: &str,
    workload_note: &str,
    wl: &Whitelist,
    history: &[History],
    n: u32,
) -> String {
    let mut lines = vec![
        "# 设备与负载".to_string(),
        device_note.to_string(),
        format!("负载: {workload_note}"),
        String::new(),
        "# 判定口径 (已冻结, 你改不了)".to_string(),
        "看三样: 帧时 p95 (越小越好)、平均帧率 (越大越好)、整机功耗 (越小越好)。".to_string(),
        "每组参数跑 A/B 交替多轮, 精确置换检验。任一指标显著改善且无任何指标显著变差 = 保留, 否则淘汰。"
            .to_string(),
        "**目标不是单纯省电** —— 把帧时压下去同样算赢。".to_string(),
        String::new(),
        "# 可改参数白名单 (只能用这些)".to_string(),
        describe(wl),
        String::new(),
    ];

    if history.is_empty() {
        lines.push("# 历史".to_string());
        lines.push("这是第一代, 还没有任何实测结果。".to_string());
    } else {
        lines.push("# 已经试过的参数组与真机实测结果".to_string());
        for h in history {
            lines.push(format!(
                "- 参数 {}",
                serde_json::to_string(&h.params).unwrap_or_default()
            ));
            lines.push(format!("  判定 {} — {}", h.verdict, h.reason));
            for m in &h.per_metric {
                lines.push(format!("  {m}"));
            }
        }
        lines.push(String::new());
        lines.push(
            "别再提交与上面雷同的组。从被淘汰的组里学到的方向也请说明在 why 里。".to_string(),
        );
    }
    lines.push(String::new());
    lines.push(format!("# 现在给我 {n} 组候选, 只输出 JSON 数组。"));
    lines.join("\n")
}

/// 从模型回包里抠出候选数组。纯函数, 容忍 markdown 围栏与前后废话。
pub fn parse_proposals(text: &str, n: usize) -> Vec<Proposal> {
    if text.trim().is_empty() {
        return Vec::new();
    }
    // 先剥 ``` 围栏 (有 json 标注或没有都行)。
    let s = strip_fence(text.trim());
    let (Some(start), Some(end)) = (s.find('['), s.rfind(']')) else {
        return Vec::new();
    };
    if end <= start {
        return Vec::new();
    }
    let Ok(arr) = serde_json::from_str::<serde_json::Value>(&s[start..=end]) else {
        return Vec::new();
    };
    let Some(arr) = arr.as_array() else {
        return Vec::new();
    };

    let mut out = Vec::new();
    for item in arr {
        let Some(obj) = item.as_object() else { continue };
        let Some(params) = obj.get("params").and_then(|p| p.as_object()) else {
            continue;
        };
        if params.is_empty() {
            continue;
        }
        let params: Params = params
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    // 模型有时把数字写成 JSON number 而不是字符串。取值表全是字符串,
                    // 这里统一成字符串再比, 免得一个 5333000 因为类型不同被判成不在表里。
                    match v {
                        serde_json::Value::String(s) => s.clone(),
                        other => other.to_string().trim_matches('"').to_string(),
                    },
                )
            })
            .collect();
        let why = obj
            .get("why")
            .and_then(|w| w.as_str())
            .unwrap_or("")
            .chars()
            .take(300)
            .collect();
        out.push(Proposal {
            why,
            params,
            origin: Origin::Llm,
        });
        if out.len() >= n {
            break;
        }
    }
    out
}

fn strip_fence(s: &str) -> &str {
    let Some(open) = s.find("```") else { return s };
    let rest = &s[open + 3..];
    // 跳过 ```json 这种语言标注那一行。
    let body_start = rest.find('\n').map_or(0, |i| i + 1);
    let body = &rest[body_start..];
    match body.find("```") {
        Some(close) => &body[..close],
        None => body,
    }
}

/// 本地降级变异器 —— LLM 全挂时闭环照常往下跑。
///
/// 策略: 随机挑 1-2 个白名单参数, 往"更激进"的方向推 (频率类取更高档,
/// pwrlevel 取更快档), 避开历史上已经试过的组, 并且必须过白名单校验。
/// 固定 seed 下产出确定, 所以降级路径本身也是可复现的。
pub fn local_mutate(wl: &Whitelist, history: &[History], n: usize, seed: u64) -> Vec<Proposal> {
    let mut rng = Lcg::new(seed);
    let mut tried: BTreeSet<String> = history
        .iter()
        .map(|h| serde_json::to_string(&h.params).unwrap_or_default())
        .collect();
    let pids: Vec<&String> = wl.keys().collect();
    if pids.is_empty() {
        return Vec::new();
    }

    let mut out = Vec::new();
    for _ in 0..(n * 60) {
        if out.len() >= n {
            break;
        }
        let take = 1 + (rng.next() % 2) as usize;
        let mut params = Params::new();
        for _ in 0..take {
            let pid = pids[(rng.next() % pids.len() as u64) as usize];
            let spec = &wl[pid];
            let cur = spec.current.trim();
            let numeric = !spec.values.is_empty()
                && spec
                    .values
                    .iter()
                    .all(|v| v.trim_start_matches('-').chars().all(|c| c.is_ascii_digit()));
            let candidates: Vec<&String> = if numeric && cur.parse::<i64>().is_ok() {
                let c: i64 = cur.parse().unwrap();
                // pwrlevel 语义反过来: 0 是最快档, 所以"更激进" = 往小走。
                if pid.contains("pwrlevel") {
                    spec.values
                        .iter()
                        .filter(|v| v.parse::<i64>().is_ok_and(|x| x < c))
                        .collect()
                } else {
                    spec.values
                        .iter()
                        .filter(|v| v.parse::<i64>().is_ok_and(|x| x > c))
                        .collect()
                }
            } else {
                spec.values.iter().filter(|v| v.as_str() != cur).collect()
            };
            // 没有更激进的取值就跳过这一项, **不往回退** —— 往回退是在试一个
            // 与"探索更高性能"意图相反的方向, 那不是变异, 是噪声。
            if candidates.is_empty() {
                continue;
            }
            let pick = candidates[(rng.next() % candidates.len() as u64) as usize];
            params.insert(pid.clone(), pick.clone());
        }
        if params.is_empty() {
            continue;
        }
        let key = serde_json::to_string(&params).unwrap_or_default();
        if tried.contains(&key) || validate(&params, wl).is_err() {
            continue;
        }
        tried.insert(key);
        out.push(Proposal {
            why: "本地变异器降级产出 (LLM 不可用)".into(),
            params,
            origin: Origin::LocalMutation,
        });
    }
    out
}

/// 一个够用的确定性伪随机源。
///
/// 不引 rand: 降级路径必须可复现, 而"可复现"只要求同一个 seed 出同一串数,
/// 不要求统计质量。用标准的 64 位 LCG 常数 (Knuth MMIX)。
struct Lcg(u64);

impl Lcg {
    fn new(seed: u64) -> Self {
        Self(seed ^ 0x9E37_79B9_7F4A_7C15)
    }
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 16
    }
}

/// 出主意的一方: 优先大模型, 挂了退回本地变异器。
///
/// 无论谁出的, 产出都要**过一遍白名单校验**才算数 —— 模型说某个值合法不作数。
pub struct Proposer<'a> {
    pub generator: Option<&'a dyn super::proposer::Complete>,
    pub seed: u64,
}

/// 一轮出主意的结果 (连同被打回的那些, 证据里要看得到)。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProposalBatch {
    pub accepted: Vec<Proposal>,
    /// 被白名单打回的: (原始参数, 原因)。
    pub rejected: Vec<(Params, String)>,
    /// 这一批是谁出的, 以及 LLM 出了什么岔子。
    pub origin: Option<Origin>,
    pub llm_error: Option<String>,
    /// 模型原始回包 —— 落进证据, 不进判定。
    pub llm_raw: Option<String>,
}

impl Proposer<'_> {
    pub fn propose(
        &self,
        device_note: &str,
        workload_note: &str,
        wl: &Whitelist,
        history: &[History],
        n: u32,
    ) -> ProposalBatch {
        let mut batch = ProposalBatch::default();

        if let Some(g) = self.generator {
            let prompt = user_prompt(device_note, workload_note, wl, history, n);
            match g.complete(SYSTEM_PROMPT, &prompt) {
                Ok(raw) => {
                    batch.llm_raw = Some(raw.clone());
                    let props = parse_proposals(&raw, n as usize);
                    if props.is_empty() {
                        batch.llm_error = Some("回包里没有可用的候选数组".into());
                    } else {
                        batch.origin = Some(Origin::Llm);
                        for p in props {
                            match validate(&p.params, wl) {
                                Ok(()) => batch.accepted.push(p),
                                Err(e) => batch.rejected.push((p.params, e)),
                            }
                        }
                    }
                }
                // LLM 会超时、限流、空回包。任何失败都只让这一代退回本地变异器,
                // 闭环不能因为某个外部服务抽风就停摆。
                Err(e) => batch.llm_error = Some(e.to_string()),
            }
        }

        if batch.accepted.is_empty() {
            let need = n as usize;
            batch.accepted = local_mutate(wl, history, need, self.seed);
            batch.origin = Some(Origin::LocalMutation);
        }
        batch
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PROBE: &str = "\
cpu.policies=0,4,7
cpu.policy0.avail_freqs=300000 700000 1200000
cpu.policy0.scaling_min_freq.writable=yes
cpu.policy0.scaling_min_freq.effect=live
cpu.policy0.scaling_min_freq.cur=300000
cpu.policy0.scaling_max_freq.writable=yes
cpu.policy0.scaling_max_freq.effect=live
cpu.policy0.scaling_max_freq.cur=1200000
gpu.num_pwrlevels=5
gpu.min_pwrlevel.writable=yes
gpu.min_pwrlevel.effect=live
gpu.min_pwrlevel.cur=4
gpu.max_pwrlevel.writable=yes
gpu.max_pwrlevel.effect=live
gpu.max_pwrlevel.cur=0
bus.DDR.boost_freq.writable=yes
bus.DDR.boost_freq.effect=live
bus.DDR.boost_freq.cur=547000
bus.DDR.avail_freqs=547000 1555000 5333000
bus.LLCC.boost_freq.writable=yes
bus.LLCC.boost_freq.effect=dead
bus.LLCC.boost_freq.cur=300000
bus.LLCC.avail_freqs=300000 1066000
thermal.zone0.trip_point_0_temp.writable=yes
thermal.zone0.trip_point_0_temp.effect=live
thermal.zone0.trip_point_0_temp.cur=95000
display.modes=60.0 x 1080, 120.0 x 1080
setting.system.peak_refresh_rate.cur=120
setting.system.min_refresh_rate.cur=60
";

    fn wl() -> Whitelist {
        build_whitelist(&parse_probe(PROBE))
    }

    /// 白名单是探测结果的函数: 写不了的、写了被退回的, 都不该进。
    #[test]
    fn only_nodes_that_actually_take_a_write_get_in() {
        let w = wl();
        assert!(w.contains_key("bus.DDR.boost_freq"));
        assert!(
            !w.contains_key("bus.LLCC.boost_freq"),
            "effect=dead 的节点进了白名单 —— 写了会被内核退回, 那是假改"
        );
    }

    /// 温控保护关键词命中即拒, 不看探测结果。这条是硬规矩, 不是启发式。
    #[test]
    fn thermal_protection_never_enters_the_whitelist() {
        let w = wl();
        assert!(w.keys().all(|k| !denied(k)), "{:?}", w.keys().collect::<Vec<_>>());
        assert!(w.values().all(|s| !denied(&s.path)));
        // 即使探测结果说它可写可生效。
        assert!(!w.contains_key("thermal.zone0.trip_point_0_temp"));

        // 而且哪怕真混进了白名单, 校验那一关也要拦住。
        let mut sneaky = wl();
        sneaky.insert(
            "cooling.device0.cur_state".into(),
            ParamSpec {
                kind: "sysfs".into(),
                path: "/sys/class/thermal/cooling_device0/cur_state".into(),
                values: vec!["0".into(), "1".into()],
                current: "0".into(),
                group: None,
                effect: "live".into(),
            },
        );
        let p = Params::from([("cooling.device0.cur_state".to_string(), "1".to_string())]);
        assert!(validate(&p, &sneaky).is_err());
    }

    #[test]
    fn values_outside_the_table_are_refused() {
        let w = wl();
        let p = Params::from([("bus.DDR.boost_freq".to_string(), "9999999".to_string())]);
        let e = validate(&p, &w).unwrap_err();
        assert!(e.contains("不在合法取值表"), "{e}");
    }

    #[test]
    fn unknown_parameters_are_refused() {
        let p = Params::from([("made.up.knob".to_string(), "1".to_string())]);
        assert!(validate(&p, &wl()).unwrap_err().contains("不在白名单"));
    }

    /// 跨项约束: CPU 频率 min <= max, GPU pwrlevel 0 是最快档。
    #[test]
    fn cross_parameter_constraints_hold() {
        let w = wl();
        let bad = Params::from([
            ("cpu.policy0.scaling_min_freq".to_string(), "1200000".to_string()),
            ("cpu.policy0.scaling_max_freq".to_string(), "700000".to_string()),
        ]);
        assert!(validate(&bad, &w).unwrap_err().contains("超过同簇 max"));

        let ok = Params::from([
            ("cpu.policy0.scaling_min_freq".to_string(), "700000".to_string()),
            ("cpu.policy0.scaling_max_freq".to_string(), "1200000".to_string()),
        ]);
        assert!(validate(&ok, &w).is_ok());

        let bad = Params::from([
            ("gpu.max_pwrlevel".to_string(), "4".to_string()),
            ("gpu.min_pwrlevel".to_string(), "0".to_string()),
        ]);
        assert!(validate(&bad, &w).unwrap_err().contains("最快档"));
    }

    /// 同簇另一项没进白名单时, 不该把合法值误判成倒置。
    #[test]
    fn a_missing_sibling_does_not_cause_a_false_inversion() {
        let mut w = wl();
        w.remove("cpu.policy0.scaling_max_freq");
        let p = Params::from([("cpu.policy0.scaling_min_freq".to_string(), "1200000".to_string())]);
        assert!(validate(&p, &w).is_ok(), "{:?}", validate(&p, &w));
    }

    /// plan 的顺序固定 —— 同一组参数每次生成逐字节相同, 证据才可比对。
    #[test]
    fn plan_is_deterministic_and_puts_ceilings_first() {
        let w = wl();
        let p = Params::from([
            ("cpu.policy0.scaling_min_freq".to_string(), "700000".to_string()),
            ("cpu.policy0.scaling_max_freq".to_string(), "1200000".to_string()),
        ]);
        let a = plan(&p, &w);
        assert_eq!(a, plan(&p, &w));
        assert!(a[0].path.ends_with("scaling_max_freq"), "{a:?}");
    }

    #[test]
    fn proposals_survive_markdown_fences_and_numeric_values() {
        let raw = "这是我的想法:\n```json\n[{\"why\":\"抬总线下限\",\
            \"params\":{\"bus.DDR.boost_freq\":5333000}}]\n```\n完毕";
        let props = parse_proposals(raw, 4);
        assert_eq!(props.len(), 1);
        assert_eq!(props[0].params["bus.DDR.boost_freq"], "5333000");
        // 数字被写成 JSON number 时也要能对上取值表。
        assert!(validate(&props[0].params, &wl()).is_ok());
    }

    #[test]
    fn garbage_replies_yield_nothing_rather_than_panicking() {
        for raw in ["", "抱歉我不能", "{\"not\":\"an array\"}", "[", "[{}]", "[{\"params\":{}}]"] {
            assert!(parse_proposals(raw, 4).is_empty(), "{raw}");
        }
    }

    /// 本地变异器: 同一个 seed 出同一批, 而且全部合法。
    #[test]
    fn local_mutation_is_reproducible_and_always_legal() {
        let w = wl();
        let a = local_mutate(&w, &[], 3, 42);
        let b = local_mutate(&w, &[], 3, 42);
        assert_eq!(a, b);
        assert!(!a.is_empty());
        for p in &a {
            validate(&p.params, &w).unwrap_or_else(|e| panic!("变异器产出了非法组: {e}"));
        }
        // 换个 seed 应该换一批 (不是硬性要求, 但同一批说明 seed 没起作用)。
        assert_ne!(a, local_mutate(&w, &[], 3, 7));
    }

    /// 已经试过的组不再重复提。
    #[test]
    fn local_mutation_avoids_what_has_been_tried() {
        let w = wl();
        let first = local_mutate(&w, &[], 1, 5);
        let hist: Vec<History> = first
            .iter()
            .map(|p| History {
                params: p.params.clone(),
                verdict: "REJECT".into(),
                reason: "没有任何指标显著改善".into(),
                per_metric: vec![],
            })
            .collect();
        for p in local_mutate(&w, &hist, 3, 5) {
            assert!(
                !hist.iter().any(|h| h.params == p.params),
                "又提了一遍试过的组"
            );
        }
    }

    /// 变异器只往更激进的方向推, 不往回退。
    #[test]
    fn local_mutation_only_pushes_forward() {
        let w = wl();
        for p in local_mutate(&w, &[], 5, 11) {
            for (pid, v) in &p.params {
                let cur = &w[pid].current;
                if let (Ok(a), Ok(b)) = (v.parse::<i64>(), cur.parse::<i64>()) {
                    if pid.contains("pwrlevel") {
                        assert!(a < b, "{pid}: {a} 不比当前 {b} 更快");
                    } else {
                        assert!(a > b, "{pid}: {a} 不比当前 {b} 更高");
                    }
                }
            }
        }
    }

    /// 没有 LLM 时 Proposer 照样出得了主意。
    #[test]
    fn the_proposer_falls_back_to_local_mutation() {
        let p = Proposer {
            generator: None,
            seed: 3,
        };
        let batch = p.propose("测试机", "转视角 36 秒", &wl(), &[], 2);
        assert_eq!(batch.origin, Some(Origin::LocalMutation));
        assert!(!batch.accepted.is_empty());
        assert!(batch.llm_raw.is_none());
    }

    #[test]
    fn the_prompt_shows_the_whitelist_and_the_history() {
        let w = wl();
        let hist = vec![History {
            params: Params::from([("bus.DDR.boost_freq".to_string(), "5333000".to_string())]),
            verdict: "KEEP".into(),
            reason: "改善 frame_p95 且无显著退化".into(),
            per_metric: vec!["frame_p95: -1.61% (p=0.0079) 显著改善".into()],
        }];
        let text = user_prompt("红魔 NX809J", "转视角 36 秒", &w, &hist, 3);
        assert!(text.contains("bus.DDR.boost_freq"));
        assert!(text.contains("-1.61%"));
        assert!(text.contains("给我 3 组"));
        // 白名单里没有的东西不该出现在提示词里。
        assert!(!text.contains("trip_point"));
    }
}
