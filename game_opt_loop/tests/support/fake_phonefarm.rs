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
            if args[0] == "gpu-op" {
                println!("{{\"candidate_id\":\"{id}\",\"status\":\"PASS\",\"metrics\":{{\"operator_latency_ms\":0.5,\"fps_p95_ms\":16.0,\"power_watt\":null,\"psnr_db\":45.0}},\"verdict\":{{\"is_pareto_improvement\":true,\"p_value\":0.001}},\"source\":\"phonefarm\"}}");
            } else {
                println!("{{\"version\":2,\"kind\":\"{kind}\",\"candidate_id\":\"{id}\",\"status\":\"REJECT\",\"metrics\":{{\"fps_p95_ms\":16.0,\"fps_mean\":60.0,\"power_watt\":null}},\"verdict\":{{\"is_pareto_improvement\":false,\"p_value\":0.5,\"reason\":\"synthetic fixture: no improvement\"}},\"conditions_observed\":{{\"apply_ok\":true,\"restored\":true}},\"source\":\"phonefarm\"}}");
            }
        },
        _ => std::process::exit(2),
    }
}
