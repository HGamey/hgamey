# GOAL: game_opt_loop 实现与真机联调 (Implementation Prompt)

> 本文档是一个自包含的 Prompt / Goal，可直接完整复制丢给新启动的 Agent 会话执行。

---

## 目标使命
在 `HGamey/game_opt_loop` 仓库中，实现纯 Rust 原生的移动端游戏自主优化闭环：
基于大模型与真机物理约束，驱动超分辨率 (SR) 与双倍插帧 (Frame Gen) Compute Shader 算子的自主变异与代际演化，通过 `phonefarm` 自动化底座完成确定性物理采样与统计裁决，最终达成**“游戏原生降载至 720p，输出 1080p 满帧且功耗显著下降”**的性能魔法。

---

## 必读上下文与硬约束 (Red-lines)
1. **纯 Rust 实现**：除调用外部已编译工具外，本仓库禁止引入 Python、Shell 胶水或动态解释器，保持单二进制的高可靠性。
2. **重活归位 phonefarm**：真机连接 (adb/hdc)、冷机温控基准、60s 脚本确定性回放、整机瓦数采样与冻结规则 t 检验全部调用同级目录的 `../phonefarm/phonefarm`，本仓库只做编排与算法进化。
3. **安全防爆原则**：生成的 Shader 必须通过本地验证器秒级防爆拦截（检查语法、无界死循环），非法代码直接本地打回，绝不推入真机。
4. **画质硬约束**：PSNR 必须 >= 基线，不可为了偷帧率而牺牲画质。

---

## 交付步骤与里程碑验收

### 阶段 1：LLM 变异生成器实现 (`src/engine/generator.rs`)
- 读取 `../phonefarm/secrets.env` 中的 API Token（优先支持 OpenAI 兼容接口 / GLM / Claude）。
- 实现 Prompt 编排：将赛道模板 (`templates/sr/template.comp`)、历史 Pareto 优胜档案 (`pareto_archive.json`) 与真机硬件预算注入 Prompt。
- 自动化解析 LLM 吐出的 GLSL Compute Shader 源码并生成 `Candidate` 结构体。

### 阶段 2：Phonefarm CLI 联动调度 (`src/harness/mod.rs`)
- 实现向 `phonefarm` 发送评测指令。
- 自动解析 phonefarm 回传的 `eval_report.json`，提取 `operator_latency_ms`、`power_watt`、`fps_p95_ms` 和 `p_value`。

### 阶段 3：多代自主进化闭环 (`src/engine/evolution.rs`)
- 实现 Pareto 支配排序算法（3维目标：延迟 ms 最小化、整机功耗 W 最小化、PSNR 画质最大化）。
- 运行 `cargo run -- --track sr --generations 3 --children 4`，成功记录至少 3 代演化日志并归档至 `runs/evo/archive.json`。

### 阶段 4：冒烟验收测试
- 运行 `cargo test` 全部通过。
- 产出 `evidence/M0_report.json`，记录单代编译拦截率与本地闭环完整度。
