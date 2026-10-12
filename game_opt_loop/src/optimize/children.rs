//! 正在跑的 phonefarm 子进程登记表。
//!
//! 目标模式收到 SIGINT (网页「停止」) 时要把信号转给正在上机的 phonefarm, 让它走还原流程。
//! 不能靠「转给整个进程组」: 节点按进程组逐级发 INT/TERM/KILL 来停一个任务, goal run 自己
//! 另开进程组的话, 节点那一下就漏掉了它和它的子进程。所以进程组不动, 子进程 pid 记在这里,
//! 信号处理里逐个转 (只读原子量 + kill, 都是 async-signal-safe 的)。

use std::sync::atomic::{AtomicI32, Ordering};

const SLOTS: usize = 32;
static PIDS: [AtomicI32; SLOTS] = [const { AtomicI32::new(0) }; SLOTS];

/// 登记一个子进程 (槽满了就不记 —— 只影响叫停时能不能转到它, 不影响主流程)。
pub fn register(pid: u32) {
    let pid = pid as i32;
    for s in PIDS.iter() {
        if s.compare_exchange(0, pid, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            return;
        }
    }
}

pub fn unregister(pid: u32) {
    let pid = pid as i32;
    for s in PIDS.iter() {
        let _ = s.compare_exchange(pid, 0, Ordering::SeqCst, Ordering::SeqCst);
    }
}

/// 现在登记着的子进程。
pub fn live() -> Vec<i32> {
    PIDS.iter()
        .map(|s| s.load(Ordering::SeqCst))
        .filter(|p| *p > 0)
        .collect()
}

/// 给每个登记着的子进程发信号 (信号处理里调)。
#[cfg(unix)]
pub fn signal_all(sig: i32) {
    for s in PIDS.iter() {
        let pid = s.load(Ordering::SeqCst);
        if pid > 0 {
            unsafe {
                libc::kill(pid, sig);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn register_and_unregister() {
        register(4242424);
        assert!(live().contains(&4242424));
        unregister(4242424);
        assert!(!live().contains(&4242424));
    }
}
