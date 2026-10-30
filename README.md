# HGamey

HGamey 是外部 AI agent 与人协作、在真实手机上研究游戏性能与画质的平台。人在网页上提出研究任务、查看过程、给出指导并评判结果；agent 在平台之外运行，通过命令行 `pf-node` 认领任务、在手机上执行实验、回应指导并交付成果。

## 核心流程
1. 人在网页上新建研究任务；agent 自行认领，或由人指派。
2. 实验以任务（job）的形式在节点上排队执行。每台手机由谁占用、在做什么，排队的任务在等哪台手机、被谁占着，网页与命令行显示同一份状态。
3. 研究页汇集方向、运行记录、留言、结果、图表与图片。agent 自己无法判断的问题（例如画质），由它向人提问或发起盲评 A/B。
4. 人在研究中给出指导，agent 采纳或回复；等待回复期间，agent 继续推进其他方向。
5. 交付包含可直接复用的成果（如配置参数、模型）和报告，并写明所依据任务的有效性、完整复现命令与适用条件。

## 组成
| 目录 | 作用 |
|---|---|
| `harness-ops/` | 节点服务、任务队列与调度、研究数据、网页看板、命令行 `pf-node`、agent skill |
| `phonefarm/` | 设备层（Rust）：设备锁、采集、灰档层、评测 |
| `game_opt_loop/` | 可选的自动优化循环 GOL（Rust） |
| `refbench/` | 白盒参考负载 |
| `deploy.py`、`deploy/` | 按单个提交构建、发布并核对一个节点 |

实验代码在 [HGamey/experiments](https://github.com/HGamey/experiments)，按固定提交运行；设计、计划与实验报告在 [HGamey/docs](https://github.com/HGamey/docs)。

## 使用
- **人**：打开节点的 `/dash/`。首页显示手机占用、进行中的任务、Agent 状态与待回复问题；查看与写操作分别需要口令。
- **Agent**：每轮从 `pf-node checkin` 开始，它列出待办与每轮做法；研究用 `pf-node research claim|note|deliver` 处理，实验用 `pf-node --research <研究ID> --detach <命令>` 提交。每个 agent 使用自己的 token，写入都记在它名下。详见[研究 skill](harness-ops/skills/hgamey-research/SKILL.md)。
- **运维**：在节点上运行 `python3 deploy.py <节点名> [--ref <提交>]`，`--status` 查看实际运行的版本；回滚即部署上一个提交。任务卡住时，负责人或管理员用 `pf-node reset <任务ID> --note <说明>` 一步释放。

## 开发
规则见 [AGENTS.md](AGENTS.md)：直接提交到 main；一个提交即一个版本，各组件按同一提交构建、测试与部署。CI 包含 `tests`（harness、refbench、phonefarm、GOL）与 `code-only` 两项。本地测试在仓库根目录执行：
```sh
python3 -m unittest discover -s harness-ops/node/tests
(cd refbench && python3 -m unittest discover -s tests -p 'test_*.py')
(cd phonefarm/src && cargo test)
(cd game_opt_loop && cargo test)
```
本仓库由四个原仓库于 2026-10-28 合并而成，旧提交号对照与导入的分支记录见 `history/`。
