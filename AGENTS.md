# AGENTS.md

HGamey 是一个仓库：`harness-ops/`（节点服务、网页、`pf-node`）、`phonefarm/`（设备层，Rust）、`game_opt_loop/`（可选优化循环，Rust）、`refbench/`（白盒载体）。各目录的 AGENTS.md 只写本组件的约束；与本文件冲突时以本文件为准。

## 提交

- 直接提交并推 main（用户 2026-10-28 决定）。推前 `git pull --rebase`，保留同事改动，不 force push，不关闭必需检查。
- 一个提交就是一个版本：跨组件的改动放在同一提交里，构建、测试和部署都按提交整体进行。
- 改了什么、为什么、验证与限制写在 commit message 正文。
- 旧仓库 HGamey/harness-ops、phonefarm、game_opt_loop、refbench 已归档只读；旧提交号对照与导入的分支见 `history/`。

## 代码仓库只放代码

- 不新增说明文档、图片、PPT、PDF、实验报告；长期文档（设计、规格、进展）放 https://github.com/HGamey/docs 。
- 允许：根目录 README.md（项目说明与部署手册，不限行数，2026-10-30 用户要求）、各组件目录的 README.md（每个不超过 40 行）、AGENTS.md、skills/、许可证、程序或测试要读的数据、`history/` 下的迁移对照。推到 main 后 CI 的 `code-only` 检查会对其他文档类文件报错。

## 测试

在仓库根目录：

```sh
python3 -m unittest discover -s harness-ops/node/tests
(cd refbench && python3 -m unittest discover -s tests -p 'test_*.py')
(cd phonefarm/src && cargo test)
(cd game_opt_loop && cargo test)
```

只跑与改动相关的检查，跨组件协议变化时再扩大回归。合并仓库前就存在的失败：`phonefarm/src/tests/confirmation_driver_cli.rs` 4 个，`game_opt_loop/tests/goal_mode.rs` 9 个；不要据此判断新改动。模拟或隔离宿主通过不等于真机验证。

## 部署

- 在节点上运行 `python3 deploy.py <节点名>`：默认 origin/main，`--ref` 指定提交，`--status` 查看节点实际运行的版本。节点配置在 `deploy/<节点名>.json`，只放路径和服务命令，不放凭据。回滚就是部署上一个提交。
- deploy.py 先停服务再换文件，独立任务继续运行，部署回执记录当时活着的任务。新执行电脑用 `deploy.py join` 加入中心，后续用它生成的 `--config` 部署。不要手工 rsync 覆盖安装、改写 `harness-ops` 游标，也不要删除 `releases/`、`deploy-state/`、`_jobs/` 或 token。
- 没有 stable 分支和自动部署脚本；`pf-node-deploy`、`update_node --git` 已停用。

## Agent 使用平台

- 每轮从 `pf-node checkin` 开始：它列出现在欠的事（人工留言、反馈、卡住的任务、过期的状态、该交付的研究）和每轮做法。
- 用自己的 token 工作：向管理员拿邀请码，设 `PF_NODE_URL` 后运行 `pf-node join --invite <码> --name <名字> --token-file <路径>`；将它打印的三行环境变量放进包装脚本。之后的写入都记在你名下。
- 任务就是 research：`pf-node research claim|note|thread|deliver`（见 `pf-node research --help`）。回应人工留言用 `--reply-to <留言> --decision adopt|defer|reject`；交付要有总结、产物和适用的游戏版本与设备。
- 一个方向等人时继续做其他方向，不要停下来等。
