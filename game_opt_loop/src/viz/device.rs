//! 设备页: 谁在用手机、心跳多久没动、后面排了谁。
//!
//! 数据就是 `devlock` 那三个文件, **直接读, 不去 exec 那个脚本**:
//! 面板是只读的, 不该有任何机会去动锁。
//!
//! ```text
//! devlock.d/owner   "<label> since YYYY-MM-DD HH:MM:SS"; 文件 mtime 就是心跳
//! devlock.queue     一行一个 label, 排第一的才轮到它拿锁
//! devlock.log       "YYYY-MM-DD HH:MM:SS acquire|release|steal …"
//! ```

use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;
use std::time::SystemTime;

/// `devlock` 认为心跳多久没动就算废弃 (脚本里的 `STALE` 缺省值)。
pub const STALE_SECS: u64 = 600;

#[derive(Debug, Clone, Serialize)]
pub struct DeviceView {
    /// `None` = 锁是空的, 现在没人用手机。
    pub holder: Option<LockHolder>,
    pub queue: Vec<String>,
    pub history: Vec<LockEvent>,
    pub recent_tasks: Vec<TaskRun>,
    /// serial → 这台机器最近一次操作。没记 serial 的旧日志不进这个表。
    pub last_touch: BTreeMap<String, LastTouch>,
    /// 一共翻了多少份对局记录 —— 空表时能分清"没翻到"和"翻了但都没记 serial"。
    pub scanned_runs: usize,
    /// 读不到锁目录时说清楚是哪条路径, 别让人对着空白页猜。
    pub problems: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LockHolder {
    pub label: String,
    /// owner 文件里自报的取锁时间 (原样字符串)。
    pub since: Option<String>,
    /// 心跳距今多少秒 (= owner 文件 mtime 距今)。
    pub heartbeat_age_s: Option<u64>,
    /// 心跳超期 → 这把锁可以被别人接管了。
    pub stale: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LockEvent {
    pub at: String,
    /// `acquire` / `release` / `steal`
    pub action: String,
    pub label: String,
    pub raw: String,
}

/// 一次真机任务回放 (`tasks/<task>/runs/<run>/log.jsonl`)。
#[derive(Debug, Clone, Serialize)]
pub struct TaskRun {
    pub task: String,
    pub run: String,
    pub goal: Option<String>,
    /// 这一局落在哪台机器上。`None` = 旧日志没记 (phonefarm 是后来才写进去的)。
    pub serial: Option<String>,
    pub modified: Option<String>,
}

/// 某台机器最近一次被动过是什么时候。
#[derive(Debug, Clone, Serialize)]
pub struct LastTouch {
    pub task: String,
    pub run: String,
    pub at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub goal: Option<String>,
}

/// 解析 owner 文件内容: `"sysknob-loop since 2026-09-23 10:12:31"`。
pub fn parse_owner(text: &str) -> Option<(String, Option<String>)> {
    let text = text.trim();
    if text.is_empty() {
        return None;
    }
    match text.split_once(" since ") {
        Some((label, since)) => Some((label.trim().to_string(), Some(since.trim().to_string()))),
        // 脚本有一条兜底分支会写进不带 "since" 的内容, 那也得认。
        None => Some((text.split_whitespace().next()?.to_string(), None)),
    }
}

/// 解析一行日志: `"2026-09-23 09:16:41 acquire sysknob-loop"`。
///
/// `steal` 那行后面还跟着被抢者和空闲秒数, 所以 label 只取动作后的第一个词。
pub fn parse_log_line(line: &str) -> Option<LockEvent> {
    let line = line.trim();
    let mut parts = line.split_whitespace();
    let date = parts.next()?;
    let time = parts.next()?;
    let action = parts.next()?;
    if !matches!(action, "acquire" | "release" | "steal") {
        return None;
    }
    // `steal` 行长这样: `steal by <新主> from [<旧主>] idle 900s` —— 跳过 "by"。
    let mut label = parts.next()?;
    if action == "steal" && label == "by" {
        label = parts.next()?;
    }
    Some(LockEvent {
        at: format!("{date} {time}"),
        action: action.to_string(),
        label: label.to_string(),
        raw: line.to_string(),
    })
}

fn age_secs(path: &Path) -> Option<u64> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    SystemTime::now().duration_since(modified).ok().map(|d| d.as_secs())
}

fn mtime_rfc3339(path: &Path) -> Option<String> {
    let modified = std::fs::metadata(path).ok()?.modified().ok()?;
    let dt: chrono::DateTime<chrono::Local> = modified.into();
    Some(dt.to_rfc3339_opts(chrono::SecondsFormat::Secs, false))
}

/// 读锁状态。`lock_dir` 是 `devlock.d` 的父目录 (缺省 `/private/tmp/claude-501`)。
pub fn load(lock_root: &Path, phonefarm_root: &Path, max_tasks: usize) -> DeviceView {
    let mut problems = Vec::new();
    let owner_path = lock_root.join("devlock.d/owner");
    let holder = match std::fs::read_to_string(&owner_path) {
        Ok(text) => parse_owner(&text).map(|(label, since)| {
            let heartbeat_age_s = age_secs(&owner_path);
            LockHolder {
                label,
                since,
                stale: heartbeat_age_s.is_some_and(|a| a > STALE_SECS),
                heartbeat_age_s,
            }
        }),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => {
            problems.push(format!("{} 读不出来: {e}", owner_path.display()));
            None
        }
    };

    let queue = std::fs::read_to_string(lock_root.join("devlock.queue"))
        .map(|t| {
            t.lines()
                .map(str::trim)
                .filter(|l| !l.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();

    let mut history: Vec<LockEvent> = std::fs::read_to_string(lock_root.join("devlock.log"))
        .map(|t| t.lines().filter_map(parse_log_line).collect())
        .unwrap_or_default();
    history.reverse(); // 最新的排最前
    history.truncate(20);

    let scan = scan_runs(phonefarm_root, max_tasks, LAST_TOUCH_SCAN_RUNS);
    DeviceView {
        holder,
        queue,
        history,
        recent_tasks: scan.recent,
        last_touch: scan.last_touch,
        scanned_runs: scan.scanned,
        problems,
    }
}

/// 回溯「最近一次操作」时最多翻多少份对局记录。
///
/// 只翻最新的那些: 按 mtime 倒序扫, 每台机器第一次命中就是它最近那一次。
/// 封顶是因为 tasks 下有上百个 run, 而面板 30 秒刷一次。
const LAST_TOUCH_SCAN_RUNS: usize = 200;

pub struct RunScan {
    pub recent: Vec<TaskRun>,
    pub last_touch: BTreeMap<String, LastTouch>,
    pub scanned: usize,
}

/// 扫一遍对局记录, 一次产出两样东西: 最近几次任务, 以及每台机器最近一次被动过。
///
/// 按目录 mtime 倒序扫, 所以每台机器**第一次**命中就是它最近那一次, 后面的不用管。
/// 每份日志**只读开头几十 KB** (start 与 goal 都在前几行) —— 任务日志能到几十 MB,
/// 为了两行字把整个文件读进内存不值当。
fn scan_runs(phonefarm_root: &Path, max_recent: usize, max_scan: usize) -> RunScan {
    let tasks_dir = phonefarm_root.join("tasks");
    let Ok(tasks) = std::fs::read_dir(&tasks_dir) else {
        return RunScan {
            recent: Vec::new(),
            last_touch: BTreeMap::new(),
            scanned: 0,
        };
    };
    let mut found: Vec<(SystemTime, TaskRun)> = Vec::new();
    for task in tasks.filter_map(Result::ok) {
        let runs_dir = task.path().join("runs");
        let Ok(runs) = std::fs::read_dir(&runs_dir) else {
            continue;
        };
        for run in runs.filter_map(Result::ok) {
            let log = run.path().join("log.jsonl");
            let Ok(meta) = std::fs::metadata(&log) else {
                continue;
            };
            let Ok(modified) = meta.modified() else {
                continue;
            };
            found.push((
                modified,
                TaskRun {
                    task: task.file_name().to_string_lossy().to_string(),
                    run: run.file_name().to_string_lossy().to_string(),
                    goal: None,
                    serial: None,
                    modified: mtime_rfc3339(&log),
                },
            ));
        }
    }
    found.sort_by_key(|a| std::cmp::Reverse(a.0));
    found.truncate(max_scan);
    let scanned = found.len();

    let mut recent = Vec::new();
    let mut last_touch: BTreeMap<String, LastTouch> = BTreeMap::new();
    for (i, (_, mut t)) in found.into_iter().enumerate() {
        let log = tasks_dir
            .join(&t.task)
            .join("runs")
            .join(&t.run)
            .join("log.jsonl");
        let head = head_of(&log, GOAL_SCAN_BYTES);
        t.goal = head.as_deref().and_then(goal_from_log);
        t.serial = head.as_deref().and_then(serial_from_log);
        if let Some(serial) = &t.serial {
            // 倒序扫, 所以第一次见到这台机器就是它最近那一次 —— 不覆盖。
            last_touch.entry(serial.clone()).or_insert_with(|| LastTouch {
                task: t.task.clone(),
                run: t.run.clone(),
                at: t.modified.clone(),
                goal: t.goal.clone(),
            });
        }
        if i < max_recent {
            recent.push(t);
        }
    }
    RunScan {
        recent,
        last_touch,
        scanned,
    }
}

/// 读文件开头至多 `limit` 字节。
///
/// 任务日志能到几十 MB (每帧一条), 而 goal 就在头几行 —— 面板 30 秒刷一次,
/// 没必要为了一行字把整个文件读进内存。
fn head_of(path: &Path, limit: usize) -> Option<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path).ok()?;
    let mut buf = vec![0u8; limit];
    let n = f.read(&mut buf).ok()?;
    buf.truncate(n);
    // 可能正好截断在一个多字节字符中间, 用 lossy 而不是让整次读取失败。
    Some(String::from_utf8_lossy(&buf).into_owned())
}

const GOAL_SCAN_BYTES: usize = 64 * 1024;

/// 从 `log.jsonl` 里找 start 行的 `serial`。
///
/// `None` 有两种可能, 对读的人来说是同一件事 —— 这份记录说不出是哪台机器:
/// 旧日志压根没这个字段, 或者当时在线的机器不止一台, phonefarm 判不出就记了 null。
pub fn serial_from_log(text: &str) -> Option<String> {
    for line in text.lines().take(8) {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if v.get("r").and_then(|r| r.as_str()) == Some("start") {
            return v
                .get("serial")
                .and_then(|s| s.as_str())
                .filter(|s| !s.trim().is_empty())
                .map(str::to_string);
        }
    }
    None
}

/// 从 `log.jsonl` 里找 `{"r":"goal","t":"…"}`。只扫前若干行 —— goal 就在开头。
pub fn goal_from_log(text: &str) -> Option<String> {
    for line in text.lines().take(8) {
        // 解析不了的行跳过, **不能**中断整次扫描:
        // 开头多一个空行就让这条任务变成「未知」, 那是把能读的数据丢掉。
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if v.get("r").and_then(|r| r.as_str()) == Some("goal") {
            return v.get("t").and_then(|t| t.as_str()).map(str::to_string);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_line_is_parsed() {
        let (label, since) = parse_owner("sysknob-loop since 2026-09-23 10:12:31\n").unwrap();
        assert_eq!(label, "sysknob-loop");
        assert_eq!(since.as_deref(), Some("2026-09-23 10:12:31"));
    }

    #[test]
    fn owner_without_since_still_yields_a_label() {
        let (label, since) = parse_owner("knobs-layer").unwrap();
        assert_eq!(label, "knobs-layer");
        assert_eq!(since, None);
    }

    #[test]
    fn empty_owner_means_nobody() {
        assert!(parse_owner("   \n").is_none());
    }

    #[test]
    fn log_lines_are_parsed_and_junk_ignored() {
        let ev = parse_log_line("2026-09-23 09:16:41 acquire sysknob-loop").unwrap();
        assert_eq!(ev.at, "2026-09-23 09:16:41");
        assert_eq!(ev.action, "acquire");
        assert_eq!(ev.label, "sysknob-loop");
        assert!(parse_log_line("这不是一行锁日志").is_none());
        assert!(parse_log_line("2026-09-23 09:16:41 renew x").is_none());
    }

    #[test]
    fn steal_line_takes_the_new_owner_as_label() {
        let ev =
            parse_log_line("2026-09-23 09:16:41 steal by wb-megacity from [old] idle 900s").unwrap();
        assert_eq!(ev.action, "steal");
        assert_eq!(ev.label, "wb-megacity", "steal 行要跳过 'by' 取到新主人");
        assert!(ev.raw.contains("idle 900s"));
    }

    #[test]
    fn goal_is_read_from_the_head_of_the_log() {
        let log = "{\"v\":1}\n{\"r\":\"goal\",\"t\":\"玩2048\"}\n{\"r\":\"screen\"}\n";
        assert_eq!(goal_from_log(log).as_deref(), Some("玩2048"));
        assert_eq!(goal_from_log("{\"v\":1}\n"), None);
        // 开头有非 JSON 行时不能就此罢手 —— 后面那行 goal 还在。
        let noisy = "\n不是 JSON\n{\"r\":\"goal\",\"t\":\"玩2048\"}\n";
        assert_eq!(goal_from_log(noisy).as_deref(), Some("玩2048"));
    }

    #[test]
    fn serial_is_read_from_the_start_line() {
        let log = "{\"v\":1}\n{\"r\":\"start\",\"pid\":1,\"serial\":\"91253241019A\",\"ts\":1}\n";
        assert_eq!(serial_from_log(log).as_deref(), Some("91253241019A"));

        // 旧日志压根没这个字段
        assert_eq!(serial_from_log("{\"v\":1}\n{\"r\":\"start\",\"pid\":1}\n"), None);
        // phonefarm 判不出是哪台时会记 null —— 同样当未知
        assert_eq!(serial_from_log("{\"r\":\"start\",\"serial\":null}\n"), None);
        assert_eq!(serial_from_log("{\"r\":\"start\",\"serial\":\"  \"}\n"), None);
        // 开头有非 JSON 行也要能继续往下找
        assert_eq!(
            serial_from_log("不是 JSON\n{\"r\":\"start\",\"serial\":\"hdc:abc\"}\n").as_deref(),
            Some("hdc:abc")
        );
    }

    #[test]
    fn missing_lock_root_is_reported_as_free_not_crashed() {
        let view = load(
            Path::new("/nonexistent/lock"),
            Path::new("/nonexistent/phonefarm"),
            5,
        );
        assert!(view.holder.is_none());
        assert!(view.queue.is_empty());
        assert!(view.recent_tasks.is_empty());
        assert!(view.last_touch.is_empty());
        assert_eq!(view.scanned_runs, 0);
    }
}
