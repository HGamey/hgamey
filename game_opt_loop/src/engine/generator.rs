//! LLM 变异生成器
//!
//! 职责: 把「赛道契约 + 硬件预算 + 历代优胜档案 + 上一代被打回的原因」编排成 prompt,
//! 要大模型吐 GLSL, 再把吐出来的东西解析成 `Candidate`。
//!
//! 三条设计约束:
//! 1. **密钥只读不带走**。从 `../phonefarm/secrets.env` 读 key, 只用于 Authorization 头,
//!    绝不进日志、prompt 或归档。
//! 2. **失败必须可降级**。LLM 是外部服务, 会超时、会限流、会空回包。
//!    任何失败都只让这一个子代作废, 由 `evolution` 用本地变异器补齐 —— 闭环不能
//!    因为某个 API 抽风就停摆 (SPEC_SR_LOOP §5.1 的同款底线)。
//! 3. **prompt 里不出现论文名与网络名**。只给语法、预算与真机反馈, 让它自己搜。

use crate::engine::validator::GateReport;
use crate::engine::{Candidate, CandidateOrigin, EvalReport};
use crate::tracks::TrackSpec;
use anyhow::{anyhow, bail, Context, Result};
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

/// 一个 OpenAI 兼容的推理服务。
#[derive(Debug, Clone)]
pub struct Provider {
    pub name: &'static str,
    pub url: &'static str,
    pub model: &'static str,
    /// 密钥在 secrets.env / 环境变量里的名字。
    pub key_env: &'static str,
    pub timeout_s: u64,
    /// 是否请求关闭模型的思考链。
    ///
    /// 实测 glm-4.7 生成 4 个算子时, 12.5k 输出 token 里有 9.1k 是思考链,
    /// 整个请求 224 s; 关掉之后 40 s, 仍然稳定吐出 4 个能过防爆门禁的算子。
    /// 变异任务要的是产量与多样性, 不是单个候选的深思熟虑 ——
    /// 好不好由真机裁决, 不由模型自己想清楚。
    pub thinking_disable: bool,
    /// 服务只接受固定温度时填这里 (Kimi For Coding 只认 0.6), 否则用默认 0.85。
    pub temperature: Option<f64>,
}

/// 候选服务表, 按优先级排列。与 phonefarm 现有的 provider 配置保持同源。
pub const PROVIDERS: &[Provider] = &[
    // Kimi For Coding 排第一: 手上有好几个号, 可以轮换 (KIMI_KEY、KIMI_KEY_2 … 或
    // KIMI_KEYS_FILE), 一个号周额度用完 (403) 或被限流 (429) 就换下一个。
    Provider {
        name: "kimi-for-coding",
        url: "https://api.kimi.com/coding/v1/chat/completions",
        model: "kimi-for-coding",
        key_env: "KIMI_KEY",
        timeout_s: 420,
        thinking_disable: true,
        temperature: Some(0.6),
    },
    Provider {
        name: "glm-4.7",
        url: "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
        model: "glm-4.7",
        key_env: "GLM_KEY",
        timeout_s: 420,
        thinking_disable: true,
        temperature: None,
    },
    Provider {
        name: "siliconflow-qwen3-coder",
        url: "https://api.siliconflow.cn/v1/chat/completions",
        model: "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        key_env: "SILICONFLOW_KEY",
        timeout_s: 420,
        thinking_disable: false,
        temperature: None,
    },
    Provider {
        name: "openrouter",
        url: "https://openrouter.ai/api/v1/chat/completions",
        model: "qwen/qwen3-coder",
        key_env: "OPENROUTER_KEY",
        timeout_s: 420,
        thinking_disable: false,
        temperature: None,
    },
];

/// 解析 `secrets.env`。
///
/// 只认 `KEY=VALUE` 与 `export KEY=VALUE` 两种形态, 引号可选。
/// 刻意**不**做任何 shell 展开 —— 这是一份配置, 不是脚本; `$(...)`、反引号、
/// 管道之类的东西一律当普通字符, 不给命令注入留口子。
pub fn parse_secrets(text: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line).trim();
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty()
            || !key
                .chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        {
            continue;
        }
        let mut value = value.trim();
        for q in ['"', '\''] {
            if value.len() >= 2 && value.starts_with(q) && value.ends_with(q) {
                value = &value[1..value.len() - 1];
                break;
            }
        }
        if value.is_empty() {
            continue;
        }
        out.insert(key.to_string(), value.to_string());
    }
    out
}

/// 同一家服务最多认几个号: `KEY`, `KEY_2` … `KEY_9`。
const MAX_KEYS_PER_PROVIDER: usize = 9;

fn key_slot(key_env: &str, i: usize) -> String {
    if i == 1 {
        key_env.to_string()
    } else {
        format!("{key_env}_{i}")
    }
}

/// 某家服务手上的全部密钥, 按 `KEY`, `KEY_2` … 的顺序, 去重。
pub fn provider_keys(secrets: &BTreeMap<String, String>, p: &Provider) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for i in 1..=MAX_KEYS_PER_PROVIDER {
        if let Some(v) = secrets.get(&key_slot(p.key_env, i)) {
            let v = v.trim();
            if !v.is_empty() && !out.iter().any(|k| k == v) {
                out.push(v.to_string());
            }
        }
    }
    out
}

/// 解析一份多密钥文件: 每行一个号, `名字 密钥` 或只写 `密钥`, `#` 开头是注释。
pub fn parse_key_file(text: &str) -> Vec<String> {
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .filter_map(|l| l.split_whitespace().last().map(str::to_string))
        .collect()
}

/// 从磁盘读密钥表。环境变量优先于文件。
///
/// 除了 `KEY` / `KEY_2` … 之外, 还认 `KEYS_FILE` (如 `KIMI_KEYS_FILE`):
/// 指向一份每行一个号的文件, 依次填进还空着的槽位。
pub fn load_secrets(path: &Path) -> BTreeMap<String, String> {
    let mut secrets = std::fs::read_to_string(path)
        .map(|t| parse_secrets(&t))
        .unwrap_or_default();
    for p in PROVIDERS {
        for i in 1..=MAX_KEYS_PER_PROVIDER {
            let slot = key_slot(p.key_env, i);
            if let Ok(v) = std::env::var(&slot) {
                if !v.trim().is_empty() {
                    secrets.insert(slot, v);
                }
            }
        }
        let file_var = format!("{}S_FILE", p.key_env);
        let file = std::env::var(&file_var)
            .ok()
            .or_else(|| secrets.get(&file_var).cloned());
        if let Some(file) = file {
            let have = provider_keys(&secrets, p);
            let extra = std::fs::read_to_string(file.trim())
                .map(|t| parse_key_file(&t))
                .unwrap_or_default();
            let mut free = (1..=MAX_KEYS_PER_PROVIDER)
                .map(|i| key_slot(p.key_env, i))
                .filter(|s| !secrets.contains_key(s))
                .collect::<Vec<_>>()
                .into_iter();
            for k in extra.into_iter().filter(|k| !have.contains(k)) {
                let Some(slot) = free.next() else { break };
                secrets.insert(slot, k);
            }
        }
    }
    secrets
}

/// 挑一个当前有密钥的服务。
pub fn select_provider<'a>(
    secrets: &BTreeMap<String, String>,
    prefer: Option<&str>,
) -> Option<&'a Provider> {
    candidate_providers(secrets, prefer).into_iter().next()
}

/// 有密钥的服务, 按优先级。指名了就只给那一家 (没密钥则为空, 不静默换别家)。
fn candidate_providers<'a>(
    secrets: &BTreeMap<String, String>,
    prefer: Option<&str>,
) -> Vec<&'a Provider> {
    PROVIDERS
        .iter()
        .filter(|p| prefer.is_none_or(|n| p.name == n))
        .filter(|p| !provider_keys(secrets, p).is_empty())
        .collect()
}

/// 喂给 LLM 的一条历史记录。
pub struct EliteSummary {
    pub id: String,
    pub notes: Option<String>,
    pub report: EvalReport,
    pub shader_source: String,
}

/// 上一代某个子代的下场, 用作反馈。
pub struct RejectionSummary {
    pub id: String,
    pub reason: String,
}

/// prompt 的全部输入。
pub struct MutationContext<'a> {
    pub track: &'a TrackSpec,
    pub generation: u32,
    pub children: u32,
    /// 目标机型的架构约束描述。
    pub device: &'a str,
    /// 当前 Pareto 优胜集 (作为亲本与范例)。
    pub elites: Vec<EliteSummary>,
    /// 上一代被本地门禁或真机打回的原因。
    pub rejections: Vec<RejectionSummary>,
    /// 赛道基线模板。
    pub template: &'a str,
}

/// 系统提示词: 规定角色、硬约束与输出格式。
pub fn system_prompt(track: &TrackSpec) -> String {
    format!(
        "你是移动端 GPU 图形算子工程师。你的任务是写 Vulkan GLSL 450 Compute Shader,\n\
         在严格的真机物理预算下完成「{display}」。\n\
         \n\
         赛道目标: {objective}\n\
         \n\
         == 不可协商的硬约束 ==\n\
         1. 零拷贝: 只能读写下面这张表里的 storage image。禁止声明 SSBO、\n   \
            禁止额外 uniform block、禁止额外 binding、禁止改变量名。\n\
            画面一旦离开显存去内存里绕一圈, 单帧就要多付 7ms —— 直接出局。\n\
         2. 单帧算子耗时必须 <= {budget:.2} ms。\n\
         3. 画质是约束不是目标: PSNR 必须 >= {baseline:.3} dB 的基线。\n   \
            严禁用模糊、粗暴降采样等手段偷帧率, 也不许产生摩尔纹 / 振铃 / 边缘破碎。\n\
         4. 所有循环必须**静态可定界**: 上下界都是编译期常量。\n   \
            `while(true)` / `for(;;)` / 上界来自 imageSize() 的循环一律本地打回。\n\
         5. barrier() 只能出现在工作组一致的控制流里 (不能放在 if / for 内部)。\n\
         6. 每一次 imageStore 都必须被 imageSize() 派生的边界条件保护住。\n\
         7. 片上 shared 内存总量 <= 32 KiB。\n\
         \n\
         == 允许的资源 (一字不能改) ==\n\
         {bindings}\n\
         == 输出格式 ==\n\
         只输出 GLSL 代码块, 每个候选一个独立的 ```glsl 代码块, 从 `#version 450` 开始。\n\
         代码块之外不要写任何解释。",
        display = track.display,
        objective = track.objective,
        budget = track.budget_ms,
        baseline = track.quality_baseline_db,
        bindings = track.binding_table(),
    )
}

/// 用户提示词: 模板 + 硬件 + 历史 + 反馈。
pub fn user_prompt(ctx: &MutationContext) -> String {
    let mut s = String::new();

    s.push_str(&format!(
        "第 {} 代变异。请给出 {} 个**互不相同**的候选算子。\n\n",
        ctx.generation, ctx.children
    ));

    s.push_str(&format!("== 目标硬件 ==\n{}\n\n", ctx.device));

    for (i, hint) in ctx.track.hints.iter().enumerate() {
        if i == 0 {
            s.push_str("== 赛道要点 ==\n");
        }
        s.push_str(&format!("- {hint}\n"));
    }
    s.push('\n');

    s.push_str("== 基线模板 (binding 布局与边界守卫照抄, 算法部分随便改) ==\n```glsl\n");
    s.push_str(ctx.template);
    s.push_str("\n```\n\n");

    if ctx.elites.is_empty() {
        s.push_str("== 历史优胜 ==\n还没有任何通过真机验证的算子。请从模板出发做第一批探索。\n\n");
    } else {
        s.push_str("== 历史优胜 (Pareto 前沿; 请在它们的基础上继续推进) ==\n");
        for e in &ctx.elites {
            s.push_str(&format!(
                "--- {} [{}] 耗时 {:.3} ms | 功耗 {} | 画质 {} | 数据来源 {:?} ---\n```glsl\n{}\n```\n",
                e.id,
                e.notes.as_deref().unwrap_or("-"),
                e.report.metrics.operator_latency_ms,
                // 没量到就照实说。给 LLM 看一个 "0.00 W" 会让它以为这个算子不耗电。
                e.report
                    .metrics
                    .power_watt
                    .map(|w| format!("{w:.2} W"))
                    .unwrap_or_else(|| "未测量".to_string()),
                e.report
                    .metrics
                    .psnr_db
                    .map(|q| format!("{q:.3} dB"))
                    .unwrap_or_else(|| "未测量".to_string()),
                e.report.source,
                e.shader_source.trim(),
            ));
        }
        s.push('\n');
    }

    if !ctx.rejections.is_empty() {
        s.push_str("== 上一代被打回的原因 (别再犯) ==\n");
        for r in &ctx.rejections {
            s.push_str(&format!("- {}: {}\n", r.id, r.reason));
        }
        s.push('\n');
    }

    s.push_str(&format!(
        "现在输出 {} 个 ```glsl 代码块。每个都必须是完整的、能独立编译的 Compute Shader。",
        ctx.children
    ));
    s
}

/// 从 LLM 的自由文本里抠出 GLSL 代码块。
///
/// 依次尝试: 带语言标签的围栏 → 任意围栏 → 整段文本本身就是 shader。
/// 模型爱在代码块外加解释、爱用 ```comp 也爱用 ```c, 解析必须宽容,
/// 但抠出来的东西照样要过防爆门禁 —— 宽容解析不等于放松校验。
pub fn extract_shaders(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = text;

    while let Some(start) = rest.find("```") {
        let after = &rest[start + 3..];
        let Some(nl) = after.find('\n') else { break };
        let body = &after[nl + 1..];
        let Some(end) = body.find("```") else {
            // 围栏没闭合: 模型被截断了。剩下的如果像 shader 就收, 否则丢。
            if body.contains("#version") && body.contains("void main") {
                out.push(body.trim().to_string());
            }
            break;
        };
        let block = body[..end].trim();
        if block.contains("#version") && block.contains("void main") {
            out.push(block.to_string());
        }
        rest = &body[end + 3..];
    }

    if out.is_empty() {
        let t = text.trim();
        if t.starts_with("#version") && t.contains("void main") {
            out.push(t.to_string());
        }
    }
    out
}

/// 一条线路的失败: 换下一条可能有救, 还是换了也没用。
enum Attempt {
    Next(anyhow::Error),
    Fatal(anyhow::Error),
}

/// LLM 客户端。
///
/// 手里是一串「服务 + 密钥」线路: 同一家的多个号排在一起, 别家的排在后面。
/// 当前线路因为额度、限流、鉴权或网络出错时, 换下一条重试; 成功的那条记住,
/// 下次从它开始, 不用每次都先撞一遍已经没额度的号。
pub struct Generator {
    routes: Vec<(&'static Provider, String)>,
    current: AtomicUsize,
    timeout_override: Option<u64>,
    client: reqwest::blocking::Client,
}

impl Generator {
    /// 按 secrets 挑一个可用服务建客户端。没有任何可用密钥时返回 Err,
    /// 上层据此降级到本地变异器。
    pub fn from_secrets(secrets: &BTreeMap<String, String>, prefer: Option<&str>) -> Result<Self> {
        Self::from_secrets_with_timeout(secrets, prefer, None)
    }

    /// 同上, 但可以覆盖超时 (供 CLI 的 --llm-timeout-s 用)。
    pub fn from_secrets_with_timeout(
        secrets: &BTreeMap<String, String>,
        prefer: Option<&str>,
        timeout_s: Option<u64>,
    ) -> Result<Self> {
        let routes: Vec<(&'static Provider, String)> = candidate_providers(secrets, prefer)
            .into_iter()
            .flat_map(|p| provider_keys(secrets, p).into_iter().map(move |k| (p, k)))
            .collect();
        if routes.is_empty() {
            bail!(
                "secrets.env 里没有任何可用的模型密钥 (需要 {})",
                PROVIDERS
                    .iter()
                    .map(|p| p.key_env)
                    .collect::<Vec<_>>()
                    .join(" / ")
            );
        }
        let client = reqwest::blocking::Client::builder()
            .build()
            .context("构造 HTTP 客户端失败")?;
        Ok(Self {
            routes,
            current: AtomicUsize::new(0),
            timeout_override: timeout_s,
            client,
        })
    }

    /// 当前 (最近一次成功或下一次先试) 的服务名。
    pub fn provider_name(&self) -> &'static str {
        self.routes[self.current.load(Ordering::Relaxed) % self.routes.len()]
            .0
            .name
    }

    /// 一共有几条线路 (服务 × 号)。
    pub fn route_count(&self) -> usize {
        self.routes.len()
    }

    /// 发一轮变异请求, 返回原始文本。线路出错时依次换号、换服务。
    pub fn complete(&self, system: &str, user: &str) -> Result<String> {
        let n = self.routes.len();
        let start = self.current.load(Ordering::Relaxed) % n;
        let mut errors = Vec::new();
        for step in 0..n {
            let i = (start + step) % n;
            let (provider, key) = &self.routes[i];
            match self.complete_on(provider, key, system, user) {
                Ok(text) => {
                    self.current.store(i, Ordering::Relaxed);
                    return Ok(text);
                }
                Err(Attempt::Fatal(e)) => return Err(e),
                Err(Attempt::Next(e)) => {
                    // 只记服务名和第几个号, 不记密钥。
                    let nth = self.routes[..i]
                        .iter()
                        .filter(|r| r.0.name == provider.name)
                        .count()
                        + 1;
                    errors.push(format!("{} 第 {nth} 个号: {e:#}", provider.name));
                }
            }
        }
        bail!("所有线路都失败了:\n  {}", errors.join("\n  "))
    }

    fn complete_on(
        &self,
        provider: &Provider,
        key: &str,
        system: &str,
        user: &str,
    ) -> std::result::Result<String, Attempt> {
        let next = |e: anyhow::Error| Attempt::Next(e);
        let mut body = serde_json::json!({
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": provider.temperature.unwrap_or(0.85),
            "stream": false,
        });
        if provider.thinking_disable {
            body["thinking"] = serde_json::json!({ "type": "disabled" });
        }

        let resp = self
            .client
            .post(provider.url)
            .timeout(Duration::from_secs(
                self.timeout_override.unwrap_or(provider.timeout_s),
            ))
            .bearer_auth(key)
            .json(&body)
            .send()
            .with_context(|| format!("调用 {} 失败", provider.name))
            .map_err(next)?;

        let status = resp.status();
        let text = resp.text().unwrap_or_default();
        if !status.is_success() {
            // 只回状态码与截断的响应体, 绝不回显请求头 (里面有密钥)。
            let e = anyhow!(
                "{} 返回 {}: {}",
                provider.name,
                status,
                text.chars().take(300).collect::<String>()
            );
            // 额度用完 / 限流 / 鉴权 / 服务端故障 → 换下一条线路; 其余 (如 400 请求本身有问题) 换了也没用。
            let code = status.as_u16();
            return Err(
                if matches!(code, 401 | 402 | 403 | 408 | 429) || status.is_server_error() {
                    Attempt::Next(e)
                } else {
                    Attempt::Fatal(e)
                },
            );
        }

        let json: serde_json::Value = serde_json::from_str(&text)
            .with_context(|| "响应不是合法 JSON")
            .map_err(next)?;
        let msg = &json["choices"][0]["message"];
        let content = msg["content"]
            .as_str()
            .or_else(|| msg["reasoning_content"].as_str())
            .or_else(|| msg["reasoning"].as_str())
            .unwrap_or_default();
        if content.trim().is_empty() {
            return Err(next(anyhow!("{} 返回空内容", provider.name)));
        }
        Ok(content.to_string())
    }

    /// 走一轮完整变异: 组 prompt → 调模型 → 抠代码 → 装成 Candidate。
    ///
    /// 这里**不**做门禁 —— 门禁由 `evolution` 统一执行, 这样 LLM 产物与本地变异
    /// 产物走的是同一条校验路径, 统计口径才一致。
    pub fn mutate(
        &self,
        ctx: &MutationContext,
        next_id: &mut dyn FnMut() -> String,
    ) -> Result<Vec<Candidate>> {
        let system = system_prompt(ctx.track);
        let user = user_prompt(ctx);
        let raw = self.complete(&system, &user)?;
        let shaders = extract_shaders(&raw);
        if shaders.is_empty() {
            bail!("模型回包里没有可解析的 GLSL 代码块");
        }

        let parent = ctx.elites.first().map(|e| e.id.clone());
        Ok(shaders
            .into_iter()
            .take(ctx.children as usize)
            .map(|src| {
                Candidate::new(
                    next_id(),
                    ctx.generation,
                    ctx.track.id,
                    parent.clone(),
                    ctx.track.budget_ms,
                    src,
                    CandidateOrigin::Llm,
                    Some(format!("llm:{}", self.provider_name())),
                )
            })
            .collect())
    }
}

/// 把一次门禁结果压成一行给 LLM 的反馈。
pub fn rejection_from_gate(id: &str, gate: &GateReport) -> RejectionSummary {
    RejectionSummary {
        id: id.to_string(),
        reason: gate.feedback_line(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tracks::SR;

    #[test]
    fn parses_both_export_and_bare_assignments() {
        let t =
            "# 注释\nexport GLM_KEY=\"abc.123\"\nKIMI_KEY=sk-plain\n\nexport EMPERO_KEY='free'\n";
        let kv = parse_secrets(t);
        assert_eq!(kv.get("GLM_KEY").unwrap(), "abc.123");
        assert_eq!(kv.get("KIMI_KEY").unwrap(), "sk-plain");
        assert_eq!(kv.get("EMPERO_KEY").unwrap(), "free");
    }

    /// secrets.env 是配置不是脚本: 命令行、`$(...)`、坏行都不能被当成键值。
    #[test]
    fn ignores_shell_noise_and_malformed_lines() {
        let t = "rm -rf /tmp/x\nBAD KEY=1\nexport EVIL=\"$(whoami)\"\nlower_case=1\nEMPTY=\n";
        let kv = parse_secrets(t);
        assert!(!kv.contains_key("BAD KEY"));
        assert!(!kv.contains_key("lower_case"));
        assert!(!kv.contains_key("EMPTY"));
        // 值里的 $() 被当作普通字符原样保留, 不会被展开成命令。
        assert_eq!(kv.get("EVIL").unwrap(), "$(whoami)");
    }

    #[test]
    fn picks_the_first_provider_with_a_key() {
        let mut kv = BTreeMap::new();
        kv.insert("SILICONFLOW_KEY".to_string(), "sk-x".to_string());
        let p = select_provider(&kv, None).expect("应选出 siliconflow");
        assert_eq!(p.key_env, "SILICONFLOW_KEY");

        kv.insert("GLM_KEY".to_string(), "g".to_string());
        // GLM 在表里优先级更高。
        assert_eq!(select_provider(&kv, None).unwrap().key_env, "GLM_KEY");

        kv.insert("KIMI_KEY_3".to_string(), "k".to_string());
        // Kimi 排第一, 且不要求从 KIMI_KEY 这个槽位开始填。
        assert_eq!(select_provider(&kv, None).unwrap().key_env, "KIMI_KEY");
    }

    #[test]
    fn collects_every_key_of_a_provider_in_slot_order() {
        let mut kv = BTreeMap::new();
        kv.insert("KIMI_KEY_2".into(), "b".into());
        kv.insert("KIMI_KEY".into(), "a".into());
        kv.insert("KIMI_KEY_9".into(), "c".into());
        kv.insert("KIMI_KEY_4".into(), "a".into()); // 重复的号只算一次
        kv.insert("GLM_KEY".into(), "g".into());
        assert_eq!(provider_keys(&kv, &PROVIDERS[0]), vec!["a", "b", "c"]);

        let g = Generator::from_secrets(&kv, None).unwrap();
        // 3 个 Kimi 号在前, GLM 兜底在后。
        assert_eq!(g.route_count(), 4);
        assert_eq!(g.provider_name(), "kimi-for-coding");
        assert_eq!(
            Generator::from_secrets(&kv, Some("glm-4.7"))
                .unwrap()
                .route_count(),
            1
        );
    }

    #[test]
    fn key_file_takes_named_or_bare_lines() {
        let t = "# 注释\nkimi-1 sk-aaa\n\nsk-bbb\n";
        assert_eq!(parse_key_file(t), vec!["sk-aaa", "sk-bbb"]);
    }

    #[test]
    fn honours_an_explicit_provider_choice() {
        let mut kv = BTreeMap::new();
        kv.insert("GLM_KEY".into(), "g".into());
        kv.insert("OPENROUTER_KEY".into(), "o".into());
        assert_eq!(
            select_provider(&kv, Some("openrouter")).unwrap().key_env,
            "OPENROUTER_KEY"
        );
        // 指名了但没密钥 → 不静默回退到别家。
        assert!(select_provider(&kv, Some("kimi-for-coding")).is_none());
    }

    #[test]
    fn no_keys_means_no_provider() {
        assert!(select_provider(&BTreeMap::new(), None).is_none());
    }

    #[test]
    fn extracts_multiple_fenced_shaders() {
        let text = "先说两句。\n```glsl\n#version 450\nvoid main() { /* a */ }\n```\n中间插话\n```glsl\n#version 450\nvoid main() { /* b */ }\n```\n收尾。";
        let got = extract_shaders(text);
        assert_eq!(got.len(), 2);
        assert!(got[0].contains("/* a */"));
        assert!(got[1].contains("/* b */"));
    }

    #[test]
    fn tolerates_odd_fence_languages_and_bare_output() {
        let tagged = "```comp\n#version 450\nvoid main() {}\n```";
        assert_eq!(extract_shaders(tagged).len(), 1);

        let bare = "#version 450\nlayout(local_size_x=16) in;\nvoid main() {}";
        assert_eq!(extract_shaders(bare).len(), 1);
    }

    #[test]
    fn skips_fences_that_are_not_shaders() {
        let text = "```bash\nrm -rf /\n```\n```glsl\n#version 450\nvoid main() {}\n```";
        let got = extract_shaders(text);
        assert_eq!(got.len(), 1);
        assert!(got[0].contains("#version"));
    }

    #[test]
    fn recovers_a_truncated_final_block() {
        let text = "```glsl\n#version 450\nvoid main() { int x = 1;";
        let got = extract_shaders(text);
        assert_eq!(got.len(), 1);
    }

    #[test]
    fn returns_nothing_for_prose() {
        assert!(extract_shaders("抱歉, 我不能这么做。").is_empty());
    }

    // ---------- prompt 内容 ----------

    #[test]
    fn system_prompt_states_every_hard_constraint() {
        let p = system_prompt(&SR);
        assert!(p.contains("1.50 ms"), "没写死耗时预算");
        assert!(p.contains("38.666"), "没写死画质基线");
        assert!(p.contains("u_InputTexture") && p.contains("u_OutputTexture"));
        assert!(p.contains("静态可定界"));
        assert!(p.contains("SSBO"));
    }

    /// 变异提示词只给语法、预算和真机反馈, 不提任何论文或算法名 ——
    /// 抄名字等于把搜索空间提前收窄, 那就不是自主探索了。
    #[test]
    fn prompts_name_no_papers_or_algorithms() {
        let ctx = MutationContext {
            track: &SR,
            generation: 3,
            children: 4,
            device: "Adreno 840",
            elites: Vec::new(),
            rejections: Vec::new(),
            template: crate::tracks::embedded_template(crate::TrackMode::Sr),
        };
        let all = format!("{}\n{}", system_prompt(&SR), user_prompt(&ctx));
        let lower = all.to_lowercase();
        for banned in [
            "fsr", "dlss", "lanczos", "amd", "nvidia", "espcn", "edsr", "xess", "gsr",
        ] {
            assert!(
                !lower.contains(banned),
                "提示词里出现了算法/厂商名: {banned}"
            );
        }
    }

    #[test]
    fn user_prompt_feeds_back_last_generation_rejections() {
        let ctx = MutationContext {
            track: &SR,
            generation: 2,
            children: 3,
            device: "Adreno 840",
            elites: Vec::new(),
            rejections: vec![RejectionSummary {
                id: "gen1_c2".into(),
                reason: "UNBOUNDED_LOOP: 循环体里找不到任何 break 出口".into(),
            }],
            template: "TEMPLATE",
        };
        let p = user_prompt(&ctx);
        assert!(p.contains("gen1_c2"));
        assert!(p.contains("UNBOUNDED_LOOP"));
        assert!(p.contains("TEMPLATE"));
        assert!(p.contains("3 个"));
    }

    #[test]
    fn gate_feedback_compresses_to_one_line() {
        let gate = crate::engine::Validator::new().gate(
            "#version 450\nlayout(local_size_x=16) in;\nvoid main() { }",
            &SR,
        );
        let r = rejection_from_gate("c1", &gate);
        assert_eq!(r.id, "c1");
        assert!(!r.reason.is_empty());
        assert!(!r.reason.contains('\n'), "反馈必须压成一行");
    }
}
