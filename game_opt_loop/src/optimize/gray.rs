//! ② 画面流程改写层 (灰档): 改写开关目录 + 出主意。
//!
//! 灰档走 Vulkan layer 注入, 在不碰游戏源码的前提下改渲染流程
//! (LoadOp / StoreOp / attachment 精度 / 离屏分辨率 / 单个 pass 的着色器)。
//! 与系统参数层同构: 这里只负责**准改什么**与**这一轮改成什么**,
//! 应用/测量/判定/还原全在 phonefarm。
//!
//! # 为什么要有"层里到底有没有这个开关"这张表
//!
//! 开关是层的 C 代码里一个一个实现出来的, 不是契约里写了就存在。
//! 让模型随便提一个层不认识的开关, 结果是真机跑完一轮、改写一次都没命中,
//! 而那个"无显著差异"长得跟"这个改写没用"一模一样。所以本地先拦一道。

use super::eval::Rewrite;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// 一个改写开关在层里的实现状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KnobStatus {
    /// 层里已实现, 可以提。
    Landed,
    /// 契约里声明了, 层里还没实现。提了也白提。
    Declared,
    /// 层里实现过, 但证据表明它会画错画面, 不再提。
    Withdrawn,
}

/// 一个改写开关。
#[derive(Debug, Clone, Copy)]
pub struct Knob {
    pub id: &'static str,
    pub status: KnobStatus,
    /// 改什么。
    pub what: &'static str,
    /// 指望省在哪。出主意的一方靠这句话判断该不该试它。
    pub why: &'static str,
    /// 会不会动画面 —— 动画面的必须量画质。
    pub touches_image: bool,
    /// 必须带 scope (指定改哪一个 pass 的哪一个附件), 而且 scope 只能取 [`StoreTarget`] 里列出的。
    pub needs_scope: bool,
}

/// 开关目录。
///
/// 状态跟着 phonefarm 灰档层走: 那边落地一个, 这里改一个。宁可这张表落后于层
/// (少提一个候选), 不可超前于层 (提一个层不认识的开关, 换回一轮白跑的真机时间)。
pub const CATALOG: &[Knob] = &[
    Knob {
        id: "loadop_dont_care",
        status: KnobStatus::Withdrawn,
        what: "把所有 render pass 的 LoadOp LOAD 一刀切改写成 DONT_CARE",
        why: "已撤回: phonefarm docs/GENSHIN_FRAME_AUDIT.md §4 —— 原神每帧 12+5 个 LOAD 读的全是上一步刚写好的内容, \
              一刀切改成 DONT_CARE 是在破坏正确性 (主深度被 4 个全分辨率 pass 当深度测试用), 上个月没量画质所以没发现",
        touches_image: true,
        needs_scope: false,
    },
    Knob {
        id: "storeop_dont_care",
        status: KnobStatus::Landed,
        what: "把指定 pass 的指定附件 StoreOp STORE 改写成 DONT_CARE (scope = \"<pass 特征码>:<附件下标>\")",
        why: "省掉一次把 tile 内容写回显存的带宽。只对 frame-audit 判定「写回之后确定没人读」的附件成立, \
              scope 只能从列出的那几个里挑",
        touches_image: true,
        needs_scope: true,
    },
    Knob {
        id: "attachment_precision",
        status: KnobStatus::Declared,
        what: "降 attachment 的格式精度",
        why: "带宽与显存占用随位宽线性下降",
        touches_image: true,
        needs_scope: false,
    },
    Knob {
        id: "render_scale",
        status: KnobStatus::Declared,
        what: "改离屏 pass 的渲染分辨率",
        why: "片元着色开销随像素数下降 —— 这正是超分算子想要的那个前提",
        touches_image: true,
        needs_scope: false,
    },
    Knob {
        id: "shader_replace",
        status: KnobStatus::Declared,
        what: "用外部 SPIR-V 顶替某个 pass 的着色器",
        why: "黑盒游戏里把画面算子挂进去的唯一通道",
        touches_image: true,
        needs_scope: false,
    },
];

pub fn knob(id: &str) -> Option<&'static Knob> {
    CATALOG.iter().find(|k| k.id == id)
}

/// 一组改写最多几条写回改写: 层的属性串 `"<16 位特征码>:<下标>,…"` 受 Android 属性值
/// 91 字节上限约束, 每条 18 字节加逗号, 4 条刚好放得下。
pub const MAX_STORE_TARGETS_PER_CANDIDATE: usize = 4;

/// 画面比对门槛 (dB): 改写臂与对照臂同位置截图的 PSNR 低于它, 接收端判 ABORT「画面出错」。
pub const MIN_PSNR_DB: f64 = 30.0;

/// 一个可以去掉写回的附件 (来自 `phonefarm frame-audit analyze`)。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StoreTarget {
    /// `"<16 位特征码>:<附件下标>"` —— 原样写进 rewrite.scope。
    pub scope: String,
    /// 是哪个 pass (给人和模型看的, 如 "pass rp103 1070x484")。
    pub pass: String,
    /// 每帧白写多少字节 (未压缩模型值)。
    pub bytes_per_frame: f64,
    /// analyze 给的理由原文。
    pub why: String,
}

impl StoreTarget {
    pub fn label(&self) -> String {
        format!("storeop_dont_care@{}", self.scope)
    }
}

fn sig_of(s: &str) -> Option<String> {
    let i = s.find("sig=")?;
    let hex: String = s[i + 4..]
        .chars()
        .take_while(|c| c.is_ascii_hexdigit())
        .collect();
    (hex.len() >= 8).then(|| hex.to_ascii_lowercase())
}

/// 从 `frame-audit analyze --json` 的输出里挑出**确定**可以去掉写回的附件。
///
/// 只收这样的 (特征码, 附件下标): 在每一份抓取的每一个可判帧里 (后面还有完整一帧可看的帧),
/// 这个特征码的**每一次**出现都有一条 `certain = true` 的 `dead_store`。只要有一次出现没被判白存,
/// 或者同一个特征码下有过 `certain = false` 的白存 (典型: 同一 sig 还有别的尺寸的 pass 被人读),
/// 就整个不收 —— 层按特征码改, 改的是这个 sig 的每一次出现。宁可少一个候选, 不可画错一帧。
pub fn targets_from_frame_audit(v: &Value) -> Vec<StoreTarget> {
    let files: Vec<&Value> = match v {
        Value::Array(a) => a.iter().collect(),
        other => vec![other],
    };
    // (文件序号, 帧) → sig → 出现次数
    let mut inst: BTreeMap<(usize, i64), BTreeMap<String, usize>> = BTreeMap::new();
    // (文件序号, 帧, sig, att) → 确定白存的次数
    let mut dead: BTreeMap<(usize, i64, String, u64), usize> = BTreeMap::new();
    let mut tainted: BTreeSet<String> = BTreeSet::new();
    let mut info: BTreeMap<(String, u64), (String, String, f64, usize)> = BTreeMap::new();
    for (fi, f) in files.iter().enumerate() {
        let last = f["frames"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|x| x["frame"].as_i64())
                    .max()
                    .unwrap_or(-1)
            })
            .unwrap_or(-1);
        for p in f["pass_times"].as_array().into_iter().flatten() {
            let (Some(fr), Some(sig)) = (p["frame"].as_i64(), p["sig"].as_str().and_then(sig_of))
            else {
                continue;
            };
            if fr < last {
                *inst.entry((fi, fr)).or_default().entry(sig).or_default() += 1;
            }
        }
        for x in f["findings"].as_array().into_iter().flatten() {
            if x["rule"].as_str() != Some("dead_store") {
                continue;
            }
            let Some(sigtext) = x["sig"].as_str() else {
                continue;
            };
            let Some(sig) = sig_of(sigtext) else { continue };
            if x["certain"].as_bool() != Some(true) {
                tainted.insert(sig);
                continue;
            }
            let (Some(fr), Some(att)) = (x["frame"].as_i64(), x["att"].as_u64()) else {
                continue;
            };
            *dead.entry((fi, fr, sig.clone(), att)).or_default() += 1;
            let e = info.entry((sig, att)).or_insert_with(|| {
                (
                    sigtext.split(" sig=").next().unwrap_or("").to_string(),
                    x["why"].as_str().unwrap_or("").to_string(),
                    0.0,
                    0,
                )
            });
            e.2 += x["bytes"].as_f64().unwrap_or(0.0);
        }
    }
    let mut out = Vec::new();
    for ((sig, att), (pass, why, bytes, _)) in info {
        if tainted.contains(&sig) {
            continue;
        }
        let mut frames_seen = 0usize;
        let mut ok = true;
        for ((fi, fr), sigs) in &inst {
            let Some(&n) = sigs.get(&sig) else { continue };
            frames_seen += 1;
            if dead
                .get(&(*fi, *fr, sig.clone(), att))
                .copied()
                .unwrap_or(0)
                != n
            {
                ok = false;
                break;
            }
        }
        if ok && frames_seen > 0 {
            out.push(StoreTarget {
                scope: format!("{sig}:{att}"),
                pass,
                bytes_per_frame: (bytes / frames_seen as f64).round(),
                why,
            });
        }
    }
    out.sort_by(|a, b| {
        b.bytes_per_frame
            .total_cmp(&a.bytes_per_frame)
            .then(a.scope.cmp(&b.scope))
    });
    out
}

/// 读 frame-audit 结果文件。
pub fn load_targets(path: &Path) -> anyhow::Result<Vec<StoreTarget>> {
    use anyhow::Context;
    let raw =
        std::fs::read_to_string(path).with_context(|| format!("读不了 {}", path.display()))?;
    let v: Value =
        serde_json::from_str(&raw).with_context(|| format!("{} 不是 JSON", path.display()))?;
    Ok(targets_from_frame_audit(&v))
}

/// 一条改写的标签: 开关名, 带范围时加 `@scope`。历史、档案、去重都按它。
pub fn rewrite_label(r: &Rewrite) -> String {
    match r.scope.as_deref() {
        Some(s) if !s.is_empty() => format!("{}@{}", r.knob, s),
        _ => r.knob.clone(),
    }
}

/// 层的本地构建产物在不在。
///
/// 不在的话整层都开不了 —— 没有 .so 可以放进目标应用的 lib 目录。
/// 探的是 phonefarm 那边的构建脚本与产物, 本仓库不自己编层。
pub fn layer_build_present(phonefarm_root: &Path) -> bool {
    phonefarm_root
        .join("knobs")
        .join("gray")
        .join("build")
        .join("build_layer.sh")
        .is_file()
}

/// 一个候选主意。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Proposal {
    pub why: String,
    pub rewrites: Vec<Rewrite>,
    pub origin: super::sysparam::Origin,
}

/// 出主意的结果。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ProposalBatch {
    pub accepted: Vec<Proposal>,
    /// 被打回的: (开关名, 原因)。
    pub rejected: Vec<(String, String)>,
    pub origin: Option<super::sysparam::Origin>,
    pub llm_error: Option<String>,
    pub llm_raw: Option<String>,
}

pub const SYSTEM_PROMPT: &str = "你是移动端图形渲染管线优化专家。给你一份 Vulkan layer 能做的\
渲染流程改写开关目录 (含每个开关准用的范围), 以及历史上每组改写在真机游戏负载下的实测结果。\
你的任务是挑下一批候选改写组。\n\
硬规矩:\n\
1. 只能用目录里**可用**的开关名, 一个字都不能改, 不许发明新开关。\n\
2. 需要范围的开关 (storeop_dont_care) 必须带 scope, 而且 scope 只能原样抄目录里列出的某一个, \
   一个字符都不能改。同一组里同一个 scope 只能出现一次, 一组最多 4 条。\n\
3. 各组之间要有明显差异 (单个改 vs 组合改都可以)。\n\
4. 这些改写会动画面: 每个候选上机时都会和对照臂同位置截图比对, 画面变了直接作废。\n\
5. 只输出 JSON, 不要解释、不要 markdown 代码块以外的任何文字。\n\
输出格式 (顶层是数组):\n\
[{\"why\":\"一句话说明这组想验证什么\",\"rewrites\":[{\"knob\":\"开关名\",\"scope\":\"范围或 null\"}]}, ...]";

/// 给出主意的一方看的开关目录 (只列可用的, 带范围清单)。
pub fn describe(targets: &[StoreTarget]) -> String {
    let mut lines = Vec::new();
    for k in CATALOG.iter().filter(|k| k.status == KnobStatus::Landed) {
        if k.needs_scope && targets.is_empty() {
            continue;
        }
        lines.push(format!("- {} — {}。指望: {}", k.id, k.what, k.why));
        if k.needs_scope {
            for t in targets {
                lines.push(format!(
                    "    scope=\"{}\" — {} 每帧白写 {:.0} KB ({})",
                    t.scope,
                    t.pass,
                    t.bytes_per_frame / 1000.0,
                    t.why
                ));
            }
        }
    }
    lines.join("\n")
}

pub fn user_prompt(
    device_note: &str,
    workload_note: &str,
    targets: &[StoreTarget],
    history: &[super::sysparam::History],
    n: u32,
) -> String {
    let mut lines = vec![
        "# 设备与负载".to_string(),
        device_note.to_string(),
        format!("负载: {workload_note}"),
        String::new(),
        "# 判定口径 (已冻结, 你改不了)".to_string(),
        "看三样: 帧时 p95 (越小越好)、平均帧率 (越大越好)、整机功耗 (越小越好)。画面是**约束**: 每轮改写臂和对照臂同位置截图比 PSNR, 低于门槛直接作废。"
            .to_string(),
        "A/B 交替多轮 + 置换检验。改写一次都没命中真机的话整轮作废, 不会被当成\"没用\"。"
            .to_string(),
        String::new(),
        "# 可用的改写开关 (只能用这些)".to_string(),
        describe(targets),
        String::new(),
    ];
    if history.is_empty() {
        lines.push("# 历史".to_string());
        lines.push("这是第一代, 还没有任何实测结果。".to_string());
    } else {
        lines.push("# 已经试过的改写组与真机实测结果".to_string());
        for h in history {
            lines.push(format!(
                "- {} → {} ({})",
                h.reason,
                h.verdict,
                h.params.len()
            ));
            for m in &h.per_metric {
                lines.push(format!("  {m}"));
            }
        }
    }
    lines.push(String::new());
    lines.push(format!("# 现在给我 {n} 组候选, 只输出 JSON 数组。"));
    lines.join("\n")
}

/// 从模型回包里抠出候选数组。
pub fn parse_proposals(text: &str, n: usize) -> Vec<Proposal> {
    let Some(arr) = extract_array(text) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in arr.as_array().into_iter().flatten() {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let rewrites: Vec<Rewrite> = obj
            .get("rewrites")
            .and_then(|r| r.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|r| {
                        let knob = r.get("knob")?.as_str()?.to_string();
                        Some(Rewrite {
                            knob,
                            scope: r.get("scope").and_then(|s| s.as_str()).map(str::to_string),
                            args: r.get("args").cloned().filter(|a| !a.is_null()),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();
        if rewrites.is_empty() {
            continue;
        }
        out.push(Proposal {
            why: obj
                .get("why")
                .and_then(|w| w.as_str())
                .unwrap_or("")
                .chars()
                .take(300)
                .collect(),
            rewrites,
            origin: super::sysparam::Origin::Llm,
        });
        if out.len() >= n {
            break;
        }
    }
    out
}

fn extract_array(text: &str) -> Option<serde_json::Value> {
    let s = text.trim();
    let start = s.find('[')?;
    let end = s.rfind(']')?;
    if end <= start {
        return None;
    }
    serde_json::from_str(&s[start..=end]).ok()
}

/// 校验一组改写。不认识的、层里还没实现的、已撤回的、范围不在清单里的、重复的, 整组作废。
pub fn validate(rewrites: &[Rewrite], targets: &[StoreTarget]) -> Result<(), String> {
    if rewrites.is_empty() {
        return Err("空改写组".into());
    }
    let mut seen = BTreeSet::new();
    let mut scoped = 0usize;
    for r in rewrites {
        let Some(k) = knob(&r.knob) else {
            return Err(format!("{} 不在开关目录里", r.knob));
        };
        match k.status {
            KnobStatus::Landed => {}
            KnobStatus::Declared => {
                return Err(format!(
                    "{} 契约里声明了, 但层里还没实现 —— 提了也不会命中",
                    r.knob
                ))
            }
            KnobStatus::Withdrawn => return Err(format!("{} 已撤回: {}", r.knob, k.why)),
        }
        if k.needs_scope {
            let Some(scope) = r.scope.as_deref().map(str::trim).filter(|s| !s.is_empty()) else {
                return Err(format!("{} 必须带 scope (从目录列出的范围里挑)", r.knob));
            };
            if !targets.iter().any(|t| t.scope == scope) {
                return Err(format!(
                    "{}@{scope} 不在 frame-audit 判定的「确定没人读」清单里",
                    r.knob
                ));
            }
            scoped += 1;
        }
        if !seen.insert(rewrite_label(r)) {
            return Err(format!("{} 出现了两次", rewrite_label(r)));
        }
    }
    if scoped > MAX_STORE_TARGETS_PER_CANDIDATE {
        return Err(format!(
            "一组最多 {MAX_STORE_TARGETS_PER_CANDIDATE} 条写回改写 (层的属性串有长度上限), 这组 {scoped} 条"
        ));
    }
    Ok(())
}

/// 本地降级: 先把每个可去的写回单独试一次 (按每帧省的字节从大到小), 再试全部一起。
///
/// **必须避开历史**: 试过的组合 (按改写标签集合比) 不再提。提不出新的就回空 —— 由调用方把
/// "已经全试过了"如实写进报告, 而不是拿重复候选把代数凑满。
pub fn local_proposals(
    n: usize,
    history: &[super::sysparam::History],
    targets: &[StoreTarget],
) -> Vec<Proposal> {
    let tried: Vec<BTreeSet<&str>> = history
        .iter()
        .map(|h| h.params.keys().map(String::as_str).collect())
        .collect();
    let rw = |t: &StoreTarget| Rewrite {
        knob: "storeop_dont_care".into(),
        scope: Some(t.scope.clone()),
        args: None,
    };
    let mut cands: Vec<Proposal> = targets
        .iter()
        .map(|t| Proposal {
            why: format!(
                "本地降级产出: 单独去掉 {} 的写回 ({}, 每帧约 {:.0} KB)",
                t.scope,
                t.pass,
                t.bytes_per_frame / 1000.0
            ),
            rewrites: vec![rw(t)],
            origin: super::sysparam::Origin::LocalMutation,
        })
        .collect();
    if targets.len() > 1 {
        let all: Vec<&StoreTarget> = targets
            .iter()
            .take(MAX_STORE_TARGETS_PER_CANDIDATE)
            .collect();
        cands.push(Proposal {
            why: format!("本地降级产出: {} 处确定没人读的写回一起去掉", all.len()),
            rewrites: all.into_iter().map(rw).collect(),
            origin: super::sysparam::Origin::LocalMutation,
        });
    }
    cands
        .into_iter()
        .filter(|p| {
            let labels: Vec<String> = p.rewrites.iter().map(rewrite_label).collect();
            let set: BTreeSet<&str> = labels.iter().map(String::as_str).collect();
            !tried.contains(&set)
        })
        .take(n)
        .collect()
}

/// 出主意的一方。
pub struct Proposer<'a> {
    pub generator: Option<&'a dyn super::proposer::Complete>,
    /// 写回改写准用的范围 (frame-audit 判定确定没人读的附件)。
    pub targets: &'a [StoreTarget],
}

impl Proposer<'_> {
    pub fn propose(
        &self,
        device_note: &str,
        workload_note: &str,
        history: &[super::sysparam::History],
        n: u32,
    ) -> ProposalBatch {
        let mut batch = ProposalBatch::default();
        if let Some(g) = self.generator {
            let prompt = user_prompt(device_note, workload_note, self.targets, history, n);
            match g.complete(SYSTEM_PROMPT, &prompt) {
                Ok(raw) => {
                    batch.llm_raw = Some(raw.clone());
                    let props = parse_proposals(&raw, n as usize);
                    if props.is_empty() {
                        batch.llm_error = Some("回包里没有可用的候选数组".into());
                    } else {
                        batch.origin = Some(super::sysparam::Origin::Llm);
                        for p in props {
                            match validate(&p.rewrites, self.targets) {
                                Ok(()) => batch.accepted.push(p),
                                Err(e) => batch.rejected.push((
                                    p.rewrites
                                        .iter()
                                        .map(rewrite_label)
                                        .collect::<Vec<_>>()
                                        .join("+"),
                                    e,
                                )),
                            }
                        }
                    }
                }
                Err(e) => batch.llm_error = Some(e.to_string()),
            }
        }
        if batch.accepted.is_empty() {
            if batch.llm_error.is_none() && !batch.rejected.is_empty() {
                batch.llm_error = Some(format!(
                    "大模型回了 {} 个候选, 全部没过开关目录校验 (见 rejected_locally)",
                    batch.rejected.len()
                ));
            }
            batch.accepted = local_proposals(n as usize, history, self.targets);
            batch.origin = Some(super::sysparam::Origin::LocalMutation);
        }
        batch
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 真机抓的原神 7.1.0 frame-audit 结果 (phonefarm knobs/evidence/10_frameaudit/analysis.json)。
    fn genshin() -> Vec<StoreTarget> {
        let raw = include_str!("../../tests/fixtures/frame_audit/genshin_7.1_analysis.json");
        targets_from_frame_audit(&serde_json::from_str(raw).unwrap())
    }

    fn so(scope: &str) -> Rewrite {
        Rewrite {
            knob: "storeop_dont_care".into(),
            scope: Some(scope.into()),
            args: None,
        }
    }

    /// 与 GENSHIN_FRAME_AUDIT.md §6 的清单一致: rp103 C2、rp71 C0/C1 三处确定白存;
    /// sig ae75a3d4bc624906 (同一个 sig 下还有被读的 pass) 不能收。
    #[test]
    fn genshin_audit_yields_exactly_the_three_certain_dead_stores() {
        let t = genshin();
        let scopes: Vec<&str> = t.iter().map(|x| x.scope.as_str()).collect();
        assert_eq!(
            scopes,
            vec![
                "15a51a879df99c32:2",
                "9d0b4db80bf552b8:1",
                "9d0b4db80bf552b8:0"
            ],
            "{t:#?}"
        );
        assert!(!scopes.iter().any(|s| s.starts_with("ae75a3d4")));
        assert_eq!(t[0].bytes_per_frame, 2071520.0);
        assert!(t[0].pass.contains("rp103"));
    }

    /// 同一个 sig 有一次出现没被判白存 (别的 pass 实例有人读), 整个 sig 不收。
    #[test]
    fn a_sig_with_any_live_instance_is_not_a_target() {
        let v = serde_json::json!([{
            "frames": [{"frame": 0}, {"frame": 1}],
            "pass_times": [
                {"frame": 0, "sig": "pass rp1 64x64 sig=aaaaaaaaaaaaaaaa"},
                {"frame": 0, "sig": "pass rp1 32x32 sig=aaaaaaaaaaaaaaaa"},
                {"frame": 0, "sig": "pass rp2 64x64 sig=bbbbbbbbbbbbbbbb"}
            ],
            "findings": [
                {"rule": "dead_store", "certain": true, "frame": 0, "att": 0, "bytes": 10.0,
                 "sig": "pass rp1 64x64 sig=aaaaaaaaaaaaaaaa", "why": "x"},
                {"rule": "dead_store", "certain": true, "frame": 0, "att": 1, "bytes": 20.0,
                 "sig": "pass rp2 64x64 sig=bbbbbbbbbbbbbbbb", "why": "y"}
            ]
        }]);
        let t = targets_from_frame_audit(&v);
        assert_eq!(t.len(), 1);
        assert_eq!(t[0].scope, "bbbbbbbbbbbbbbbb:1");
    }

    #[test]
    fn landed_scoped_rewrites_pass_and_bad_ones_are_refused() {
        let t = genshin();
        assert!(validate(&[so("15a51a879df99c32:2")], &t).is_ok());
        assert!(validate(&[so("15a51a879df99c32:2"), so("9d0b4db80bf552b8:0")], &t).is_ok());
        for (bad, why) in [
            (vec![so("ae75a3d4bc624906:0")], "清单"),
            (vec![so("15a51a879df99c32:1")], "清单"),
            (
                vec![Rewrite {
                    knob: "storeop_dont_care".into(),
                    scope: None,
                    args: None,
                }],
                "scope",
            ),
            (
                vec![so("15a51a879df99c32:2"), so("15a51a879df99c32:2")],
                "两次",
            ),
            (
                vec![Rewrite {
                    knob: "loadop_dont_care".into(),
                    scope: None,
                    args: None,
                }],
                "撤回",
            ),
            (
                vec![Rewrite {
                    knob: "render_scale".into(),
                    scope: None,
                    args: None,
                }],
                "还没实现",
            ),
            (
                vec![Rewrite {
                    knob: "make_it_faster".into(),
                    scope: None,
                    args: None,
                }],
                "不在开关目录",
            ),
            (vec![], "空"),
        ] {
            let e = validate(&bad, &t).unwrap_err();
            assert!(e.contains(why), "{bad:?}: {e}");
        }
        // 没有 frame-audit 结果时, 写回改写一个都不准提
        assert!(validate(&[so("15a51a879df99c32:2")], &[]).is_err());
    }

    #[test]
    fn too_many_store_targets_in_one_group_are_refused() {
        let t: Vec<StoreTarget> = (0..5)
            .map(|i| StoreTarget {
                scope: format!("0123456789abcdef:{i}"),
                pass: "p".into(),
                bytes_per_frame: 1.0,
                why: String::new(),
            })
            .collect();
        let rw: Vec<Rewrite> = t.iter().map(|x| so(&x.scope)).collect();
        assert!(validate(&rw, &t).unwrap_err().contains("最多"));
    }

    /// 只把可用的开关和准用的范围给模型看。
    #[test]
    fn the_catalog_shown_to_the_model_lists_targets_and_hides_the_rest() {
        let d = describe(&genshin());
        assert!(d.contains("storeop_dont_care"));
        assert!(d.contains("scope=\"15a51a879df99c32:2\""));
        assert!(!d.contains("loadop_dont_care"), "撤回的开关不该露出来");
        for k in CATALOG.iter().filter(|k| k.status == KnobStatus::Declared) {
            assert!(!d.contains(k.id), "目录里露出了还没落地的 {}", k.id);
        }
        // 没有清单时, 需要范围的开关也不露
        assert!(!describe(&[]).contains("storeop_dont_care"));
    }

    /// 每一个改写都会动画面, 所以这一层永远要做画面比对。
    #[test]
    fn every_rewrite_touches_the_image() {
        assert!(CATALOG.iter().all(|k| k.touches_image));
    }

    #[test]
    fn proposals_parse_from_a_reply() {
        let raw = "```json\n[{\"why\":\"去掉 rp103 的白写\",\
            \"rewrites\":[{\"knob\":\"storeop_dont_care\",\"scope\":\"15a51a879df99c32:2\"}]}]\n```";
        let p = parse_proposals(raw, 3);
        assert_eq!(p.len(), 1);
        assert_eq!(
            p[0].rewrites[0].scope.as_deref(),
            Some("15a51a879df99c32:2")
        );
        assert!(validate(&p[0].rewrites, &genshin()).is_ok());
    }

    #[test]
    fn garbage_replies_yield_nothing() {
        for raw in ["", "不行", "[]", "[{\"rewrites\":[]}]"] {
            assert!(parse_proposals(raw, 3).is_empty(), "{raw}");
        }
    }

    /// 本地降级: 先单个 (按字节从大到小), 再全部一起; 都过校验; 试过的组合不再提。
    #[test]
    fn the_local_fallback_singles_then_combines_and_never_repeats() {
        let t = genshin();
        let first = local_proposals(10, &[], &t);
        assert_eq!(first.len(), 4, "3 个单独 + 1 个全部");
        assert_eq!(first[0].rewrites, vec![so("15a51a879df99c32:2")]);
        assert_eq!(first[3].rewrites.len(), 3);
        for p in &first {
            validate(&p.rewrites, &t).unwrap();
        }
        let history: Vec<super::super::sysparam::History> = first[..2]
            .iter()
            .map(|p| super::super::sysparam::History {
                params: p
                    .rewrites
                    .iter()
                    .map(|r| (rewrite_label(r), "on".to_string()))
                    .collect(),
                verdict: "REJECT".into(),
                reason: String::new(),
                per_metric: vec![],
            })
            .collect();
        let next = local_proposals(10, &history, &t);
        assert_eq!(next.len(), 2);
        assert_eq!(next[0].rewrites, first[2].rewrites);
        // 没有清单就什么都提不出来 —— 不拿撤回的 loadop 凑数
        assert!(local_proposals(3, &[], &[]).is_empty());
    }

    struct Fake(&'static str);
    impl super::super::proposer::Complete for Fake {
        fn complete(&self, _s: &str, _u: &str) -> anyhow::Result<String> {
            Ok(self.0.to_string())
        }
    }

    /// 大模型可以在这些旋钮之间组合; 抄错 scope 的那组被拦下, 其余照收。
    #[test]
    fn the_model_combines_targets_and_wrong_scopes_are_screened() {
        let t = genshin();
        let g = Fake(
            "[{\"why\":\"两处一起\",\"rewrites\":[{\"knob\":\"storeop_dont_care\",\"scope\":\"15a51a879df99c32:2\"},\
             {\"knob\":\"storeop_dont_care\",\"scope\":\"9d0b4db80bf552b8:1\"}]},\
             {\"why\":\"抄错\",\"rewrites\":[{\"knob\":\"storeop_dont_care\",\"scope\":\"15a51a87:2\"}]}]",
        );
        let b = Proposer {
            generator: Some(&g),
            targets: &t,
        }
        .propose("d", "w", &[], 2);
        assert_eq!(b.accepted.len(), 1);
        assert_eq!(b.accepted[0].rewrites.len(), 2);
        assert_eq!(b.rejected.len(), 1);
        assert_eq!(b.origin, Some(super::super::sysparam::Origin::Llm));
    }
}
