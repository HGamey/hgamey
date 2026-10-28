# game_opt_loop

外部 Agent 可选的游戏优化方法客户端（Rust）：候选生成、方法编排及与 [phonefarm](../phonefarm) 的实验对接。[harness-ops](../harness-ops) 的网页与 CLI/API 提供共享研究状态和异步人工协助；研究方向与优先级由外部 Agent 决定。

## 构建与运行

```bash
cargo build --release

# 可选客户端：打印计划；设备观察仍受能力与租约约束
cargo run --release -- optimize --game 原神 --plan

# 受控执行示例：先核设备能力，显式指定设备与时长预算
cargo run --release -- optimize --game 原神 --device <serial> --budget 2h

# 本机只读面板
cargo run --release -- serve --port 8765
```

本客户端的本地配置需要 `../phonefarm` 与本仓库并排放。大模型密钥从 `../phonefarm/secrets.env` 读取（KIMI_KEY / GLM_KEY / SILICONFLOW_KEY / OPENROUTER_KEY 任填一个）；不填或加 `--no-llm` 时用本地变异器。

## 文档

- [产品准则](https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/PRODUCT-CHARTER.md)：GOL 可选，网页与 CLI/API 同等优先；源码与隔离验收不等于指定设备或完整流程已可用。
- 完整说明、目标模式、面板：[HGamey/docs · repos/game_opt_loop](https://github.com/HGamey/docs/tree/main/repos/game_opt_loop)
- 现状：[STATUS.md](https://github.com/HGamey/docs/blob/main/repos/game_opt_loop/STATUS.md)
- 确认/案例/候选准备/模型工件等子系统规格：[docs/SPEC_*.md](https://github.com/HGamey/docs/tree/main/repos/game_opt_loop/docs)
- 第三方接入：[INTEGRATION.md](https://github.com/HGamey/docs/blob/main/repos/game_opt_loop/docs/INTEGRATION.md)
- 给 AI 编程助手：[skills/game_opt_loop/SKILL.md](skills/game_opt_loop/SKILL.md)

本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
