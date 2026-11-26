//! 敏感动作审批: 动作过门之后、真正下发之前, 判断它会不会花钱、删东西、对外发消息或输密码,
//! 需要时停下来等人点头。
//!
//! 设计参考 Poki(AGPL-3.0, JosssphZhou/poki)的产品行为——「付款、删除、钱包永远要人确认;
//! 发消息、输密码等由使用者选择怎么问」——只借行为, 实现、词表、协议都是本仓库重写。
//!
//! # 分级
//! - **关键**(Payment / Delete / Wallet): 只能 `ask` 或 `deny`, 配置写 `auto` 也按 `ask` 处理。
//! - **敏感**(Send / Password / Account): `ask`(缺省)| `auto` | `deny`, 由 `[approval]` 配置。
//!
//! # 通道
//! 请求写成 `<dir>/<id>.json`(同目录放一张当屏截图), 决定写在 `<dir>/<id>.decision.json`:
//! `{"decision":"allow"|"deny","by":"…","note":"…"}`。
//! `dir` 取值: `PF_APPROVAL_DIR` → `$PF_JOB_OUT/approvals`(平台任务, 节点服务扫描它并在网页/
//! Telegram 上展示) → 本局目录 `approvals/`。交互终端下同时在终端问一句, 谁先答算谁的。
//! 超时(缺省 300 s)按拒绝处理并写回 `decision.json`(by=timeout), 保证事后可审计。
//!
//! # 判定口径
//! 只看程序拿得到的事实: 动作类型、模型点名的目标文字(`what`)、落点所在元素的文字、
//! 当屏全部文字和前台包名。词表命中即认定, 不让模型自评风险——模型说「这不是付款」不算数。

use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, IsTerminal};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Category {
    Payment,
    Delete,
    Wallet,
    Send,
    Password,
    Account,
    /// 不是审批而是「请人接手」: 验证码、密码、付款确认等 agent 不该或不能做的一步。
    /// allow = 人做完了, 继续; deny = 人不处理。
    Handoff,
}

impl Category {
    pub fn id(self) -> &'static str {
        match self {
            Category::Payment => "payment",
            Category::Delete => "delete",
            Category::Wallet => "wallet",
            Category::Send => "send",
            Category::Password => "password",
            Category::Account => "account",
            Category::Handoff => "handoff",
        }
    }
    pub fn zh(self) -> &'static str {
        match self {
            Category::Payment => "付款",
            Category::Delete => "删除",
            Category::Wallet => "钱包",
            Category::Send => "对外发送",
            Category::Password => "输入密码",
            Category::Account => "账号变更",
            Category::Handoff => "需要你接手",
        }
    }
    /// 关键类: 不允许免审。
    pub fn critical(self) -> bool {
        matches!(self, Category::Payment | Category::Delete | Category::Wallet)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Ask,
    Auto,
    Deny,
}

impl Mode {
    pub fn parse(s: &str) -> Option<Mode> {
        Some(match s.trim().to_ascii_lowercase().as_str() {
            "ask" => Mode::Ask,
            "auto" | "allow" => Mode::Auto,
            "deny" => Mode::Deny,
            _ => return None,
        })
    }
}

/// `[approval]` 配置。缺省: 全部 ask, 300 s 超时。
#[derive(Clone, Debug)]
pub struct Policy {
    pub send: Mode,
    pub password: Mode,
    pub account: Mode,
    /// 关键类 ask 还是直接 deny(无人值守批跑时设 deny)。
    pub critical: Mode,
    pub timeout_s: u64,
    /// 接手(handoff)等人的上限, 缺省 600 s(人要拿起手机操作, 比点允许慢)。
    pub handoff_timeout_s: u64,
    /// 额外按包名视为「钱包」的应用。
    pub wallet_pkgs: Vec<String>,
    /// 额外按包名视为「通讯」的应用(发送类词只在这些应用里拦)。
    pub messaging_pkgs: Vec<String>,
    /// 一次允许覆盖之后多少秒内同一应用同一目标的同类动作(0 = 每次都问)。
    pub grant_s: u64,
    /// 整体关闭(只给离线回放/基准用; 关键类仍然拦, 见 [`Policy::mode_for`])。
    pub enabled: bool,
}

impl Default for Policy {
    fn default() -> Self {
        Policy {
            send: Mode::Ask,
            password: Mode::Ask,
            account: Mode::Ask,
            critical: Mode::Ask,
            timeout_s: 300,
            handoff_timeout_s: 600,
            wallet_pkgs: Vec::new(),
            messaging_pkgs: Vec::new(),
            grant_s: 45,
            enabled: true,
        }
    }
}

impl Policy {
    /// 从 TOML 表读; 未知值忽略并保留缺省(宁可多问)。环境变量 `PF_APPROVAL_<CAT>` 覆盖。
    pub fn from_toml(v: Option<&toml::Value>) -> Policy {
        let mut p = Policy::default();
        if let Some(t) = v.and_then(|v| v.as_table()) {
            let m = |k: &str| t.get(k).and_then(|x| x.as_str()).and_then(Mode::parse);
            if let Some(x) = m("send") { p.send = x; }
            if let Some(x) = m("password") { p.password = x; }
            if let Some(x) = m("account") { p.account = x; }
            if let Some(x) = m("critical") { p.critical = x; }
            if let Some(x) = t.get("timeout_s").and_then(|x| x.as_integer()) { p.timeout_s = x.clamp(5, 86_400) as u64; }
            if let Some(x) = t.get("handoff_timeout_s").and_then(|x| x.as_integer()) { p.handoff_timeout_s = x.clamp(5, 86_400) as u64; }
            if let Some(x) = t.get("enabled").and_then(|x| x.as_bool()) { p.enabled = x; }
            let list = |k: &str| t.get(k).and_then(|x| x.as_array())
                .map(|a| a.iter().filter_map(|s| s.as_str().map(str::to_string)).collect::<Vec<_>>());
            if let Some(a) = list("wallet_pkgs") { p.wallet_pkgs = a; }
            if let Some(a) = list("messaging_pkgs") { p.messaging_pkgs = a; }
            if let Some(x) = t.get("grant_s").and_then(|x| x.as_integer()) { p.grant_s = x.clamp(0, 600) as u64; }
        }
        for (k, slot) in [("SEND", &mut p.send), ("PASSWORD", &mut p.password), ("ACCOUNT", &mut p.account),
                          ("CRITICAL", &mut p.critical)] {
            if let Some(m) = std::env::var(format!("PF_APPROVAL_{k}")).ok().as_deref().and_then(Mode::parse) {
                *slot = m;
            }
        }
        if let Some(t) = std::env::var("PF_APPROVAL_TIMEOUT_S").ok().and_then(|v| v.parse::<u64>().ok()) {
            p.timeout_s = t.clamp(5, 86_400);
        }
        p
    }

    pub fn mode_for(&self, c: Category) -> Mode {
        if c.critical() {
            // 关键类永远不免审: auto 视同 ask; enabled=false 也不放过。
            return if self.critical == Mode::Deny { Mode::Deny } else { Mode::Ask };
        }
        if !self.enabled {
            return Mode::Auto;
        }
        match c {
            Category::Send => self.send,
            Category::Password => self.password,
            Category::Account => self.account,
            _ => Mode::Ask,
        }
    }
}

/// 判定输入: 全部是程序观察到的事实。
pub struct Facts<'a> {
    /// tap / type / swipe / launch …
    pub action: &'a str,
    /// 模型点名的目标文字(可空)。
    pub what: Option<&'a str>,
    /// type 的内容 / launch 的包名。
    pub text: Option<&'a str>,
    /// 落点所在(或最近)元素的文字, 由调用方按坐标取。
    pub target_texts: Vec<&'a str>,
    /// 当屏全部可见文字。
    pub screen_texts: Vec<&'a str>,
    /// 前台包名。
    pub pkg: &'a str,
    /// 落点元素的资源 id / 类名(判断密码框)。
    pub target_ids: Vec<&'a str>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Hit {
    pub category: Category,
    /// 确认框上的「确定」这类后续确认: 同应用同类别在有效期内批过一次就算数, 不必再问。
    pub follow_up: bool,
    /// 命中的词或规则, 写进请求让人知道为什么被拦。
    pub reason: String,
}

// 词表: 按「按钮上会出现的短语」收, 不收泛词(「确定」「好的」单独出现不算付款)。
const PAY_WORDS: &[&str] = &[
    "立即支付", "确认支付", "去支付", "确认付款", "立即付款", "付款", "支付", "提交订单", "确认下单", "立即下单",
    "去结算", "结算", "立即购买", "确认购买", "购买", "充值", "免密支付", "确认交易", "转账", "红包", "打赏",
    "pay now", "pay", "place order", "checkout", "check out", "buy now", "buy", "purchase", "confirm payment",
    "subscribe", "top up", "send money", "transfer",
];
/// 当屏出现这些字样时, 页面本身就是收银台: 任何「确认」类点击都按付款处理。
const CASHIER_WORDS: &[&str] = &["收银台", "支付密码", "请输入支付密码", "指纹支付", "面容支付", "确认付款", "订单金额", "实付"];
const CONFIRM_WORDS: &[&str] = &["确认", "确定", "同意", "继续", "下一步", "confirm", "ok", "continue", "agree"];
const DELETE_WORDS: &[&str] = &[
    "删除", "永久删除", "彻底删除", "移除", "清空", "清除数据", "清除全部", "注销", "卸载", "恢复出厂", "抹掉",
    "delete", "remove", "erase", "clear all", "uninstall", "factory reset", "wipe",
];
/// 发送类词只在通讯/社交应用里算数(到处都有「提交」「分享」, 全拦会把人问烦);
/// 「拨打」「呼叫」在任何应用里都算(打电话不可撤回)。
const SEND_WORDS: &[&str] = &[
    "发送", "发布", "发表", "回复", "评论", "转发", "分享", "投递",
    "send", "post", "publish", "reply", "comment", "share",
];
const CALL_WORDS: &[&str] = &["拨打", "呼叫", "视频通话", "语音通话", "call", "facetime"];
const MESSAGING_PKGS: &[&str] = &[
    "com.tencent.mm", "com.tencent.mobileqq", "com.tencent.wework", "com.alibaba.android.rimet", "com.ss.android.lark",
    "org.telegram.messenger", "com.whatsapp", "com.android.mms", "com.google.android.apps.messaging",
    "com.huawei.message", "com.hihonor.mms", "com.ss.android.ugc.aweme", "com.sina.weibo", "com.xingin.xhs",
    "com.apple.MobileSMS", "ph.telegra.Telegraph", "net.whatsapp.WhatsApp", "com.tencent.xin", "com.burbn.instagram",
    "com.twitter.android", "com.atebits.Tweetie2", "com.android.email", "com.google.android.gm", "com.apple.mobilemail",
];
const ACCOUNT_WORDS: &[&str] = &[
    "退出登录", "解绑", "修改密码", "更换手机号", "授权登录", "同意授权", "允许访问", "确认授权",
    "log out", "sign out", "unlink", "authorize", "change password",
];
const PASSWORD_HINTS: &[&str] = &["密码", "password", "passcode", "pin码", "支付密码", "验证码"];
const WALLET_PKGS: &[&str] = &[
    "com.apple.Passbook", "com.google.android.apps.walletnfcrel", "com.huawei.wallet", "com.hihonor.wallet",
    "com.mipay.wallet", "com.coloros.wallet", "com.vivo.wallet", "com.samsung.android.spay",
];

/// 滑动类控件的字样(「滑动付款」「右滑删除」「slide to pay」)。
const SWIPE_HINTS: &[&str] = &["滑动", "右滑", "左滑", "上滑", "滑块", "slide", "swipe"];

/// 模型点名(what)的长度上限: 超过这个长度、又不是落点控件上的文字, 多半是在点标题/段落。
const WHAT_MAX_CHARS: usize = 16;

/// 目标文字长度上限(字符), 超过视为说明段落而不是按钮。
const LABEL_MAX_CHARS: usize = 40;

fn norm(s: &str) -> String {
    s.trim().to_lowercase().chars().filter(|c| !c.is_whitespace()).collect()
}

/// 词命中: 短语出现在标签里, 且标签不长(长段落里出现「支付」多半是说明文字, 不是按钮)。
fn label_hits<'a>(label: &str, words: &[&'a str]) -> Option<&'a str> {
    let l = norm(label);
    // 上限放宽到 40 字: 「立即支付 ¥128.00 (已优惠¥20.00)」这类真实按钮常超过 16 字;
    // 再长的多半是说明段落(只有落点正好在段落上才会进 target_texts)
    if l.is_empty() || l.chars().count() > LABEL_MAX_CHARS {
        return None;
    }
    // 英文按「词序列」比: 先按非字母数字切词(不能先去空白, 否则 "Delete account" 会粘成一个词)
    let toks: Vec<String> = label.to_lowercase().split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty()).map(str::to_string).collect();
    words.iter().copied().find(|w| {
        if w.is_ascii() {
            let wt: Vec<&str> = w.split_whitespace().collect();
            !wt.is_empty() && toks.windows(wt.len()).any(|win| win.iter().zip(&wt).all(|(a, b)| a == b))
        } else {
            l.contains(&norm(w))
        }
    })
}

fn screen_has(texts: &[&str], words: &[&str]) -> Option<String> {
    for t in texts {
        let tn = norm(t);
        for w in words {
            if tn.contains(&norm(w)) {
                return Some((*w).to_string());
            }
        }
    }
    None
}

/// 屏上有一句带删除词的问句(「要删除此记事吗?」「Delete this photo?」), 视为删除确认框。
fn delete_dialog(texts: &[&str]) -> Option<String> {
    texts.iter().find(|t| {
        let n = t.trim();
        let ask = n.ends_with('?') || n.ends_with('？') || n.ends_with('吗') || n.contains("确定要") || n.contains("是否");
        ask && n.chars().count() <= 40 && DELETE_WORDS.iter().any(|w| n.to_lowercase().contains(&w.to_lowercase()))
    }).map(|t| t.trim().to_string())
}

/// 判定一个即将执行的动作。返回 None 表示放行。
/// 只拦会改变外部状态的动作: tap / type / launch(钱包) / swipe 不拦(滑动不提交东西)。
pub fn classify(f: &Facts, policy: &Policy) -> Option<Hit> {
    let wallet = WALLET_PKGS.iter().any(|p| *p == f.pkg) || policy.wallet_pkgs.iter().any(|p| p == f.pkg);
    match f.action {
        "launch" => {
            let t = f.text.unwrap_or("");
            if WALLET_PKGS.contains(&t) || policy.wallet_pkgs.iter().any(|p| p == t) {
                return Some(Hit { follow_up: false, category: Category::Wallet, reason: format!("打开钱包应用 {t}") });
            }
            None
        }
        "swipe" => {
            // 滑动付款、左滑删除: 起点元素上同时有「滑」类词和删除/付款词才算(普通滚动从「待支付」上起手不算)
            let labels: Vec<&str> = f.target_texts.iter().copied()
                .filter(|l| { let n = norm(l); SWIPE_HINTS.iter().any(|h| n.contains(&norm(h))) }).collect();
            if let Some((l, w)) = labels.iter().find_map(|l| label_hits(l, DELETE_WORDS).map(|w| (l, w))) {
                return Some(Hit { follow_up: false, category: Category::Delete, reason: format!("滑动起点「{l}」含「{w}」") });
            }
            if let Some((l, w)) = labels.iter().find_map(|l| label_hits(l, PAY_WORDS).map(|w| (l, w))) {
                return Some(Hit { follow_up: false, category: Category::Payment, reason: format!("滑动起点「{l}」含「{w}」") });
            }
            None
        }
        // 长按在没有长按监听的控件上会落成一次点击, 必须和 tap 同样判
        "tap" | "long_press" => {
            if wallet {
                return Some(Hit { follow_up: false, category: Category::Wallet, reason: format!("钱包应用 {} 内的点击", f.pkg) });
            }
            // what 是模型自报的: 长的 what 只有和落点控件文字一致时才当按钮看(防「如何删除…」这类标题误判)
            let what = f.what.filter(|w| w.chars().count() <= WHAT_MAX_CHARS || f.target_texts.iter().any(|t| norm(t) == norm(w)));
            let labels: Vec<&str> = what.into_iter().chain(f.target_texts.iter().copied()).collect();
            let first = |words: &[&'static str]| labels.iter().find_map(|l| label_hits(l, words).map(|w| (l.to_string(), w)));
            if let Some((l, w)) = first(DELETE_WORDS) {
                return Some(Hit { follow_up: false, category: Category::Delete, reason: format!("目标「{l}」含「{w}」") });
            }
            if let Some((l, w)) = first(PAY_WORDS) {
                return Some(Hit { follow_up: false, category: Category::Payment, reason: format!("目标「{l}」含「{w}」") });
            }
            if let Some(cw) = screen_has(&f.screen_texts, CASHIER_WORDS) {
                if let Some((l, _)) = first(CONFIRM_WORDS) {
                    return Some(Hit { follow_up: false, category: Category::Payment, reason: format!("收银台页面(「{cw}」)上点「{l}」") });
                }
            }
            if let Some(q) = delete_dialog(&f.screen_texts) {
                if let Some((l, _)) = first(CONFIRM_WORDS) {
                    return Some(Hit { follow_up: true, category: Category::Delete, reason: format!("删除确认框(「{q}」)上点「{l}」") });
                }
            }
            if let Some((l, w)) = first(ACCOUNT_WORDS) {
                return Some(Hit { follow_up: false, category: Category::Account, reason: format!("目标「{l}」含「{w}」") });
            }
            if let Some((l, w)) = first(CALL_WORDS) {
                return Some(Hit { follow_up: false, category: Category::Send, reason: format!("目标「{l}」含「{w}」(通话)") });
            }
            let messaging = MESSAGING_PKGS.contains(&f.pkg) || policy.messaging_pkgs.iter().any(|p| p == f.pkg);
            if messaging {
                if let Some((l, w)) = first(SEND_WORDS) {
                    return Some(Hit { follow_up: false, category: Category::Send, reason: format!("{} 里的「{l}」含「{w}」", f.pkg) });
                }
            }
            None
        }
        "type" => {
            if wallet {
                return Some(Hit { follow_up: false, category: Category::Wallet, reason: format!("钱包应用 {} 内输入", f.pkg) });
            }
            let id_hit = f.target_ids.iter().any(|i| {
                let i = i.to_lowercase();
                i.contains("password") || i.contains("passwd") || i.contains("pwd") || i.contains("pin")
            });
            let label_hit = f.target_texts.iter().chain(f.what.iter())
                .find_map(|l| label_hits(l, PASSWORD_HINTS).map(|w| w.to_string()));
            if id_hit || label_hit.is_some() {
                let why = label_hit.map(|w| format!("输入框标注「{w}」")).unwrap_or_else(|| "输入框是密码类控件".into());
                // 收银台上的密码 = 支付密码, 升级为付款
                if let Some(cw) = screen_has(&f.screen_texts, CASHIER_WORDS) {
                    return Some(Hit { follow_up: false, category: Category::Payment, reason: format!("收银台(「{cw}」)上输入密码") });
                }
                return Some(Hit { follow_up: false, category: Category::Password, reason: why });
            }
            None
        }
        _ => None,
    }
}

/// 审批请求(落盘形状即对外契约, 节点服务按它展示)。
#[derive(Clone, Debug)]
pub struct Request {
    pub id: String,
    pub category: Category,
    pub reason: String,
    pub action_line: String,
    pub goal: String,
    pub task: String,
    pub run_id: String,
    pub step: u32,
    pub serial: String,
    pub pkg: String,
    pub screenshot: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Decision {
    pub allow: bool,
    pub by: String,
    pub note: String,
}

pub fn approval_dir(run_dir: &str) -> PathBuf {
    if let Ok(d) = std::env::var("PF_APPROVAL_DIR") {
        if !d.trim().is_empty() {
            return PathBuf::from(d);
        }
    }
    if let Ok(o) = std::env::var("PF_JOB_OUT") {
        if !o.trim().is_empty() {
            // 放在作业目录下、out/ 之外: 作业产物下载接口(out/images/artifacts)不会把截图和请求带出去
            let out = Path::new(&o);
            return out.parent().unwrap_or(out).join("approvals");
        }
    }
    Path::new(run_dir).join("approvals")
}

fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub fn new_id(run_id: &str, step: u32) -> String {
    let safe: String = run_id.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' { c } else { '_' }).collect();
    // 64 位随机(RandomState 每次取系统随机种子), 末 4 位兼作 Telegram 回复码
    let r = {
        use std::hash::{BuildHasher, Hasher};
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u128(SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0));
        h.write_u32(std::process::id());
        h.finish()
    };
    format!("ap-{safe}-{step}-{r:016x}")
}

fn write_atomic(path: &Path, v: &Value) -> std::io::Result<()> {
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(v).unwrap_or_default())?;
    fs::rename(tmp, path)
}

/// 交互终端下的 stdin 行通道(全进程一个读线程)。非终端或 PF_APPROVAL_NO_TTY 时为 None。
fn tty_lines() -> Option<&'static std::sync::Mutex<std::sync::mpsc::Receiver<(Instant, String)>>> {
    static RX: std::sync::OnceLock<Option<std::sync::Mutex<std::sync::mpsc::Receiver<(Instant, String)>>>> = std::sync::OnceLock::new();
    RX.get_or_init(|| {
        if !std::io::stdin().is_terminal() || std::env::var("PF_APPROVAL_NO_TTY").is_ok() {
            return None;
        }
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for line in std::io::stdin().lock().lines() {
                let Ok(line) = line else { break };
                if tx.send((Instant::now(), line)).is_err() { break; }
            }
        });
        Some(std::sync::Mutex::new(rx))
    }).as_ref()
}

/// 只在决定文件还不存在时写入(先到先得; 节点服务那边同样用 O_EXCL)。
fn write_first(path: &Path, v: &Value) -> bool {
    // 先写临时文件再硬链接过去: 读方看不到半截内容; 目标已存在时 link 失败 = 先写者胜
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    if fs::write(&tmp, serde_json::to_vec(v).unwrap_or_default()).is_err() { return false; }
    let ok = match fs::hard_link(&tmp, path) {
        Ok(()) => true,
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
        // 不支持硬链接的文件系统: 退回独占创建
        Err(_) => {
            use std::io::Write;
            fs::OpenOptions::new().write(true).create_new(true).open(path)
                .and_then(|mut f| f.write_all(&serde_json::to_vec(v).unwrap_or_default())).is_ok()
        }
    };
    let _ = fs::remove_file(&tmp);
    ok
}

pub fn request_json(r: &Request, timeout_s: u64) -> Value {
    json!({
        "schema": "phonefarm/approval@1",
        "id": r.id, "state": "pending",
        "category": r.category.id(), "category_zh": r.category.zh(), "critical": r.category.critical(),
        "reason": r.reason, "action": r.action_line,
        "goal": r.goal, "task": r.task, "run_id": r.run_id, "step": r.step,
        "serial": r.serial, "pkg": r.pkg,
        "screenshot": r.screenshot,
        "job_id": std::env::var("PF_JOB_ID").ok(),
        "created_at": now_s(), "expires_at": now_s() + timeout_s as f64,
    })
}

pub fn parse_decision(text: &str) -> Option<Decision> {
    let v: Value = serde_json::from_str(text).ok()?;
    let d = v.get("decision")?.as_str()?.trim().to_ascii_lowercase();
    let allow = match d.as_str() {
        "allow" | "approve" | "yes" => true,
        "deny" | "reject" | "no" => false,
        _ => return None,
    };
    Some(Decision {
        allow,
        by: v.get("by").and_then(|x| x.as_str()).unwrap_or("?").to_string(),
        note: v.get("note").and_then(|x| x.as_str()).unwrap_or("").to_string(),
    })
}

/// 发出请求并阻塞等待决定。终端可交互时同时读 stdin(y/n)。
/// 返回的 Decision 已经写回 `<id>.decision.json`(超时与终端答复也写), 请求文件 state 同步更新。
pub fn request_and_wait(dir: &Path, r: &Request, timeout_s: u64) -> Decision {
    let _ = fs::create_dir_all(dir);
    let req_path = dir.join(format!("{}.json", r.id));
    let dec_path = dir.join(format!("{}.decision.json", r.id));
    // 同名旧决定(理论上 64 位 id 不会撞)一律作废, 决不沿用
    let _ = fs::remove_file(&dec_path);
    let mut req = request_json(r, timeout_s);
    if let Err(e) = write_atomic(&req_path, &req) {
        // 请求都写不出去 = 没人能看到 = 拒绝
        return Decision { allow: false, by: "error".into(), note: format!("审批请求写入失败: {e}") };
    }
    println!("      🛑 审批[{}] {}: {} — 等待决定(≤{}s) 请求 {}", r.category.zh(), r.action_line, r.reason, timeout_s,
             req_path.display());

    // 终端问答: 全局只有一个读 stdin 的线程, 行经通道送来; 每个请求开始前先丢掉积压的行(防误触回车)。
    let tty = tty_lines();
    // 提示出现之后(再留 300 ms)才敲的行才算答复; 之前积压或刚好在路上的回车一律丢弃
    let asked_at = Instant::now() + Duration::from_millis(300);
    if let Some(rx) = tty {
        if let Ok(rx) = rx.lock() { while rx.try_recv().is_ok() {} }
        if r.category == Category::Handoff {
            print!("      在手机上做完后输入 y 回车(放弃输入 n): ");
        } else {
            print!("      允许执行? [y/N] ");
        }
        let _ = std::io::Write::flush(&mut std::io::stdout());
    }

    let deadline = Instant::now() + Duration::from_secs(timeout_s);
    let decision = loop {
        if let Ok(t) = fs::read_to_string(&dec_path) {
            if let Some(d) = parse_decision(&t) {
                break d;
            }
        }
        if let Some(rx) = tty {
            let line = rx.lock().ok().and_then(|rx| rx.try_recv().ok());
            if let Some((at, line)) = line {
                if at < asked_at { continue; }
                let l = line.trim().to_ascii_lowercase();
                let allow = matches!(l.as_str(), "y" | "yes") || (r.category == Category::Handoff && l == "完成")
                    || (r.category != Category::Handoff && l == "允许");
                let _ = write_first(&dec_path, &json!({"decision": if allow {"allow"} else {"deny"}, "by": "tty"}));
                continue;
            }
        }
        if Instant::now() >= deadline {
            let d = Decision { allow: false, by: "timeout".into(), note: format!("{timeout_s}s 内无人决定, 按拒绝处理") };
            if !write_first(&dec_path, &json!({"decision":"deny","by":"timeout","note":d.note})) {
                // 最后一刻有人答了: 以先落盘的为准
                std::thread::sleep(Duration::from_millis(200));
                if let Some(d2) = fs::read_to_string(&dec_path).ok().and_then(|t| parse_decision(&t)) { break d2; }
            }
            break d;
        }
        std::thread::sleep(Duration::from_millis(500));
    };
    req["state"] = json!(if decision.allow { "allowed" } else { "denied" });
    req["decided_by"] = json!(decision.by);
    req["decided_at"] = json!(now_s());
    let _ = write_atomic(&req_path, &req);
    decision
}

/// 允许的有效期: 同一应用、同一类、同一目标文字, 在 `grant_s` 秒内再次出现不再问。
/// 目标文字一变(金额、收款人、条数)就重新问——不做模糊匹配。
#[derive(Default)]
pub struct Grants {
    /// 非关键类: 同应用同类同目标文字, grant_s 内可复用
    items: Vec<(String, Instant)>,
    /// 后续确认额度: 每次人批准后给同应用同类别一次, FOLLOW_UP_S 内用掉一次即失效
    follow: Vec<(String, Instant)>,
}

/// 批准后的后续确认(如删除确认框上的「确定」)要在多少秒内发生才算同一件事。
pub const FOLLOW_UP_S: u64 = 60;

/// 页面上下文签名: 当屏最靠上的几个稳定短文字(不含数字), 用来区分「同名按钮、不同对象」
/// (聊天 A 和聊天 B 的「发送」、两个订单页的「确认」)。
pub fn context_sig(screen: &[&str]) -> String {
    let mut v: Vec<String> = screen.iter().map(|t| norm(t))
        .filter(|t| !t.is_empty() && t.chars().count() <= 20 && !t.chars().any(|c| c.is_ascii_digit()))
        .take(6).collect();
    v.dedup();
    v.join("|")
}

impl Grants {
    fn key(pkg: &str, c: Category, target: &str) -> String {
        format!("{pkg}\u{1f}{}\u{1f}{}", c.id(), norm(target))
    }
    fn fkey(pkg: &str, c: Category) -> String {
        format!("{pkg}\u{1f}{}", c.id())
    }
    /// 这次动作是否已被先前的批准覆盖。关键类(付款/删除/钱包)本身每次都要问, 只有紧跟其后的
    /// 后续确认能用掉一次额度; 非关键类按「观察到的目标文字」在 grant_s 内复用。
    /// 收回全部额度: 敏感动作发出后结果不明时用, 之后同类动作一律重新问人。
    pub fn clear(&mut self) {
        self.items.clear();
        self.follow.clear();
    }
    pub fn covers(&mut self, pkg: &str, c: Category, target: &str, grant_s: u64, follow_up: bool) -> bool {
        let now = Instant::now();
        self.items.retain(|(_, t)| now.duration_since(*t).as_secs() < grant_s.max(1));
        self.follow.retain(|(_, t)| now.duration_since(*t).as_secs() < FOLLOW_UP_S);
        if follow_up {
            if let Some(i) = self.follow.iter().position(|(k, _)| *k == Self::fkey(pkg, c)) {
                self.follow.remove(i);
                return true;
            }
            return false;
        }
        if c.critical() || grant_s == 0 {
            return false;
        }
        self.items.iter().any(|(k, _)| *k == Self::key(pkg, c, target))
    }
    pub fn add(&mut self, pkg: &str, c: Category, target: &str) {
        if !c.critical() {
            self.items.push((Self::key(pkg, c, target), Instant::now()));
        }
        self.follow.push((Self::fkey(pkg, c), Instant::now()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts<'a>(action: &'a str, what: Option<&'a str>, target: Vec<&'a str>, screen: Vec<&'a str>) -> Facts<'a> {
        Facts { action, what, text: None, target_texts: target, screen_texts: screen, pkg: "com.example", target_ids: vec![] }
    }

    #[test]
    fn pay_button_is_critical() {
        let h = classify(&facts("tap", Some("立即支付"), vec![], vec![]), &Policy::default()).unwrap();
        assert_eq!(h.category, Category::Payment);
        assert!(h.category.critical());
    }

    #[test]
    fn long_paragraph_mentioning_pay_is_not_a_button() {
        let p = "本服务支持多种支付方式, 详情请阅读用户协议与隐私政策, 如有疑问请联系客服或在帮助中心查看更多说明";
        assert!(classify(&facts("tap", None, vec![p], vec![p]), &Policy::default()).is_none());
    }

    #[test]
    fn english_multi_word_and_long_buttons() {
        let p = Policy::default();
        for (w, c) in [("Delete account", Category::Delete), ("Remove friend", Category::Delete),
                       ("Buy for $4.99", Category::Payment), ("Place order now", Category::Payment),
                       ("Pay with card", Category::Payment), ("Sign out", Category::Account)] {
            assert_eq!(classify(&facts("tap", Some(w), vec![], vec![]), &p).map(|h| h.category), Some(c), "{w}");
        }
        assert!(classify(&facts("tap", Some("Order history"), vec![], vec![]), &p).is_none());
        // 真实的长按钮(超过旧的 16 字上限)
        let h = classify(&facts("tap", None, vec!["立即支付 ¥128.00 (已优惠¥20.00)"], vec![]), &p).unwrap();
        assert_eq!(h.category, Category::Payment);
        assert_eq!(classify(&facts("tap", None, vec!["删除所选的 12 张照片和视频"], vec![]), &p).unwrap().category, Category::Delete);
    }

    #[test]
    fn long_press_and_swipe_are_classified() {
        let p = Policy::default();
        assert_eq!(classify(&facts("long_press", Some("确认支付"), vec![], vec![]), &p).unwrap().category, Category::Payment);
        assert_eq!(classify(&facts("swipe", None, vec!["滑动删除"], vec![]), &p).unwrap().category, Category::Delete);
        assert_eq!(classify(&facts("swipe", None, vec!["右滑付款"], vec![]), &p).unwrap().category, Category::Payment);
        assert!(classify(&facts("swipe", None, vec!["推荐"], vec![]), &p).is_none());
        // 普通滚动从「待支付」「清除数据」上起手不算
        assert!(classify(&facts("swipe", None, vec!["待支付"], vec![]), &p).is_none());
        assert!(classify(&facts("swipe", None, vec!["清除数据"], vec![]), &p).is_none());
    }

    #[test]
    fn long_what_needs_to_be_the_tapped_control() {
        let p = Policy::default();
        let title = "如何删除微信聊天记录而不被对方发现";
        assert!(classify(&facts("tap", Some(title), vec![], vec![]), &p).is_none());
        let btn = "删除所选的 12 张照片和视频";
        assert_eq!(classify(&facts("tap", Some(btn), vec![btn], vec![]), &p).unwrap().category, Category::Delete);
    }

    #[test]
    fn context_sig_tells_chats_apart() {
        assert_ne!(context_sig(&["张三", "发送"]), context_sig(&["李四", "发送"]));
        assert_eq!(context_sig(&["张三", "12:30", "发送"]), context_sig(&["张三", "12:31", "发送"]));
    }

    #[test]
    fn ids_are_long_and_unique() {
        let a = new_id("run-1", 3);
        let b = new_id("run-1", 3);
        assert_ne!(a, b);
        assert_eq!(a.rsplit('-').next().unwrap().len(), 16);
    }

    #[test]
    fn english_word_boundary() {
        assert!(classify(&facts("tap", Some("Display"), vec![], vec![]), &Policy::default()).is_none());
        assert_eq!(classify(&facts("tap", Some("Pay"), vec![], vec![]), &Policy::default()).unwrap().category, Category::Payment);
        assert_eq!(classify(&facts("tap", Some("Place order"), vec![], vec![]), &Policy::default()).unwrap().category,
                   Category::Payment);
    }

    #[test]
    fn confirm_on_cashier_page_is_payment() {
        let h = classify(&facts("tap", Some("确认"), vec![], vec!["收银台", "¥12.00"]), &Policy::default()).unwrap();
        assert_eq!(h.category, Category::Payment);
        assert!(classify(&facts("tap", Some("确认"), vec![], vec!["设置"]), &Policy::default()).is_none());
    }

    #[test]
    fn confirm_in_delete_dialog() {
        let h = classify(&facts("tap", Some("确定"), vec![], vec!["要删除此记事吗?", "取消", "确定"]), &Policy::default()).unwrap();
        assert_eq!((h.category, h.follow_up), (Category::Delete, true));
        // 编辑页上有个「删除」按钮, 但没有问句: 点「确定」不算
        assert!(classify(&facts("tap", Some("确定"), vec![], vec!["删除", "分享"]), &Policy::default()).is_none());
        let mut g = Grants::default();
        g.add("p", Category::Delete, "删除");
        // 关键类本身不复用: 再点一次「删除」还得问
        assert!(!g.covers("p", Category::Delete, "删除", 45, false));
        // 紧跟的确认框可用掉一次额度, 只一次
        assert!(!g.covers("p", Category::Payment, "确定", 45, true));
        assert!(g.covers("p", Category::Delete, "确定", 45, true));
        assert!(!g.covers("p", Category::Delete, "确定", 45, true));
    }

    #[test]
    fn delete_beats_send() {
        let h = classify(&facts("tap", Some("删除并发送"), vec![], vec![]), &Policy::default()).unwrap();
        assert_eq!(h.category, Category::Delete);
    }

    #[test]
    fn send_and_password() {
        assert!(classify(&facts("tap", None, vec!["发送"], vec![]), &Policy::default()).is_none(), "非通讯应用的发送不拦");
        let mut m = facts("tap", None, vec!["发送"], vec![]);
        m.pkg = "com.tencent.mm";
        assert_eq!(classify(&m, &Policy::default()).unwrap().category, Category::Send);
        assert_eq!(classify(&facts("tap", Some("拨打"), vec![], vec![]), &Policy::default()).unwrap().category, Category::Send);
        let mut f = facts("type", None, vec!["请输入密码"], vec![]);
        assert_eq!(classify(&f, &Policy::default()).unwrap().category, Category::Password);
        f.screen_texts = vec!["请输入支付密码"];
        assert_eq!(classify(&f, &Policy::default()).unwrap().category, Category::Payment);
        let mut g = facts("type", None, vec![], vec![]);
        g.target_ids = vec!["id/et_pwd"];
        assert_eq!(classify(&g, &Policy::default()).unwrap().category, Category::Password);
    }

    #[test]
    fn wallet_app_any_tap() {
        let mut f = facts("tap", Some("卡片"), vec![], vec![]);
        f.pkg = "com.huawei.wallet";
        assert_eq!(classify(&f, &Policy::default()).unwrap().category, Category::Wallet);
        let mut l = facts("launch", None, vec![], vec![]);
        l.text = Some("com.apple.Passbook");
        assert_eq!(classify(&l, &Policy::default()).unwrap().category, Category::Wallet);
    }

    #[test]
    fn benign_actions_pass() {
        for w in ["搜索", "天气", "设置", "返回", "我的", "确定"] {
            assert!(classify(&facts("tap", Some(w), vec![], vec![]), &Policy::default()).is_none(), "{w}");
        }
        assert!(classify(&facts("swipe", Some("删除"), vec![], vec![]), &Policy::default()).is_none());
    }

    #[test]
    fn critical_cannot_be_auto() {
        let mut p = Policy::default();
        p.critical = Mode::Auto;
        p.enabled = false;
        assert_eq!(p.mode_for(Category::Payment), Mode::Ask);
        assert_eq!(p.mode_for(Category::Send), Mode::Auto);
        p.critical = Mode::Deny;
        assert_eq!(p.mode_for(Category::Delete), Mode::Deny);
    }

    #[test]
    fn grants_scope_to_same_target() {
        let mut g = Grants::default();
        g.add("com.tencent.mm", Category::Send, "发送");
        assert!(g.covers("com.tencent.mm", Category::Send, " 发送 ", 45, false));
        assert!(!g.covers("com.tencent.mm", Category::Send, "发送(2)", 45, false));
        assert!(!g.covers("com.tencent.mobileqq", Category::Send, "发送", 45, false));
        assert!(!g.covers("com.tencent.mm", Category::Send, "发送", 0, false));
    }

    #[test]
    fn policy_from_toml() {
        let v: toml::Value = toml::from_str("send='auto'\npassword='deny'\ntimeout_s=60\nwallet_pkgs=['com.x.pay']").unwrap();
        let p = Policy::from_toml(Some(&v));
        assert_eq!((p.send, p.password, p.timeout_s), (Mode::Auto, Mode::Deny, 60));
        assert_eq!(p.wallet_pkgs, vec!["com.x.pay".to_string()]);
    }

    #[test]
    fn file_channel_roundtrip_and_timeout() {
        let dir = std::env::temp_dir().join(format!("pf-approval-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        std::env::set_var("PF_APPROVAL_NO_TTY", "1");
        let r = Request { id: "ap-t-1".into(), category: Category::Send, reason: "r".into(), action_line: "tap".into(),
            goal: "g".into(), task: "t".into(), run_id: "x".into(), step: 1, serial: "s".into(), pkg: "p".into(), screenshot: None };
        // 请求之前就存在的同名决定必须作废(不沿用旧答复)
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("ap-t-1.decision.json"), r#"{"decision":"allow","by":"stale"}"#).unwrap();
        // 请求发出后才写的决定才算数
        let dp = dir.join("ap-t-1.decision.json");
        let writer = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(1200));
            fs::write(dp, r#"{"decision":"allow","by":"web"}"#).unwrap();
        });
        let d = request_and_wait(&dir, &r, 5);
        writer.join().unwrap();
        assert_eq!(d, Decision { allow: true, by: "web".into(), note: String::new() });
        let req: Value = serde_json::from_str(&fs::read_to_string(dir.join("ap-t-1.json")).unwrap()).unwrap();
        assert_eq!(req["state"], "allowed");
        // 无人答: 超时拒绝并落决定
        let mut r2 = r.clone();
        r2.id = "ap-t-2".into();
        let d2 = request_and_wait(&dir, &r2, 5);
        assert!(!d2.allow);
        assert_eq!(d2.by, "timeout");
        assert!(dir.join("ap-t-2.decision.json").exists());
        let _ = fs::remove_dir_all(&dir);
    }
}
