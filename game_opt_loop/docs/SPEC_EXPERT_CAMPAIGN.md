# 实验 campaign 与每代专家决策

来源：[0924 会议纪要](https://github.com/orgs/HGamey/discussions/10)第 6 条。专家的工作是判断方向、风险、收益与成本，测量与统计裁决仍归 phonefarm。

## 模式与兼容

- 不给 `--campaign-mode`：保留既有运行方式，不声称有专家审查或断点保障。
- `--campaign-mode autonomous`：持久化 campaign，程序自动允许候选；报告明确标记 autonomous，没有专家审核。
- `--campaign-mode expert`：每代先落候选方向与静态门禁结果；未接受的候选不会调用测量。基线也需要接受，不能绕过审批占用设备。
- 两种 campaign 模式都使用 `--resume` 恢复同一输出目录。参数、冻结协议、负载或候选工件改变时拒绝恢复；新实验使用新目录。模式不能在恢复时从 expert 偷换 autonomous。
- 本功能覆盖独立 Shader 演化入口及 `optimize` 的系统参数、灰档路线。它不增加黑盒 SR 注入、真实连续质量检测、训练或量化能力。
- 独立 Shader 的真机 campaign 必须给 `--device` 固定序列号；`optimize` 冻结初次摸底选出的设备。已存在的评测二进制也记录摘要，升级接收端需要新实验目录。

## 生命周期

1. **冻结**：记录入口、设备/目标、随机种子、生成方式、测量来源、预算、载体、冷机/统计协议及输入工件摘要。`optimize` 的能力与计划只在初次摸底生成，恢复读冻结快照。
2. **每代提案**：候选、父代/历史结果、理由、预期收益、风险与成本估计进入 campaign。估计明确是估计，不伪造毫秒或瓦数收益。随机状态和本代候选批次一同保存，恢复不再请求模型重生成本代。
3. **专家决策**：接受、拒绝或延期，必须填写 reviewer 与理由。决策绑定候选完整内容、评测请求/冻结协议、输入工件 SHA-256，拒绝旧审批覆盖新工件。延期可在后续重新决策，已有决定保留历史。
4. **评测**：先原子保存 in-flight，再调用 phonefarm 或明确标记的 dry-run 成本模型。成功/失败逐候选落盘；完成报告复用，恢复不重复评测。评测中崩溃留 in-flight，不自动重试；必须人工核对证据，不能将不确定状态当负样本。
5. **归档与迭代**：统计结果、专家理由与模式进入 campaign/report；拒绝方向反馈到下一代，接受不等于测量通过。每代都有新的审批，上一代批准不自动批准下一代。

## 操作

```bash
game_opt_loop --track sr --dry-run --no-llm --generations 2 --children 2 \
  --campaign-mode expert --out runs/review
game_opt_loop campaign status --out runs/review
game_opt_loop campaign decide --out runs/review --candidate gen0_baseline \
  --action accept --reviewer engineer --reason '先建立同场基线'
# 使用完全相同的运行参数，加 --resume；下一代会再次等待审查。
```

`optimize --game 原神 --campaign-mode expert --no-llm ...` 使用同一套审查命令。也可用 `--all-pending` 对当前待决批次接受、拒绝或延期。

若进程在下发评测后退出，核对接收端的现场还原与回执，再导入报告：

```bash
game_opt_loop campaign reconcile --out runs/review --candidate gen0_baseline \
  --report verified_phonefarm_report.json --reviewer engineer \
  --reason '已核对接收端任务、冻结请求及还原记录'
```

此命令验证回执契约、候选 ID、来源及路线，保存核对人和理由；它不能自动证明外部报告来自同一物理实验，核对责任由操作者承担。

批量决定只对命令执行时的当前待决候选生效，不批准未来代际。报告区分 pending、deferred、rejected、completed、failed、in-flight。`campaign.json` 是审查/恢复的权威账本，`campaign.md` 是可读摘要。两文件均不含密钥。

## 不变量与离线验收

- 专家模式初次运行与延期恢复：fake phonefarm 的实际评测调用为零（只读摸底/命令探活可发生）。
- 接受/拒绝同代候选，恢复只调用接受者；下一代重新等待决定。
- 已完成候选再次恢复不调用评测；在测量返回但归档前中断仍复用账本报告。
- 候选内容、SPIR-V、负载、探测结果或冷机/统计条件被篡改，恢复失败且不启动测量。
- in-flight 恢复失败并指向人工核对，不因进程消失偷偷重复实验。
- 模式和每次决定历史在 JSON/Markdown 与两入口报告可见；autonomous 不显示“专家已审查”。
- `--no-llm` 完整离线集成测试、`cargo test`、clippy，不连真机、不使用 API Token。

## 边界

本地 SHA-256 绑定用于防止工件/协议意外错配，并非对抗能同时修改账本和摘要的恶意本机用户的签名系统。外部评测与本地落盘不能做分布式事务，因此采用 at-most-once 下发：不确定的 in-flight 宁可等待人工核对，也不自动再测。设备租约仍按现有 devlock 操作规程执行，本 PR 不冒充实现租约。
