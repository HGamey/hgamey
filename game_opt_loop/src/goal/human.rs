//! human.md: 人的掌舵指令 (大白话)。能机读的转成覆盖, 其余原样交给出方案的一方。

use serde::{Deserialize, Serialize};

/// 从 human.md 读出来的东西。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Steer {
    /// 「停止 / stop」: 本代结束后停 (在代与代之间读到就是马上停)。
    pub stop: bool,
    /// 「暂停」: 等 human.md 再改才继续。
    pub pause: bool,
    /// 「预算 +N 分钟」累加。
    pub budget_add_min: i64,
    /// 「不要动 X」。
    pub forbid: Vec<String>,
    /// 「只试 Y」, Y 是层名时 → 只跑这些层。
    pub only_layers: Option<Vec<String>>,
    /// 机读不了的行 (原文), 交给出方案的一方作为最高优先级偏好。
    pub free_text: String,
}

fn layer_of(word: &str) -> Option<&'static str> {
    let w = word.trim().to_lowercase();
    match w.as_str() {
        "sysparam" | "系统参数" | "③" | "3" => Some("sysparam"),
        "gray" | "画面流程" | "画面流程改写" | "灰档" | "②" | "2" => Some("gray"),
        "whitebox" | "白盒" | "引擎设置" | "④" | "4" => Some("whitebox"),
        "upop" | "画面算子" | "①" | "1" => Some("upop"),
        _ => None,
    }
}

fn first_int(s: &str) -> Option<i64> {
    let digits: String = s
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

/// 中文否定词：关键词前面贴着这些字，意思就是反的（「无暂停」「不要停止」「取消暂停」）。
const NEGATIONS: [&str; 6] = ["无", "不", "别", "勿", "取消", "解除"];

/// `kw`（暂停/停止）在这行里是不是被否定了。看关键词前 3 个字符内有没有否定词。
fn negated(line: &str, kw: &str) -> bool {
    let Some(i) = line.find(kw) else { return false };
    let tail: Vec<char> = line[..i].chars().rev().take(3).collect();
    let before: String = tail.into_iter().rev().collect();
    NEGATIONS.iter().any(|n| before.contains(n))
}

/// 第一行结构化指令：`action: go|pause|stop`（也认 `指令: 继续|暂停|停止`）。
/// 写了就是权威：body 里再出现「暂停/停止」字样也不再按关键词猜，只走其他覆盖解析。
/// 返回 (action, body)。
fn split_action(text: &str) -> (Option<&str>, &str) {
    for (idx, raw) in text.lines().enumerate() {
        let line = raw.trim().trim_start_matches(['-', '*', '•']).trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let low = line.to_lowercase();
        for pre in ["action:", "action：", "指令:", "指令："] {
            if let Some(v) = low.strip_prefix(pre).map(str::trim) {
                let body_start = text.lines().take(idx + 1).map(|l| l.len() + 1).sum::<usize>();
                let action = match v {
                    "go" | "continue" | "run" | "继续" | "正常推进" | "放行" => Some("go"),
                    "pause" | "hold" | "暂停" => Some("pause"),
                    "stop" | "halt" | "停止" => Some("stop"),
                    _ => None,
                };
                if action.is_some() {
                    return (action, text.get(body_start..).unwrap_or(""));
                }
            }
        }
        // 第一条有效行不是 action 指令 → 全文走兼容路径
        return (None, text);
    }
    (None, text)
}

/// 读 human.md。第一行是 `action: go|pause|stop`（或 `指令: 继续|暂停|停止`）时以它为准；
/// 没写则按行认关键词（带否定词防护：「无暂停」「不要停止」不再误判）。认得的转成覆盖, 认不得的进 free_text。
pub fn parse(text: &str) -> Steer {
    let mut s = Steer::default();
    let (action, body) = split_action(text);
    let mut free = Vec::new();
    for raw in body.lines() {
        let line = raw.trim().trim_start_matches(['-', '*', '•']).trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let low = line.to_lowercase();
        if action.is_none() && line.contains("暂停") && !negated(line, "暂停") || (action.is_none() && low == "pause") {
            s.pause = true;
        } else if action.is_none() && line.contains("停止") && !negated(line, "停止")
            || (action.is_none() && (low == "stop" || low.starts_with("stop "))) {
            s.stop = true;
        } else if line.contains("预算") && (line.contains('+') || line.contains("加")) {
            match first_int(line) {
                Some(n) if line.contains("小时") => s.budget_add_min += n * 60,
                Some(n) => s.budget_add_min += n,
                None => free.push(line.to_string()),
            }
        } else if let Some(rest) = line
            .strip_prefix("不要动")
            .or_else(|| line.strip_prefix("别动"))
            .or_else(|| line.strip_prefix("禁止"))
        {
            let items: Vec<String> = rest
                .split([',', '，', '、', ' '])
                .map(|x| x.trim().trim_matches(['`', '"', '“', '”', '。']).to_string())
                .filter(|x| !x.is_empty())
                .collect();
            if items.is_empty() {
                free.push(line.to_string());
            }
            s.forbid.extend(items);
        } else if let Some(rest) = line.strip_prefix("只试").or_else(|| line.strip_prefix("只跑")) {
            let words: Vec<&str> = rest
                .split([',', '，', '、', ' ', '和'])
                .map(|x| x.trim().trim_matches(['。', '`']))
                .filter(|x| !x.is_empty())
                .collect();
            let layers: Vec<String> = words.iter().filter_map(|w| layer_of(w)).map(String::from).collect();
            if !words.is_empty() && layers.len() == words.len() {
                s.only_layers = Some(layers);
            } else {
                // 「只试某个旋钮」机读不了 (不知道它属于哪层、范围是什么), 原样交给出方案的一方
                free.push(line.to_string());
            }
        } else {
            free.push(line.to_string());
        }
    }
    match action {
        Some("pause") => s.pause = true,
        Some("stop") => s.stop = true,
        _ => {}
    }
    s.free_text = free.join("\n");
    s
}

impl Steer {
    /// harness 怎么理解的 (一句话, 进 steer 事件)。
    pub fn interpreted(&self) -> String {
        let mut parts = Vec::new();
        if self.stop {
            parts.push("停止: 这一代结束后停".to_string());
        }
        if self.pause {
            parts.push("暂停: 等掌舵指令再改才继续".to_string());
        }
        if self.budget_add_min != 0 {
            parts.push(format!("预算 {:+} 分钟", self.budget_add_min));
        }
        if !self.forbid.is_empty() {
            parts.push(format!("禁止动 {}", self.forbid.join(", ")));
        }
        if let Some(l) = &self.only_layers {
            parts.push(format!("只跑 {}", l.join("+")));
        }
        if !self.free_text.is_empty() {
            parts.push("其余的话原样转给出主意的一方, 优先照办".to_string());
        }
        if parts.is_empty() {
            "没有指令".into()
        } else {
            parts.join("; ")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn machine_readable_lines_become_overrides() {
        let s = parse(
            "# 注释\n预算 +30 分钟\n不要动 gpu.min_pwrlevel、bus.DDR.boost_freq\n只试 系统参数\n优先试降 CPU 大核上限\n",
        );
        assert_eq!(s.budget_add_min, 30);
        assert_eq!(s.forbid, vec!["gpu.min_pwrlevel", "bus.DDR.boost_freq"]);
        assert_eq!(s.only_layers, Some(vec!["sysparam".to_string()]));
        assert_eq!(s.free_text, "优先试降 CPU 大核上限");
        assert!(!s.stop && !s.pause);
        assert!(s.interpreted().contains("预算 +30"));
    }

    #[test]
    fn stop_and_pause() {
        assert!(parse("停止").stop);
        assert!(parse("stop").stop);
        let p = parse("先暂停一下");
        assert!(p.pause && !p.stop);
        assert!(parse("预算 +1 小时").budget_add_min == 60);
    }

    #[test]
    fn only_a_knob_is_not_machine_readable() {
        let s = parse("只试 storeop_dont_care@15a51a879df99c32:2");
        assert_eq!(s.only_layers, None);
        assert!(s.free_text.contains("storeop"));
    }

    #[test]
    fn empty_means_no_instruction() {
        assert_eq!(parse("\n# 在这里写\n").interpreted(), "没有指令");
    }

    #[test]
    fn action_line_is_authoritative() {
        // 结构化指令行：body 里的「停止/暂停」字样不再被关键词猜中
        let s = parse("action: go\n这次不要停止得太早，也别暂停测量\n");
        assert!(!s.stop && !s.pause);
        assert!(s.free_text.contains("不要停止得太早"));
        assert!(parse("action: pause\n正常推进\n").pause);
        assert!(parse("action: stop\n").stop);
        assert!(parse("指令：暂停\n").pause);
        assert!(!parse("指令: 继续\n先暂停一下\n").pause);
    }

    #[test]
    fn negation_is_not_pause_or_stop() {
        // 兼容路径的否定词防护（今天的事故：「无暂停」被当成「暂停」）
        assert!(!parse("无暂停，正常推进").pause);
        assert!(!parse("解除暂停，直接开始第 1 代").pause);
        assert!(!parse("取消暂停").pause);
        assert!(!parse("不要停止").stop);
        assert!(!parse("别停止").stop);
        // 真指令照样认
        assert!(parse("先暂停一下").pause);
        assert!(parse("停止").stop);
    }
}
