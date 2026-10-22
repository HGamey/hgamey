//! 设备锁: `optimize` 自己拿锁、心跳、释放。
//!
//! 锁本体是农场共用的 `devlock` 脚本 (phonefarm 仓库 `tools/devlock`, 原子 mkdir +
//! 心跳 + 排队)。这里不重新实现锁协议, 只调那个脚本 —— 与 phonefarm 入口那一道
//! (`phonefarm/src/devlock.rs`) 同一个做法, 两边语义由构造保证一致。
//!
//! 流程:
//!
//! 1. `devlock renew <label>` 成功 = 锁已经在本 label 手里 (外层 `devlock run <label>`
//!    包着我们)。原地跑, 心跳与释放归外层, 不然就是自己等自己。
//! 2. 否则 `devlock acquire <label>` 排队 (45 秒重试, 重试不丢队位); 等满
//!    `--lock-wait` 还没轮到就报错退出, **一个字节都不碰设备**。
//! 3. 拿到后后台每 60 秒续一次心跳; 守卫 drop (正常返回、`?` 提前返回、panic 展开)
//!    时停心跳并释放。
//!
//! 持锁期间把 `DEVLOCK_LABEL=<label>` 放进环境: 我们调起的 `phonefarm probe/eval`
//! 走它自己入口的第 1 步, 认出锁是这一侧的, 原地执行而不是再排一次队。

use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// 没给 label、环境里也没有 `DEVLOCK_LABEL` 时用的名字。
pub const DEFAULT_LABEL: &str = "game_opt_loop";
const POLL: Duration = Duration::from_secs(45);
const HEARTBEAT: Duration = Duration::from_secs(60);

/// 锁脚本在哪: `DEVLOCK_BIN` > `<phonefarm 二进制所在目录>/tools/devlock` > PATH 上的 `devlock`。
pub fn script_path(phonefarm_bin: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("DEVLOCK_BIN") {
        if !p.is_empty() {
            return Some(PathBuf::from(p));
        }
    }
    let repo = phonefarm_bin
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let p = repo.join("tools/devlock");
    if p.is_file() {
        return Some(p);
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|d| d.join("devlock"))
            .find(|p| p.is_file())
    })
}

/// 只读面板跟随所选 phonefarm 的锁目录，不根据主机或旧版本路径猜测。
/// 显式目录只覆盖面板读取的位置，不授予设备写入权限。
pub fn resolve_root(phonefarm: &Path, explicit: Option<&Path>) -> Result<PathBuf> {
    if let Some(root) = explicit {
        return Ok(std::path::absolute(root)?);
    }
    let output = Command::new(phonefarm)
        .args(["lease", "status", "--json"])
        .output()
        .with_context(|| format!("读取 {} 的锁目录失败", phonefarm.display()))?;
    if !output.status.success() {
        bail!(
            "{} lease status --json 失败 ({}): {}",
            phonefarm.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let status: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("phonefarm lease status 没有返回有效 JSON")?;
    let storage = &status["storage"];
    if storage["schema_version"].as_u64() != Some(1) {
        bail!(
            "phonefarm storage.schema_version 不支持: {} (需要版本 1)",
            storage["schema_version"]
        );
    }
    if storage["readiness"].as_str() != Some("ready") {
        bail!(
            "phonefarm 锁存储未就绪或版本不支持 storage.readiness: {}；请先升级或完成存储迁移",
            storage
        );
    }
    let root = storage["devlock_dir"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .context("phonefarm 未返回绝对路径 storage.devlock_dir；请升级 phonefarm")?;
    Ok(root)
}

/// label 的来源: 显式参数 > 环境 `DEVLOCK_LABEL` > [`DEFAULT_LABEL`]。
pub fn resolve_label(explicit: Option<&str>) -> String {
    explicit
        .map(str::to_string)
        .filter(|s| !s.is_empty())
        .or_else(|| {
            std::env::var("DEVLOCK_LABEL")
                .ok()
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_else(|| DEFAULT_LABEL.to_string())
}

/// 解析 `45m` / `2h` / `900s` / 裸数字 (按分钟)。
pub fn parse_wait(s: &str) -> Result<Duration> {
    let s = s.trim();
    let (num, mult) = match s.chars().last() {
        Some('s') => (&s[..s.len() - 1], 1),
        Some('m') => (&s[..s.len() - 1], 60),
        Some('h') => (&s[..s.len() - 1], 3600),
        _ => (s, 60),
    };
    match num.trim().parse::<u64>() {
        Ok(n) => Ok(Duration::from_secs(n * mult)),
        Err(_) => bail!("--lock-wait 看不懂 {s:?} (要 45m / 2h / 900s / 裸数字按分钟)"),
    }
}

fn call(script: &Path, phonefarm: &Path, args: &[&str]) -> (Option<i32>, String) {
    match Command::new(script)
        .env("PHONEFARM_BIN", phonefarm)
        .args(args)
        .output()
    {
        Ok(o) => (
            o.status.code(),
            format!(
                "{}{}",
                String::from_utf8_lossy(&o.stdout),
                String::from_utf8_lossy(&o.stderr)
            )
            .trim()
            .to_string(),
        ),
        Err(e) => (None, format!("调不起 {}: {e}", script.display())),
    }
}

/// 持锁守卫。drop 时停心跳并释放 (只释放自己拿的; 借外层的锁不动)。
pub struct Guard {
    script: Option<PathBuf>,
    phonefarm: PathBuf,
    label: String,
    owned: bool,
    stop: Arc<AtomicBool>,
    hb: Option<JoinHandle<()>>,
}

impl Guard {
    /// 显式不拿锁 (`--no-devlock`)。
    pub fn disabled() -> Self {
        eprintln!("⚠ --no-devlock: 不拿设备锁直接碰设备。共享手机上两边的数都可能作废。");
        Self {
            script: None,
            phonefarm: PathBuf::new(),
            label: String::new(),
            owned: false,
            stop: Arc::new(AtomicBool::new(true)),
            hb: None,
        }
    }

    pub fn label(&self) -> &str {
        &self.label
    }

    /// 拿锁。拿不到返回 Err, 调用方据此不碰设备。
    pub fn acquire(
        script: PathBuf,
        phonefarm: PathBuf,
        label: String,
        wait: Duration,
    ) -> Result<Self> {
        let phonefarm = std::path::absolute(phonefarm)?;
        // 1. 已在本 label 手里: 借用, 不心跳不释放
        if call(&script, &phonefarm, &["renew", &label]).0 == Some(0) {
            println!("设备锁: 已在 {label} 手里 (外层持有), 原地执行");
            std::env::set_var("DEVLOCK_LABEL", &label);
            return Ok(Self {
                script: Some(script),
                phonefarm,
                label,
                owned: false,
                stop: Arc::new(AtomicBool::new(true)),
                hb: None,
            });
        }
        // 2. 排队
        let t0 = Instant::now();
        loop {
            let (code, out) = call(&script, &phonefarm, &["acquire", &label]);
            match code {
                Some(0) => {
                    println!("设备锁: 已拿到 ({label})");
                    break;
                }
                Some(1) => {} // 明确锁忙才排队重试。
                _ => {
                    let status = code
                        .map(|c| format!("exit {c}"))
                        .unwrap_or_else(|| "启动失败或被信号终止".into());
                    bail!("设备锁不可重试错误 ({status}): {out}；没碰设备就退出");
                }
            }
            if t0.elapsed() + POLL > wait {
                bail!(
                    "设备锁: 等了 {} 秒还没轮到 {label}, 没碰设备就退出 ({out})。\
                     --lock-wait 可以放长; lead 预排了队列时 --lock-label 要用队列里的名字",
                    t0.elapsed().as_secs()
                );
            }
            println!(
                "设备锁: 还没轮到 {label} ({out}); {} 秒后重试, 已等 {} 秒",
                POLL.as_secs(),
                t0.elapsed().as_secs()
            );
            // 排队时被叫停 (SIGINT): 一秒一秒地睡, 好及时退出 —— 还没碰设备, 直接走
            for _ in 0..POLL.as_secs() {
                if crate::optimize::interrupted() {
                    bail!("设备锁: 排队时被叫停, 没碰设备就退出");
                }
                std::thread::sleep(Duration::from_secs(1));
            }
        }
        std::env::set_var("DEVLOCK_LABEL", &label);
        // 3. 心跳
        let stop = Arc::new(AtomicBool::new(false));
        let hb = {
            let (script, phonefarm, label, stop) = (
                script.clone(),
                phonefarm.clone(),
                label.clone(),
                stop.clone(),
            );
            std::thread::spawn(move || {
                let mut warned = false;
                let mut last = Instant::now();
                while !stop.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(500));
                    if last.elapsed() < HEARTBEAT {
                        continue;
                    }
                    last = Instant::now();
                    let (code, out) = call(&script, &phonefarm, &["renew", &label]);
                    if code != Some(0) && !warned {
                        // 不杀主流程: 正在跑的评测半途停下会把设备留在半施加态
                        eprintln!("⚠ 设备锁续期失败 ({label}): {out}");
                        warned = true;
                    }
                }
            })
        };
        Ok(Self {
            script: Some(script),
            phonefarm,
            label,
            owned: true,
            stop,
            hb: Some(hb),
        })
    }
}

impl Drop for Guard {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(h) = self.hb.take() {
            let _ = h.join();
        }
        if self.owned {
            if let Some(s) = &self.script {
                let (code, out) = call(s, &self.phonefarm, &["release", &self.label]);
                if code == Some(0) {
                    println!("设备锁: 已释放 ({})", self.label);
                } else {
                    eprintln!("设备锁释放失败 ({}): {out}", self.label);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wait_parses_units_and_bare_minutes() {
        assert_eq!(parse_wait("45m").unwrap(), Duration::from_secs(2700));
        assert_eq!(parse_wait("2h").unwrap(), Duration::from_secs(7200));
        assert_eq!(parse_wait("900s").unwrap(), Duration::from_secs(900));
        assert_eq!(parse_wait("10").unwrap(), Duration::from_secs(600));
        assert!(parse_wait("soon").is_err());
    }

    #[test]
    fn explicit_label_wins() {
        assert_eq!(resolve_label(Some("core")), "core");
    }

    #[test]
    #[cfg(unix)]
    fn root_follows_selected_phonefarm_and_rejects_unready_or_old_storage() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("gol_lock_root_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let phonefarm = dir.join("phonefarm");
        let response = dir.join("response.json");
        std::fs::write(&phonefarm, "#!/bin/sh\n[ \"$*\" = 'lease status --json' ] || exit 2\ncat \"$(dirname \"$0\")/response.json\"\n").unwrap();
        std::fs::set_permissions(&phonefarm, std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = dir.join("durable-locks");
        std::fs::write(
            &response,
            serde_json::json!({"storage": {
                "schema_version": 1, "readiness": "ready", "devlock_dir": root
            }})
            .to_string(),
        )
        .unwrap();
        assert_eq!(resolve_root(&phonefarm, None).unwrap(), root);
        for schema_version in [serde_json::Value::Null, serde_json::json!(2)] {
            std::fs::write(
                &response,
                serde_json::json!({"storage": {
                    "schema_version": schema_version, "readiness": "ready", "devlock_dir": root
                }})
                .to_string(),
            )
            .unwrap();
            assert!(resolve_root(&phonefarm, None)
                .unwrap_err()
                .to_string()
                .contains("schema_version"));
        }
        for readiness in ["unavailable", "migration_required"] {
            std::fs::write(
                &response,
                serde_json::json!({"storage": {
                    "schema_version": 1, "readiness": readiness, "devlock_dir": root
                }})
                .to_string(),
            )
            .unwrap();
            assert!(resolve_root(&phonefarm, None)
                .unwrap_err()
                .to_string()
                .contains(readiness));
        }
        std::fs::write(&response, "{\"status\":\"clear\"}").unwrap();
        assert!(
            resolve_root(&phonefarm, None).is_err(),
            "旧协议不能被猜成空闲"
        );
        std::fs::write(&phonefarm, "#!/bin/sh\nexit 7\n").unwrap();
        assert!(resolve_root(&phonefarm, None)
            .unwrap_err()
            .to_string()
            .contains("失败"));
        assert_eq!(
            resolve_root(&dir.join("missing-phonefarm"), Some(&root)).unwrap(),
            root
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    /// 用一个假锁脚本走一遍: 拿锁 → drop 释放; 外层已持有时借用、不释放。
    #[test]
    fn guard_acquires_releases_and_borrows() {
        let dir = std::env::temp_dir().join(format!("gol_devlock_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("log");
        let held = dir.join("held");
        let script = dir.join("devlock");
        let phonefarm = dir.join("selected-phonefarm");
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\n[ \"$PHONEFARM_BIN\" = '{phonefarm}' ] || exit 9\necho \"$1 $2\" >> {log}\ncase $1 in\n\
                 renew) [ -f {held} ];;\n\
                 acquire) touch {held};;\n\
                 release) rm -f {held};;\nesac\n",
                log = log.display(),
                held = held.display(),
                phonefarm = phonefarm.display()
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        {
            let g = Guard::acquire(
                script.clone(),
                phonefarm.clone(),
                "t".into(),
                Duration::from_secs(60),
            )
            .unwrap();
            assert!(g.owned);
            assert!(held.exists());
            // 外层已持有: 借用
            let inner = Guard::acquire(
                script.clone(),
                phonefarm.clone(),
                "t".into(),
                Duration::from_secs(60),
            )
            .unwrap();
            assert!(!inner.owned);
            drop(inner);
            assert!(held.exists(), "借用的守卫不该释放外层的锁");
        }
        assert!(!held.exists(), "自己拿的锁 drop 时要释放");
        let l = std::fs::read_to_string(&log).unwrap();
        assert!(l.contains("acquire t") && l.contains("release t"), "{l}");

        // 配置错误不能消耗排队预算；renew 失败后仅尝试 acquire 一次。
        std::fs::write(&log, "").unwrap();
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\necho \"$1 $2\" >> {}\necho migration_required >&2\nexit 2\n",
                log.display()
            ),
        )
        .unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        let attempt = std::thread::spawn(move || {
            tx.send(
                Guard::acquire(
                    script,
                    phonefarm,
                    "storage-error".into(),
                    Duration::from_secs(7200),
                )
                .map(|_| ())
                .map_err(|e| e.to_string()),
            )
            .unwrap();
        });
        let error = rx
            .recv_timeout(Duration::from_secs(3))
            .expect("storage error must return without waiting for the busy retry interval")
            .unwrap_err();
        attempt.join().unwrap();
        assert!(
            error.contains("exit 2") && error.contains("migration_required"),
            "{error}"
        );
        assert_eq!(
            std::fs::read_to_string(&log).unwrap(),
            "renew storage-error\nacquire storage-error\n"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
