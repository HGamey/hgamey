//! 手机状态: 调 `phonefarm fleet --json` 拿回农场里每台手机的现状。
//!
//! 为什么是调外部命令而不是自己读设备: 设备这件事归 phonefarm 管 —— adb/hdc 两族的
//! 枚举、一趟 shell 的探测脚本、别人占着手机时该读多少, 那些纪律都在那边。
//! 本仓库只管把它吐出来的 JSON 摆到页面上。两边各守各的边界, 面板不碰设备。
//!
//! 两个必须有的东西:
//!
//! - **超时**。掉线的设备会把 adb 卡住; 面板不能跟着一起卡死。
//! - **缓存**。页面 30 秒自刷新, 开两个浏览器标签就是两倍探测。一次探测的结果
//!   在 TTL 内共享, 同时只允许一个探测在跑。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// 探测结果在这么久之内直接复用。
///
/// 比前端 30 秒的刷新间隔略短一点: 正好让每次刷新都拿到一份新的,
/// 又不会因为两个标签页同时刷而探两次。
pub const DEFAULT_TTL_S: u64 = 25;

/// 单次 `phonefarm fleet` 的墙钟上限。
pub const DEFAULT_TIMEOUT_S: u64 = 30;

// ══════════════ phonefarm/fleet@1 的形状 ══════════════
//
// 每个字段都是 Option + #[serde(default)]: phonefarm 那侧加字段、改字段
// 都不该让面板打不开。读不到就是读不到, 前端渲染成「未知」。

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Fleet {
    #[serde(default)]
    pub schema: Option<String>,
    #[serde(default)]
    pub generated_at: Option<String>,
    #[serde(default)]
    pub backends: Backends,
    #[serde(default)]
    pub lock: Option<Lock>,
    #[serde(default)]
    pub devices: Vec<Card>,
    #[serde(default)]
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Backends {
    #[serde(default)]
    pub adb: Option<String>,
    #[serde(default)]
    pub hdc: bool,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Lock {
    #[serde(default)]
    pub dir: Option<String>,
    #[serde(default)]
    pub held_by: Option<String>,
    #[serde(default)]
    pub since: Option<String>,
    #[serde(default)]
    pub heartbeat_age_s: Option<u64>,
    #[serde(default)]
    pub stale: bool,
    #[serde(default)]
    pub queue: Vec<String>,
    #[serde(default)]
    pub recent: Vec<LockEvent>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct LockEvent {
    #[serde(default)]
    pub at: String,
    #[serde(default)]
    pub action: String,
    #[serde(default)]
    pub label: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Card {
    #[serde(default)]
    pub serial: String,
    #[serde(default)]
    pub transport: Option<String>,
    #[serde(default)]
    pub state: Option<String>,
    #[serde(default)]
    pub online: bool,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub brand: Option<String>,
    #[serde(default)]
    pub os: Option<String>,
    #[serde(default)]
    pub root: Option<bool>,
    #[serde(default)]
    pub battery: Battery,
    #[serde(default)]
    pub thermal: Thermal,
    #[serde(default)]
    pub fan: Fan,
    #[serde(default)]
    pub screen_on: Option<bool>,
    #[serde(default)]
    pub foreground: Option<String>,
    #[serde(default)]
    pub keepalive: Keepalive,
    #[serde(default)]
    pub uptime_s: Option<u64>,
    #[serde(default)]
    pub screenshot: Option<Screenshot>,
    /// `full` / `light` / `none` —— 这一轮读到什么深度。
    #[serde(default)]
    pub probe_depth: Option<String>,
    #[serde(default)]
    pub probe_ms: Option<u64>,
    #[serde(default)]
    pub unknown: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Battery {
    #[serde(default)]
    pub level_pct: Option<i32>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub plugged: Option<bool>,
    #[serde(default)]
    pub temp_c: Option<f64>,
    #[serde(default)]
    pub voltage_mv: Option<i32>,
    #[serde(default)]
    pub on_battery: Option<bool>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Thermal {
    #[serde(default)]
    pub soc_max_c: Option<f64>,
    #[serde(default)]
    pub soc_zone: Option<String>,
    #[serde(default)]
    pub gpu_c: Option<f64>,
    #[serde(default)]
    pub battery_c: Option<f64>,
    #[serde(default)]
    pub zones: usize,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Fan {
    #[serde(default)]
    pub supported: bool,
    #[serde(default)]
    pub enabled: Option<bool>,
    #[serde(default)]
    pub speed_level: Option<i64>,
    #[serde(default)]
    pub node: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Keepalive {
    #[serde(default)]
    pub screen_off_timeout: Option<String>,
    #[serde(default)]
    pub stay_on_while_plugged_in: Option<String>,
    #[serde(default)]
    pub policy_ok: Option<bool>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Screenshot {
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub at: Option<String>,
    #[serde(default)]
    pub age_s: Option<u64>,
    #[serde(default)]
    pub fresh: bool,
}

/// 面板拿到的东西: 要么是一份快照, 要么是一句"为什么没有"。
#[derive(Debug, Clone, Serialize)]
pub struct FleetView {
    /// `None` = 这一轮没拿到 (原因在 `problem` 里)。
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fleet: Option<Fleet>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub problem: Option<String>,
    /// 这份数据是什么时候探的 —— 缓存命中时它会比页面时间旧。
    pub probed_at: String,
    pub age_s: u64,
    /// 探这一次花了多久。
    pub took_ms: u64,
    /// 跑的是哪条命令, 原样写出来 —— 出问题时能照着手敲一遍。
    pub command: String,
}

// ══════════════ 取数 ══════════════

#[derive(Debug, Clone)]
pub struct FleetConfig {
    /// phonefarm 可执行文件。
    pub bin: PathBuf,
    /// 跑命令时的工作目录 (phonefarm 仓库根)。
    pub cwd: PathBuf,
    pub ttl: Duration,
    pub timeout: Duration,
    /// 传给 `--lock-dir`。
    pub lock_dir: Option<PathBuf>,
    /// 截图落盘目录。`None` = 不截 (缺省)。
    ///
    /// 必须落在 phonefarm 仓库之内, 否则 `/api/file` 交不出这张图 (白名单只认两个仓库根)。
    pub screenshot_dir: Option<PathBuf>,
    /// 截图最小间隔 (秒)。面板 30 秒刷一次, 这个数必须远大于它。
    pub screenshot_every_s: u64,
    /// 关掉这条通路 (纯看历史产物时用)。
    pub enabled: bool,
}

impl FleetConfig {
    pub fn argv(&self) -> Vec<String> {
        let mut v = vec![
            self.bin.display().to_string(),
            "fleet".to_string(),
            "--json".to_string(),
        ];
        if let Some(d) = &self.lock_dir {
            v.push("--lock-dir".to_string());
            v.push(d.display().to_string());
        }
        if let Some(d) = &self.screenshot_dir {
            v.push("--screenshot-dir".to_string());
            v.push(d.display().to_string());
            v.push("--screenshot-every".to_string());
            v.push(self.screenshot_every_s.to_string());
            // 面板自己从不持锁。带上标签只是让 phonefarm 知道"这不是我的锁",
            // 于是别人持锁时它会跳过截图 —— 正是我们要的。
            v.push("--lock-label".to_string());
            v.push("gol-panel".to_string());
        }
        v
    }
}

struct Cached {
    view: FleetView,
    at: Instant,
}

fn cache() -> &'static Mutex<Option<Cached>> {
    static CACHE: OnceLock<Mutex<Option<Cached>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(None))
}

fn now_rfc3339() -> String {
    chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false)
}

/// 取一份手机状态。TTL 内直接复用上一次的结果。
///
/// 锁是整段持有的 —— 探测期间来的第二个请求会等这一次探完, 而不是自己再探一次。
/// 面板同时开两个标签页是常事, 每个标签页都去戳一次设备不可接受。
pub fn get(cfg: &FleetConfig) -> Option<FleetView> {
    if !cfg.enabled {
        return None;
    }
    let mut guard = cache().lock().unwrap_or_else(|e| e.into_inner());
    if let Some(c) = guard.as_ref() {
        let age = c.at.elapsed();
        if age < cfg.ttl {
            let mut view = c.view.clone();
            view.age_s = age.as_secs();
            return Some(view);
        }
    }
    let view = probe(cfg);
    *guard = Some(Cached {
        view: view.clone(),
        at: Instant::now(),
    });
    Some(view)
}

/// 真的去跑一次 `phonefarm fleet --json`。
fn probe(cfg: &FleetConfig) -> FleetView {
    let started = Instant::now();
    let argv = cfg.argv();
    let command = argv.join(" ");
    let mut view = FleetView {
        fleet: None,
        problem: None,
        probed_at: now_rfc3339(),
        age_s: 0,
        took_ms: 0,
        command: command.clone(),
    };

    if !cfg.bin.is_file() {
        view.problem = Some(format!(
            "没找到 phonefarm 可执行文件: {} (用 --phonefarm-bin 指过去)",
            cfg.bin.display()
        ));
        return view;
    }

    let child = Command::new(&argv[0])
        .args(&argv[1..])
        .current_dir(&cfg.cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            view.problem = Some(format!("起不来 `{command}`: {e}"));
            view.took_ms = started.elapsed().as_millis() as u64;
            return view;
        }
    };

    // std 没有带超时的 wait —— 轮询 try_wait, 到点就杀。
    // 不能无限等: 一台掉线的手机能把 adb 卡到天荒地老, 面板不该跟着一起卡死。
    let deadline = Instant::now() + cfg.timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    view.problem = Some(format!(
                        "`{command}` 超过 {}s 没回来, 已终止",
                        cfg.timeout.as_secs()
                    ));
                    view.took_ms = started.elapsed().as_millis() as u64;
                    return view;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                view.problem = Some(format!("等 `{command}` 时出错: {e}"));
                view.took_ms = started.elapsed().as_millis() as u64;
                return view;
            }
        }
    }

    let out = match child.wait_with_output() {
        Ok(o) => o,
        Err(e) => {
            view.problem = Some(format!("读 `{command}` 的输出失败: {e}"));
            view.took_ms = started.elapsed().as_millis() as u64;
            return view;
        }
    };
    view.took_ms = started.elapsed().as_millis() as u64;

    let stdout = String::from_utf8_lossy(&out.stdout);
    match parse(&stdout) {
        Ok(f) => view.fleet = Some(f),
        Err(e) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            view.problem = Some(format!(
                "`{command}` 的输出解析不了: {e}{}",
                if stderr.trim().is_empty() {
                    String::new()
                } else {
                    format!(" (stderr: {})", stderr.trim())
                }
            ));
        }
    }
    view
}

/// 解析 `phonefarm fleet --json` 的回包。
///
/// 退出码**不作判据**: phonefarm 约定 1 = 一台设备都没有, 那时 JSON 照样是好的,
/// 面板要显示"农场里一台机器都没有"而不是"读取失败"。能不能用只看 JSON 解不解得开。
pub fn parse(stdout: &str) -> Result<Fleet, String> {
    // 从第一个 `{` 读到末尾, 而不是"找出那一行 JSON":
    // phonefarm 现在吐的是单行, 但换成 to_string_pretty 也不该让面板挂掉。
    let start = stdout.find('{').ok_or("输出里没有 JSON")?;
    serde_json::from_str(&stdout[start..]).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const REAL: &str = include_str!("testdata/fleet_light.json");

    #[test]
    fn parses_a_real_light_probe() {
        let f = parse(REAL).expect("真机回包必须解得开");
        assert_eq!(f.schema.as_deref(), Some("phonefarm/fleet@1"));
        assert_eq!(f.devices.len(), 1);
        let d = &f.devices[0];
        assert_eq!(d.model.as_deref(), Some("NX809J"));
        assert_eq!(d.os.as_deref(), Some("Android 16 (SDK 36)"));
        assert!(d.online);
        assert_eq!(d.battery.level_pct, Some(100));
        assert!(
            d.thermal.gpu_c.is_some_and(|c| c > 20.0 && c < 100.0),
            "GPU 温度要是个像样的读数: {:?}",
            d.thermal.gpu_c
        );
        assert_eq!(d.fan.enabled, Some(true));
        assert_eq!(d.fan.speed_level, Some(5));

        // 轻量档: dumpsys 那几项是空的, 而且带着为什么
        assert_eq!(d.probe_depth.as_deref(), Some("light"));
        assert_eq!(d.foreground, None);
        assert_eq!(d.screen_on, None);
        assert_eq!(d.root, None, "没探过 root 就该是 null, 不是 false");
        assert!(d.unknown.iter().any(|u| u.contains("设备锁被别人持有")));

        let lock = f.lock.as_ref().expect("回包里带着设备锁");
        assert_eq!(lock.held_by.as_deref(), Some("wb-vksamples"));
        assert_eq!(lock.queue.first().map(String::as_str), Some("gol-probe"));
        assert!(!lock.recent.is_empty());
    }

    /// phonefarm 那侧加字段不该让面板打不开。
    #[test]
    fn unknown_fields_do_not_break_parsing() {
        let json = r#"{"schema":"phonefarm/fleet@2","brand_new_field":42,
            "devices":[{"serial":"X","online":true,"something_new":{"a":1}}]}"#;
        let f = parse(json).expect("多出来的字段要能忽略");
        assert_eq!(f.devices.len(), 1);
        assert_eq!(f.devices[0].serial, "X");
    }

    /// 一台设备都没有时 phonefarm 退出码是 1, 但 JSON 是好的 ——
    /// 面板要显示"农场里没有机器", 不是"读取失败"。
    #[test]
    fn an_empty_farm_is_valid_data_not_an_error() {
        let f = parse(r#"{"schema":"phonefarm/fleet@1","devices":[],"notes":["没找到 adb"]}"#)
            .expect("空农场也是一份好数据");
        assert!(f.devices.is_empty());
        assert_eq!(f.notes.len(), 1);
    }

    #[test]
    fn junk_before_the_json_is_skipped() {
        let f = parse("[keepalive] 正在探测\n某个进度行\n{\"devices\":[]}\n").unwrap();
        assert!(f.devices.is_empty());
        assert!(parse("完全没有 JSON").is_err());
        assert!(parse("").is_err());
    }

    /// 现在 phonefarm 吐的是单行, 但换成缩进过的 JSON 也不该让面板挂掉。
    #[test]
    fn pretty_printed_json_also_parses() {
        let f = parse("{\n  \"devices\": [\n    {\"serial\": \"X\"}\n  ]\n}").unwrap();
        assert_eq!(f.devices[0].serial, "X");
    }

    #[test]
    fn argv_carries_the_lock_dir() {
        let cfg = FleetConfig {
            bin: PathBuf::from("/x/phonefarm"),
            cwd: PathBuf::from("/x"),
            ttl: Duration::from_secs(25),
            timeout: Duration::from_secs(30),
            lock_dir: Some(PathBuf::from("/private/tmp/claude-501")),
            screenshot_dir: None,
            screenshot_every_s: 300,
            enabled: true,
        };
        let argv = cfg.argv();
        assert_eq!(argv[1], "fleet");
        assert!(argv.contains(&"--json".to_string()));
        assert!(argv.contains(&"/private/tmp/claude-501".to_string()));
        assert!(
            !argv.contains(&"--screenshot-dir".to_string()),
            "截图默认不开"
        );
    }

    #[test]
    fn screenshots_are_opt_in_and_carry_the_interval_and_label() {
        let cfg = FleetConfig {
            bin: PathBuf::from("/x/phonefarm"),
            cwd: PathBuf::from("/x"),
            ttl: Duration::from_secs(25),
            timeout: Duration::from_secs(30),
            lock_dir: None,
            screenshot_dir: Some(PathBuf::from("/x/tasks/_fleet")),
            screenshot_every_s: 300,
            enabled: true,
        };
        let argv = cfg.argv();
        assert!(argv.contains(&"--screenshot-dir".to_string()));
        assert!(argv.contains(&"300".to_string()), "最小间隔要传过去");
        // 面板从不持锁, 所以带上标签等于告诉 phonefarm「别人持锁时别截」
        assert!(argv.contains(&"gol-panel".to_string()));
    }

    #[test]
    fn a_missing_binary_is_reported_not_panicked() {
        let cfg = FleetConfig {
            bin: PathBuf::from("/绝对不存在/phonefarm"),
            cwd: PathBuf::from("/tmp"),
            ttl: Duration::from_secs(25),
            timeout: Duration::from_secs(5),
            lock_dir: None,
            screenshot_dir: None,
            screenshot_every_s: 300,
            enabled: true,
        };
        let view = probe(&cfg);
        assert!(view.fleet.is_none());
        assert!(view.problem.as_ref().unwrap().contains("没找到 phonefarm"));
    }

    #[test]
    fn disabled_means_no_view_at_all() {
        let cfg = FleetConfig {
            bin: PathBuf::from("/x"),
            cwd: PathBuf::from("/x"),
            ttl: Duration::from_secs(25),
            timeout: Duration::from_secs(5),
            lock_dir: None,
            screenshot_dir: None,
            screenshot_every_s: 300,
            enabled: false,
        };
        assert!(get(&cfg).is_none());
    }
}
