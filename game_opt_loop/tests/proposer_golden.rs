//! 出方案接口改造前后行为不变的证明。
//!
//! 做法: 用一个假的 phonefarm (shell 脚本, 认 `devices` / `probe` / `eval` 三个子命令,
//! 回录好的输出) 把 `optimize` 整条链路跑一遍 (本地确定性变异器, 固定种子),
//! 把它写进 `requests/` 的每一份评测请求和 `golden/` 里的逐字节比对。
//!
//! `tests/fixtures/proposer_golden/` 是**接口改造之前**的代码跑出来的 (`GOL_BLESS=1`
//! 生成, 单独一个提交)。改造之后同一个种子必须下发一模一样的请求 —— 请求就是
//! 交给真机的全部东西, 请求一样, 真机上发生的事就一样。
//!
//! 不连设备、不调大模型、不需要真的 phonefarm。

use game_opt_loop::optimize::{self, plan::Budget};
use std::collections::BTreeMap;
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

/// 假 phonefarm。`eval` 一律回 REJECT (退出码 1 = 量到了、不好), 带逐指标明细,
/// 这样第二代的出方案输入里有真实形状的历史。
fn fake_phonefarm(probe_out: &Path) -> String {
    format!(
        r#"#!/bin/sh
case "$1" in
  devices) echo "91253241019A  NX809J  adb"; exit 0 ;;
  probe) cat '{probe}'; exit 0 ;;
  onboard) echo '{{"status":"INGAME"}}'; exit 0 ;;
  eval)
    req="$3"
    if [ ! -f "$req" ]; then echo "读取请求失败: $req" >&2; exit 2; fi
    id=$(grep -o '"candidate_id": *"[^"]*"' "$req" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    kind=$(grep -o '"kind": *"[^"]*"' "$req" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    cat <<EOF
{{"candidate_id":"$id","status":"REJECT","kind":"$kind","version":2,
 "metrics":{{"fps_p95_ms":19.4,"fps_mean":59.2,"power_watt":4.8}},
 "verdict":{{"is_pareto_improvement":false,"p_value":0.3143,"reason":"没有任何指标显著改善",
   "per_metric":{{"frame_p95":{{"status":"无显著变化","diff_pct":-0.91,"p":0.3143}},
                 "fps_mean":{{"status":"无显著变化","diff_pct":0.12,"p":0.6857}}}}}},
 "conditions_observed":{{"apply_ok":true,"snapshot_identical":true,"restored":true}},
 "source":"phonefarm"}}
EOF
    exit 1 ;;
  *) echo "未知子命令 $1" >&2; exit 2 ;;
esac
"#,
        probe = probe_out.display()
    )
}

/// 搭一个临时的 phonefarm 根目录: 假二进制 + 灰档层构建脚本 (灰档那一层看它在不在决定开不开)。
fn scaffold(tag: &str) -> PathBuf {
    let root = std::env::temp_dir().join(format!("gol_golden_{tag}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let pf = root.join("phonefarm");
    std::fs::create_dir_all(pf.join("knobs/gray/build")).unwrap();
    std::fs::write(pf.join("knobs/gray/build/build_layer.sh"), "#!/bin/sh\n").unwrap();
    std::fs::write(root.join("probe_out.txt"), ROOTED_NX809J).unwrap();
    std::fs::write(root.join("probe.txt"), PROBE).unwrap();
    let bin = pf.join("phonefarm");
    std::fs::write(&bin, fake_phonefarm(&root.join("probe_out.txt"))).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    root
}

fn config(root: &Path, seed: u64) -> optimize::Config {
    optimize::Config {
        game: "原神".into(),
        package: None,
        serial: Some("91253241019A".into()),
        extra_devices: vec![],
        // 形状写死成 2 代 × 3 个 (② 已实现的开关只有一个, 第二代就停), 不靠预算推:
        // 金样钉的是「同一种子下发同样的请求」, 不该随每候选估时的口径一起漂。
        budget: Budget::parse("6h").unwrap(),
        plan_only: false,
        out_dir: root.join("out"),
        phonefarm: root.join("phonefarm/phonefarm"),
        secrets: root.join("no_secrets.env"),
        use_llm: false,
        llm_provider: None,
        llm_timeout_s: None,
        seed,
        quality_reference: None,
        sysparam_probe: Some(root.join("probe.txt")),
        proposer: Default::default(),
        shape: optimize::plan::Shape {
            layers: None,
            generations: Some(2),
            children: Some(3),
        },
        // 空档案库: 原神走内置条目, 请求与金样一致
        profiles_dir: root.join("profiles"),
        game_profile: None,
        // ② 的旋钮来自真机抓的原神 frame-audit 结果 (rp103 C2、rp71 C0/C1 三处确定白存)
        frame_audit: Some(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/frame_audit/genshin_7.1_analysis.json"),
        ),
        screen: false,
        goal: Default::default(),
    }
}

/// 读出一个目录下全部请求 (文件名 → JSON)。
fn requests(dir: &Path) -> BTreeMap<String, serde_json::Value> {
    let mut m = BTreeMap::new();
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.extension().is_some_and(|x| x == "json") {
            let v: serde_json::Value =
                serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            m.insert(p.file_name().unwrap().to_string_lossy().into_owned(), v);
        }
    }
    m
}

fn golden_dir(seed: u64) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/proposer_golden")
        .join(format!("seed_{seed}"))
}

fn check_against_golden(seed: u64) {
    let root = scaffold(&seed.to_string());
    optimize::run(&config(&root, seed)).expect("假 phonefarm 下整条链路应该跑得通");
    let got = requests(&root.join("out").join("requests"));
    assert!(
        got.keys().any(|k| k.starts_with("sys_")) && got.keys().any(|k| k.starts_with("gray_")),
        "两层都该下发过请求: {:?}",
        got.keys().collect::<Vec<_>>()
    );

    let gold = golden_dir(seed);
    if std::env::var("GOL_BLESS").is_ok() {
        let _ = std::fs::remove_dir_all(&gold);
        std::fs::create_dir_all(&gold).unwrap();
        for (name, v) in &got {
            std::fs::write(gold.join(name), serde_json::to_string_pretty(v).unwrap() + "\n")
                .unwrap();
        }
    }
    let want = requests(&gold);
    assert_eq!(
        got.keys().collect::<Vec<_>>(),
        want.keys().collect::<Vec<_>>(),
        "下发的候选个数/编号与改造前不同"
    );
    for (name, v) in &want {
        assert_eq!(&got[name], v, "{name} 与改造前下发的请求不一致");
    }
    std::fs::remove_dir_all(&root).ok();
}

/// 固定种子下, 本地确定性变异器经整条 optimize 链路下发的请求与改造前逐字节一致。
#[test]
fn local_proposer_requests_match_the_pre_refactor_golden_seed_20260923() {
    check_against_golden(20260923);
}

/// 换一个种子再钉一次 —— 只钉一个种子的话, "恰好一样"排除不了。
#[test]
fn local_proposer_requests_match_the_pre_refactor_golden_seed_7() {
    check_against_golden(7);
}

/// 外部进程出方案走完整条链路: 用仓库里给第三方的那个例子 (examples/proposers/fixed_one.py),
/// ③ 第一代下发且只下发它给的那一个候选, ② 它不出候选, 报告里记着是谁出的、问了几次。
#[test]
fn the_example_external_proposer_runs_end_to_end() {
    let root = scaffold("ext");
    let mut cfg = config(&root, 1);
    let example = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/proposers/fixed_one.py");
    cfg.proposer.specs = vec![format!("cmd:{}", example.display())];
    optimize::run(&cfg).expect("外部出方案应该跑得通");

    let got = requests(&root.join("out").join("requests"));
    let names: Vec<_> = got.keys().cloned().collect();
    assert_eq!(names, vec!["sys_g1c1.json".to_string()], "只该下发一个 ③ 候选");
    let params = &got["sys_g1c1.json"]["payload"]["params"];
    assert_eq!(params.as_object().unwrap().len(), 1);
    assert!(params.get("gpu.min_pwrlevel").is_some(), "{params}");

    let report: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(root.join("out").join("report.json")).unwrap(),
    )
    .unwrap();
    assert!(report["proposer"]["spec"].as_str().unwrap().starts_with("cmd:"));
    assert_eq!(report["proposer"]["model_calls"], 0);
    // ③ 两代 (第二代回空就停) + ② 一代 (回空就停)。
    assert_eq!(report["proposer"]["calls"], 3);
    assert!(root.join("out/proposer/sysparam_g1.input.json").is_file());
    std::fs::remove_dir_all(&root).ok();
}

/// 对比模式: 两个出方案的一方 (本地变异器 vs 外部例子) 在同样的配置下各跑一遍,
/// 各臂的请求与单独跑时一样, 对比报告里的账和各臂 report.json 对得上。
#[test]
fn compare_mode_runs_each_proposer_on_the_same_terms() {
    let root = scaffold("cmp");
    let mut cfg = config(&root, 20260923);
    let example = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/proposers/fixed_one.py");
    cfg.proposer.specs = vec!["local".into(), format!("cmd:{}", example.display())];
    let cmp = optimize::compare::run(&cfg).expect("对比模式应该跑得通");

    assert_eq!(cmp.arms.len(), 2);
    let (a, b) = (&cmp.arms[0], &cmp.arms[1]);
    assert_eq!(a.label, "arm1_local");
    assert_eq!(b.label, "arm2_cmd_fixed_one.py");
    assert_eq!(a.serial, b.serial);

    // 臂 1 就是 --no-llm 单独跑: 下发的请求与 golden 一致。
    let got = requests(&root.join("out/arm1_local/requests"));
    let want = requests(&golden_dir(20260923));
    assert_eq!(got, want, "对比模式下的本地变异器臂与单独跑不一致");
    assert_eq!(a.device_evals, want.len());
    // 臂 2: 外部例子只给一个 ③ 候选。
    assert_eq!(b.device_evals, 1);
    assert_eq!(b.proposer_calls, 3);
    assert_eq!(a.model_calls + b.model_calls, 0);
    // 假 phonefarm 一律 REJECT: 两边都没留下东西。
    assert!(a.kept.is_empty() && b.kept.is_empty());

    let md = std::fs::read_to_string(root.join("out/compare.md")).unwrap();
    assert!(md.contains("arm1_local") && md.contains("arm2_cmd_fixed_one.py"), "{md}");
    assert!(root.join("out/compare.json").is_file());
    std::fs::remove_dir_all(&root).ok();
}

/// 分段跑 (每段各自持锁) 的单臂输出, 离线拼成的对比与一次跑完的对比账目一致。
#[test]
fn arms_run_in_separate_sessions_can_be_compared_offline() {
    let root = scaffold("cmp_off");
    let example = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/proposers/fixed_one.py");
    let mut dirs = Vec::new();
    for (name, spec) in [("seg1_local", "local".to_string()), ("seg2_ext", format!("cmd:{}", example.display()))] {
        let mut cfg = config(&root, 20260923);
        cfg.proposer.specs = vec![spec];
        cfg.out_dir = root.join(name);
        optimize::run(&cfg).unwrap();
        dirs.push(cfg.out_dir);
    }
    let c = optimize::compare::from_arm_dirs(&dirs, &root.join("cmp")).unwrap();
    assert_eq!(c.arms.len(), 2);
    assert_eq!(c.arms[0].label, "seg1_local");
    assert_eq!(c.arms[0].spec, "local");
    assert!(c.arms[1].spec.starts_with("cmd:"));
    assert_eq!(c.arms[0].device_evals, requests(&golden_dir(20260923)).len());
    assert_eq!(c.arms[1].device_evals, 1);
    assert!(c.arms.iter().all(|a| a.wall_s.is_none()));
    assert_eq!(c.seed, None);
    assert_eq!(c.game, "原神");
    let md = std::fs::read_to_string(root.join("cmp/compare.md")).unwrap();
    assert!(md.contains("未记录") && md.contains("离线拼成"), "{md}");
    // 只给一个臂、或者给了 --plan 的输出, 拒绝。
    assert!(optimize::compare::from_arm_dirs(&dirs[..1], &root.join("x")).is_err());
    std::fs::remove_dir_all(&root).ok();
}
