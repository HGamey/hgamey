//! 测试共用: 写出(或拷出)随后要执行的假程序。
//!
//! 由子进程 `sh` 来写, 测试进程自己从不以写方式打开它: 测试进程里别的测试线程 fork 时会把打开着的写句柄带进子进程,
//! 那个子进程 exec 之前, 执行这个文件都会得到 ETXTBSY ("Text file busy")。先写临时文件再改名没用, inode 还是那个。
//! 单元测试经 `crate::test_script` 用同一份文件。
#![allow(dead_code)]

use std::path::Path;
use std::process::Command;

fn sh(script: &str, args: &[&std::ffi::OsStr]) {
    let status = Command::new("sh").arg("-c").arg(script).args(args).status().unwrap();
    assert!(status.success(), "sh -c {script} failed");
}

/// `body` 写到 `path`, 权限 `mode` (如 0o755)。
pub fn write_script(path: &Path, body: &str, mode: u32) {
    let mode = format!("{mode:o}");
    sh("printf '%s' \"$1\" > \"$0\" && chmod \"$2\" \"$0\"",
       &[path.as_os_str(), body.as_ref(), mode.as_ref()]);
}

/// 把可执行文件 `from` 拷到 `to`, 权限 `mode`。
pub fn copy_executable(from: &Path, to: &Path, mode: u32) {
    let mode = format!("{mode:o}");
    sh("cp \"$1\" \"$0\" && chmod \"$2\" \"$0\"", &[to.as_os_str(), from.as_os_str(), mode.as_ref()]);
}
