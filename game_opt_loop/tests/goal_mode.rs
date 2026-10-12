//! 目标模式端到端: 假 phonefarm 上「起草 → 确认 → 三代 → 人中途改 human.md → 第三代后按 no_gain 停」,
//! 以及「第一代就达到目标 → target_reached 停」「开跑前人写了停止」。
//! 产物 (goal.md / state.json / events.jsonl) 对契约校验。
//!
//! 不连设备、不调大模型 (本地变异器, 模板起草)、不拿设备锁。

#[path = "support/schema.rs"]
mod schema;

use game_opt_loop::goal;
use serde_json::Value;
use std::path::{Path, PathBuf};

/// 一台已 root、装着原神、正在放电的红魔 (与 optimize_flow.rs 同一份录音)。
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
zone=cpu-0-0 41300
zone=gpuss-0 38900
pkg_path=package:/data/app/~~abc/com.miHoYo.Yuanshen-1/base.apk
";

/// 系统参数探测结果 (与 sysparam.rs 单测同一份)。
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
display.modes=60.0 x 1080, 120.0 x 1080
setting.system.peak_refresh_rate.cur=120
setting.system.min_refresh_rate.cur=60
";

/// 假 phonefarm。`win` = eval 一律回 PASS 且帧时 p95 显著改善 6%; 否则一律 REJECT。
fn fake_phonefarm(probe_out: &Path, win: bool) -> String {
    let (status, keep, wins, d, code) = if win {
        ("PASS", "true", "[\"frame_p95\"]", "-6.0", 0)
    } else {
        ("REJECT", "false", "[]", "-0.91", 1)
    };
    let mut s = String::new();
    s.push_str("#!/bin/sh\ncase \"$1\" in\n");
    s.push_str("  devices) echo \"91253241019A  NX809J  adb\"; exit 0 ;;\n");
    s.push_str(&format!("  probe) cat '{}'; exit 0 ;;\n", probe_out.display()));
    s.push_str("  onboard) echo '{\"status\":\"INGAME\"}'; exit 0 ;;\n");
    s.push_str("  eval)\n    req=\"$3\"\n");
    s.push_str("    if [ ! -f \"$req\" ]; then echo \"读取请求失败: $req\" >&2; exit 2; fi\n");
    s.push_str("    id=$(grep -o '\"candidate_id\": *\"[^\"]*\"' \"$req\" | head -1 | sed 's/.*\"\\([^\"]*\\)\"$/\\1/')\n");
    s.push_str("    kind=$(grep -o '\"kind\": *\"[^\"]*\"' \"$req\" | head -1 | sed 's/.*\"\\([^\"]*\\)\"$/\\1/')\n");
    s.push_str(&format!(
        "    printf '%s' '{{\"candidate_id\":\"'\"$id\"'\",\"status\":\"{status}\",\"kind\":\"'\"$kind\"'\",\"version\":2,\
\"metrics\":{{\"fps_p95_ms\":19.4,\"fps_mean\":59.2,\"power_watt\":4.8,\"psnr_db\":41.0}},\
\"verdict\":{{\"is_pareto_improvement\":{keep},\"p_value\":0.0079,\"reason\":\"假判定\",\"wins\":{wins},\
\"per_metric\":{{\"frame_p95\":{{\"status\":\"x\",\"diff_pct\":{d},\"p\":0.0079}},\
\"fps_mean\":{{\"status\":\"无显著变化\",\"diff_pct\":0.12,\"p\":0.6857}}}}}},\
\"conditions_observed\":{{\"apply_ok\":true,\"snapshot_identical\":true,\"restored\":true,\"contested_arms\":0}},\
\"source\":\"phonefarm\"}}'\n    exit {code} ;;\n"
    ));
    s.push_str("  *) echo \"未知子命令 $1\" >&2; exit 2 ;;\nesac\n");
    s
}

fn scaffold(tag: &str, win: bool) -> PathBuf {
    let root = std::env::temp_dir().join(format!("gol_goal_{tag}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let pf = root.join("phonefarm");
    std::fs::create_dir_all(pf.join("knobs/gray/build")).unwrap();
    std::fs::write(pf.join("knobs/gray/build/build_layer.sh"), "#!/bin/sh\n").unwrap();
    std::fs::write(root.join("probe_out.txt"), ROOTED_NX809J).unwrap();
    std::fs::write(root.join("probe.txt"), PROBE).unwrap();
    let bin = pf.join("phonefarm");
    std::fs::write(&bin, fake_phonefarm(&root.join("probe_out.txt"), win)).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    root
}

fn opts<'a>(root: &Path, cb: Option<&'a dyn Fn(u32)>) -> goal::RunOpts<'a> {
    goal::RunOpts {
        phonefarm: root.join("phonefarm/phonefarm"),
        secrets: root.join("no_secrets.env"),
        llm_provider: None,
        sysparam_probe: Some(root.join("probe.txt")),
        frame_audit: Some(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/frame_audit/genshin_7.1_analysis.json"),
        ),
        profiles_dir: root.join("profiles"),
        lock: None,
        poll: std::time::Duration::from_millis(50),
        before_generation: cb,
    }
}

fn events(dir: &Path) -> Vec<Value> {
    std::fs::read_to_string(dir.join("events.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn check_contracts(dir: &Path) {
    let st: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    schema::assert_conforms(&st, "goal_state.schema.json");
    for e in events(dir) {
        schema::assert_conforms(&e, "goal_event.schema.json");
    }
    let doc = goal::spec::parse(&std::fs::read_to_string(dir.join("goal.md")).unwrap()).unwrap();
    schema::assert_conforms(&serde_json::to_value(&doc.spec).unwrap(), "goal.schema.json");
}

/// 把模板起草的 goal.md 改成测试要的样子 (人确认前直接改文本 —— 网页上就是这么用的)。
fn confirm_with(dir: &Path, edit: impl Fn(&mut goal::spec::GoalSpec)) {
    let doc = goal::spec::parse(&std::fs::read_to_string(dir.join("goal.md")).unwrap()).unwrap();
    let mut s = doc.spec.clone();
    s.proposer = "local".into();
    edit(&mut s);
    std::fs::write(dir.join("goal.md"), goal::spec::render(&s, &doc.body).unwrap()).unwrap();
}

#[test]
fn draft_confirm_three_generations_steer_then_no_gain() {
    let root = scaffold("nogain", false);
    let dir = root.join("goals/run1");
    let (_, by_template) =
        goal::draft("原神打团的时候卡顿, 想让帧时稳一点", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template)
            .unwrap();
    assert!(by_template);
    let st: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    assert_eq!(st["status"], "awaiting_confirm");
    confirm_with(&dir, |s| {
        assert_eq!(s.objective.metric, "frame_p95", "模板该从「卡顿」认出帧时");
        s.budget.max_generations = 6;
        s.budget.min_generations = 3;
        s.per_generation = 1;
    });

    // 第 2 代开始前, 人改 human.md: 一条能机读 (不要动), 一条不能 (偏好)
    let human = dir.join("human.md");
    let cb = |g: u32| {
        if g == 2 {
            std::fs::write(&human, "不要动 gpu.min_pwrlevel\n多试试 CPU 频率\n").unwrap();
        }
    };
    let st = goal::run(&dir, &opts(&root, Some(&cb))).unwrap();
    assert_eq!(st.status, "done");
    assert_eq!(st.stop_reason.as_deref(), Some("no_gain"));
    assert_eq!(st.generation, 3, "min_generations=3, no_gain=2: 第三代后停");
    assert_eq!(st.human_md_rev, 1);
    assert!(st.best.is_none());
    assert!(st.goal_sha256.is_some());

    let ev = events(&dir);
    let types: Vec<&str> = ev.iter().filter_map(|e| e["type"].as_str()).collect();
    assert_eq!(types.first(), Some(&"note"));
    for t in ["start", "plan", "propose", "measure", "verdict", "steer"] {
        assert!(types.contains(&t), "缺 {t} 事件: {types:?}");
    }
    assert_eq!(types.last(), Some(&"stop"));
    let steer = ev.iter().find(|e| e["type"] == "steer").unwrap();
    assert_eq!(steer["human_md_rev"], 1);
    assert!(steer["interpreted"].as_str().unwrap().contains("禁止动 gpu.min_pwrlevel"));
    for g in 1..=3 {
        assert!(ev.iter().any(|e| e["type"] == "propose" && e["gen"] == g), "第 {g} 代没有 propose");
    }
    // 第 2、3 代: 禁止项不再出现在 ③ 的白名单里; 偏好原文进了出方案的输入; 前几代的记录接着喂
    for g in 2..=3 {
        let p = dir.join(format!("optimize/gen{g}/proposer/sysparam_g{g}.input.json"));
        let inp: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        assert!(inp["constraints"]["whitelist"].get("gpu.min_pwrlevel").is_none(), "第 {g} 代白名单里还有禁止项");
        assert!(inp["steer"].as_str().unwrap().contains("多试试 CPU 频率"));
        assert!(!inp["history"].as_array().unwrap().is_empty(), "第 {g} 代没拿到历史");
    }
    let report = std::fs::read_to_string(dir.join("report.md")).unwrap();
    assert!(report.contains("这条路没收益"), "{report}");
    assert!(report.contains("human.md 第 1 版"), "{report}");
    check_contracts(&dir);
    // 跑完的目录不能再启动
    assert!(goal::run(&dir, &opts(&root, None)).is_err());
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn reaching_the_target_stops_early() {
    let root = scaffold("target", true);
    let dir = root.join("goals/run2");
    goal::draft("团战卡顿", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.layers = vec!["sysparam".into()];
        s.per_generation = 1;
    });
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    assert_eq!(st.stop_reason.as_deref(), Some("target_reached"));
    assert_eq!(st.generation, 1);
    let b = st.best.clone().unwrap();
    assert_eq!((b.metric.as_str(), b.delta_pct), ("frame_p95", -6.0));
    assert_eq!(st.progress_to_target, 1.0);
    assert!(events(&dir).iter().any(|e| e["type"] == "keep"));
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

#[test]
fn a_human_stop_before_the_first_generation_stops_without_touching_the_device() {
    let root = scaffold("stop", false);
    let dir = root.join("goals/run3");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |_| {});
    std::fs::write(dir.join("human.md"), "停止\n").unwrap();
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    assert_eq!(st.stop_reason.as_deref(), Some("human_stop"));
    assert_eq!(st.generation, 0);
    assert!(!dir.join("optimize/gen1").exists());
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}
