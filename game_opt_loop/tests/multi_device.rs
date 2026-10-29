//! 多台同型号设备并行 (`--extra-device`) 的整条链路测试, 用假 phonefarm 模拟两台手机。
//!
//! 钉住: 每个候选只在一台设备上量、请求里的 serial 就是量它的那台; 两个 serial
//! 同时停在 started 回执才放行; 档案逐条记下设备, 单机档案不变。不连设备、不调大模型。

use game_opt_loop::optimize::{self, plan::Budget};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

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


/// 假 phonefarm: 两台在线; 双机测试在 started 回执后等待 release, 回 REJECT 并记录设备。
fn fake_phonefarm(probe_out: &Path, rendezvous: Option<&Path>) -> String {
    let gate = rendezvous.map(|root| {
        format!(
            r#"    if [ -n "$serial" ] && [ -n "$id" ]; then
      marker='{started}/'"$serial.$id.started"
      printf '%s\n' "$serial" > "$marker.tmp.$$"
      mv "$marker.tmp.$$" "$marker"
      while [ ! -e '{release}' ]; do sleep 0.01; done
    fi
"#,
            started = root.join("started").display(),
            release = root.join("release").display()
        )
    }).unwrap_or_default();
    format!(
        r#"#!/bin/sh
case "$1" in
  devices) echo "91253241019A device"; echo "91253241019B device"; exit 0 ;;
  probe) cat '{probe}'; exit 0 ;;
  onboard) echo '{{"status":"INGAME"}}'; exit 0 ;;
  eval)
    req="$3"
    id=$(grep -o '"candidate_id": *"[^"]*"' "$req" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    kind=$(grep -o '"kind": *"[^"]*"' "$req" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
    serial=$(grep -o '"serial": *"[^"]*"' "$req" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
{gate}    cat <<EOF
{{"candidate_id":"$id","status":"REJECT","kind":"$kind","version":2,
 "metrics":{{"fps_p95_ms":19.4,"fps_mean":59.2,"power_watt":4.8}},
 "verdict":{{"is_pareto_improvement":false,"p_value":0.3143,"reason":"没有任何指标显著改善"}},
 "conditions_observed":{{"apply_ok":true,"snapshot_identical":true,"restored":true}},
 "evidence_dir":"ev/$serial/$id",
 "source":"phonefarm"}}
EOF
    exit 1 ;;
  *) echo "未知子命令 $1" >&2; exit 2 ;;
esac
"#,
        probe = probe_out.display(),
        gate = gate
    )
}

fn scaffold(tag: &str, rendezvous: bool) -> PathBuf {
    let root = std::env::temp_dir().join(format!("gol_multi_{tag}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let pf = root.join("phonefarm");
    std::fs::create_dir_all(&pf).unwrap();
    std::fs::create_dir_all(root.join("started")).unwrap();
    std::fs::write(root.join("probe_out.txt"), ROOTED_NX809J).unwrap();
    std::fs::write(root.join("probe.txt"), PROBE).unwrap();
    let bin = pf.join("phonefarm");
    std::fs::write(
        &bin,
        fake_phonefarm(
            &root.join("probe_out.txt"),
            rendezvous.then_some(root.as_path()),
        ),
    ).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    root
}

fn config(root: &Path, extra: Vec<String>) -> optimize::Config {
    optimize::Config {
        game: "原神".into(),
        package: None,
        serial: Some("91253241019A".into()),
        extra_devices: extra,
        budget: Budget::parse("6h").unwrap(),
        plan_only: false,
        out_dir: root.join("out"),
        phonefarm: root.join("phonefarm/phonefarm"),
        secrets: root.join("no_secrets.env"),
        use_llm: false,
        llm_provider: None,
        llm_timeout_s: None,
        seed: 7,
        quality_reference: None,
        sysparam_probe: Some(root.join("probe.txt")),
        proposer: Default::default(),
        shape: optimize::plan::Shape {
            layers: Some(vec![optimize::eval::Kind::Sysparam]),
            generations: Some(1),
            children: Some(4),
        },
        profiles_dir: root.join("profiles"),
        game_profile: None,
        frame_audit: None,
        screen: false,
        goal: Default::default(),
        campaign: None,
    }
}

fn sys_requests(root: &Path) -> Vec<(String, serde_json::Value)> {
    let mut v: Vec<(String, serde_json::Value)> = std::fs::read_dir(root.join("out/requests"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.file_name().unwrap().to_string_lossy().starts_with("sys_"))
        .map(|p| {
            (
                p.file_name().unwrap().to_string_lossy().into_owned(),
                serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap(),
            )
        })
        .collect();
    v.sort_by(|a, b| a.0.cmp(&b.0));
    v
}

fn started_serials(root: &Path) -> std::collections::BTreeSet<String> {
    std::fs::read_dir(root.join("started"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|ext| ext == "started"))
        .map(|p| {
            std::fs::read_to_string(p)
                .unwrap()
                .trim()
                .to_string()
        })
        .collect()
}

#[test]
fn two_devices_split_a_generation_and_record_where_each_ran() {
    let single = scaffold("single", false);
    optimize::run(&config(&single, vec![])).unwrap();

    let multi = scaffold("multi", true);
    let worker_root = multi.clone();
    let worker = std::thread::spawn(move || {
        optimize::run(&config(&worker_root, vec!["91253241019B".into()]))
    });
    let deadline = Instant::now() + Duration::from_secs(10);
    let (started, running) = loop {
        let started = started_serials(&multi);
        let running = !worker.is_finished();
        if started.len() == 2 || !running || Instant::now() >= deadline {
            break (started, running);
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    std::fs::write(multi.join("release"), b"test").unwrap();
    worker.join().unwrap().unwrap();
    assert!(running, "双机任务在 release 前已经退出");
    assert_eq!(
        started.iter().map(String::as_str).collect::<Vec<_>>(),
        vec!["91253241019A", "91253241019B"],
        "两个不同 serial 必须同时停在 started"
    );

    let (a, b) = (sys_requests(&single), sys_requests(&multi));
    assert_eq!(a.len(), 4);
    assert_eq!(
        a.iter().map(|x| &x.0).collect::<Vec<_>>(),
        b.iter().map(|x| &x.0).collect::<Vec<_>>(),
        "多机与单机下发的候选应一一对应"
    );
    let mut used = std::collections::BTreeSet::new();
    for ((_, ra), (name, rb)) in a.iter().zip(&b) {
        // 除了 serial, 请求内容与单机完全相同
        let (mut ra, mut rb) = (ra.clone(), rb.clone());
        let s = rb["serial"].as_str().unwrap().to_string();
        assert!(s == "91253241019A" || s == "91253241019B", "{name}: {s}");
        used.insert(s);
        ra["serial"] = serde_json::Value::Null;
        rb["serial"] = serde_json::Value::Null;
        assert_eq!(ra, rb, "{name}");
    }
    assert_eq!(used.len(), 2, "两台都该分到候选");

    // 档案逐条记下设备, 而且就是请求里那台; 单机档案不带 device
    let text = std::fs::read_to_string(multi.join("out/report.json")).unwrap();
    let compact: String = text.split_whitespace().collect();
    for (name, rb) in &b {
        let id = name.trim_end_matches(".json");
        let ev = format!("ev/{}/{id}", rb["serial"].as_str().unwrap());
        assert!(text.contains(&ev), "{id} 的证据目录没指到量它的设备");
    }
    assert!(compact.contains("\"device\":\"91253241019B\""), "档案里没记设备");
    let rep1 = std::fs::read_to_string(single.join("out/report.json")).unwrap();
    assert!(!rep1.contains("\"device\":"), "单机档案不该多出 device 字段");

    std::fs::remove_dir_all(&single).ok();
    std::fs::remove_dir_all(&multi).ok();
}

#[test]
fn an_offline_extra_device_is_refused_before_touching_anything() {
    let root = scaffold("offline", false);
    let err = optimize::run(&config(&root, vec!["NOT_ONLINE".into()])).unwrap_err();
    assert!(err.to_string().contains("不在线"), "{err}");
    assert!(!root.join("out/requests").exists(), "拒绝之前不该下发任何请求");
    std::fs::remove_dir_all(&root).ok();
}
