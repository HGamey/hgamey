---
name: hgamey-remote
description: 远程调试 HGamey 手机节点上的设备（截图、点击、滑动、输入、adb shell、装 APK、logcat、推拉文件）。当用户让你在 HGamey 节点/远程真机/模拟器上操作或排查 App 时使用。
---

# HGamey 远程真机

你只需要两样东西：节点地址（如 `https://mbp1.liyao.space`）和你自己的**操作员令牌** `hgop_...`
（找节点主人签发：节点上 `python3 node/node_remote.py operator add <你的名字>`）。令牌只走 HTTPS，别写进仓库。

## 接入 Claude Code（推荐，MCP）

```bash
claude mcp add --transport http hgamey-remote https://mbp1.liyao.space/remote/mcp \
  --header "Authorization: Bearer $HGAMEY_OPERATOR_TOKEN"
```

工具：`hgremote_devices`、`hgremote_acquire` / `renew` / `release`、`hgremote_screenshot`（返回图片）、
`hgremote_tap` / `swipe` / `text` / `key`、`hgremote_shell`、`hgremote_logcat`、`hgremote_app_start` / `app_stop`、
`hgremote_install_apk`（base64 ≤ 48 MiB）、`hgremote_uninstall`、`hgremote_push` / `pull`（≤ 16 MiB）。

## 规矩

1. 先 `hgremote_devices` 看哪些设备 `enabled`、谁在用（`holder`）。只操作 `enabled` 的设备。
2. 任何设备操作前 `hgremote_acquire`；用完一定 `hgremote_release`。300 秒没有任何动作会自动释放，最长时长缺省 60 分钟。
3. 被拒绝（409）说明实验或别人正占用：**不要重试抢占**，把返回的持有者和开始时间告诉用户，换设备或稍后再来。
4. tap/swipe 坐标是**设备像素**；截图可能被缩小，用截图结果里的 `size`（原始宽高）换算。
5. `hgremote_shell` 在设备上执行（不是节点宿主机），超时缺省 20 秒、最多 120 秒，输出截断到 256 KiB；
   不要跑会长时间阻塞的命令（用 `timeout` 参数或 `logcat` 工具代替 `logcat -f`）。
6. 每个操作都记审计（谁、哪台、什么参数、结果、IP）。

## 不用 MCP 时（curl / 脚本）

```bash
H="Authorization: Bearer $HGAMEY_OPERATOR_TOKEN"; B=https://mbp1.liyao.space/remote; D=$B/devices/emulator-5554
curl -H "$H" $B/devices
curl -H "$H" -H 'Content-Type: application/json' -X POST -d '{"max_minutes":30}' $D/session      # 接管
curl -H "$H" "$D/screenshot?max=1280" -o s.jpg                                                     # max=0 取原图 PNG
curl -H "$H" -H 'Content-Type: application/json' -X POST -d '{"x":540,"y":1200}' $D/tap
curl -H "$H" -H 'Content-Type: application/json' -X POST -d '{"cmd":"dumpsys window | grep mCurrentFocus"}' $D/shell
curl -H "$H" -H 'Content-Type: application/octet-stream' -X PUT --data-binary @app.apk $D/install
curl -H "$H" "$D/logcat?lines=200&filter=ActivityManager:I%20*:S"
curl -H "$H" -H 'Content-Type: application/json' -X DELETE -d '{}' $D/session                     # 释放
```

其余动作同名 POST：`swipe {x1,y1,x2,y2,ms}`、`text {text}`、`key {key}`、`app_start {package[,activity]}`、
`app_stop {package}`、`uninstall {package}`、`renew {}`；`PUT push?path=/data/local/tmp/x`、`GET pull?path=...`。
人可以用网页：`/dash/control`（先输查看密码，再输自己的操作员令牌）。
