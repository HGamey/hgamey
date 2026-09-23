//! 摸底: 这台设备、这个游戏, 到底能改什么。
//!
//! 产出 `caps.json`。三条纪律:
//!
//! 1. **只读**。摸底阶段一个字节都不往设备上写 —— `--plan` 必须是安全的,
//!    人才敢在别人占着机器的间隙跑它。停充、挂层、写 sysfs 全都留给真跑那一步。
//! 2. **每一条结论都带出处**。每个能力都记下是用哪条命令、看到什么原始输出判的。
//!    没有出处的 "root: true" 在排查时等于没有。
//! 3. **探不出来就是探不出来**。`Probe::unknown` 与 `Probe::no` 是两件事:
//!    前者是"这次没问出来", 后者是"问了, 答案是不行"。把它们混成一个 false,
//!    计划里就会出现"没有 root"这种其实没验证过的断言。
//!
//! 设备通路全部走 `phonefarm probe --serial <S> "<只读命令>"` —— 本仓库不碰
//! adb/hdc, 这是分工红线。

use super::games::{BoxKind, Target};
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// 一次探测的结论 + 出处。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Probe<T> {
    /// None = 没探出来 (不是"否")。
    pub value: Option<T>,
    /// 用什么探的。
    pub via: String,
    /// 看到了什么 —— 原始输出的要点, 不是复述结论。
    pub detail: String,
}

impl<T> Probe<T> {
    pub fn yes(value: T, via: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            value: Some(value),
            via: via.into(),
            detail: detail.into(),
        }
    }

    pub fn unknown(via: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            value: None,
            via: via.into(),
            detail: detail.into(),
        }
    }
}

impl Probe<bool> {
    /// 确定为真。
    pub fn ok(via: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::yes(true, via, detail)
    }
    /// 确定为假 —— 探过了, 答案是不行。
    pub fn no(via: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::yes(false, via, detail)
    }
    /// 只有确定为真才算真。探不出来当不行用, 但两者在证据里仍然分得开。
    pub fn is_true(&self) -> bool {
        self.value == Some(true)
    }
}

/// 设备后端。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Platform {
    /// Android, 走 adb。
    Adb,
    /// OpenHarmony, 走 hdc (序列号带 `hdc:` 前缀)。
    Hdc,
    Unknown,
}

impl Platform {
    /// 从 phonefarm 报的序列号判后端。`hdc:` 前缀是 phonefarm 的约定。
    pub fn of(serial: &str) -> Self {
        if serial.starts_with("hdc:") {
            Platform::Hdc
        } else if serial.is_empty() {
            Platform::Unknown
        } else {
            Platform::Adb
        }
    }
}

/// 电池与供电状态。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BatteryState {
    pub status: Option<String>,
    pub capacity_pct: Option<i64>,
    pub current_now_ua: Option<i64>,
    /// 现在是不是真在放电。只有放电态量到的瓦数才是整机功耗。
    pub on_battery: Option<bool>,
}

/// 设备能力。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceCaps {
    pub serial: String,
    pub platform: Platform,
    pub model: Option<String>,
    pub sdk: Option<String>,
    pub root: Probe<bool>,
    /// 能不能停充。只做**只读**的节点存在性检查 —— 真停要写节点, 那是真跑那一步的事。
    pub charging_suspend: Probe<bool>,
    /// 探到的停充候选节点 (存在且可写位已设), 按 phonefarm hwcond 的优先级排列。
    pub charge_ctl_nodes: Vec<String>,
    pub battery: BatteryState,
    /// 游戏装没装。
    pub game_installed: Probe<bool>,
    /// 能不能挂 Vulkan 层。
    pub vulkan_layer: Probe<bool>,
    /// 挂层时 `debug.vulkan.layers` 属性当前的值 —— 非空说明别人正挂着,
    /// 这时候再挂会互相踩, 而且会误伤第三方应用。
    pub vulkan_layers_prop: Option<String>,
    /// SoC 最高结温 (摄氏度)。用来定等冷目标与温度上限, 不是优化目标。
    pub soc_temp_c: Option<f32>,
}

/// 载体能力: 白盒靶场在不在, 算子能不能挂进去。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CarrierCaps {
    pub box_kind: BoxKind,
    /// 白盒载体的本地落点; None = 黑盒。
    pub carrier: Option<super::games::CarrierRef>,
    /// 负载脚本在不在。黑盒游戏没有它就跑不出可重复的负载。
    pub workload_ready: Probe<bool>,
}

/// 三层各自能不能开。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LayerCaps {
    /// ① 画面算子。
    pub shader: LayerVerdict,
    /// ② 画面流程改写 (灰档 Vulkan layer)。
    pub gray: LayerVerdict,
    /// ③ 系统参数 (黑档)。
    pub sysparam: LayerVerdict,
}

/// 一层能不能开, 以及凭什么。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LayerVerdict {
    pub open: bool,
    /// 为什么开/为什么不开。不开的时候这句话就是用户唯一能拿到的交代。
    pub reason: String,
    /// 开的话走哪条路 (白盒载体 / 灰档注入 / 直接写系统参数)。
    pub route: Option<String>,
    /// 还差什么才能开 —— 只在不开时有内容。
    pub blockers: Vec<String>,
}

impl LayerVerdict {
    fn open(route: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            open: true,
            reason: reason.into(),
            route: Some(route.into()),
            blockers: Vec::new(),
        }
    }
    fn shut(reason: impl Into<String>, blockers: Vec<String>) -> Self {
        Self {
            open: false,
            reason: reason.into(),
            route: None,
            blockers,
        }
    }
}

/// 摸底总结果。落盘即 `caps.json`。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Caps {
    pub schema: &'static str,
    pub generated_at: String,
    pub tool_version: &'static str,
    pub target: Target,
    pub device: DeviceCaps,
    pub carrier: CarrierCaps,
    pub layers: LayerCaps,
    /// 摸底过程中**没有**对设备做任何写入。这一条显式写进证据 ——
    /// `--plan` 的全部价值就建立在它上面。
    pub read_only: bool,
}

impl Caps {
    /// 有没有哪一层是开的。三层全关时只测量, 不改动。
    pub fn any_layer_open(&self) -> bool {
        self.layers.shader.open || self.layers.gray.open || self.layers.sysparam.open
    }
}

/// 停充候选节点, 与 phonefarm `src/hwcond.rs` 的 `CHARGE_CTL_NODES` 同源同序。
///
/// 这里只做**存在性**检查。真机实测的结论 (2026-09-23) 是: `power_supply`
/// 下面那几个在红魔上一个都停不了充, 真正管用的是不在 `power_supply` class 下的
/// `/sys/class/qcom-battery/charging_enabled` —— 所以只看 `power_supply` 会整个错过。
pub const CHARGE_CTL_NODES: &[&str] = &[
    "/sys/class/power_supply/battery/input_suspend",
    "/sys/class/power_supply/usb/input_suspend",
    "/sys/class/qcom-battery/charging_enabled",
    "/sys/class/qcom-battery/battery_charging_enabled",
    "/sys/class/power_supply/battery/charging_enabled",
    "/sys/class/power_supply/battery/battery_charging_enabled",
];

/// 设备侧只读探测脚本。
///
/// 一次 shell 调用问完所有问题, 输出 `key=value`。分成十几次 adb 往返的话,
/// 每次都是一秒级的开销, 而且中途设备状态可能变 —— 一次问完拿到的是同一时刻的快照。
///
/// 全部是读操作: `getprop` / `cat` / `pm path` / `id`。没有任何 `setprop`、
/// 重定向写入或 `settings put`。
pub fn probe_script(package: Option<&str>) -> String {
    let mut s = String::new();
    s.push_str("echo \"model=$(getprop ro.product.model)\";");
    s.push_str("echo \"sdk=$(getprop ro.build.version.sdk)\";");
    s.push_str("echo \"abi=$(getprop ro.product.cpu.abi)\";");
    // root: 有 su 且能拿到 uid 0。`su -c id -u` 在没有 su 的机器上直接报错,
    // 所以吞掉 stderr 并给一个哨兵值, 免得把"没有 su"变成一条看不懂的报错。
    s.push_str("echo \"root_uid=$(su -c id -u 2>/dev/null || echo NA)\";");
    s.push_str("echo \"vklayers=$(getprop debug.vulkan.layers)\";");
    s.push_str("echo \"battery_status=$(cat /sys/class/power_supply/battery/status 2>/dev/null)\";");
    s.push_str("echo \"battery_capacity=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)\";");
    s.push_str(
        "echo \"battery_current_now=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null)\";",
    );
    for node in CHARGE_CTL_NODES {
        s.push_str(&format!("[ -e {node} ] && echo \"charge_node={node}\";"));
    }
    // 结温: 取所有热区里的最大值 (毫摄氏度)。挑热区名的活留给 phonefarm,
    // 这里只要一个"现在大概多热"用来定等冷目标。
    s.push_str("echo \"temp_mc=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1)\";");
    if let Some(pkg) = package {
        // pm path 只读, 查不到就什么都不输出。
        s.push_str(&format!(
            "echo \"pkg_path=$(pm path {pkg} 2>/dev/null | head -1)\";"
        ));
    }
    s
}

/// `key=value` 文本 → map。与 phonefarm `probe_sysparam.sh` 同一套口径。
///
/// 同一个 key 出现多次时全部留下 (`charge_node` 会有多行), 所以值是 Vec。
pub fn parse_kv(text: &str) -> BTreeMap<String, Vec<String>> {
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        let (k, v) = (k.trim(), v.trim());
        if k.is_empty() {
            continue;
        }
        out.entry(k.to_string()).or_default().push(v.to_string());
    }
    out
}

fn first<'a>(kv: &'a BTreeMap<String, Vec<String>>, key: &str) -> Option<&'a str> {
    kv.get(key)
        .and_then(|v| v.first())
        .map(String::as_str)
        .filter(|s| !s.is_empty())
}

/// 从探测输出拼出电池状态。
pub fn battery_from(kv: &BTreeMap<String, Vec<String>>) -> BatteryState {
    let status = first(kv, "battery_status").map(str::to_string);
    let capacity_pct = first(kv, "battery_capacity").and_then(|s| s.parse().ok());
    let current_now_ua = first(kv, "battery_current_now").and_then(|s| s.parse::<i64>().ok());
    // 放电的判据: status 明说 Discharging, 或者电流是负的 (在往外抽)。
    // 两条都读不到就是 None —— 不知道就说不知道, 别默认成"在放电"然后量一个假瓦数。
    let on_battery = match (status.as_deref(), current_now_ua) {
        (Some(s), _) if s.eq_ignore_ascii_case("discharging") => Some(true),
        (Some(s), _) if s.eq_ignore_ascii_case("charging") || s.eq_ignore_ascii_case("full") => {
            Some(false)
        }
        (_, Some(i)) => Some(i < 0),
        _ => None,
    };
    BatteryState {
        status,
        capacity_pct,
        current_now_ua,
        on_battery,
    }
}

/// 摸底器。
pub struct Prober {
    phonefarm: PathBuf,
    /// 测试替身: 给定 (serial, 命令) 回一段 stdout。None = 真的去调 phonefarm。
    #[allow(clippy::type_complexity)]
    shell: Option<Box<dyn Fn(&str, &str) -> Result<String> + Send + Sync>>,
}

impl Prober {
    pub fn new(phonefarm: impl Into<PathBuf>) -> Self {
        Self {
            phonefarm: phonefarm.into(),
            shell: None,
        }
    }

    /// 注入替身 (只供测试用)。
    pub fn with_shell(
        mut self,
        f: impl Fn(&str, &str) -> Result<String> + Send + Sync + 'static,
    ) -> Self {
        self.shell = Some(Box::new(f));
        self
    }

    /// 列出在线设备。
    pub fn devices(&self) -> Result<Vec<String>> {
        if !self.phonefarm.is_file() {
            bail!("找不到 phonefarm ({})", self.phonefarm.display());
        }
        let out = Command::new(&self.phonefarm)
            .arg("devices")
            .output()
            .with_context(|| format!("调起 `{} devices` 失败", self.phonefarm.display()))?;
        if !out.status.success() {
            bail!(
                "`{} devices` 退出码 {:?}",
                self.phonefarm.display(),
                out.status.code()
            );
        }
        Ok(crate::harness::parse_device_list(
            &String::from_utf8_lossy(&out.stdout),
        ))
    }

    /// 在设备上跑一条只读命令。
    pub fn shell(&self, serial: &str, cmd: &str) -> Result<String> {
        if let Some(f) = self.shell.as_ref() {
            return f(serial, cmd);
        }
        let out = Command::new(&self.phonefarm)
            .arg("probe")
            .arg("--serial")
            .arg(serial)
            .arg(cmd)
            .output()
            .with_context(|| format!("调起 `{} probe` 失败", self.phonefarm.display()))?;
        // probe 是只读通道, 非零退出码通常是设备掉了。把 stderr 带上, 别只报个码。
        if !out.status.success() {
            bail!(
                "`phonefarm probe --serial {serial}` 退出码 {:?}: {}",
                out.status.code(),
                String::from_utf8_lossy(&out.stderr)
                    .lines()
                    .next()
                    .unwrap_or("")
            );
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// 摸一台设备 + 一个目标。
    pub fn run(&self, target: &Target, serial: &str) -> Result<Caps> {
        let raw = self.shell(serial, &probe_script(target.package.as_deref()))?;
        Ok(assemble(target, serial, &raw))
    }
}

/// 把探测原始输出拼成 `Caps`。纯函数 —— 不读设备、不读时钟以外的东西,
/// 所以同一份输出每次拼出来的结论一样, 可以拿录下来的输出做回归。
pub fn assemble(target: &Target, serial: &str, raw: &str) -> Caps {
    let kv = parse_kv(raw);
    let platform = Platform::of(serial);

    let root_uid = first(&kv, "root_uid");
    let root = match root_uid {
        Some("0") => Probe::ok("su -c id -u", "uid=0"),
        Some("NA") | None => Probe::no("su -c id -u", "没有 su, 或 su 拒绝了这次调用"),
        Some(other) => Probe::no("su -c id -u", format!("uid={other}, 不是 0")),
    };

    let charge_ctl_nodes: Vec<String> = kv.get("charge_node").cloned().unwrap_or_default();
    let charging_suspend = if !root.is_true() {
        // 停充节点存在也没用 —— 写不了。这里报 no 而不是 unknown:
        // "没有 root" 是一个确定的答案。
        Probe::no(
            "[ -e <停充节点> ] + root 检查",
            format!(
                "探到 {} 个候选节点, 但没有 root, 写不了",
                charge_ctl_nodes.len()
            ),
        )
    } else if charge_ctl_nodes.is_empty() {
        Probe::no(
            "[ -e <停充节点> ]",
            format!(
                "{} 个候选节点一个都不存在 (phonefarm hwcond 的同一张表)",
                CHARGE_CTL_NODES.len()
            ),
        )
    } else {
        // 只验证了"节点在 + 有 root"。真能不能停充要写下去才知道 ——
        // 实测过 power_supply 下的节点写了也不生效。所以这里是"大概能",
        // 真跑那一步由 phonefarm 写完回读判定, 判不成就把 power_watt 留 null。
        Probe::ok(
            "[ -e <停充节点> ] + root 检查",
            format!("候选节点: {}", charge_ctl_nodes.join(", ")),
        )
    };

    let battery = battery_from(&kv);
    let vulkan_layers_prop = first(&kv, "vklayers").map(str::to_string);

    let game_installed = match (target.package.as_deref(), first(&kv, "pkg_path")) {
        (None, _) => Probe::unknown("pm path <包名>", "还不知道包名, 没得查"),
        (Some(pkg), Some(p)) if p.starts_with("package:") => {
            Probe::ok("pm path", format!("{pkg} → {p}"))
        }
        (Some(pkg), _) => Probe::no("pm path", format!("{pkg} 没装, 或 pm 查不到")),
    };

    // 挂层的充要条件 (phonefarm knobs/gray/FEASIBILITY.md 真机结论):
    //   1. `setprop debug.vulkan.layers <层名>` —— 要 root
    //   2. .so 放进**目标应用自己的** nativeLibraryDir —— 要 root, 且应用得装着
    // `settings put global gpu_debug_*` 那一套在本机一条都不生效, 不要再试。
    let vulkan_layer = if !root.is_true() {
        Probe::no(
            "root 检查 + knobs/gray 的挂层充要条件",
            "挂层要 setprop debug.vulkan.layers 并往应用 lib 目录放 .so, 两件都要 root",
        )
    } else if platform != Platform::Adb {
        Probe::unknown(
            "平台判定",
            "灰档层只在 Android 上验证过; 鸿蒙的等价机制还没有设备可验",
        )
    } else if !game_installed.is_true() {
        Probe::no(
            "pm path + 挂层充要条件",
            ".so 必须放进目标应用自己的 nativeLibraryDir, 应用没装就没有这个目录",
        )
    } else {
        Probe::ok(
            "root + pm path",
            "有 root 且应用已装, 满足 setprop + 放 .so 两个条件",
        )
    };

    let soc_temp_c = first(&kv, "temp_mc")
        .and_then(|s| s.parse::<f64>().ok())
        // 热区读数是毫摄氏度。0 与超出量程的值都不是测量结果, 丢掉而不是记 0。
        .filter(|mc| *mc > 0.0 && *mc < 150_000.0)
        .map(|mc| (mc / 1000.0) as f32);

    let device = DeviceCaps {
        serial: serial.to_string(),
        platform,
        model: first(&kv, "model").map(str::to_string),
        sdk: first(&kv, "sdk").map(str::to_string),
        root,
        charging_suspend,
        charge_ctl_nodes,
        battery,
        game_installed,
        vulkan_layer,
        vulkan_layers_prop,
        soc_temp_c,
    };

    let workload_ready = match (target.box_kind, &target.workload.script) {
        (BoxKind::WhiteBox, _) => Probe::ok("载体契约", "白盒载体自带确定性负载, 不需要回放脚本"),
        (BoxKind::BlackBox, Some(s)) if target.workload.script_present => {
            Probe::ok("文件存在性", format!("负载脚本 {s}"))
        }
        (BoxKind::BlackBox, Some(s)) => Probe::no("文件存在性", format!("负载脚本 {s} 不存在")),
        (BoxKind::BlackBox, None) => Probe::no(
            "内置表",
            "这个黑盒目标还没有确定性负载脚本 —— 没有可重复的负载就没有可比的数字",
        ),
    };

    let carrier = CarrierCaps {
        box_kind: target.box_kind,
        carrier: target.carrier.clone(),
        workload_ready,
    };

    let layers = decide_layers(&device, &carrier);

    Caps {
        schema: "game_opt_loop/caps@1",
        generated_at: crate::engine::evolution::now(),
        tool_version: crate::VERSION,
        target: target.clone(),
        device,
        carrier,
        layers,
        read_only: true,
    }
}

/// 按能力开路。三层各自的前置条件是互相独立的 —— 一层开不了不影响另一层。
pub fn decide_layers(device: &DeviceCaps, carrier: &CarrierCaps) -> LayerCaps {
    // ③ 系统参数: 只要 root。不动画面, 所以跟白盒黑盒无关。
    let sysparam = if device.root.is_true() {
        LayerVerdict::open(
            "phonefarm eval --request kind=sysparam",
            "有 root, 可以探白名单并改可写系统参数",
        )
    } else {
        LayerVerdict::shut(
            "没有 root, 写不了 sysfs 与 settings",
            vec![format!("root: {}", device.root.detail)],
        )
    };

    // ② 画面流程改写: 要能挂 Vulkan 层。
    let gray = if device.vulkan_layer.is_true() {
        let mut v = LayerVerdict::open(
            "phonefarm eval --request kind=gray (Vulkan layer 注入)",
            "能挂层, 可以改写 LoadOp / 精度 / 渲染分辨率",
        );
        if device
            .vulkan_layers_prop
            .as_deref()
            .is_some_and(|p| !p.is_empty())
        {
            // 属性是全局的: 别人挂着的时候我们再挂会互相踩, 而且窗口期里
            // 第三方 Vulkan 应用会因为找不到被指名的层而启动失败。
            v.blockers.push(format!(
                "debug.vulkan.layers 当前非空 (`{}`) —— 有人正挂着层, 先确认能不能动",
                device.vulkan_layers_prop.as_deref().unwrap_or("")
            ));
        }
        v
    } else {
        LayerVerdict::shut(
            "挂不了 Vulkan 层",
            vec![format!("vulkan_layer: {}", device.vulkan_layer.detail)],
        )
    };

    // ① 画面算子: 白盒载体直接挂; 黑盒要靠灰档层把算子注进去。
    let shader = match carrier.box_kind {
        BoxKind::WhiteBox => LayerVerdict::open(
            "phonefarm eval --request kind=shader (白盒载体)",
            "有白盒载体, 算子直接挂进它的管线, 画质有确定真值",
        ),
        BoxKind::BlackBox if device.vulkan_layer.is_true() => LayerVerdict::open(
            "phonefarm eval --request kind=shader + inject.via=vulkan_layer",
            "黑盒游戏, 但能挂层 —— 算子经灰档层注入",
        ),
        BoxKind::BlackBox => LayerVerdict::shut(
            "黑盒游戏且挂不了层, 算子没有挂进去的通道",
            vec![
                "没有白盒载体 (phonefarm loop_v1/carriers/ 与同级仓库里都没有对应的 contract/launch.json)"
                    .to_string(),
                format!("vulkan_layer: {}", device.vulkan_layer.detail),
            ],
        ),
    };

    LayerCaps {
        shader,
        gray,
        sysparam,
    }
}

/// 把 caps 落盘。
pub fn write(caps: &Caps, path: &Path) -> Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, serde_json::to_string_pretty(caps)?)
        .with_context(|| format!("写入 {} 失败", path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::optimize::games::{self, CarrierRoots};

    fn blackbox_target() -> Target {
        games::resolve(
            "原神",
            None,
            &CarrierRoots {
                carriers_dir: PathBuf::from("/nonexistent"),
                siblings_dir: PathBuf::from("/nonexistent"),
            },
        )
    }

    const ROOTED: &str = "model=NX809J\nsdk=36\nabi=arm64-v8a\nroot_uid=0\nvklayers=\n\
battery_status=Discharging\nbattery_capacity=84\nbattery_current_now=-1200000\n\
charge_node=/sys/class/qcom-battery/charging_enabled\n\
temp_mc=41300\npkg_path=package:/data/app/~~x/com.miHoYo.Yuanshen-1/base.apk\n";

    const UNROOTED: &str = "model=Pixel\nsdk=34\nabi=arm64-v8a\nroot_uid=NA\nvklayers=\n\
battery_status=Charging\nbattery_capacity=91\nbattery_current_now=1072000\n\
temp_mc=38000\npkg_path=package:/data/app/x/base.apk\n";

    #[test]
    fn probe_script_is_read_only() {
        let s = probe_script(Some("com.example.app"));
        // 一个写操作都不许有。这条测试是 `--plan` 安全性的唯一自动化保证。
        //
        // 重定向单独处理: `2>/dev/null` 是把 stderr 丢掉, 不写任何东西;
        // 先把这一种摘掉, 剩下的 `>` 就是真往文件里写了。
        let redirect_free = s.replace("2>/dev/null", "");
        for forbidden in [
            "setprop",
            "settings put",
            "pm install",
            "rm ",
            "mv ",
            "chmod",
            "am start",
        ] {
            assert!(
                !s.contains(forbidden),
                "只读探测脚本里出现了写操作 `{forbidden}`: {s}"
            );
        }
        assert!(
            !redirect_free.contains('>'),
            "只读探测脚本里出现了写文件的重定向: {redirect_free}"
        );
        assert!(s.contains("getprop ro.product.model"));
        assert!(s.contains("pm path com.example.app"));
    }

    #[test]
    fn probe_script_skips_the_package_query_when_we_do_not_know_the_package() {
        assert!(!probe_script(None).contains("pm path"));
    }

    #[test]
    fn kv_keeps_every_line_for_repeated_keys() {
        let kv = parse_kv("a=1\n# 注释\nb=2\na=3\n\n坏行\n");
        assert_eq!(kv["a"], vec!["1", "3"]);
        assert_eq!(kv["b"], vec!["2"]);
        assert!(!kv.contains_key("坏行"));
    }

    #[test]
    fn rooted_device_opens_all_three_layers_for_an_installed_blackbox_game() {
        let caps = assemble(&blackbox_target(), "91253241019A", ROOTED);
        assert_eq!(caps.device.platform, Platform::Adb);
        assert_eq!(caps.device.model.as_deref(), Some("NX809J"));
        assert!(caps.device.root.is_true());
        assert!(caps.layers.sysparam.open, "有 root 就该开系统参数层");
        assert!(caps.layers.gray.open, "有 root + 应用已装就该开灰档层");
        assert!(caps.layers.shader.open, "能挂层的黑盒也该开算子层");
        assert!(caps.layers.shader.route.as_deref().unwrap().contains("vulkan_layer"));
        assert!(caps.read_only, "摸底必须是只读的");
    }

    #[test]
    fn unrooted_device_shuts_everything_and_says_why() {
        let caps = assemble(&blackbox_target(), "emulator-5554", UNROOTED);
        assert!(!caps.any_layer_open());
        for v in [&caps.layers.shader, &caps.layers.gray, &caps.layers.sysparam] {
            assert!(!v.reason.is_empty(), "关掉一层必须给出理由");
            assert!(!v.blockers.is_empty(), "关掉一层必须说还差什么");
        }
    }

    /// 白盒载体不依赖挂层: 没有 root 也能挂算子 —— 靶场自己就收算子。
    #[test]
    fn whitebox_opens_the_shader_layer_without_a_vulkan_layer() {
        let tmp = std::env::temp_dir().join(format!("gol_caps_{}", std::process::id()));
        let dir = tmp.join("myrig").join("contract");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("launch.json"), r#"{"package":"io.example.myrig"}"#).unwrap();
        let target = games::resolve(
            "myrig",
            None,
            &CarrierRoots {
                carriers_dir: tmp.clone(),
                siblings_dir: PathBuf::from("/nonexistent"),
            },
        );

        let caps = assemble(&target, "emulator-5554", UNROOTED);
        assert!(caps.layers.shader.open);
        assert!(!caps.layers.sysparam.open, "没 root 仍然不许动系统参数");
        std::fs::remove_dir_all(&tmp).ok();
    }

    /// 充电态必须被认出来 —— 那时候量到的瓦数不是整机功耗。
    #[test]
    fn charging_state_is_detected_from_status_and_current() {
        let caps = assemble(&blackbox_target(), "s", UNROOTED);
        assert_eq!(caps.device.battery.on_battery, Some(false));
        assert_eq!(caps.device.battery.capacity_pct, Some(91));

        let caps = assemble(&blackbox_target(), "s", ROOTED);
        assert_eq!(caps.device.battery.on_battery, Some(true));

        // 两条线索都读不到 = 不知道, 不是"在放电"。
        let caps = assemble(&blackbox_target(), "s", "root_uid=0\n");
        assert_eq!(caps.device.battery.on_battery, None);
    }

    /// 探不出来 (unknown) 与探过了是否 (no) 必须分得开。
    #[test]
    fn unknown_is_not_the_same_as_no() {
        let mut t = blackbox_target();
        t.package = None;
        let caps = assemble(&t, "s", "root_uid=0\n");
        assert!(caps.device.game_installed.value.is_none(), "不知道包名就是不知道");
        assert!(caps.device.game_installed.detail.contains("包名"));
    }

    /// 温度读数是毫摄氏度; 0 不是测量结果。
    #[test]
    fn zero_temperature_is_not_a_measurement() {
        let caps = assemble(&blackbox_target(), "s", "root_uid=0\ntemp_mc=0\n");
        assert!(caps.device.soc_temp_c.is_none());
        let caps = assemble(&blackbox_target(), "s", "root_uid=0\ntemp_mc=41300\n");
        assert_eq!(caps.device.soc_temp_c, Some(41.3));
    }

    /// 别人正挂着层的时候要提醒 —— 那个属性是全局的, 抢着挂会互相踩。
    #[test]
    fn an_already_mounted_layer_is_flagged() {
        let raw = ROOTED.replace("vklayers=", "vklayers=VK_LAYER_someone_else");
        let caps = assemble(&blackbox_target(), "s", &raw);
        assert!(caps.layers.gray.open);
        assert!(
            caps.layers.gray.blockers.iter().any(|b| b.contains("debug.vulkan.layers")),
            "属性非空时必须提醒: {:?}",
            caps.layers.gray.blockers
        );
    }

    #[test]
    fn prober_uses_the_injected_shell() {
        let p = Prober::new("/nonexistent/phonefarm")
            .with_shell(|serial, cmd| {
                assert_eq!(serial, "S1");
                assert!(cmd.contains("getprop"));
                Ok(ROOTED.to_string())
            });
        let caps = p.run(&blackbox_target(), "S1").unwrap();
        assert!(caps.device.root.is_true());
    }
}
