# HGamey

外部 Agent 使用远程真机优化游戏的平台。2026-10-28 起四个代码仓合并于此，各自完整历史保留在同名子目录下。

| 目录 | 原仓库 | 作用 |
|---|---|---|
| `harness-ops/` | HGamey/harness-ops | 节点服务、网页看板、CLI `pf-node` |
| `phonefarm/` | HGamey/phonefarm | 设备层：lease、灰层、采集、评测（Rust） |
| `game_opt_loop/` | HGamey/game_opt_loop | 可选的优化循环 GOL（Rust） |
| `refbench/` | HGamey/refbench | 白盒参考载体 |

一个提交就是一个版本：运行组件按同一 SHA 构建、测试和部署。目录名与节点上的运行布局（`~/node/{harness-ops,phonefarm,game_opt_loop}`）一致，组件之间的 `../phonefarm`、`../refbench` 等相对路径不变。

部署：在节点上运行 `python3 deploy.py <节点名>`（默认 origin/main，`--ref` 指定提交，`--status` 查看节点实际运行的版本）。节点配置在 `deploy/<节点名>.json`，只有路径和服务命令，不含凭据。回滚就是部署上一个提交。

各组件测试（在仓库根目录执行）：

```sh
python3 -m unittest discover -s harness-ops/node/tests
(cd refbench && python3 -m unittest discover -s tests -p 'test_*.py')
(cd phonefarm/src && cargo test)
(cd game_opt_loop && cargo test)
```

旧仓库提交号与本仓库提交号的对应见 `history/*.commit-map`。设计、计划与实验记录仍在 HGamey/docs。
