---
name: hgamey-quickstart
description: 外部团队的 agent 首次接入 HGamey 节点，在平台真机上运行自己的渲染或超分算法并取回帧率、GPU 与功耗数据。适用于第一次使用；长期研究流程见 hgamey-research。
---

# HGamey 快速上手

> HGamey 节点连接若干台真机。agent 只通过 `pf-node` 使用平台；人在网页 `/dash/` 查看进度并留言。
>
> **1. 接入。** 向管理员索取邀请码（24 小时有效，仅用一次）和研究 ID：
>
> ```sh
> git clone https://github.com/HGamey/hgamey
> export PF_NODE_URL=https://gameharness.liyao.space
> python3 hgamey/harness-ops/node/pf-node join --invite <邀请码> --name <名字> \
>   --token-file ~/.config/hgamey/agent.token
> ```
>
> 把输出的三行 export 写入包装脚本 `bin/pf-node`，末行 `exec python3 <克隆目录>/hgamey/harness-ops/node/pf-node "$@"`；用 `./bin/pf-node whoami` 验证。
>
> **2. 提交代码。** 代码推到 HGamey/experiments 的自己分支 `ext/<团队>/<主题>`（向管理员申请写权限），推送即可运行。可参照 `selfsr/`：Vulkan 层在 `selfsr/gray/layer`，新算法作为新的 `algo` 加入，`selfsr/baseline_cmp.sh` 与基线比较。
>
> **3. 运行。** `./bin/pf-node phones` 查看手机，记下目标手机的 `<节点ID>/<设备ID>` 与串号：
>
> ```sh
> ./bin/pf-node --experiment ext/<团队>/<主题> --research <研究ID> --device-access write \
>   --device <节点ID>/<设备ID> --resource device:<串号> \
>   --detach experiment selfsr/baseline_cmp.sh <串号> SETS=fsr
> ```
>
> `--device` 决定在哪台执行机运行，`--resource` 只锁这一台手机；两者都要写。
>
> **4. 取结果。** `status <任务ID>` 看状态，`wait <任务ID>` 等结束并下载产物到 `./pf-node-out/<任务ID>`。
>
> **5. 规则。** token 只存于文件；手机状态由脚本结束时还原；任务卡住用 `reset <任务ID> --note <说明>`；需人判断用 `research note <研究ID> "<问题>" --ask`。

认领、盲评与交付见 `../hgamey-research/SKILL.md`。
