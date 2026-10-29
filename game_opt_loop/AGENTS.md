# AGENTS.md

## 代码仓库只放代码

见根目录 [AGENTS.md](../AGENTS.md)：提交、文档、测试与部署规则对全仓库统一。

## 产品与验证

整套平台服务于外部 Agent：使用远程真机系统性优化指定游戏/系统，探索多种方向，
完成可复现实验并留下可审核结果。网页与 CLI/API 同等优先，人类异步参与；
只阻断依赖其反馈的方向，优先级由外部 Agent 决定。
GOL 提供可选的优化编排、候选与判定工具；不要求使用者把研究目标交给内置 Agent。
设备枚举只说明连接条目；legacy gpu-op 单独检查显式目标与 Android 载体前提，
不能借另一台在线设备的资格，也不能把此限制外推到所有 eval 路线。合同：
https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/GOL-DEVICE-ELIGIBILITY.md 。
产品准则与开发计划：https://github.com/HGamey/docs/tree/main/plans/2026-10-22-agent-platform 。
运行受改动影响的关键验证，不为覆盖数堆测试或无用哈希；共享合同变化再扩大回归。

## 直接提交与团队开发

- 基于当前 main 分批开发，提交与部署按根目录 AGENTS.md。
- 主 Agent 聚焦系统结构、协议与整合；前端等独立模块可交具体 subagent，划分文件所有权。
- 每批记录已做、未做、测试层级和恢复义务；部署与代码提交分开。

## 任务锁

GOL 向所选 PF 取得同一组内核锁 fd，单设备范围按 serial／登记别名归一；没有第二套 owner、队列或续期。默认等待到拿到或被取消，显式等待期限仍可指定。人工暂停关闭持有的 fd，继续时重新取锁；label 只作显示。PF 与 GOL 须同版升级，宿主替身通过不代表真机验证。
