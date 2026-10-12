//! ④ 白盒引擎设置层: 有源码的游戏 (Megacity) 把引擎渲染设置做成启动参数, 每个是一个旋钮。
//!
//! 契约: `contracts/eval_request.schema.json` 的 kind=whitebox 分支; 接收端 phonefarm
//! `eval` 的 whitebox_eval (BH3GEI/phonefarm#119); 旋钮语义见 phonefarm
//! `docs/MEGACITY_WHITEBOX_KNOBS.md`。改写臂 = 对照臂的 launch.extras 后面追加 `--es <旋钮> <值>`。
//!
//! 与 ②③ 同构: 这里只管**准改什么、这一轮改成什么**, 应用 / 测量 / 判定 / 还原都在 phonefarm。

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// 一个白盒旋钮。
#[derive(Debug, Clone, Copy)]
pub struct Knob {
    pub id: &'static str,
    /// 准用的取值 (空 = 见 `free`)。
    pub values: &'static [&'static str],
    /// 取值是自由文本 (如 feat_off 的特性名子串列表) 时的说明。
    pub free: Option<&'static str>,
    /// 改了画面必然变: 接收端只量上限 (measure_only, status 恒 REJECT), 不可能被保留。
    pub measure_only: bool,
    pub what: &'static str,
    pub why: &'static str,
}

pub const CATALOG: &[Knob] = &[
    Knob {
        id: "store_opt",
        values: &["auto", "discard", "store"],
        free: None,
        measure_only: false,
        what: "URP storeActionsOptimization (写回策略)",
        why: "discard 让 URP 尽量不写回; MSAA 之后还要被读时会被迫保留, 用来验证写回是被谁逼出来的",
    },
    Knob {
        id: "fog_event",
        values: &["BeforeRenderingTransparents", "AfterRenderingTransparents", "AfterRenderingOpaques", "BeforeRenderingPostProcessing"],
        free: Some("也可以是 RenderPassEvent 的整数值"),
        measure_only: false,
        what: "体积雾特性的注入时机 (RenderPassEvent)",
        why: "雾若是把主 pass 拆开、逼 MSAA 中途解析的元凶, 挪到透明物之后可让主 pass 合并、MSAA 在片上解析 —— 几乎不损画质又去掉约 186 MB/帧的搬运",
    },
    Knob {
        id: "ssao_src",
        values: &["depth", "depthnormals"],
        free: None,
        measure_only: false,
        what: "SSAO 的输入源",
        why: "只用深度可以省掉深度+法线预渲染 (rp17, 打点约 13%); SSAO 质量略降, 靠画面比对把关",
    },
    Knob {
        id: "ssao_after_opaque",
        values: &["0", "1"],
        free: None,
        measure_only: false,
        what: "SSAO 放在不透明物之后",
        why: "配合 ssao_src=depth 省预渲染",
    },
    Knob {
        id: "depth_priming",
        values: &["off", "auto", "forced"],
        free: None,
        measure_only: false,
        what: "渲染器 depthPrimingMode",
        why: "当前是 forced, 看深度预渲染 (rp17) 有没有一部分是强制带来的; 画面不变",
    },
    Knob {
        id: "msaa",
        values: &["1", "2", "4"],
        free: None,
        measure_only: true,
        what: "MSAA 采样数 (当前 4)",
        why: "量上限: 关掉后 4×MSAA 的搬运整块消失, 告诉我们这块浪费值多少; 边缘锯齿, 只当量尺",
    },
    Knob {
        id: "feat_off",
        values: &[],
        free: Some("逗号分隔的渲染器特性名子串, 如 fog / ambientocclusion"),
        measure_only: true,
        what: "关掉渲染器特性 (诊断)",
        why: "逐个关, 看主 pass 中途的那次 MSAA 解析是谁引起的; 关雾 / SSAO 有画质代价, 只当诊断",
    },
    Knob {
        id: "shadowres",
        values: &["256", "512", "1024", "2048", "4096"],
        free: None,
        measure_only: true,
        what: "主光源阴影图分辨率 (当前 4096)",
        why: "阴影 pass 打点约 41%; 降分辨率阴影变糊, 是明确的画质取舍",
    },
];

pub fn knob(id: &str) -> Option<&'static Knob> {
    CATALOG.iter().find(|k| k.id == id)
}

/// 接收端只许 `[A-Za-z0-9_,.-]`, 长度 ≤ 64 (要拼进 am start)。
fn safe_value(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= 64
        && v.chars()
            .all(|c| c.is_ascii_alphanumeric() || "_,.-".contains(c))
}

/// 校验一组旋钮。
pub fn validate(knobs: &BTreeMap<String, String>) -> Result<(), String> {
    if knobs.is_empty() {
        return Err("空旋钮组".into());
    }
    if knobs.len() > 3 {
        return Err(format!(
            "一组最多 3 个旋钮 (好归因), 这组 {} 个",
            knobs.len()
        ));
    }
    for (k, v) in knobs {
        let Some(spec) = knob(k) else {
            return Err(format!("{k} 不在白盒旋钮目录里"));
        };
        if !safe_value(v) {
            return Err(format!("{k}={v:?}: 取值只许字母数字和 _,.- 且不超过 64 字"));
        }
        let ok = spec.values.contains(&v.as_str())
            || (k == "fog_event" && v.parse::<i64>().is_ok())
            || (k == "feat_off" && !v.is_empty());
        if !ok {
            return Err(format!("{k}={v} 不在准用的取值里 ({:?})", spec.values));
        }
    }
    Ok(())
}

/// 这组里有没有「改了画面必然变」的旋钮 (接收端只量上限, 不可能被保留)。
pub fn is_measure_only(knobs: &BTreeMap<String, String>) -> bool {
    knobs
        .keys()
        .any(|k| knob(k).is_some_and(|s| s.measure_only))
}

/// 一组旋钮的标签 (历史、档案、去重)。
pub fn label(knobs: &BTreeMap<String, String>) -> String {
    knobs
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join(",")
}

/// 给出主意的一方看的目录。
pub fn describe() -> String {
    CATALOG
        .iter()
        .map(|k| {
            format!(
                "- {} ∈ {}{} — {}。{}{}",
                k.id,
                if k.values.is_empty() {
                    "(自由)".to_string()
                } else {
                    format!("{:?}", k.values)
                },
                k.free.map(|f| format!(" ({f})")).unwrap_or_default(),
                k.what,
                k.why,
                if k.measure_only {
                    " 【只量上限: 改了画面必然变, 永远不会被保留, 只在想知道上限时用】"
                } else {
                    ""
                }
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub const SYSTEM_PROMPT: &str = "你是 Unity URP 移动端渲染优化专家。给你一份可以通过启动参数改的引擎渲染设置旋钮目录, \
以及历史上每组设置在真机 (同一段确定性飞行路线) 下的实测结果。挑下一批候选设置组。\n\
硬规矩:\n\
1. 只能用目录里的旋钮名和准用取值, 一个字符都不能改。\n\
2. 每组 1-2 个旋钮 (最多 3), 好归因。各组之间要有明显差异。\n\
3. 画面是约束: 每组都会和对照在静态场景逐像素比, 变了直接作废。标了「只量上限」的旋钮改了画面必然变, \
   永远不会被保留 —— 除非想知道某块浪费的上限, 否则别提。\n\
4. 只输出 JSON 数组, 不要解释。\n\
输出格式: [{\"why\":\"一句话\",\"knobs\":{\"旋钮\":\"值\"}}, ...]";

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
        "# 判定口径 (已冻结)".to_string(),
        "帧时 p95、平均帧率、整机功耗; A/B 交替多轮 + 置换检验。画面是约束 (静态场景逐像素比)。旋钮没生效 (回读对不上) 整组作废, 不算负样本。".to_string(),
        String::new(),
        "# 白盒旋钮目录".to_string(),
        describe(),
        String::new(),
    ];
    if history.is_empty() {
        lines.push("# 历史\n这是第一代。".into());
    } else {
        lines.push("# 已经试过的设置组与真机结果".into());
        for h in history {
            lines.push(format!("- {} → {}", h.reason, h.verdict));
            for m in &h.per_metric {
                lines.push(format!("  {m}"));
            }
        }
    }
    lines.push(format!("\n# 现在给我 {n} 组候选, 只输出 JSON 数组。"));
    lines.join("\n")
}

/// 一个候选。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Proposal {
    pub why: String,
    pub knobs: BTreeMap<String, String>,
    pub origin: super::sysparam::Origin,
}

#[derive(Debug, Clone, Default)]
pub struct ProposalBatch {
    pub accepted: Vec<Proposal>,
    pub rejected: Vec<(String, String)>,
    pub origin: Option<super::sysparam::Origin>,
    pub llm_error: Option<String>,
    pub llm_raw: Option<String>,
}

pub fn parse_proposals(text: &str, n: usize) -> Vec<Proposal> {
    let s = text.trim();
    let (Some(a), Some(b)) = (s.find('['), s.rfind(']')) else {
        return vec![];
    };
    if b <= a {
        return vec![];
    }
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&s[a..=b]) else {
        return vec![];
    };
    let mut out = Vec::new();
    for item in v.as_array().into_iter().flatten() {
        let Some(obj) = item["knobs"].as_object() else {
            continue;
        };
        let knobs: BTreeMap<String, String> = obj
            .iter()
            .filter_map(|(k, v)| {
                let v = match v {
                    serde_json::Value::String(s) => s.clone(),
                    serde_json::Value::Number(n) => n.to_string(),
                    serde_json::Value::Bool(b) => {
                        if *b {
                            "1".into()
                        } else {
                            "0".into()
                        }
                    }
                    _ => return None,
                };
                Some((k.clone(), v))
            })
            .collect();
        if knobs.is_empty() {
            continue;
        }
        out.push(Proposal {
            why: item["why"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(300)
                .collect(),
            knobs,
            origin: super::sysparam::Origin::Llm,
        });
        if out.len() >= n {
            break;
        }
    }
    out
}

/// 本地降级: 只提画面不该变的旋钮, 每个非缺省取值单独试一次, 试过的组合不再提。
pub fn local_proposals(n: usize, history: &[super::sysparam::History]) -> Vec<Proposal> {
    let tried: Vec<BTreeSet<String>> = history
        .iter()
        .map(|h| h.params.keys().cloned().collect())
        .collect();
    // 当前出包的缺省值 (docs/MEGACITY_WHITEBOX_KNOBS.md §1): 试它等于拿 A 和 A 比
    let default =
        |k: &str, v: &str| matches!((k, v), ("store_opt", "auto") | ("depth_priming", "forced"));
    let mut out = Vec::new();
    for k in CATALOG.iter().filter(|k| !k.measure_only) {
        for v in k.values {
            if default(k.id, v) {
                continue;
            }
            let knobs = BTreeMap::from([(k.id.to_string(), v.to_string())]);
            let set: BTreeSet<String> = [format!("{}={}", k.id, v)].into_iter().collect();
            if tried.contains(&set) {
                continue;
            }
            out.push(Proposal {
                why: format!("本地降级产出: 单独试 {}={} ({})", k.id, v, k.what),
                knobs,
                origin: super::sysparam::Origin::LocalMutation,
            });
            if out.len() >= n {
                return out;
            }
        }
    }
    out
}

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
            match g.complete(
                SYSTEM_PROMPT,
                &user_prompt(device_note, workload_note, history, n),
            ) {
                Ok(raw) => {
                    batch.llm_raw = Some(raw.clone());
                    let props = parse_proposals(&raw, n as usize);
                    if props.is_empty() {
                        batch.llm_error = Some("回包里没有可用的候选数组".into());
                    } else {
                        batch.origin = Some(super::sysparam::Origin::Llm);
                        for p in props {
                            match validate(&p.knobs) {
                                Ok(()) => batch.accepted.push(p),
                                Err(e) => batch.rejected.push((label(&p.knobs), e)),
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
                    "大模型回了 {} 个候选, 全部没过白盒旋钮目录校验 (见 rejected_locally)",
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

    fn m(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn values_are_checked_per_knob() {
        assert!(validate(&m(&[("store_opt", "discard")])).is_ok());
        assert!(validate(&m(&[
            ("fog_event", "AfterRenderingTransparents"),
            ("ssao_src", "depth")
        ]))
        .is_ok());
        assert!(validate(&m(&[("fog_event", "550")])).is_ok());
        assert!(validate(&m(&[("feat_off", "fog,ambientocclusion")])).is_ok());
        for bad in [
            m(&[]),
            m(&[("resscale", "0.5")]),
            m(&[("msaa", "8")]),
            m(&[("store_opt", "maybe")]),
            m(&[("feat_off", "fog; rm -rf /")]),
            m(&[
                ("store_opt", "discard"),
                ("msaa", "1"),
                ("shadowres", "1024"),
                ("ssao_src", "depth"),
            ]),
        ] {
            assert!(validate(&bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn measure_only_knobs_are_flagged() {
        assert!(is_measure_only(&m(&[("msaa", "1")])));
        assert!(!is_measure_only(&m(&[("store_opt", "discard")])));
        assert!(describe().contains("只量上限"));
    }

    #[test]
    fn local_fallback_skips_defaults_measure_only_and_history() {
        let first = local_proposals(20, &[]);
        assert!(!first.is_empty());
        for p in &first {
            validate(&p.knobs).unwrap();
            assert!(!is_measure_only(&p.knobs));
            assert_ne!(label(&p.knobs), "store_opt=auto");
            assert_ne!(label(&p.knobs), "depth_priming=forced");
        }
        let h = vec![super::super::sysparam::History {
            params: [(label(&first[0].knobs), "on".to_string())]
                .into_iter()
                .collect(),
            verdict: "REJECT".into(),
            reason: String::new(),
            per_metric: vec![],
        }];
        let next = local_proposals(20, &h);
        assert_eq!(next.len(), first.len() - 1);
        assert!(next.iter().all(|p| p.knobs != first[0].knobs));
    }

    #[test]
    fn replies_parse_with_numbers_and_strings() {
        let p = parse_proposals(
            r#"[{"why":"挪雾","knobs":{"fog_event":"AfterRenderingTransparents","ssao_after_opaque":1}}]"#,
            3,
        );
        assert_eq!(p[0].knobs["ssao_after_opaque"], "1");
        assert!(validate(&p[0].knobs).is_ok());
        assert!(parse_proposals("不行", 3).is_empty());
    }
}
