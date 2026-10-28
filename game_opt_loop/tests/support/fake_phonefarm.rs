//! Native external process fixture. No device, shell glue, network or API key.
use std::io::Write;

fn field(text: &str, key: &str) -> String {
    let tail = text.split(&format!("\"{key}\"")).nth(1).unwrap_or("");
    tail.split(':')
        .nth(1)
        .unwrap_or("")
        .trim()
        .trim_start_matches('"')
        .split('"')
        .next()
        .unwrap_or("")
        .to_string()
}
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("devices") => println!("List of devices attached\nTEST0001\tdevice"),
        // 黑盒游戏上机前的进门 (`onboard --enter-only`): 夹具直接当已在游戏场景。
        Some("onboard") => {}
        Some("probe") => print!("model=TEST_FIXTURE\nsdk=36\nabi=arm64-v8a\nroot_uid=0\nvklayers=\nbattery_status=Discharging\nbattery_capacity=84\nbattery_current_now=-1200000\ncharge_node=/sys/class/qcom-battery/charging_enabled\nzone=cpu-0-0 41300\npkg_path=package:/data/app/com.miHoYo.Yuanshen/base.apk\n"),
        Some("gpu-op") | Some("eval") => {
            let Some(pos) = args.iter().position(|v| v == "--request") else {
                eprintln!("需要 --request"); std::process::exit(2);
            };
            let Ok(request) = std::fs::read_to_string(&args[pos + 1]) else {
                eprintln!("读取请求失败: 文件不存在"); std::process::exit(2);
            };
            let id = field(&request, "candidate_id");
            let kind = field(&request, "kind");
            let mut calls = std::fs::OpenOptions::new().create(true).append(true)
                .open(std::env::var("FAKE_PHONEFARM_LOG").unwrap()).unwrap();
            writeln!(calls, "{id}").unwrap(); calls.sync_all().unwrap();
            if std::env::var_os("FAKE_PAUSE").is_some() {
                std::thread::sleep(std::time::Duration::from_secs(2));
            }
            let evidence = std::env::var("FAKE_QUALIFICATION").map(|stage| {
                let only = stage == "qualification_only";
                let stage = if only { "conclusion" } else { &stage };
                format!(",\"qualification\":{{\"stage\":\"{stage}\",\"eligible\":true}},\"qualification_only\":{only},\"artifacts\":{{\"fixture\":\"synthetic: no real frame\"}},\"metric_provenance\":{{\"power_watt\":\"unavailable\"}},\"future_receiver_evidence\":{{\"nested\":[null,false,{{\"sha256\":\"synthetic\"}}]}}")
            }).unwrap_or_default();
            if args[0] == "gpu-op" {
                let psnr = if id.starts_with("gen2_") { 47.0 } else if id.starts_with("gen1_") { 46.0 } else { 45.0 };
                println!("{{\"candidate_id\":\"{id}\",\"status\":\"PASS\",\"metrics\":{{\"operator_latency_ms\":0.5,\"fps_p95_ms\":16.0,\"power_watt\":null,\"psnr_db\":{psnr}}},\"verdict\":{{\"is_pareto_improvement\":true,\"p_value\":0.001}},\"source\":\"phonefarm\"{evidence}}}");
            } else {
                let passed = std::env::var_os("FAKE_PASS").is_some();
                let status = if passed { "KEEP" } else { "REJECT" };
                let p = if passed { 0.001 } else { 0.5 };
                println!("{{\"version\":2,\"kind\":\"{kind}\",\"candidate_id\":\"{id}\",\"status\":\"{status}\",\"metrics\":{{\"fps_p95_ms\":16.0,\"fps_mean\":60.0,\"power_watt\":null}},\"verdict\":{{\"is_pareto_improvement\":{passed},\"p_value\":{p},\"reason\":\"synthetic fixture\"}},\"conditions_observed\":{{\"apply_ok\":true,\"restored\":true}},\"source\":\"phonefarm\"{evidence}}}");
            }
        },
        _ => std::process::exit(2),
    }
}
