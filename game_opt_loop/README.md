# game_opt_loop

手机游戏性能优化的入口与决策层（Rust）。接一句话任务「把 <游戏> 在 <手机> 上优化一下」：摸底设备、决定开哪几层优化、让大模型出候选改动、交给 [phonefarm](https://github.com/HGamey/phonefarm) 在真机上做对照实验、按统计结果保留或回退，最后出报告。

## 构建与运行

```bash
cargo build --release

# 只摸底并打印计划，不往手机写任何东西
cargo run --release -- optimize --game 原神 --plan

# 真跑：指定设备和真机时长预算
cargo run --release -- optimize --game 原神 --device <serial> --budget 2h

# 本机只读面板
cargo run --release -- serve --port 8765
```

需要 `../phonefarm` 与本仓库并排放。大模型密钥从 `../phonefarm/secrets.env` 读取（KIMI_KEY / GLM_KEY / SILICONFLOW_KEY / OPENROUTER_KEY 任填一个）；不填或加 `--no-llm` 时用本地变异器。

## 文档

- 完整说明、目标模式、面板：[HGamey/docs · repos/game_opt_loop](https://github.com/HGamey/docs/tree/main/repos/game_opt_loop)
- 现状：[STATUS.md](https://github.com/HGamey/docs/blob/main/repos/game_opt_loop/STATUS.md)
- 第三方接入：[INTEGRATION.md](https://github.com/HGamey/docs/blob/main/repos/game_opt_loop/docs/INTEGRATION.md)
- 给 AI 编程助手：[skills/game_opt_loop/SKILL.md](skills/game_opt_loop/SKILL.md)

本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
