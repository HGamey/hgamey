//! 游戏名 → 被测目标。
//!
//! `optimize --game <名>` 收到的是人写的名字 ("原神" / "genshin" / "refbench"),
//! 下面的一切都需要包名、载体、负载脚本。这个模块负责把前者变成后者, 并且在
//! 变不出来的时候**说清楚缺什么**, 而不是猜一个包名接着往下跑 —— 猜错包名的
//! 代价是几十分钟真机时间量了另一个应用。
//!
//! 白盒 / 黑盒的判定不靠这张表拍板, 靠**文件系统上有没有那个载体**:
//! `../phonefarm/loop_v1/carriers/<名>/contract/launch.json` 或 `../refbench/contract/launch.json`。
//! 载体是会被人加进来的, 写死一张表会让新载体永远进不了自动开路。

use serde::{Deserialize, Serialize};
use std::borrow::Cow;
use std::path::{Path, PathBuf};

/// 被测目标是白盒还是黑盒。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BoxKind {
    /// 有第一方载体: 源码在手, 算子可以直接挂进它的管线, 画质有确定的真值。
    WhiteBox,
    /// 第三方游戏: 只能从外面改 (系统参数 / Vulkan layer 注入)。
    BlackBox,
}

/// 负载的输入方式。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputMethod {
    /// 触控事件。
    Touch,
    /// 手柄摇杆 / 按键。
    Gamepad,
    /// 载体自己跑完就退出, 不需要外部输入。
    SelfDriven,
}

/// 一条已知的坑。随计划与报告一起打印 —— 写在 SKILL.md 里的坑, 如果不在
/// 运行时也说一遍, 下一个人照样会踩。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Pitfall {
    pub id: Cow<'static, str>,
    /// 现象: 不说清现象, 读的人分不出「这条适用于我」还是「这条是别人的事」。
    pub symptom: Cow<'static, str>,
    /// 怎么办。
    pub workaround: Cow<'static, str>,
}

pub const GENSHIN_GAMEPAD: Pitfall = Pitfall {
    id: Cow::Borrowed("genshin_gamepad_dead_7_1_0"),
    symptom: Cow::Borrowed("原神 7.1.0 起手柄输入失效, 摇杆推不动视角 —— 负载看着在跑, 画面其实没动"),
    workaround: Cow::Borrowed("改用触控负载 (phonefarm knobs/gray/workload_spin_touch_v1.json)"),
};

pub const GENSHIN_LOGIN_GATE: Pitfall = Pitfall {
    id: Cow::Borrowed("genshin_login_gate"),
    symptom: Cow::Borrowed("冷启动停在登录页, 负载脚本照常跑完, 量到的是登录页而不是大世界"),
    workaround: Cow::Borrowed("开跑前确认已在大世界探索态; 自动化进门只点登录按钮, 别乱点"),
};

pub const GENSHIN_NO_BACK_NO_A: Pitfall = Pitfall {
    id: Cow::Borrowed("genshin_no_back_no_a"),
    symptom: Cow::Borrowed("负载里按 BACK 或 A 会开箱子 / 触发对话 / 退出场景, 直接毁掉可重复性"),
    workaround: Cow::Borrowed("负载只推右摇杆 (或等效触控转视角), 不按任何功能键"),
};

pub const GENSHIN_FPS_SETTING: Pitfall = Pitfall {
    id: Cow::Borrowed("ingame_fps_setting_unrecorded"),
    symptom: Cow::Borrowed("游戏内帧率/画质设置改了, 之前所有基线全部作废, 而报告里看不出来"),
    workaround: Cow::Borrowed("每轮把游戏内帧率设置记进 conditions.ingame_fps_setting; 量不到就如实写 null"),
};

pub const CHARGING_POWER_INVALID: Pitfall = Pitfall {
    id: Cow::Borrowed("charging_power_invalid"),
    symptom: Cow::Borrowed("充电态下电池轨量到的是充放相抵的余量, USB 轨含着灌进电池的那一份, 两者都不是整机功耗"),
    workaround: Cow::Borrowed("开跑前停充 (phonefarm 的 charge ctl 节点表); 停不掉就让 power_watt 如实留 null, 不拿假数字比"),
};

pub const FAN_STATE_UNRECORDED: Pitfall = Pitfall {
    id: Cow::Borrowed("fan_state_unrecorded"),
    symptom: Cow::Borrowed("挂不挂外置风扇, 同一台机器稳态温度差十几度, 帧时与功耗跨风扇状态不可比"),
    workaround: Cow::Borrowed("每轮把风扇状态记进 conditions.fan; 量不到写 unknown, 别猜"),
};

/// 一条内置的游戏条目。表里只放"名字 → 包名 + 负载 + 已知坑"这类知识,
/// 白盒/黑盒仍由载体是否存在决定。
struct Known {
    aliases: &'static [&'static str],
    display: &'static str,
    package: &'static str,
    /// 载体目录名 (相对 carriers/), 或 `refbench` 这种同级仓库。None = 黑盒游戏。
    carrier: Option<&'static str>,
    script: Option<&'static str>,
    seconds: u32,
    input: InputMethod,
    pitfalls: &'static [Pitfall],
    launch: Option<KnownLaunch>,
    render_comm: Option<&'static str>,
}

const KNOWN: &[Known] = &[
    Known {
        aliases: &["原神", "genshin", "yuanshen", "com.miHoYo.Yuanshen"],
        display: "原神",
        package: "com.miHoYo.Yuanshen",
        carrier: None,
        // 定点匀速转视角。手柄在 7.1.0 起失效, 所以缺省就是触控版。
        script: Some("../phonefarm/knobs/gray/workload_spin_touch_v1.json"),
        seconds: 36,
        input: InputMethod::Touch,
        pitfalls: &[
            GENSHIN_GAMEPAD,
            GENSHIN_LOGIN_GATE,
            GENSHIN_NO_BACK_NO_A,
            GENSHIN_FPS_SETTING,
            CHARGING_POWER_INVALID,
            FAN_STATE_UNRECORDED,
        ],
        launch: None,
        render_comm: None,
    },
    Known {
        // Megacity (Unity 官方样例, 有源码的真游戏): 带 extras 启动后相机沿固定路线穿城飞行,
        // 帧序钉死, frame_p95 离散度 ~1% (phonefarm loop_v1/carriers/megacity)。对 optimize
        // 来说它是黑盒 —— ② 照样靠灰档层注入, 不改它的源码。
        aliases: &["megacity", "megacity-metro", "com.unity.megacity.metro"],
        display: "Megacity",
        package: "com.unity.megacity.metro",
        carrier: None,
        script: None,
        seconds: 30,
        input: InputMethod::SelfDriven,
        pitfalls: &[CHARGING_POWER_INVALID, FAN_STATE_UNRECORDED],
        launch: Some(KnownLaunch {
            extras: "--es scene city_flythrough --es run_id hgamey --es frames 3600 --es route route_a \
                     --es warmup 600 --es settle 7200 --es resscale 1.0 --es vsync off",
            marker: Some("megacity_started"),
            static_extras: Some(
                "--es scene city_static --es run_id hgamey_static --es frames 100000 --es route route_a \
                 --es warmup 600 --es settle 7200 --es resscale 1.0 --es vsync off",
            ),
        }),
        // 载体自报并固化进 contract (https://github.com/HGamey/docs/blob/main/repos/phonefarm/loop_v1/carriers/megacity/README.md)
        render_comm: Some("UnityGfxDeviceW"),
    },
    Known {
        aliases: &["refbench", "io.github.hgamey.refbench", "白盒靶场"],
        display: "refbench (白盒靶场)",
        package: "io.github.hgamey.refbench",
        carrier: Some("refbench"),
        script: None,
        seconds: 0,
        input: InputMethod::SelfDriven,
        pitfalls: &[CHARGING_POWER_INVALID, FAN_STATE_UNRECORDED],
        launch: None,
        render_comm: None,
    },
];

/// 载体的落点。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CarrierRef {
    pub name: String,
    /// 本地目录 (相对当前工作目录)。
    pub dir: String,
    /// 从载体自己的 `contract/launch.json` 读到的包名; None = 契约里没写。
    pub package: Option<String>,
    /// 契约文件路径 —— 证据里要能看出白盒结论是凭什么下的。
    pub contract: String,
}

/// 负载描述。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Workload {
    pub script: Option<String>,
    pub seconds: u32,
    pub input: InputMethod,
    /// 脚本文件在不在。不在就不能声称"负载已就绪"。
    pub script_present: bool,
    /// 渲染线程名 (ftrace comm)。None = 按 Unity 缺省 (UnityGfxDeviceW) 解析;
    /// 非 Unity 游戏由游戏档案给出, 否则解析出零帧。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub render_comm: Option<String>,
    /// 自驱动负载: 应用带 intent extras 启动后自己跑确定性路线 (Megacity city_flythrough)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub launch: Option<Launch>,
}

/// 自驱动负载的启动方式 (原样进评测请求 workload.launch)。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Launch {
    /// 测量用的 intent extras。
    pub extras: String,
    /// 应用进入被测窗口时在自己 files/ 下落的标记文件名。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub marker: Option<String>,
    /// 画面比对用的静态场景 extras (相机不动)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub static_extras: Option<String>,
}

struct KnownLaunch {
    extras: &'static str,
    marker: Option<&'static str>,
    static_extras: Option<&'static str>,
}

/// 解析出来的被测目标。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Target {
    /// 用户敲进来的原文。
    pub input: String,
    pub display: String,
    pub package: Option<String>,
    pub box_kind: BoxKind,
    pub carrier: Option<CarrierRef>,
    pub workload: Workload,
    pub pitfalls: Vec<Pitfall>,
    /// 名字是怎么认出来的 —— 内置表命中, 还是当成包名/载体名直接用。
    pub resolved_via: String,
    /// 用上的游戏档案 (`phonefarm onboard` 实测出来的, 见 `profile.rs`)。
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub profile: Option<super::profile::ProfileRef>,
}

/// 载体的搜索根。默认 `../phonefarm/loop_v1/carriers` 与 `..`(找同级 refbench)。
#[derive(Debug, Clone)]
pub struct CarrierRoots {
    pub carriers_dir: PathBuf,
    pub siblings_dir: PathBuf,
}

impl Default for CarrierRoots {
    fn default() -> Self {
        Self {
            carriers_dir: PathBuf::from("../phonefarm/loop_v1/carriers"),
            siblings_dir: PathBuf::from(".."),
        }
    }
}

impl CarrierRoots {
    /// 找一个叫 `name` 的白盒载体。
    ///
    /// 两个落点: `carriers/<name>/contract/launch.json` (phonefarm 收的第三方载体),
    /// 与 `../<name>/contract/launch.json` (同级仓库, refbench 就在这)。
    pub fn find(&self, name: &str) -> Option<CarrierRef> {
        for dir in [self.carriers_dir.join(name), self.siblings_dir.join(name)] {
            let contract = dir.join("contract").join("launch.json");
            if contract.is_file() {
                return Some(CarrierRef {
                    name: name.to_string(),
                    dir: dir.display().to_string(),
                    package: read_carrier_package(&contract),
                    contract: contract.display().to_string(),
                });
            }
        }
        None
    }

    /// 列出所有认得出来的白盒载体 (给"这个名字我不认识"时的提示用)。
    pub fn list(&self) -> Vec<String> {
        let mut out = Vec::new();
        for root in [&self.carriers_dir, &self.siblings_dir] {
            let Ok(entries) = std::fs::read_dir(root) else {
                continue;
            };
            for e in entries.flatten() {
                let p = e.path();
                if p.join("contract").join("launch.json").is_file() {
                    if let Some(n) = p.file_name().and_then(|s| s.to_str()) {
                        if !out.iter().any(|x| x == n) {
                            out.push(n.to_string());
                        }
                    }
                }
            }
        }
        out.sort();
        out
    }
}

/// 从载体契约里读包名。读不出来就是 None —— 不拿目录名当包名。
fn read_carrier_package(contract: &Path) -> Option<String> {
    let raw = std::fs::read_to_string(contract).ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("package")
        .and_then(|p| p.as_str())
        .map(str::to_string)
}

/// 看着像不像一个安卓包名。
///
/// 用来决定"这个不认识的名字能不能当包名直接用"。判据只有两条: 有点、
/// 每一段都是合法标识符。宁可判不像 (退回报错要求 `--package`),
/// 也不要把 "megacity-metro" 这种载体名当成包名发下去。
pub fn looks_like_package(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    parts.len() >= 2
        && parts.iter().all(|p| {
            !p.is_empty()
                && p.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
                && p.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
        })
}

/// 名字 → 目标。
///
/// `package_override` 是命令行给的包名, 给了就压过一切推断。
pub fn resolve(name: &str, package_override: Option<&str>, roots: &CarrierRoots) -> Target {
    let needle = name.trim();
    let lower = needle.to_ascii_lowercase();

    if let Some(k) = KNOWN
        .iter()
        .find(|k| k.aliases.iter().any(|a| a.eq_ignore_ascii_case(&lower) || *a == needle))
    {
        let carrier = k.carrier.and_then(|c| roots.find(c));
        let box_kind = if carrier.is_some() {
            BoxKind::WhiteBox
        } else {
            BoxKind::BlackBox
        };
        // 自驱动负载不需要回放脚本: 启动方式本身就是负载
        let script_present = k.launch.is_some() || k.script.is_some_and(|s| Path::new(s).is_file());
        return Target {
            input: needle.to_string(),
            display: k.display.to_string(),
            package: Some(
                package_override
                    .unwrap_or(k.package)
                    .to_string(),
            ),
            box_kind,
            carrier,
            workload: Workload {
                script: k.script.map(str::to_string),
                seconds: k.seconds,
                input: k.input,
                script_present,
                render_comm: k.render_comm.map(str::to_string),
                launch: k.launch.as_ref().map(|l| Launch {
                    extras: l.extras.split_whitespace().collect::<Vec<_>>().join(" "),
                    marker: l.marker.map(str::to_string),
                    static_extras: l.static_extras.map(|e| e.split_whitespace().collect::<Vec<_>>().join(" ")),
                }),
            },
            pitfalls: k.pitfalls.to_vec(),
            resolved_via: format!("内置条目 `{}`", k.aliases[0]),
            profile: None,
        };
    }

    // 不在内置表里: 看它是不是一个白盒载体的名字。
    if let Some(carrier) = roots.find(needle) {
        let package = package_override
            .map(str::to_string)
            .or_else(|| carrier.package.clone());
        return Target {
            input: needle.to_string(),
            display: format!("{needle} (白盒载体)"),
            package,
            box_kind: BoxKind::WhiteBox,
            carrier: Some(carrier),
            workload: Workload {
                script: None,
                seconds: 0,
                input: InputMethod::SelfDriven,
                script_present: true,
                render_comm: None,
                launch: None,
            },
            pitfalls: vec![CHARGING_POWER_INVALID, FAN_STATE_UNRECORDED],
            resolved_via: "载体目录里有 contract/launch.json".to_string(),
            profile: None,
        };
    }

    // 剩下的只能当黑盒。包名要么是命令行给的, 要么名字本身就长得像包名。
    let package = package_override
        .map(str::to_string)
        .or_else(|| looks_like_package(needle).then(|| needle.to_string()));
    let resolved_via = if package.is_some() {
        "不在内置表里, 按黑盒处理".to_string()
    } else {
        "不认识这个名字".to_string()
    };
    Target {
        input: needle.to_string(),
        display: needle.to_string(),
        package,
        box_kind: BoxKind::BlackBox,
        carrier: None,
        workload: Workload {
            script: None,
            seconds: 36,
            input: InputMethod::Touch,
            script_present: false,
            render_comm: None,
            launch: None,
        },
        pitfalls: vec![CHARGING_POWER_INVALID, FAN_STATE_UNRECORDED],
        resolved_via,
        profile: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roots() -> CarrierRoots {
        CarrierRoots {
            carriers_dir: PathBuf::from("/nonexistent/carriers"),
            siblings_dir: PathBuf::from("/nonexistent/siblings"),
        }
    }

    #[test]
    fn genshin_resolves_to_its_package_and_is_blackbox() {
        for name in ["原神", "genshin", "GENSHIN", "com.miHoYo.Yuanshen"] {
            let t = resolve(name, None, &roots());
            assert_eq!(t.package.as_deref(), Some("com.miHoYo.Yuanshen"), "{name}");
            assert_eq!(t.box_kind, BoxKind::BlackBox, "{name}");
        }
    }

    /// 手柄那条坑必须跟着原神一起出现 —— 这是 7.1.0 之后负载失效的唯一原因,
    /// 不随目标打印出来的话, 下一个人还会拿手柄负载跑一遍空转。
    #[test]
    fn genshin_carries_the_gamepad_pitfall() {
        let t = resolve("原神", None, &roots());
        assert!(t.pitfalls.iter().any(|p| p.id == GENSHIN_GAMEPAD.id));
        assert_eq!(t.workload.input, InputMethod::Touch);
    }

    /// 包名覆盖压过内置表 —— 同一个游戏可能有国服/国际服不同包名。
    #[test]
    fn explicit_package_wins() {
        let t = resolve("原神", Some("com.miHoYo.GenshinImpact"), &roots());
        assert_eq!(t.package.as_deref(), Some("com.miHoYo.GenshinImpact"));
    }

    /// 载体是文件系统的函数, 不是写死的表: 目录在就白盒, 不在就不白盒。
    #[test]
    fn whitebox_comes_from_the_carrier_contract_on_disk() {
        let tmp = std::env::temp_dir().join(format!("gol_games_{}", std::process::id()));
        let dir = tmp.join("myrig").join("contract");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("launch.json"),
            r#"{"contract_version":1,"package":"io.example.myrig"}"#,
        )
        .unwrap();

        let r = CarrierRoots {
            carriers_dir: tmp.clone(),
            siblings_dir: PathBuf::from("/nonexistent"),
        };
        let t = resolve("myrig", None, &r);
        assert_eq!(t.box_kind, BoxKind::WhiteBox);
        assert_eq!(t.package.as_deref(), Some("io.example.myrig"));
        assert!(t.carrier.is_some());

        // 同一个名字, 换一个没有载体的根, 就不再是白盒。
        let t2 = resolve("myrig", None, &roots());
        assert_eq!(t2.box_kind, BoxKind::BlackBox);

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// 不认识的名字不许猜包名 —— 猜错的代价是量了另一个应用。
    #[test]
    fn unknown_name_without_a_package_stays_unknown() {
        let t = resolve("某个没听过的游戏", None, &roots());
        assert!(t.package.is_none());
        assert_eq!(t.box_kind, BoxKind::BlackBox);
    }

    #[test]
    fn package_shaped_names_are_accepted_as_packages() {
        assert!(looks_like_package("com.example.app"));
        assert!(looks_like_package("io.github.hgamey.refbench"));
        assert!(!looks_like_package("megacity-metro"));
        assert!(!looks_like_package("原神"));
        assert!(!looks_like_package("nodots"));
        assert!(!looks_like_package("com..app"));
        assert!(!looks_like_package("1com.app"));
    }

    /// Megacity 是自驱动负载: 不要回放脚本, 带 extras 启动, 渲染线程是 Unity 的。
    #[test]
    fn megacity_is_a_self_driven_black_box() {
        let t = resolve("megacity", None, &CarrierRoots::default());
        assert_eq!(t.package.as_deref(), Some("com.unity.megacity.metro"));
        assert_eq!(t.box_kind, BoxKind::BlackBox, "② 靠灰档层注入, 对 optimize 是黑盒");
        assert!(t.workload.script_present);
        assert_eq!(t.workload.render_comm.as_deref(), Some("UnityGfxDeviceW"));
        let l = t.workload.launch.unwrap();
        assert!(l.extras.contains("--es scene city_flythrough") && !l.extras.contains("  "));
        assert_eq!(l.marker.as_deref(), Some("megacity_started"));
        assert!(l.static_extras.unwrap().contains("city_static"));
        // 包名也认
        assert_eq!(resolve("com.unity.megacity.metro", None, &CarrierRoots::default()).display, "Megacity");
    }
}

