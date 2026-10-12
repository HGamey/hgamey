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

/// 读 human.md。按行认: 认得的转成覆盖, 认不得的进 free_text。
pub fn parse(text: &str) -> Steer {
    let mut s = Steer::default();
    let mut free = Vec::new();
    for raw in text.lines() {
        let line = raw.trim().trim_start_matches(['-', '*', '•']).trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let low = line.to_lowercase();
        if line.contains("暂停") || low == "pause" {
            s.pause = true;
        } else if line.contains("停止") || low == "stop" || low.starts_with("stop ") {
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
                .map(|x| {
                    x.trim()
                        .trim_matches(['`', '"', '“', '”', '。'])
                        .to_string()
                })
                .filter(|x| !x.is_empty())
                .collect();
            if items.is_empty() {
                free.push(line.to_string());
            }
            s.forbid.extend(items);
        } else if let Some(rest) = line
            .strip_prefix("只试")
            .or_else(|| line.strip_prefix("只跑"))
        {
            let words: Vec<&str> = rest
                .split([',', '，', '、', ' ', '和'])
                .map(|x| x.trim().trim_matches(['。', '`']))
                .filter(|x| !x.is_empty())
                .collect();
            let layers: Vec<String> = words
                .iter()
                .filter_map(|w| layer_of(w))
                .map(String::from)
                .collect();
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
}
