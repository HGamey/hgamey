//! `optimize` 全链路 (不连设备): 一份录下来的探测输出 → caps → 计划 → 报告。
//!
//! 单测覆盖的是每一块自己的判断; 这里覆盖的是**接缝**: 摸底的结论有没有一路
//! 传到计划里、计划的理由有没有一路传到报告里。接缝断了的时候, 每一块的单测
//! 还是全绿, 而用户拿到的是一份"某一层没跑"但不说为什么的报告。
//!
//! 设备通路用 `Prober::with_shell` 的替身喂录下来的输出, 所以这组测试不连真机、
//! 不装 phonefarm 也能跑。

use game_opt_loop::optimize::caps::{self, Prober};
use game_opt_loop::optimize::games::{self, CarrierRoots};
use game_opt_loop::optimize::plan::{self, Budget};
use game_opt_loop::optimize::report;
use std::path::PathBuf;

/// 一台已 root、装着目标游戏、正在放电的红魔。
const ROOTED_NX809J: &str = "\
model=NX809J
sdk=36
abi=arm64-v8a
root_uid=0
vklayers=
battery_status=Discharging
battery_capacity=84
battery_current_now=-1200000
charge_node=/sys/class/qcom-battery/charging_enabled
temp_mc=41300
pkg_path=package:/data/app/~~abc/com.miHoYo.Yuanshen-1/base.apk
";

/// 一台没 root 的普通机器。
const UNROOTED: &str = "\
model=Pixel 8
sdk=34
abi=arm64-v8a
root_uid=NA
vklayers=
battery_status=Charging
battery_capacity=91
battery_current_now=1072000
temp_mc=38000
pkg_path=package:/data/app/x/base.apk
";

fn no_carriers() -> CarrierRoots {
    CarrierRoots {
        carriers_dir: PathBuf::from("/nonexistent/carriers"),
        siblings_dir: PathBuf::from("/nonexistent/siblings"),
    }
}

fn survey(raw: &'static str, game: &str) -> caps::Caps {
    let target = games::resolve(game, None, &no_carriers());
    Prober::new("/nonexistent/phonefarm")
        .with_shell(move |_, _| Ok(raw.to_string()))
        .run(&target, "91253241019A")
        .expect("摸底不该失败 —— 替身直接回了录下来的输出")
}

/// 完整的 `--plan` 链路: 摸底 → 计划 → 报告, 两份产物都落盘且可再读。
#[test]
fn a_plan_run_produces_caps_and_a_report_that_explain_themselves() {
    let caps = survey(ROOTED_NX809J, "原神");
    let plan = plan::build(&caps, Budget::parse("6h").unwrap(), true);

    // 摸底的结论一路传到计划。
    assert!(caps.device.root.is_true());
    // optimize 自己驱动的两层进 steps; ① 能开但另有入口, 进 skipped 并说清去哪跑。
    assert_eq!(plan.steps.len(), 2, "{:?}", plan.skipped);
    assert!(!plan.measure_only);
    assert!(plan
        .skipped
        .iter()
        .any(|s| s.blockers.iter().any(|b| b.contains("--track sr"))));

    let layers: Vec<report::LayerOutcome> = plan
        .skipped
        .iter()
        .map(|s| report::LayerOutcome::unused(s.layer, s.reason.clone(), s.blockers.clone()))
        .collect();
    let r = report::build(&plan, &caps, layers, vec![], true);

    let dir = std::env::temp_dir().join(format!("gol_flow_{}", std::process::id()));
    let (json, md) = report::write(&r, &caps, &dir).unwrap();

    let back: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&json).unwrap()).unwrap();
    assert_eq!(back["schema"], "game_opt_loop/optimize_report@1");
    assert_eq!(back["plan_only"], true);
    // 设备事实要在报告里留底 —— 换台机器之后没有它就对不上账。
    assert_eq!(back["device_model"], "NX809J");
    assert_eq!(back["serial"], "91253241019A");

    let text = std::fs::read_to_string(&md).unwrap();
    assert!(text.contains("没有对设备做任何写入"));
    assert!(text.contains("测试条件"));

    std::fs::remove_dir_all(&dir).ok();
}

/// 一层都开不了的时候, 报告必须把三层各自的原因都写出来。
///
/// 这是这条链路最容易悄悄退化的地方: 少了理由, 用户看到的是
/// "什么都没跑", 既不知道缺什么也不知道下一步该做什么。
#[test]
fn when_nothing_can_be_opened_every_reason_reaches_the_report() {
    let caps = survey(UNROOTED, "原神");
    let plan = plan::build(&caps, Budget::default(), true);
    assert!(plan.measure_only);
    assert_eq!(plan.skipped.len(), 3);

    let layers: Vec<report::LayerOutcome> = plan
        .skipped
        .iter()
        .map(|s| report::LayerOutcome::unused(s.layer, s.reason.clone(), s.blockers.clone()))
        .collect();
    let r = report::build(&plan, &caps, layers, vec![], true);
    let md = report::markdown(&r, &caps);

    for layer in ["① 画面算子", "② 画面流程改写", "③ 系统参数"] {
        assert!(md.contains(layer), "报告里漏了 {layer}");
    }
    assert!(md.contains("root"), "没 root 这条原因没传到报告: {md}");
    // 充电态的功耗量不了, 报告里必须看得出来。
    assert!(md.contains("Charging"));
}

/// 摸底是只读的 —— `--plan` 能在别人占着机器的间隙跑, 全靠这一条。
#[test]
fn the_survey_only_ever_issues_one_read_only_command() {
    let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let sink = seen.clone();
    let target = games::resolve("原神", None, &no_carriers());
    Prober::new("/nonexistent/phonefarm")
        .with_shell(move |_, cmd| {
            sink.lock().unwrap().push(cmd.to_string());
            Ok(ROOTED_NX809J.to_string())
        })
        .run(&target, "S1")
        .unwrap();

    let cmds = seen.lock().unwrap();
    assert_eq!(cmds.len(), 1, "摸底应该一次问完, 而不是来回往返");
    let script = cmds[0].replace("2>/dev/null", "");
    for forbidden in ["setprop", "settings put", "am start", "pm install", ">"] {
        assert!(
            !script.contains(forbidden),
            "摸底命令里出现了写操作 `{forbidden}`"
        );
    }
}

/// 认不出来的名字不许猜包名 —— 猜错了就是花几十分钟量了另一个应用。
#[test]
fn an_unrecognized_game_is_reported_as_unknown_rather_than_guessed() {
    let t = games::resolve("某个没听说过的游戏", None, &no_carriers());
    assert!(t.package.is_none());
    // 给了包名就认。
    let t = games::resolve("某个没听说过的游戏", Some("com.example.x"), &no_carriers());
    assert_eq!(t.package.as_deref(), Some("com.example.x"));
}

/// 同一份探测输出, 排两次计划得到同样的步骤 —— 计划是 caps 的纯函数,
/// 所以可以拿录下来的 caps 做回归。
#[test]
fn the_whole_pipeline_is_reproducible_from_a_recorded_probe() {
    let a = plan::build(&survey(ROOTED_NX809J, "原神"), Budget::parse("2h").unwrap(), true);
    let b = plan::build(&survey(ROOTED_NX809J, "原神"), Budget::parse("2h").unwrap(), true);
    assert_eq!(
        serde_json::to_string(&a.steps).unwrap(),
        serde_json::to_string(&b.steps).unwrap()
    );
    assert_eq!(
        serde_json::to_string(&a.conditions).unwrap(),
        serde_json::to_string(&b.conditions).unwrap()
    );
}
