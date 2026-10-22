# AGENTS.md

## 代码仓库只放代码

- 不要在本仓库新增或修改说明文档、图片、PPT、PDF、实验报告。
- 这次改了什么、为什么改：写在 commit message 正文。
- 一个功能或修复的说明、验证与限制写进 commit 正文；长期记录归 HGamey/docs。
- 长期文档（设计、规格、架构图、进展汇报）：提交到 https://github.com/HGamey/docs ，本仓库的旧文档在那里的 `repos/<仓库名>/` 下。
- 允许的例外：根目录 README.md（40 行以内）、AGENTS.md、skills/、许可证、程序或测试要读的数据。CI 检查 `code-only` 会拦下其他文档类文件。

## 产品方向与提交

- 面向外部 Agent 的远程真机研究平台；网页与 CLI/API 同等优先，GOL 是可选客户端。
- 目标、方向、实验、证据和异步人工反馈独立于聊天工具；一个方向等人时其他方向继续。
- 产品准则与计划：https://github.com/HGamey/docs/tree/main/plans/2026-10-22-agent-platform 。维护者接续时先读准则、讨论、todo，并持续记录决定与验证范围。
- 用户已授权直接 commit/push，不新开 PR。每批围绕一个可验收变化；提交前核对当前 main，保留同事改动，不 force push。
- 修改节点协议后运行 `python3 -m unittest discover -s node/tests -v`。模拟节点通过不能写成真机验证通过。
- 测试聚焦关键合同和已发生故障，避免镜像实现或重复覆盖。优先简化状态与资源所有权；哈希用于传输完整性/内容标识，不对每层对象重复计算。
