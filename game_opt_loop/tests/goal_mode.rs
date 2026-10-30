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

/// 假 phonefarm（明显更差版）：eval 一律 REJECT 且帧时 p95 变差 3.5% —— 给两级测量的初筛用。
fn fake_phonefarm_worse(probe_out: &Path) -> String {
    fake_phonefarm_d(probe_out, "REJECT", "false", "[]", "3.5", 1)
}

/// 假 phonefarm。`win` = eval 一律回 PASS 且帧时 p95 显著改善 6%; 否则一律 REJECT。
fn fake_phonefarm(probe_out: &Path, win: bool) -> String {
    let (status, keep, wins, d, code) = if win {
        ("PASS", "true", "[\"frame_p95\"]", "-6.0", 0)
    } else {
        ("REJECT", "false", "[]", "-0.91", 1)
    };
    fake_phonefarm_d(probe_out, status, keep, wins, d, code)
}

fn fake_phonefarm_d(probe_out: &Path, status: &str, keep: &str, wins: &str, d: &str, code: u8) -> String {
    let mut s = String::new();
    s.push_str("#!/bin/sh\ncase \"$1\" in\n");
    s.push_str("  __lease-handoff) exec python3 \"$(dirname \"$0\")/fd_phonefarm.py\" \"$@\" ;;\n");
    s.push_str("  devices) echo \"91253241019A device\"; exit 0 ;;\n");
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
    std::fs::write(pf.join("fd_phonefarm.py"), include_str!("fd_phonefarm.py")).unwrap();
    let bin = pf.join("phonefarm");
    std::fs::write(&bin, fake_phonefarm(&root.join("probe_out.txt"), win)).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    root
}

/// 「明显更差」假机的脚手架（初筛集成测试用）。
fn scaffold_worse(tag: &str) -> PathBuf {
    let root = scaffold(tag, false);
    let bin = root.join("phonefarm/phonefarm");
    std::fs::write(&bin, fake_phonefarm_worse(&root.join("probe_out.txt"))).unwrap();
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
        resume: false,
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
    // 确认前可以整份替换 goal.md (坏的格式拒收)
    let bad = root.join("bad_goal.md");
    std::fs::write(&bad, "---\ngame: x\n---\n").unwrap();
    assert!(goal::edit(&dir, &bad).is_err());
    let same = root.join("same_goal.md");
    std::fs::copy(dir.join("goal.md"), &same).unwrap();
    goal::edit(&dir, &same).unwrap();
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
    // 这次用的输入留在 run 目录里
    assert!(dir.join("inputs/probe.txt").is_file());
    assert!(dir.join("inputs/frame_audit.json").is_file());
    // show 看得到状态与事件; 冻结之后不许 edit
    let shown = goal::show(&dir, 5).unwrap();
    assert!(shown.contains("\"stop_reason\": \"no_gain\"") && shown.contains("-- report.md --"), "{shown}");
    assert!(goal::edit(&dir, &dir.join("goal.md")).is_err());
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

/// 连续两次宕机/被杀后续跑：已完成的代保留，只重跑没有报告的那一代。
#[test]
fn resume_after_consecutive_crashes_keeps_completed_and_redoes_half_done() {
    let root = scaffold("resumekeep", true);
    let dir = root.join("goals/run4");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.layers = vec!["sysparam".into()];
        s.budget.max_generations = 2;
        s.budget.min_generations = 1;
        s.per_generation = 1;
        s.stop_when = vec!["budget_exhausted".into()];
    });
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    assert_eq!(st.stop_reason.as_deref(), Some("budget_exhausted"));
    assert_eq!(st.generation, 2);

    // 伪装成「第 3 代开始前进程死了」：状态还是 running（其余字段就是第 2 代完成时的样子）
    let mut v: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    v["status"] = "running".into();
    v["stop_reason"] = Value::Null;
    std::fs::write(dir.join("state.json"), serde_json::to_string_pretty(&v).unwrap()).unwrap();

    // 不带 resume：拒绝，并提示 goal resume
    let e = goal::run(&dir, &opts(&root, None)).unwrap_err();
    assert!(format!("{e:#}").contains("goal resume"), "{e:#}");

    // 带 resume：重建已完成的 2 代，立刻按代预算用完收尾，不重测
    let ro = goal::RunOpts { resume: true, ..opts(&root, None) };
    let st = goal::run(&dir, &ro).unwrap();
    assert_eq!(st.status, "done");
    assert_eq!(st.stop_reason.as_deref(), Some("budget_exhausted"));
    assert_eq!(st.generation, 2, "已完成的 2 代保留，不重跑");
    let ev = events(&dir);
    assert!(
        ev.iter().any(|e| e["type"] == "note" && e["message"].as_str().unwrap_or_default().contains("断点续跑")),
        "缺断点续跑事件"
    );
    // 重跑没有发生：每代仍只有一条 propose
    for g in 1..=2u64 {
        assert_eq!(ev.iter().filter(|e| e["type"] == "propose" && e["gen"] == g).count(), 1, "第 {g} 代被重测了");
    }
    // 档案: 第一次跑的结论已落盘（迁移后的新 run 也能读到）
    let arch = std::fs::read_to_string(root.join("goals/archive.jsonl")).unwrap();
    assert!(arch.lines().count() >= 2, "{arch}");
    check_contracts(&dir);

    // 第二次故障伪装成「第 2 代测到一半死了」：报告没了，目录残渣还在。
    std::fs::remove_file(dir.join("optimize/gen2/report.json")).unwrap();
    let mut v: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    v["status"] = "running".into();
    v["stop_reason"] = Value::Null;
    std::fs::write(dir.join("state.json"), serde_json::to_string_pretty(&v).unwrap()).unwrap();

    let st = goal::run(&dir, &ro).unwrap();
    assert_eq!(st.status, "done");
    assert_eq!(st.generation, 2, "第 2 代重跑后按预算收尾");
    let ev = events(&dir);
    assert_eq!(
        ev.iter().filter(|e| e["type"] == "propose" && e["gen"] == 2).count(),
        2,
        "第 2 代应该被测了两次（第一次中途死了）"
    );
    assert_eq!(ev.iter().filter(|e| e["type"] == "propose" && e["gen"] == 1).count(), 1, "第 1 代不该重测");
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

/// 两级测量：明显更差的候选在初筛就被淘汰，不花全量对照的钱；
/// SCREENED_OUT 进历史与档案，下一代出题看得到。
#[test]
fn screen_drops_clearly_worse_candidates_before_full_measurement() {
    let root = scaffold_worse("screen");
    let dir = root.join("goals/run_screen");
    goal::draft("团战卡顿", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.layers = vec!["sysparam".into()];
        s.per_generation = 1;
        s.screen = true;
        s.budget.max_generations = 2;
        s.budget.min_generations = 1;
    });
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    let ev = events(&dir);
    let so: Vec<_> = ev.iter().filter(|e| e["type"] == "screened_out").collect();
    assert!(!so.is_empty(), "明显更差的候选没被初筛淘汰: {ev:?}");
    assert!(so[0]["reason"].as_str().unwrap().contains("初筛淘汰"));
    // 初筛淘汰的候选没有进全量对照（没有全量 verdict 事件）
    let screened_id = so[0]["candidate_id"].as_str().unwrap().to_string();
    assert!(
        !ev.iter().any(|e| e["type"] == "verdict" && e["candidate_id"] == screened_id),
        "被淘汰的候选还跑了全量对照"
    );
    // 连续无收益收尾（初筛淘汰不算收益）
    assert_eq!(st.stop_reason.as_deref(), Some("no_gain"));
    // SCREENED_OUT 进了档案（下次迁移后出题仍看得到）
    let arch = std::fs::read_to_string(root.join("goals/archive.jsonl")).unwrap();
    assert!(arch.contains("SCREENED_OUT"), "{arch}");
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

/// 网页的「停止」= 向 goal run 进程发 SIGINT: 当前这一代中断, 正在上机的 phonefarm 子进程
/// 同组收到 SIGINT 走还原 (这里用标记文件代替), 写 stop 事件, 状态 done / human_stop。
#[cfg(unix)]
fn sigint_case(tag: &str, to_group: bool) {
    use std::os::unix::process::CommandExt;
    let root = scaffold(tag, false);
    let mark = root.join("child_got_sigint");
    let ready = root.join("child_ready");
    // eval 慢一点, 并在收到 SIGINT 时留下标记 (真 phonefarm 在这里走还原)
    let bin = root.join("phonefarm/phonefarm");
    let script = std::fs::read_to_string(&bin).unwrap().replace(
        "  eval)\n",
        &format!(
            "  eval)\n    trap 'echo restored > {}; exit 130' INT\n    echo started > {}\n    sleep 20 >/dev/null 2>&1 &\n    wait $!\n",
            mark.display(),
            ready.display()
        ),
    );
    std::fs::write(&bin, script).unwrap();
    let dir = root.join("goals/run4");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.layers = vec!["sysparam".into()];
        s.per_generation = 1;
    });
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["goal", "run"])
        .arg(&dir)
        .arg("--phonefarm")
        .arg(&bin)
        .arg("--secrets")
        .arg(root.join("no_secrets.env"))
        .arg("--sysparam-probe")
        .arg(root.join("probe.txt"))
        .arg("--profiles")
        .arg(root.join("profiles"))
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        // 节点那样: 每个任务自己一个进程组, 停任务时对整组发信号
        .process_group(0)
        .spawn()
        .unwrap();
    // 等 fake PF 已启动并安装 INT trap，再发停止；不靠固定等待预算。
    while !ready.exists() {
        assert!(child.try_wait().unwrap().is_none(), "PF exited before becoming ready");
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    let target = if to_group { -(child.id() as i32) } else { child.id() as i32 };
    unsafe {
        libc::kill(target, libc::SIGINT);
    }
    let status = child.wait().unwrap();
    assert!(status.success(), "{status:?}");
    assert!(mark.exists(), "phonefarm 子进程没收到 SIGINT");
    let st: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    assert_eq!(st["status"], "done");
    assert_eq!(st["stop_reason"], "human_stop");
    let ev = events(&dir);
    assert_eq!(ev.last().unwrap()["type"], "stop");
    assert_eq!(ev.last().unwrap()["reason"], "human_stop");
    assert!(dir.join("report.md").exists());
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

/// `proposer: cloud` 走外部进程那条路 (proposer@1)。测试里用 GOL_CLOUD_PROPOSER 把云端适配器换成
/// 仓库里给第三方的例子, 不真的去触发 GitHub Actions; 适配器不存在时 goal run 在冻结之前就拒绝。
#[test]
fn proposer_cloud_runs_through_the_external_adapter() {
    let root = scaffold("cloud", false);
    let dir = root.join("goals/run_cloud");
    goal::draft("原神太费电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.proposer = "cloud".into();
        s.layers = vec!["sysparam".into()];
        s.per_generation = 1;
        s.budget.max_generations = 1;
        s.budget.min_generations = 1;
    });

    // 适配器不存在: 冻结之前就拒, 目录还能重新确认。
    std::env::set_var("GOL_CLOUD_PROPOSER", root.join("nope.py"));
    let err = goal::run(&dir, &opts(&root, None)).unwrap_err().to_string();
    assert!(err.contains("不存在"), "{err}");
    assert!(!dir.join("goal.frozen.md").exists());

    let example = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/proposers/fixed_one.py");
    std::env::set_var("GOL_CLOUD_PROPOSER", &example);
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    std::env::remove_var("GOL_CLOUD_PROPOSER");
    assert_eq!(st.generation, 1);
    let rep: Value = serde_json::from_str(
        &std::fs::read_to_string(dir.join("optimize/gen1/report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(rep["proposer"]["spec"], format!("cmd:{}", example.display()));
    assert!(events(&dir).iter().any(|e| e["type"] == "propose" && e["gen"] == 1));
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

/// 网页只给 goal run 本身发 SIGINT: 由它转给正在上机的 phonefarm。
#[cfg(unix)]
#[test]
fn sigint_to_the_process_stops_the_generation_and_reaches_the_child() {
    sigint_case("sigint_pid", false);
}

/// 节点停任务是对整个进程组发 INT: goal run 不另开进程组/会话, 子进程一起收到。
#[cfg(unix)]
#[test]
fn sigint_to_the_process_group_also_works() {
    sigint_case("sigint_grp", true);
}

/// Megacity: 自驱动负载。请求带 workload.launch, 不走进门 (onboard), 并且合契约。
#[test]
fn megacity_requests_carry_the_launch_and_skip_the_entry_flow() {
    let root = scaffold("megacity", false);
    let dir = root.join("goals/run_mc");
    goal::draft("Megacity 城市飞行省电", "com.unity.megacity.metro", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.per_generation = 1;
        s.budget.max_generations = 1;
        s.budget.min_generations = 1;
        s.layers = vec!["sysparam".into(), "gray".into(), "whitebox".into()];
    });
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    assert_eq!(st.generation, 1);
    let gen = dir.join("optimize/gen1");
    // ④ 白盒: 请求 kind=whitebox, 带 launch 与旋钮, 合契约; 出方案输入带白盒目录并合契约
    let wb: Value = serde_json::from_str(&std::fs::read_to_string(gen.join("requests/wb_g1c1.json")).unwrap()).unwrap();
    assert_eq!(wb["kind"], "whitebox");
    assert!(wb["workload"]["launch"]["extras"].as_str().unwrap().contains("city_flythrough"));
    assert!(!wb["payload"]["knobs"].as_object().unwrap().is_empty());
    schema::assert_conforms(&wb, "eval_request.schema.json");
    let inp: Value = serde_json::from_str(&std::fs::read_to_string(gen.join("proposer/whitebox_g1.input.json")).unwrap()).unwrap();
    assert!(inp["constraints"]["whitebox"].as_array().unwrap().iter().any(|k| k["id"] == "fog_event"));
    schema::assert_conforms(&inp, "proposer_input.schema.json");
    assert!(events(&dir).iter().any(|e| e["type"] == "propose" && e["layer"] == "whitebox"));
    assert!(!gen.join("enter").exists(), "自驱动负载不该走进门流程");
    for name in ["sys_g1c1.json", "gray_g1c1.json"] {
        let req: Value = serde_json::from_str(&std::fs::read_to_string(gen.join("requests").join(name)).unwrap()).unwrap();
        assert_eq!(req["package"], "com.unity.megacity.metro");
        let l = &req["workload"]["launch"];
        assert!(l["extras"].as_str().unwrap().contains("city_flythrough"), "{req}");
        assert_eq!(l["marker"], "megacity_started");
        assert!(req["workload"].get("script").is_none());
        assert!(req["workload"].get("enter").is_none());
        assert_eq!(req["workload"]["render_comm"], "UnityGfxDeviceW");
        schema::assert_conforms(&req, "eval_request.schema.json");
    }
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}

/// 冻结后一代都没开始就停了 (排队时被杀): 同一个目录可以接着跑; goal.md 被改过就不行。
/// 卡住的运行用 cancel 收尾, 已结束的不能再 cancel。
#[test]
fn a_frozen_but_unstarted_run_can_be_resumed_and_a_stuck_one_cancelled() {
    let root = scaffold("resume", false);
    let dir = root.join("goals/run_resume");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| {
        s.layers = vec!["sysparam".into()];
        s.per_generation = 1;
        s.budget.max_generations = 1;
        s.budget.min_generations = 1;
    });
    // 模拟「冻结了、排队时被杀」: status=running, generation=0, sha 与 goal.md 一致
    let text = std::fs::read_to_string(dir.join("goal.md")).unwrap();
    let sha = {
        use sha2::{Digest, Sha256};
        format!("{:x}", Sha256::digest(text.as_bytes()))
    };
    let mut st: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    st["status"] = "running".into();
    st["goal_sha256"] = sha.clone().into();
    std::fs::write(dir.join("state.json"), serde_json::to_string(&st).unwrap()).unwrap();
    // goal.md 改过 → 拒绝
    std::fs::write(dir.join("goal.md"), text.replace("per_generation: 1", "per_generation: 2")).unwrap();
    assert!(goal::run(&dir, &opts(&root, None)).is_err());
    std::fs::write(dir.join("goal.md"), &text).unwrap();
    let st = goal::run(&dir, &opts(&root, None)).unwrap();
    assert_eq!(st.status, "done");
    assert_eq!(st.generation, 1);
    assert!(goal::cancel(&dir, "x").is_err(), "已结束的不用收尾");

    // 卡在 running 的另一次运行: cancel 收尾, 写 stop
    let dir2 = root.join("goals/run_stuck");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir2, goal::Drafter::Template).unwrap();
    let mut st: Value = serde_json::from_str(&std::fs::read_to_string(dir2.join("state.json")).unwrap()).unwrap();
    st["status"] = "running".into();
    st["generation"] = 2.into();
    std::fs::write(dir2.join("state.json"), serde_json::to_string(&st).unwrap()).unwrap();
    assert!(goal::run(&dir2, &opts(&root, None)).is_err(), "跑到一半的不能接着跑");
    let st = goal::cancel(&dir2, "节点重启, 进程不在了").unwrap();
    assert_eq!((st.status.as_str(), st.stop_reason.as_deref()), ("done", Some("human_stop")));
    assert_eq!(events(&dir2).last().unwrap()["type"], "stop");
    check_contracts(&dir2);
    std::fs::remove_dir_all(&root).ok();
}

/// 排队等锁时被叫停 (网页「停止」或节点停任务): 立即退出, 没碰设备, 写 stop(human_stop)。
#[cfg(unix)]
#[test]
fn sigint_while_queuing_for_the_lock_stops_without_touching_the_device() {
    use std::os::unix::fs::PermissionsExt;
    let root = scaffold("queue_sigint", false);
    let dir = root.join("goals/run_q");
    goal::draft("省电", "com.miHoYo.Yuanshen", &dir, goal::Drafter::Template).unwrap();
    confirm_with(&dir, |s| s.layers = vec!["sysparam".into()]);
    // The selected PF helper records its call, then waits without sending a lock fd.
    let lock = root.join("phonefarm/phonefarm");
    let called = root.join("lock_called");
    std::fs::write(&lock, format!("#!/usr/bin/env python3\nimport pathlib,time\npathlib.Path({:?}).write_text('called')\nwhile True: time.sleep(.02)\n", called.to_str().unwrap())).unwrap();
    std::fs::set_permissions(&lock, std::fs::Permissions::from_mode(0o755)).unwrap();
    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_game_opt_loop"))
        .args(["goal", "run"])
        .arg(&dir)
        .arg("--phonefarm")
        .arg(root.join("phonefarm/phonefarm"))
        .arg("--secrets")
        .arg(root.join("no_secrets.env"))
        .arg("--sysparam-probe")
        .arg(root.join("probe.txt"))
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    while !called.exists() {
        assert!(child.try_wait().unwrap().is_none(), "goal exited before calling the lock");
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    unsafe {
        libc::kill(child.id() as i32, libc::SIGINT);
    }
    let status = child.wait().unwrap();
    assert!(status.success(), "{status:?}");
    let st: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
    assert_eq!(st["stop_reason"], "human_stop");
    assert_eq!(events(&dir).last().unwrap()["type"], "stop");
    assert!(!dir.join("optimize").exists(), "没拿到锁就不该碰设备");
    check_contracts(&dir);
    std::fs::remove_dir_all(&root).ok();
}
