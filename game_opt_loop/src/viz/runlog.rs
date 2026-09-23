//! 读 phonefarm 单次运行留下的那几个文件。
//!
//! 一次运行的目录长这样 (`loop_v1/runs/knob1/`):
//!
//! ```text
//! summary.json       帧时/GPU/带宽的汇总
//! attribution.json   归因结论 (谁在卡)
//! snap_at_run.txt    开跑那一刻的设备状态快照 (key=value)
//! workload.log       动作脚本回放日志, 里面带电池温度
//! trace.txt          原始 ftrace, 几十 MB —— **面板永不读它**
//! ```
//!
//! 每个解析器都容许文件不存在: 面板宁可显示「未知/未测」, 也不要因为
//! 少一个文件就整页打不开。

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::Path;

/// `summary.json` 里面板用得上的那几项。
///
/// 只声明用到的字段 + `#[serde(default)]`: phonefarm 那侧加字段不该让面板炸。
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct RunSummary {
    #[serde(default)]
    pub span_s: Option<f64>,
    #[serde(default)]
    pub n_frames: Option<u64>,
    #[serde(default)]
    pub fps_mean: Option<f64>,
    #[serde(default)]
    pub frame_p50: Option<f64>,
    #[serde(default)]
    pub frame_p95: Option<f64>,
    #[serde(default)]
    pub frame_p99: Option<f64>,
    #[serde(default)]
    pub frame_mean: Option<f64>,
    #[serde(default)]
    pub gpu_active_mean: Option<f64>,
    #[serde(default)]
    pub gpu_active_p95: Option<f64>,
    #[serde(default)]
    pub queue_p50: Option<f64>,
    #[serde(default)]
    pub bw_median: Option<f64>,
    #[serde(default)]
    pub n_thermal_events: Option<u64>,
}

impl RunSummary {
    pub fn load(dir: &Path) -> Option<Self> {
        let raw = std::fs::read_to_string(dir.join("summary.json")).ok()?;
        serde_json::from_str(&raw).ok()
    }

    /// 按指标名取值 —— 报告里的 `frame_p95` 之类是字符串键, 前端也按名字切换。
    pub fn metric(&self, name: &str) -> Option<f64> {
        match name {
            "fps_mean" => self.fps_mean,
            "frame_p50" => self.frame_p50,
            "frame_p95" => self.frame_p95,
            "frame_p99" => self.frame_p99,
            "frame_mean" => self.frame_mean,
            "gpu_active_mean" => self.gpu_active_mean,
            "gpu_active_p95" => self.gpu_active_p95,
            "queue_p50" => self.queue_p50,
            "bw_median" => self.bw_median,
            _ => None,
        }
    }
}

/// `attribution.json` —— 归因结论。
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Attribution {
    #[serde(default)]
    pub verdict: Option<String>,
    #[serde(default)]
    pub evidence: Option<String>,
    #[serde(default)]
    pub actionable: Option<String>,
    #[serde(default)]
    pub measures: BTreeMap<String, serde_json::Value>,
}

impl Attribution {
    pub fn load(dir: &Path) -> Option<Self> {
        let raw = std::fs::read_to_string(dir.join("attribution.json")).ok()?;
        serde_json::from_str(&raw).ok()
    }

    pub fn measure_f64(&self, key: &str) -> Option<f64> {
        self.measures.get(key)?.as_f64()
    }
}

/// `snap_at_run.txt` —— 开跑那一刻的设备状态, 一行一个 `key=value`。
///
/// 以 `#` 开头的是注释行 (`# device_snapshot v1` / `# end`)。
#[derive(Debug, Clone, Default)]
pub struct DeviceSnapshot {
    pub entries: BTreeMap<String, String>,
}

impl DeviceSnapshot {
    pub fn parse(text: &str) -> Self {
        let mut entries = BTreeMap::new();
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some((k, v)) = line.split_once('=') {
                entries.insert(k.trim().to_string(), v.trim().to_string());
            }
        }
        DeviceSnapshot { entries }
    }

    pub fn load(dir: &Path) -> Option<Self> {
        let raw = std::fs::read_to_string(dir.join("snap_at_run.txt")).ok()?;
        Some(Self::parse(&raw))
    }

    /// 取值。`<unreadable>` 与 `null` 都当成"读不到", 返回 `None` ——
    /// 把 phonefarm 已经如实标出的"读不到"再翻译成一个字面值会丢信息。
    pub fn get(&self, key: &str) -> Option<&str> {
        match self.entries.get(key).map(String::as_str) {
            Some("<unreadable>") | Some("null") | Some("") | None => None,
            Some(v) => Some(v),
        }
    }

    /// 屏幕刷新率设置。两项都读不到时返回 `None` (= 系统没设, 交给应用自己限帧)。
    pub fn refresh_rate_setting(&self) -> Option<String> {
        let peak = self.get("settings.peak_refresh_rate");
        let min = self.get("settings.min_refresh_rate");
        match (peak, min) {
            (None, None) => None,
            (p, m) => Some(format!(
                "peak={} / min={}",
                p.unwrap_or("未设"),
                m.unwrap_or("未设")
            )),
        }
    }

    /// 与另一份快照逐键比对, 返回 (key, 我方, 对方) 的差异列表。
    ///
    /// 层③ 的"这一轮到底改了哪个系统参数"就是这么看出来的 —— 不靠人写标签。
    pub fn diff<'a>(&'a self, other: &'a DeviceSnapshot) -> Vec<(String, String, String)> {
        let mut out = Vec::new();
        let mut keys: Vec<&String> = self.entries.keys().chain(other.entries.keys()).collect();
        keys.sort();
        keys.dedup();
        for k in keys {
            let a = self.entries.get(k).map(String::as_str).unwrap_or("<缺>");
            let b = other.entries.get(k).map(String::as_str).unwrap_or("<缺>");
            if a != b {
                out.push((k.clone(), a.to_string(), b.to_string()));
            }
        }
        out
    }
}

/// 从 `workload.log` 里抠出电池温度。
///
/// 日志行形如 `[1/5] 动作: sleep 等待 3000ms | CPU:36% | PSS:2881.4MB | 电池:40.0℃`。
/// 取最后一次出现的值 —— 那是最贴近"跑完时"的温度。读不到返回 `None`。
pub fn battery_temp_c(text: &str) -> Option<f64> {
    let mut last = None;
    for line in text.lines() {
        let Some(rest) = line.split("电池:").nth(1) else {
            continue;
        };
        let num: String = rest
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == '.' || *c == '-')
            .collect();
        if let Ok(v) = num.parse::<f64>() {
            last = Some(v);
        }
    }
    last
}

/// 从 `workload.log` 里抠出被监控的应用包名 (`监控应用: com.miHoYo.Yuanshen (…)`）。
pub fn monitored_package(text: &str) -> Option<String> {
    for line in text.lines() {
        if let Some(rest) = line.split("监控应用:").nth(1) {
            let pkg = rest.split_whitespace().next()?;
            if !pkg.is_empty() {
                return Some(pkg.to_string());
            }
        }
    }
    None
}

/// 读一次运行目录里的 `workload.log`, 返回 (电池温度, 包名)。
pub fn workload_facts(dir: &Path) -> (Option<f64>, Option<String>) {
    match std::fs::read_to_string(dir.join("workload.log")) {
        Ok(text) => (battery_temp_c(&text), monitored_package(&text)),
        Err(_) => (None, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SNAP: &str = "# device_snapshot v1\n\
        kgsl.thermal_pwrlevel=0\n\
        bus.DDR.boost_freq=547000\n\
        kgsl.devfreq_governor=<unreadable>\n\
        settings.peak_refresh_rate=null\n\
        settings.min_refresh_rate=null\n\
        wm.size=Physical size: 1216x2688\n\
        # end\n";

    #[test]
    fn snapshot_skips_comments_and_keeps_values_with_equals() {
        let snap = DeviceSnapshot::parse(SNAP);
        assert_eq!(snap.get("bus.DDR.boost_freq"), Some("547000"));
        assert_eq!(snap.get("wm.size"), Some("Physical size: 1216x2688"));
        assert!(!snap.entries.contains_key("# end"));
    }

    #[test]
    fn snapshot_treats_unreadable_and_null_as_unknown() {
        let snap = DeviceSnapshot::parse(SNAP);
        assert_eq!(snap.get("kgsl.devfreq_governor"), None);
        assert_eq!(snap.get("settings.peak_refresh_rate"), None);
        assert_eq!(snap.refresh_rate_setting(), None, "两项都没设应报未知");
    }

    #[test]
    fn snapshot_diff_finds_the_changed_knob() {
        let a = DeviceSnapshot::parse(SNAP);
        let b = DeviceSnapshot::parse(&SNAP.replace("boost_freq=547000", "boost_freq=5333000"));
        let diff = a.diff(&b);
        assert_eq!(diff.len(), 1);
        assert_eq!(diff[0].0, "bus.DDR.boost_freq");
        assert_eq!(diff[0].1, "547000");
        assert_eq!(diff[0].2, "5333000");
    }

    #[test]
    fn battery_temp_takes_the_last_reading() {
        let log = "[1/3] 动作: sleep | CPU:36% | 电池:40.0℃\n\
                   [2/3] 动作: exec | CPU:36% | 电池:41.5℃\n\
                   summary: run=x stop=done\n";
        assert_eq!(battery_temp_c(log), Some(41.5));
        assert_eq!(battery_temp_c("没有温度"), None);
    }

    #[test]
    fn monitored_package_is_extracted() {
        let log = "监控应用: com.miHoYo.Yuanshen (Root 遥测: 开启)\n";
        assert_eq!(
            monitored_package(log).as_deref(),
            Some("com.miHoYo.Yuanshen")
        );
        assert_eq!(monitored_package("无此行"), None);
    }

    #[test]
    fn summary_metric_lookup_returns_none_for_unknown_name() {
        let s = RunSummary {
            frame_p95: Some(19.9),
            ..Default::default()
        };
        assert_eq!(s.metric("frame_p95"), Some(19.9));
        assert_eq!(s.metric("不存在的指标"), None);
    }
}
