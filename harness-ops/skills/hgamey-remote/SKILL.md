---
name: hgamey-remote
description: 通过 HGamey 节点操作已授权的远程 Android 设备：显式绑定节点身份、取得持久会话，提交截图、点击、滑动、输入、按键和应用启停，并查询原请求结果。适用于远程 UI 操作与排查；不提供任意 shell、安装、文件推拉或 logcat。研究目标与反馈接续可另用 hgamey-research。
---

# HGamey 远程设备操作

此入口使用当前 `node/pf-remote` 或同协议 MCP 工具。先确认用户授权的节点、设备和动作范围；安装 skill、持有令牌或动作不消耗模型 token，都不扩大授权。

## 显式选择节点

需要节点 URL、预期 `node_id` 和自己的操作员凭据文件。`node_id` 是 32 位小写十六进制；令牌由节点管理员签发，以 `hgop_` 开头，不写入仓库或命令正文。

```bash
# NODE_URL、NODE_ID、OPERATOR_TOKEN_FILE 均来自本次明确选择的目标。
# 以下在 harness-ops checkout 中运行；不是某个生产节点的默认配置。
remote() {
  python3 node/pf-remote --url "$NODE_URL" --node-id "$NODE_ID" \
    --token-file "$OPERATOR_TOKEN_FILE" "$@"
}
remote node
remote devices
```

首次只读发现可用 `pf-remote --url … --token-file … node`；确认目标后保存其 ID，后续读写都带原 ID。URL、主机显示名或存储路径不是身份替代品。身份变化或缺少 `node.target-v1` 时停止写入，不接受新 ID 来绕过拒绝。当前 CLI 拒绝自动重定向，并发送 `X-Expected-Node-ID`；非 loopback 连接要求 HTTPS。显式环境变量 `PF_NODE_URL`、`PF_NODE_ID` 和 `PF_NODE_TOKEN_FILE` 也可使用，没有隐式生产 URL 或祖先目录凭据搜索。

`devices` 返回策略、持有者和能力；只对本次授权且 `enabled` 的设备继续。还要检查 `capabilities.available`、设备动作列表及 `ownership: strong-lease-v1`。设备在目录中出现，不代表可取得租约。当前动作仅支持 Android/ADB；HDC 设备可能列出，但不支持此远程控制协议。

## 会话、动作与原 ID 续查

为每次新接管和新动作分配不同的 `request_id`，在发送前保存它与 URL、node ID、serial。ID 为 1–64 个 ASCII 字母、数字、点、下划线或连字符。接管 ID 后续称 `acquisition_id`；从接管结果保存 `session_id`。

```bash
remote acquire "$SERIAL" --request-id "$ACQUISITION_ID" --max-minutes 30
remote session "$SERIAL" --acquisition-id "$ACQUISITION_ID"

# 仅在原会话已确认为 ready 后提交动作。OUT_FILE 必须不存在。
remote action "$SERIAL" screenshot --session-id "$SESSION_ID" \
  --request-id "$SCREENSHOT_ID" --params '{"max_side":1280}' --out "$OUT_FILE"
remote receipt "$SERIAL" "$SCREENSHOT_ID" --session-id "$SESSION_ID"

# 用单独的动作 ID 请求关闭，再查询原会话。
remote action "$SERIAL" release --session-id "$SESSION_ID" --request-id "$RELEASE_ID"
remote session "$SERIAL" --session-id "$SESSION_ID"
```

- `accepted` / `started` 只表示待定；继续只读查询原 acquisition/session/request ID。截图尚未完成时，之后可用 `receipt … --out NEW_FILE` 获取已完成原件，不会重新截屏。
- 超时、断线、`unknown` 或 `recovery_required` 不证明动作未执行。保留原 ID，只读续查，不换 ID 重放。409 也不统一等于“别人占用”；按原会话与回执判断，不能绕过冲突抢占。
- `release` 先关闭新动作准入；此前已开始的一条有界动作可结束，其余未开始动作应为 `not_executed`。回执已接受不等于已释放，以会话 `phase: closed` 为准。
- 心跳 TTL 和最长时长触发有序关闭，不是强制解锁证明。节点退出、owner 丢失或未决传输可能保留真实租约屏障，需管理员按恢复合同核验；操作员不能靠删除文件、等待 TTL 或更换锁域解除。
- 正常关闭只证明该会话的命令已收束，不恢复应用/UI 状态。若状态保留 `original_outcome: unknown`，不能因人工退休后 `closed` 就改称原动作成功或未开始。

CLI 退出码：`0` 为正常返回，`1` 为拒绝/失败/未执行或输入错误，`3` 为仍待定，`4` 为未知或恢复所需。结合 JSON 的动作状态和会话 phase 判断，不把进程退出码替代设备恢复证据。

## 当前动作

动作都使用 `action SERIAL ACTION --session-id … --request-id … --params JSON`：

| ACTION | 参数 |
| --- | --- |
| `tap` | `x`, `y` |
| `swipe` | `x1`, `y1`, `x2`, `y2`, `ms`（默认 300，范围 1–10000） |
| `text` | `text`，1–1000 个非控制字符；实际输入支持仍取决于设备 |
| `key` | `key`，Android keycode 0–400，或服务端支持的按键名 |
| `screenshot` | `max_side`，默认 1280，范围 0–8192；0 请求原尺寸 PNG |
| `app_start` / `app_stop` | `package`；start 可带 `activity` |
| `renew` / `release` | `{}` |

坐标是设备原始像素，整数范围 0–100000。截图可缩小；普通回执的原尺寸是 `result.original_size`，图片响应的 `X-Meta`（CLI 打印的元数据）另含顶层 `size`。按原尺寸换算，不能拿预览像素直接点击。截图原件上限 8 MiB。`shell`、`install`、`uninstall`、`push`、`pull`、`logcat` 当前明确不支持；不退回旧 raw API 或自行运行远程 ADB 来补齐。

## MCP、网页和进一步资料

`/remote/mcp` 使用操作员认证。工具包括 `hgremote_devices`、`hgremote_acquire`、`hgremote_status`，以及 `hgremote_` 前缀的上表动作。接管必须带 `request_id`；动作必须带 `serial`、`session_id`、`request_id`。MCP 客户端也需固定 `X-Expected-Node-ID`、确认节点/动作能力并拒绝跨重定向转发凭据；不能满足这些条件时使用当前 CLI，不照搬旧的仅 Authorization 配置。

人可使用节点的 `/dash/control`。共享研究目标、方向、协助与反馈接续见可选的 [hgamey-research](../hgamey-research/SKILL.md)，无需把一次 UI 操作扩展成研究流程。

- [目标身份合同](https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/FLEET-TARGET.md)
- [远程所有权与恢复边界](https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/REMOTE-OWNERSHIP-REPAIR.md)
- 精确参数以当前 `python3 node/pf-remote --help`、子命令 help 和实例返回能力为准；文档存在不代表该节点已部署对应版本。
