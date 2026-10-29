//! 多台同型号设备并行评测的调度。
//!
//! 一代的候选彼此独立 (下一代才看这一代的结果), 所以同一代可以分给几台手机同时量。
//! 每个候选在**它被分到的那台手机上**自带对照臂 (phonefarm eval 的 ABBA), 判定只在
//! 同一台设备内部比较, 不跨设备比 —— 同型号的两台手机体质也不一样, 跨设备比就把
//! 机器差异算进了候选效果。
//!
//! 调度是一个共享游标: 每台设备一个线程, 跑完一个就领下一个, 快的机器多干。结果按
//! 候选的原顺序交回, 下一代的历史、档案顺序与单机时一致。
//!
//! 只有一台设备时不起线程, 在当前线程里按顺序跑 —— 与引入调度之前逐字节相同的行为。

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// 把 `items` 分给 `devices` 跑, 每台同一时刻只跑一个。返回 `(设备, 结果)`, 按 `items` 原顺序。
///
/// `devices` 为空时什么都不跑, 返回空表。
pub fn run_on_devices<T, R, F>(items: &[T], devices: &[String], f: F) -> Vec<(String, R)>
where
    T: Sync,
    R: Send,
    F: Fn(&str, usize, &T) -> R + Sync,
{
    if devices.is_empty() || items.is_empty() {
        return Vec::new();
    }
    if devices.len() == 1 {
        let d = &devices[0];
        return items
            .iter()
            .enumerate()
            .map(|(i, it)| (d.clone(), f(d, i, it)))
            .collect();
    }
    let next = AtomicUsize::new(0);
    let slots: Mutex<Vec<Option<(String, R)>>> = Mutex::new((0..items.len()).map(|_| None).collect());
    std::thread::scope(|s| {
        for d in devices {
            let (next, slots, f) = (&next, &slots, &f);
            s.spawn(move || loop {
                let i = next.fetch_add(1, Ordering::SeqCst);
                if i >= items.len() {
                    break;
                }
                let r = f(d, i, &items[i]);
                slots.lock().unwrap_or_else(|e| e.into_inner())[i] = Some((d.clone(), r));
            });
        }
    });
    slots
        .into_inner()
        .unwrap_or_else(|e| e.into_inner())
        .into_iter()
        .map(|x| x.expect("每个候选都被某台设备领走并跑完"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::{Barrier, Condvar};

    fn devs(n: usize) -> Vec<String> {
        (0..n).map(|i| format!("dev{i}")).collect()
    }

    #[test]
    fn one_device_runs_in_order_on_the_calling_thread() {
        let me = std::thread::current().id();
        let order = Mutex::new(Vec::new());
        let out = run_on_devices(&[10, 20, 30], &devs(1), |d, i, x| {
            assert_eq!(std::thread::current().id(), me);
            order.lock().unwrap().push(i);
            format!("{d}:{x}")
        });
        assert_eq!(*order.lock().unwrap(), vec![0, 1, 2]);
        assert_eq!(
            out,
            vec![
                ("dev0".to_string(), "dev0:10".to_string()),
                ("dev0".into(), "dev0:20".into()),
                ("dev0".into(), "dev0:30".into())
            ]
        );
    }

    /// 首批三个任务都在 Barrier 前持忙等待，证明三台设备同时进入；
    /// 每个候选恰好跑一次，结果按原顺序，每台同一时刻只跑一个。
    #[test]
    fn several_devices_share_the_work_and_keep_order() {
        let busy: Mutex<BTreeMap<String, bool>> = Mutex::new(BTreeMap::new());
        let entered = Barrier::new(3);
        let runs = AtomicUsize::new(0);
        let items: Vec<usize> = (0..6).collect();
        let out = run_on_devices(&items, &devs(3), |d, i, x| {
            {
                let mut b = busy.lock().unwrap();
                assert!(!b.get(d).copied().unwrap_or(false), "{d} 同时跑了两个候选");
                b.insert(d.to_string(), true);
            }
            runs.fetch_add(1, Ordering::SeqCst);
            if i < 3 {
                entered.wait();
            }
            busy.lock().unwrap().insert(d.to_string(), false);
            assert_eq!(i, *x);
            x * 100
        });
        assert_eq!(runs.load(Ordering::SeqCst), 6);
        assert_eq!(out.iter().map(|(_, r)| *r).collect::<Vec<_>>(), vec![0, 100, 200, 300, 400, 500]);
        assert!(out.iter().all(|(d, _)| d.starts_with("dev")));
    }

    /// 慢设备占住首项，快设备完成其余五项后用 Condvar 释放它，证明动态领活。
    #[test]
    fn faster_device_takes_more_candidates() {
        const FAST_TASKS: usize = 5;
        let entered = Barrier::new(2);
        let fast_completed = Mutex::new(0usize);
        let release_slow = Condvar::new();
        let items: Vec<usize> = (0..=FAST_TASKS).collect();
        let out = run_on_devices(&items, &devs(2), |d, i, _| {
            if i < 2 {
                entered.wait();
            }
            if d == "dev0" {
                let mut completed = fast_completed.lock().unwrap();
                while *completed < FAST_TASKS {
                    completed = release_slow.wait(completed).unwrap();
                }
            } else {
                let mut completed = fast_completed.lock().unwrap();
                *completed += 1;
                if *completed == FAST_TASKS {
                    release_slow.notify_one();
                }
            }
        });
        let fast = out.iter().filter(|(d, _)| d == "dev1").count();
        assert_eq!(fast, FAST_TASKS, "快机应领走慢机阻塞期间的全部剩余任务");
        assert_eq!(out.iter().filter(|(d, _)| d == "dev0").count(), 1);
    }

    #[test]
    fn nothing_to_do_returns_empty() {
        assert!(run_on_devices(&[1, 2], &[], |_, _, x| *x).is_empty());
        assert!(run_on_devices::<i32, i32, _>(&[], &devs(2), |_, _, x| *x).is_empty());
    }
}
