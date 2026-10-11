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
use std::collections::BTreeSet;
use std::path::Path;

/// 一个改写开关在层里的实现状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KnobStatus {
    /// 层里已实现, 并且在真机上验证过改写真的被绑上。
    Landed,
    /// 契约里声明了, 层里还没实现。提了也白提。
    Declared,
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
}

/// 开关目录。
///
/// 状态跟着 phonefarm `knobs/gray/FEASIBILITY.md` 走: 那边落地一个,
/// 这里改一个。宁可这张表落后于层 (少提一个候选), 不可超前于层
/// (提一个层不认识的开关, 换回一轮白跑的真机时间)。
pub const CATALOG: &[Knob] = &[
    Knob {
        id: "loadop_dont_care",
        status: KnobStatus::Landed,
        what: "把 render pass 的 LoadOp LOAD 改写成 DONT_CARE",
        why: "省掉一次把上一帧内容从显存读回 tile 的带宽开销",
        touches_image: true,
    },
    Knob {
        id: "storeop_dont_care",
        status: KnobStatus::Declared,
        what: "把 StoreOp STORE 改写成 DONT_CARE",
        why: "省掉一次把 tile 写回显存的带宽开销 (只对之后不再被读的 attachment 成立)",
        touches_image: true,
    },
    Knob {
        id: "attachment_precision",
        status: KnobStatus::Declared,
        what: "降 attachment 的格式精度",
        why: "带宽与显存占用随位宽线性下降",
        touches_image: true,
    },
    Knob {
        id: "render_scale",
        status: KnobStatus::Declared,
        what: "改离屏 pass 的渲染分辨率",
        why: "片元着色开销随像素数下降 —— 这正是超分算子想要的那个前提",
        touches_image: true,
    },
    Knob {
        id: "shader_replace",
        status: KnobStatus::Declared,
        what: "用外部 SPIR-V 顶替某个 pass 的着色器",
        why: "黑盒游戏里把画面算子挂进去的唯一通道",
        touches_image: true,
    },
];

pub fn knob(id: &str) -> Option<&'static Knob> {
    CATALOG.iter().find(|k| k.id == id)
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
渲染流程改写开关目录, 以及历史上每组改写在真机游戏负载下的实测结果。你的任务是挑下一批候选改写组。\n\
硬规矩:\n\
1. 只能用目录里**已实现**的开关名, 一个字都不能改, 不许发明新开关。\n\
2. 每组 1-2 个开关。改写少一点容易归因。\n\
3. 各组之间要有明显差异。\n\
4. 这些改写会动画面, 画质是约束不是目标 —— 掉画质的改写会被一票否决, \
   所以别提「反正看不出来」这种赌运气的组合。\n\
5. 只输出 JSON, 不要解释、不要 markdown 代码块以外的任何文字。\n\
输出格式 (顶层是数组):\n\
[{\"why\":\"一句话说明这组想验证什么\",\"rewrites\":[{\"knob\":\"开关名\",\"scope\":null}]}, ...]";

/// 给出主意的一方看的开关目录 (只列已实现的)。
pub fn describe() -> String {
    CATALOG
        .iter()
        .filter(|k| k.status == KnobStatus::Landed)
        .map(|k| format!("- {} — {}。指望: {}", k.id, k.what, k.why))
        .collect::<Vec<_>>()
        .join("\n")
}

pub fn user_prompt(
    device_note: &str,
    workload_note: &str,
    history: &[super::sysparam::History],
    n: u32,
) -> String {
    let mut lines = vec![
        "# 设备与负载".to_string(),
        device_note.to_string(),
        format!("负载: {workload_note}"),
        String::new(),
        "# 判定口径 (已冻结, 你改不了)".to_string(),
        "看四样: 帧时 p95 (越小越好)、平均帧率 (越大越好)、整机功耗 (越小越好)、画质 (**约束**, 不得低于基线)。"
            .to_string(),
        "A/B 交替多轮 + 置换检验。改写一次都没命中真机的话整轮作废, 不会被当成\"没用\"。"
            .to_string(),
        String::new(),
        "# 可用的改写开关 (只能用这些)".to_string(),
        describe(),
        String::new(),
    ];
    if history.is_empty() {
        lines.push("# 历史".to_string());
        lines.push("这是第一代, 还没有任何实测结果。".to_string());
    } else {
        lines.push("# 已经试过的改写组与真机实测结果".to_string());
        for h in history {
            lines.push(format!("- {} → {} ({})", h.reason, h.verdict, h.params.len()));
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
        let Some(obj) = item.as_object() else { continue };
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

/// 校验一组改写。不认识的、层里还没实现的、重复的, 整组作废。
pub fn validate(rewrites: &[Rewrite]) -> Result<(), String> {
    if rewrites.is_empty() {
        return Err("空改写组".into());
    }
    let mut seen = BTreeSet::new();
    for r in rewrites {
        let Some(k) = knob(&r.knob) else {
            return Err(format!("{} 不在开关目录里", r.knob));
        };
        if k.status != KnobStatus::Landed {
            return Err(format!(
                "{} 契约里声明了, 但层里还没实现 —— 提了也不会命中",
                r.knob
            ));
        }
        if !seen.insert(&r.knob) {
            return Err(format!("{} 出现了两次", r.knob));
        }
    }
    Ok(())
}

/// 本地降级: 把还没试过的已实现开关各提一次。
///
/// 这一层的开关数量是个位数, 没必要随机采样 —— 穷举反而更好解释。
///
/// **必须避开历史**: 已实现的开关目前只有一个, 不看历史的话第二代会原样再提
/// 一遍同一个候选, 换回来的是一轮重复的真机 A/B 和两条一模一样的档案记录。
/// 提不出新的就回空 —— 由调用方把"已经全试过了"如实写进报告, 而不是
/// 拿重复候选把代数凑满。
pub fn local_proposals(n: usize, history: &[super::sysparam::History]) -> Vec<Proposal> {
    let tried: BTreeSet<&str> = history
        .iter()
        .flat_map(|h| h.params.keys().map(String::as_str))
        .collect();
    CATALOG
        .iter()
        .filter(|k| k.status == KnobStatus::Landed && !tried.contains(k.id))
        .take(n)
        .map(|k| Proposal {
            why: format!("本地降级产出: 单独验证 {} ({})", k.id, k.why),
            rewrites: vec![Rewrite {
                knob: k.id.to_string(),
                scope: None,
                args: None,
            }],
            origin: super::sysparam::Origin::LocalMutation,
        })
        .collect()
}

/// 出主意的一方。
pub struct Proposer<'a> {
    pub generator: Option<&'a dyn super::proposer::Complete>,
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
            let prompt = user_prompt(device_note, workload_note, history, n);
            match g.complete(SYSTEM_PROMPT, &prompt) {
                Ok(raw) => {
                    batch.llm_raw = Some(raw.clone());
                    let props = parse_proposals(&raw, n as usize);
                    if props.is_empty() {
                        batch.llm_error = Some("回包里没有可用的候选数组".into());
                    } else {
                        batch.origin = Some(super::sysparam::Origin::Llm);
                        for p in props {
                            match validate(&p.rewrites) {
                                Ok(()) => batch.accepted.push(p),
                                Err(e) => batch.rejected.push((
                                    p.rewrites
                                        .iter()
                                        .map(|r| r.knob.clone())
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
            batch.accepted = local_proposals(n as usize, history);
            batch.origin = Some(super::sysparam::Origin::LocalMutation);
        }
        batch
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 层里还没实现的开关一律拦下 —— 让它上机换回来的是一轮白跑的真机时间,
    /// 而那个"无显著差异"长得跟"这个改写没用"一模一样。
    #[test]
    fn declared_but_unimplemented_knobs_are_refused() {
        let declared: Vec<&Knob> = CATALOG
            .iter()
            .filter(|k| k.status == KnobStatus::Declared)
            .collect();
        assert!(!declared.is_empty(), "目录里该有还没落地的开关");
        for k in declared {
            let e = validate(&[Rewrite {
                knob: k.id.into(),
                scope: None,
                args: None,
            }])
            .unwrap_err();
            assert!(e.contains("还没实现"), "{e}");
        }
    }

    #[test]
    fn unknown_knobs_are_refused() {
        let e = validate(&[Rewrite {
            knob: "make_it_faster".into(),
            scope: None,
            args: None,
        }])
        .unwrap_err();
        assert!(e.contains("不在开关目录"), "{e}");
    }

    #[test]
    fn duplicate_knobs_are_refused() {
        let r = Rewrite {
            knob: "loadop_dont_care".into(),
            scope: None,
            args: None,
        };
        assert!(validate(&[r.clone(), r]).unwrap_err().contains("两次"));
    }

    #[test]
    fn a_landed_knob_passes() {
        assert!(validate(&[Rewrite {
            knob: "loadop_dont_care".into(),
            scope: None,
            args: None,
        }])
        .is_ok());
    }

    /// 只把已实现的开关给模型看 —— 目录里列了它做不到的东西, 它就会去提。
    #[test]
    fn the_catalog_shown_to_the_model_only_lists_landed_knobs() {
        let d = describe();
        assert!(d.contains("loadop_dont_care"));
        for k in CATALOG.iter().filter(|k| k.status == KnobStatus::Declared) {
            assert!(!d.contains(k.id), "目录里露出了还没落地的 {}", k.id);
        }
    }

    /// 每一个改写都会动画面, 所以这一层永远要量画质。
    #[test]
    fn every_rewrite_touches_the_image() {
        assert!(CATALOG.iter().all(|k| k.touches_image));
    }

    #[test]
    fn proposals_parse_from_a_reply() {
        let raw = "```json\n[{\"why\":\"省一次 tile 回读\",\
            \"rewrites\":[{\"knob\":\"loadop_dont_care\",\"scope\":null}]}]\n```";
        let p = parse_proposals(raw, 3);
        assert_eq!(p.len(), 1);
        assert_eq!(p[0].rewrites[0].knob, "loadop_dont_care");
        assert!(validate(&p[0].rewrites).is_ok());
    }

    #[test]
    fn garbage_replies_yield_nothing() {
        for raw in ["", "不行", "[]", "[{\"rewrites\":[]}]"] {
            assert!(parse_proposals(raw, 3).is_empty(), "{raw}");
        }
    }

    #[test]
    fn the_local_fallback_only_proposes_landed_knobs() {
        for p in local_proposals(5, &[]) {
            validate(&p.rewrites).unwrap();
        }
    }

    /// 试过的开关不再重提。已实现的开关只有一个, 不看历史的话第二代会原样
    /// 再提一遍 —— 那是一轮重复的真机 A/B 加两条一样的档案记录。
    #[test]
    fn the_local_fallback_never_repeats_a_knob_it_already_tried() {
        let first = local_proposals(3, &[]);
        assert!(!first.is_empty());
        let history: Vec<super::super::sysparam::History> = first
            .iter()
            .map(|p| super::super::sysparam::History {
                params: p
                    .rewrites
                    .iter()
                    .map(|r| (r.knob.clone(), "on".to_string()))
                    .collect(),
                verdict: "REJECT".into(),
                reason: String::new(),
                per_metric: vec![],
            })
            .collect();
        for p in local_proposals(3, &history) {
            for r in &p.rewrites {
                assert!(
                    !first.iter().any(|f| f.rewrites.iter().any(|x| x.knob == r.knob)),
                    "又提了一遍试过的 {}",
                    r.knob
                );
            }
        }
        // 已实现的开关全试过之后, 提不出新的就回空 —— 不拿重复候选凑代数。
        assert!(local_proposals(3, &history).len() < first.len());
    }

    #[test]
    fn proposer_falls_back_without_an_llm() {
        let b = Proposer { generator: None }.propose("机器", "负载", &[], 2);
        assert_eq!(b.origin, Some(super::super::sysparam::Origin::LocalMutation));
        assert!(!b.accepted.is_empty());
    }
}
