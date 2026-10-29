//! Lock acquisition belongs to the selected phonefarm; GOL holds the transferred fds.
use anyhow::{bail, Context, Result};
use std::fs::File;
use std::os::fd::AsRawFd;
use std::os::unix::{net::UnixStream, process::CommandExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;
#[path = "../../phonefarm/src/lock_handoff.rs"]
mod handoff;
pub const DEFAULT_LABEL: &str = "game_opt_loop";

/// 只读面板跟随所选 phonefarm 的锁目录。
/// 显式目录只覆盖面板读取的位置，不授予设备写入权限。
pub fn resolve_lock_dir(phonefarm: &Path, explicit: Option<&Path>) -> Result<PathBuf> {
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
    if let Some(error) = storage["error"].as_str() { bail!("{error}"); }
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

pub struct Guard {
    files: Vec<File>,
    socket: Option<UnixStream>,
    helper: Child,
    label: String,
    previous: Vec<(&'static str, Option<std::ffi::OsString>)>,
}
impl Guard {
    pub fn label(&self) -> &str {
        &self.label
    }
    pub fn acquire(
        phonefarm: PathBuf,
        serial: Option<&str>,
        label: String,
        wait: Option<Duration>,
    ) -> Result<Self> {
        let (mut socket, peer) = UnixStream::pair()?;
        let fd = peer.as_raw_fd();
        let mut command = Command::new(phonefarm);
        command
            .args(["__lease-handoff", &fd.to_string(), "--label", &label])
            .stdin(Stdio::null())
            .stdout(Stdio::null());
        if let Some(serial) = serial {
            command.args(["--serial", serial]);
        }
        if let Some(wait) = wait {
            command.args(["--timeout-ms", &wait.as_millis().to_string()]);
        }
        unsafe {
            command.pre_exec(move || {
                if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut helper = command
            .spawn()
            .context("start selected phonefarm lock helper")?;
        drop(peer);
        socket.set_read_timeout(Some(Duration::from_millis(100)))?;
        let received = loop {
            if crate::optimize::interrupted() {
                break Err(anyhow::anyhow!("cancelled while waiting for device lock"));
            }
            match handoff::receive(&mut socket) {
                Ok(value) => break Ok(value),
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::WouldBlock
                            | std::io::ErrorKind::TimedOut
                            | std::io::ErrorKind::Interrupted
                    ) =>
                {
                    ()
                }
                Err(error) => break Err(error.into()),
            }
        };
        let (files, payload) = match received {
            Ok(value) => value,
            Err(error) => {
                let _ = helper.kill();
                let status = helper.wait()?;
                return Err(error.context(format!("phonefarm lock helper {status}")));
            }
        };
        let scope: serde_json::Value = serde_json::from_slice(&payload)?;
        let binding = (!scope["binding"].is_null()).then(|| scope["binding"].to_string());
        let values = [
            (
                "PF_LOCK_FDS",
                Some(
                    files
                        .iter()
                        .map(|file| file.as_raw_fd().to_string())
                        .collect::<Vec<_>>()
                        .join(","),
                ),
            ),
            ("PF_LOCK_SCOPE", Some(scope.to_string())),
            ("DEVLOCK_LABEL", Some(label.clone())),
            ("PF_JOB_DEVICE_BINDING", binding),
        ];
        let mut previous = Vec::new();
        for (key, value) in values {
            previous.push((key, std::env::var_os(key)));
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
        Ok(Self {
            files,
            socket: Some(socket),
            helper,
            label,
            previous,
        })
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        for (key, value) in self.previous.drain(..).rev() {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
        self.files.clear();
        drop(self.socket.take());
        if let Err(error) = self.helper.wait() {
            eprintln!("phonefarm lock helper: {error}");
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
    fn lock_directory_follows_selected_phonefarm_without_layout_readiness() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("gol_lock_root_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let phonefarm = dir.join("phonefarm");
        let response = dir.join("response.json");
        std::fs::write(&phonefarm, "#!/bin/sh\n[ \"$*\" = 'lease status --json' ] || exit 2\ncat \"$(dirname \"$0\")/response.json\"\n").unwrap();
        std::fs::set_permissions(&phonefarm, std::fs::Permissions::from_mode(0o755)).unwrap();
        let root = dir.join("durable-locks");
        let mut value = serde_json::json!({"storage": {"schema_version":1,"devlock_dir":root,"error":null}});
        std::fs::write(&response, value.to_string()).unwrap();
        assert_eq!(resolve_lock_dir(&phonefarm, None).unwrap(), root);
        value["storage"]["layout_version"] = serde_json::json!(2);
        value["storage"]["queue_dir"] = serde_json::json!("obsolete/queue");
        std::fs::write(&response, value.to_string()).unwrap();
        assert_eq!(resolve_lock_dir(&phonefarm, None).unwrap(), root);
        for schema_version in [serde_json::Value::Null, serde_json::json!(2)] {
            value["storage"]["schema_version"] = schema_version;
            std::fs::write(&response, value.to_string()).unwrap();
            assert!(resolve_lock_dir(&phonefarm, None).unwrap_err().to_string().contains("schema_version"));
        }
        value["storage"]["schema_version"] = serde_json::json!(1);
        value["storage"]["error"] = serde_json::json!("actual path I/O error");
        std::fs::write(&response, value.to_string()).unwrap();
        assert!(resolve_lock_dir(&phonefarm, None).unwrap_err().to_string().contains("actual path I/O error"));
        value["storage"]["error"] = serde_json::Value::Null;
        value["storage"]["devlock_dir"] = serde_json::json!("relative");
        std::fs::write(&response, value.to_string()).unwrap();
        assert!(resolve_lock_dir(&phonefarm, None).is_err());
        std::fs::write(&response, "{\"status\":\"clear\"}").unwrap();
        assert!(resolve_lock_dir(&phonefarm, None).is_err());
        std::fs::write(&phonefarm, "#!/bin/sh\nexit 7\n").unwrap();
        assert!(resolve_lock_dir(&phonefarm, None).unwrap_err().to_string().contains("失败"));
        assert_eq!(resolve_lock_dir(&dir.join("missing-phonefarm"), Some(&root)).unwrap(), root);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn guard_acquires_releases_and_borrows() {
        if std::env::var_os("GOL_FD_GUARD_TEST").is_none() {
            // Environment and inheritable fds belong to one runtime, not to
            // unrelated tests spawning concurrently in libtest's process.
            let status = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "devlock::tests::guard_acquires_releases_and_borrows",
                ])
                .env("GOL_FD_GUARD_TEST", "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("gol_fd_lock_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let phonefarm = dir.join("selected-phonefarm");
        std::fs::write(&phonefarm, include_str!("../tests/fd_phonefarm.py")).unwrap();
        std::fs::set_permissions(&phonefarm, std::fs::Permissions::from_mode(0o755)).unwrap();
        let guard = Guard::acquire(phonefarm.clone(), Some("A"), "outer".into(), None).unwrap();
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&std::env::var("PF_LOCK_SCOPE").unwrap())
                .unwrap()["serial"],
            "A"
        );
        let probe = File::options()
            .read(true)
            .write(true)
            .open(dir.join("device.lock"))
            .unwrap();
        assert!(probe.try_lock().is_err());
        let nested = Guard::acquire(phonefarm.clone(), Some("A"), "nested".into(), None).unwrap();
        drop(nested);
        assert_eq!(std::env::var("DEVLOCK_LABEL").unwrap(), "outer");
        assert!(
            probe.try_lock().is_err(),
            "a nested drop must not release the outer fd"
        );
        let mut child = Command::new("python3").args(["-c", "import os,time;[os.fstat(int(fd)) for fd in os.environ['PF_LOCK_FDS'].split(',')];print('ready',flush=True);time.sleep(.3)"])
            .stdout(Stdio::piped()).spawn().unwrap();
        use std::io::BufRead;
        let mut ready = String::new();
        std::io::BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut ready)
            .unwrap();
        assert_eq!(ready, "ready\n");
        drop(guard);
        assert!(
            probe.try_lock().is_err(),
            "live task child must keep the inherited fd"
        );
        assert!(child.wait().unwrap().success());
        probe.try_lock().unwrap();
        drop(probe);
        let again =
            Guard::acquire(phonefarm.clone(), Some("A"), "after-pause".into(), None).unwrap();
        drop(again);
        let error = Guard::acquire(
            PathBuf::from("/usr/bin/false"),
            None,
            "failure".into(),
            None,
        )
        .err()
        .unwrap();
        assert!(
            error.to_string().contains("phonefarm lock helper"),
            "{error:#}"
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
}
