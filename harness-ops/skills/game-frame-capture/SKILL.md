---
name: game-frame-capture
description: 在 HGamey 的真机上采游戏的连续序列帧：每帧同时拿到带 UI 的画面、不带 UI 的画面、深度和速度图（motion vector），可选几档分辨率，逐帧检查后登记成数据集。适用于原神这类 Unity Vulkan 手游；换游戏或换手机时按本文先找 pass 再采。
---

# 游戏序列帧采集

以原神 7.2（红魔 NX809J）为例，整套流程已经跑通。换场景、换手机只改参数；换游戏要先做「找 pass」那一步。
脚本都在 HGamey/experiments 的 `dataset/` 下，平台上用 `pf-node --experiment <完整提交号> experiment dataset/<脚本>` 跑，参数里写的本机文件会自动上传。

## 能拿到什么

| 内容 | 怎么拿 | 原神 7.2 实测 |
|---|---|---|
| 带 UI 的画面 | 平台 framedump（送显时读回） | RGBA8，尺寸见下一节 |
| 不带 UI 的画面 | 灰层 framedump_att 拷 tonemap 输出 | RGBA8；16:9 各档和送显同尺寸，native 下比送显少 1 像素（2140x968 对 2141x969） |
| 深度 | framedump_att 拷 G-buffer 的深度附件 | D24S8，reverse-Z（近处值大） |
| 速度图 | framedump_att 拷 A2R10G10B10 的速度 pass | 见「深度和速度图」 |
| 分辨率 | `wm size` | 1920x1080、1280x720、960x540 精确；2K 拿不到（封顶 1080p） |

## 准备

1. 手机只通过 `pf-node` 用，每次提交都带 `--resource device:<串号>`。`pf-node phones` 看有哪几台、装没装游戏。
2. 游戏版本：`adb shell dumpsys package <包名> | grep versionName`。需要更新时在节点上下载官方 APK，`pm install -r`。不用私服、不用改过的 APK。
3. 账号要已经登录。登录、验证码、实名、维护公告都要人来处理：用 `research note --ask` 提出来，同时 ping lead，然后去做别的事。账号密码不写进留言、脚本和日志。
4. 第一次启动会下约 500 MB 资源，要几分钟。先单独跑一次进门，确认能进到世界里，再开始采。
5. 进世界用标准的 `phonefarm onboard`（Linux 节点上用 RapidOCR，第一次运行会自动安装）。onboard 进不去时的备用办法见「已知的坑」。
6. 采之前确认手机上没有别人留下的层、knobs 和 wm size 覆盖。`gi_capture.py` 发现这些会直接退出，不去动它们。
7. 层：平台自带的灰层从 hgamey #136 起有 framedump_att，#138 修了 960x540 拷错 pass 和 summary.json 截断。中心部署了 #138 以后不用传 `--so`。要用还没合入的改动，就在 hgamey 里 `REFBENCH_NDK=<NDK 路径> bash phonefarm/knobs/gray/build/build_layer.sh` 编层，用 `--so phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so` 传上去。

## 找 pass（换游戏、换大版本时做一次）

不带 UI 的画面、深度、速度图都在游戏中间的 render pass 里，要先知道是哪几个 pass。

```
pf-node --experiment <提交> --research <研究> --resource device:<串号> --detach \
  experiment dataset/fa_probe.py <串号>
```

`fa_probe.py` 进世界后用平台 frame-audit 记 3 帧，产物是 `knobs_frameaudit.json`。用 `fa_view.py <json> <帧号>` 看每个 pass 的尺寸、附件格式、load/store、读了哪些图。按下面的特征找：

- 不带 UI：UI 合成 pass（送显尺寸、写 swapchain）之前最后一个全尺寸 RGBA8 pass 的输出，它被合成 pass 读。原神是 sig `ac24d328`，附件 0。同 sig 还有一个 512x512 的 pass 排在它前面，层会跳过长边不到送显一半、或宽高比和送显差 10% 以上的 pass，所以每档都拷到真正的那张。
- 深度：G-buffer pass（全尺寸、几个 RGBA8 加一个 D24S8、draw 最多）的深度附件。原神是 sig `0ed8a53d`（有时轮换成 `039d10d4`），附件 3，规则写 `fmt129:3`。
- 速度图：TAA 之前一个全尺寸的单色附件 pass，格式通常是 RG16F 或 A2R10G10B10，深度只读，后面的 TAA pass 读它。原神是 A2R10G10B10，规则写 `fmt58:0`（按格式匹配，原因见「已知的坑」）。

## 分辨率控制

- 用 `wm size <短边>x<长边>` 改逻辑分辨率，游戏冷启动后按它渲染。给 16:9 的值就能拿到精确的档：`wm size 1080x1920` → 1920x1080，`720x1280` → 1280x720，`540x960` → 960x540。
- 不设 wm size（native）时，原神按屏幕宽高比、约 1080p 的像素数渲染：红魔 1216x2688 屏上送显是 2141x969。
- 2K（2560x1440）拿不到：`wm size 1440x2560` 时送显仍是 1920x1080，游戏把输出封顶在约 1080p 的像素数。游戏内「渲染精度」调到最高会不会让不带 UI 的 tonemap 输出超过 1080p，要等有进度的号能打开设置菜单才能查（新号派蒙菜单没解锁）。
- 档位写在 `gi_capture.py --tiers 1920x1080,1280x720,960x540`，脚本每档 force-stop 后冷启动。收尾一定 `wm size reset`，脚本会回读确认。

## UI 开关

原神没有能在普通游玩中随时关 UI 的开关，所以不靠游戏关 UI，而是同一帧拿两份：送显图（带 UI）和 UI 合成之前的 tonemap 输出（不带 UI）。两份是同一帧，`checks.csv` 的 `noui_vs_ui_med`（画面中央的差值中位数）实测 0 到 1；如果是几十，说明翻转或对帧出了问题。

## 深度和速度图

这几样都用灰层的 framedump_att 拷：在指定 pass 的 vkCmdEndRenderPass 之后，同一条命令缓冲里录「屏障 → vkCmdCopyImageToBuffer → 屏障回原布局」，和送显图按同一个帧号写盘。不改游戏的图和 pass；附件没有 TRANSFER_SRC usage 或 storeOp 不是 STORE 时不拷，原因写进 summary.json。

规则写在属性 `debug.knobs.framedump_att` 里，最多 4 条，格式是 `<名字>=<sig 前缀>:<附件下标>` 或 `<名字>=fmt<VkFormat 十进制>:<附件下标>`。原神 7.2：

```
noui=ac24d328:0,depth=fmt129:3,mv=fmt58:0
```

- 深度 D24S8：24 位深度，reverse-Z，近处值大，天空是 0。原始值在 0.002 到 0.08 之间，看图要拉伸。`fd_convert.py` 存两份：`depth16/`（高 16 位，16 位灰度 PNG）和 `depth24/`（R=高 8 位，G=中 8 位，B=低 8 位，无损）。
- 速度图 A2R10G10B10（和不带 UI 的画面同尺寸）：R 是水平速度，G 是竖直速度，B 是运动物体掩码（0 或 1023，原神里是角色轮廓），A 恒为 0。
  编码是开平方：`v = sign(c − 509) · ((c − 509) / 511.5)²`，单位是画面宽（R）或高（G），零点是 509（不是 511.5）。
  v 是这一帧像素相对上一帧的位移：上一帧的位置 = 这一帧位置 − v。翻正之后 y 方向要取反。
  认定依据：静止时 98.5% 的像素 R=G=509，相邻帧画面完全一样；转镜头时 R 按列呈透视旋转的形状（两边大、中间小）；
  按上面的公式逐像素把上一帧对齐到这一帧，有纹理处的误差从 30–33 降到 5–6（降 80–85%），线性编码最多只能降到 18–20。
  检验：`fd_sheet.py <档目录> --mv-check 3`，对齐后误差应明显下降；静止或很暗的画面位移本来就小，降不了多少。换游戏时要重新做这个检验。
- 所有附件在内存里是上下颠倒的（Unity 的 Vulkan 渲染目标），`fd_convert.py` 默认翻正；别的游戏不需要翻就加 `--no-flip`。

## 序列采集

```
pf-node --experiment <提交> --research <研究> --resource device:<串号> --detach --max-runtime-seconds 5400 \
  experiment dataset/gi_capture.py <串号> --scene beach_day --so <层.so> \
  --att noui=ac24d328:0,depth=fmt129:3,mv=fmt58:0 --tiers 1920x1080,1280x720,960x540 --frames 300 --motion run
```

每档的做法：wm size → 设 framedump 属性 → `phonefarm gray-layer session --so` 冷启动游戏 → onboard 进世界 → 按 `--motion` 给输入（run 按住摇杆往 `--angle` 方向跑，每 3 秒左右摆 30°；turn 原地左右拖镜头；static 不动，每 2 秒点一下空白处保持 60 帧）→ `framedump_go` 开一段 → 停会话、停游戏 → 整段 tar 流到节点 → `fd_convert.py` 转 PNG 并逐帧检查 → 删掉手机上的段。
换地方：`--walk-s 35 --angle 180` 在第一档开采前先往后跑 35 秒。位置存在服务器上，冷启动后还在新地方。从开场海滩往前跑会进海里游泳，往后跑回到陆地。
昼夜不用调：游戏里一天约 24 分钟，三档依次冷启动，本身就会落在不同光照（实测一个场景的三档分别是白天、傍晚、日落）。
`--pull meta` 只拉元数据和首、中、尾三帧，用来测很长的序列（见「每百帧耗时」）。

产物在 `gi_<场景>_<时间>/<档>/`：`ui/ noui/ depth16/ depth24/ mv/` 下的 PNG，`index.csv`（每帧时间戳和间隔），`checks.csv`，`summary.json`，开采前后的截图，`report.json`（尺寸、帧数、完整帧数、每个附件的格式和尺寸、耗时、温度、收尾回读）。

## 逐帧检查

`checks.csv` 每帧一行：
- `gap_before`：和上一帧之间漏掉几个 present，应全为 0。
- 各附件一列：ok / MISSING。每段第 1 帧没有附件，这是正常的：附件挂在开段之后的下一次 present 上。
- `motion_med`：画面中央和上一帧的差值中位数。有运动时中位数一般是 1 左右（原神 300 帧实测最大 6）；夜里、水面、天空这种暗而均匀的画面可能整段接近 0，这时看速度图判断有没有动。
- `noui_vs_ui_med`：不带 UI 和带 UI 的差值，应在 0 到 1。
- `depth_hi_range`：深度高 8 位的范围，全 0 说明没拷到。
- 附件缺帧：看 summary.json 里每条规则的 `sig_matched` 和 `sigs_seen`。sig_matched 比帧数少，多半是 pass 换了 sig（见「已知的坑」）。

再用 `fd_sheet.py <档目录> --out sheet.png --mv-check 3` 出接触表，看首、中、尾几帧，确认不带 UI 的那张确实没有 UI、深度轮廓和画面对得上、速度图跟着镜头转。

## 登记数据集

每个场景登记一条。`dataset/gi_manifest.py <任务> <report.json> "<场景名>" "<场景说明>" ["<另外的局限>"…] > m.json` 生成清单（每档的帧数、完整帧数、漏掉的 present、每百帧耗时都作为指标指回 report.json，中心登记时逐项核对），再 `pf-node results register m.json`。清单的样子：

```json
{"schema": "phonefarm/result@1", "result_id": "gi-seq-beach-day-20261113", "title": "原神海滩白天连续序列帧（3 档分辨率，带/不带 UI、深度、速度图）",
 "job_id": "<采集任务>", "labels": {"device": "REDMAGIC NX809J", "game_id": "genshin", "game_version": "7.2.0", "research_id": "<研究>"},
 "origin": {"producer": "dataset/gi_capture.py", "producer_version": "<提交>", "evidence_origin": "device", "observed_at": 1794550000},
 "sources": [{"name": "report", "kind": "job_output", "job_id": "<任务>", "path": "gi_beach_day_<时间>/report.json"}],
 "dataset": {"name": "原神海滩白天序列", "version": "20261113", "pairing": "real", "purpose": "红魔上录的原神连续画面，每帧有带 UI、不带 UI、深度和速度图，用来训练和评估超分、插帧",
             "capture_method": "灰层 framedump 送显读回 + framedump_att 拷中间附件，wm size 控制分辨率", "frames": 900, "scenes": 1,
             "splits": null, "size_bytes": 0, "location": "job:<任务>:gi_beach_day_<时间>/"}}
```

version 写采集日期，同一天第二版写 `20261113-2`。labels.device 只写型号。

## 已知的坑

- onboard 进不去：用 `dataset/gi_enter.py` 的备用办法。它看屏幕底部血条（0.43–0.46, 0.918 处是绿的）判断是否已在世界里，只在游戏在前台时点屏幕中央，游戏被别的界面盖住就切回来，游戏死了就重开。
- RenderDoc 用不了：挂 RenderDoc 层后原神登录完 20 多秒就自己退出，没有 tombstone。不要去绕。
- 不要设全局 `debug.vulkan.layers`，红魔的 gameassist 会崩。层只通过 `gray-layer session` 挂到游戏上。
- 红魔上 logcat 是空的，排查看层自己写的 summary.json 和 session 日志。
- 采集期间屏幕显示只有约 27 fps（红魔的 SF waitframe 会跳帧），但游戏自己仍按 60 帧 present，序列是连续的。看 `index.csv` 的 `dt_ns`，不要看屏幕帧率。
- 不碰屏幕时原神会降到 30 帧。static 段要隔几秒点一下空白处。
- `input` 只有一个触点：按住摇杆跑的同时再 `input swipe` 拖镜头，会把摇杆顶掉，角色就站住了。跑和转镜头分开做；要确认真的在跑，看速度图里有没有从画面中心向外扩散的运动，不要只看帧间差（水面、天空这种均匀画面帧间差本来就小）。
- 720p 那档有时会被「游戏空间」的悬浮界面抢走前台，盲点会点开相册。`gi_enter` 只在游戏有焦点时才点；收尾一律按 HOME。
- `wm size` 覆盖时，`screencap` 返回的是逻辑尺寸，不用再做黑边校正；`input tap/swipe/motionevent` 用的也是逻辑坐标。按物理分辨率算的触摸点到了 1280x720、960x540 会落到屏幕外，角色和镜头都不动（`checks.csv` 的 `motion_med` 整段为 0）。脚本里的输入一律按当前逻辑尺寸算（`gi_capture.py` 已经这样做）。
- 第一次在 Linux 节点上跑 `phonefarm onboard` 会先装 RapidOCR，实测那一次卡了 13 分钟才失败，之后就正常了。赶时间时先单独跑一次 onboard 预热。
- 任务被取消（`pf-node kill`）时，平台先发 SIGINT 再升级；收尾如果没跑完，手机上会留下 wm size 覆盖、knobs 和输出目录。`gi_capture.py` 已经改成收到第一个信号就屏蔽后续信号、跑完收尾；别的脚本被取消后，用 `dataset/phone_state.py <串号> --restore --recover-job <被取消的任务>` 查并还原。下一次采集前 `gi_capture.py` 会检查现场，有残留就拒绝开始。
- 同一个逻辑 pass，游戏会轮换几种 render pass 对象，它们只差颜色附件上没有意义的 stencilLoadOp，sig 却不同。原神的速度 pass 就有 8b4e9a51 和 cb6838c7 两个 sig，TAA 有 14515c1e 和 001baa30。所以 frame-audit 要看 3 帧以上；一个 pass 出现多个 sig、格式又唯一时，规则改用 `fmt<格式>`。注意 `f58` 这种写法会被当成十六进制 sig 前缀，必须写成 `fmt58`。
- 中心拉 GitHub 不稳。现在中心有一份每 5 分钟同步的 experiments 镜像，任务优先用镜像里已有的提交（hgamey #137）。所以推送后等 5 分钟再提交；太早提交会在准备阶段失败，重交即可。`--experiment` 要写完整提交号。`pf-node out` 偶尔返回 409 或 502，重试就好。
- `phonefarm <子命令> --help` 在某些子命令上会真的提交任务，查用法看源码或 README。
- 本机 shell 里不要用 `pkill -f`，会把自己的 shell 也杀掉。

## 每百帧耗时和 token

单台手机（红魔 NX809J，USB 接在中心 ThinkCentre 上），带 UI + 不带 UI + 深度 + 速度图，任务 3ddb34adc483 实测：

| 档 | 采集 | 读回到节点 | 转 PNG 并检查 | 合计 | 成品大小 |
|---|---|---|---|---|---|
| 1920x1080 | 1.7 秒 | 123 秒 | 21 秒 | 约 145 秒 | 1.15 GB |
| 1280x720 | 1.7 秒 | 52 秒 | 9 秒 | 约 63 秒 | 0.51 GB |
| 960x540 | 1.7 秒 | 29 秒 | 5 秒 | 约 36 秒 | 0.29 GB |

- 以上都按每 100 帧算。采集就是 100 帧按 60 帧播放的时长；最慢的是读回：adb 经 USB 约 27–34 MB/s，原始数据每帧 33 MB（1080p，四张 RGBA 大小的图）。
- 另有每档的固定开销：冷启动加进门约 60–90 秒。一个场景三档各 300 帧，整个任务约 17 分钟。
- 单段能多长：代码上限是每段 36000 帧（60 帧下 10 分钟），实际卡在手机写盘速度。读回槽满了就丢这一帧，index.csv 里记成 `DROP_SLOT_BUSY`，下一帧的 `gap_before` 记丢了几帧，游戏本身一直是 60 帧。实测（红魔，带不带 UI、深度、速度图全拷）：

| 档 | 每帧写盘 | 需要的写入速度 | 不丢帧的最长连续段 | 之后 |
|---|---|---|---|---|
| 1920x1080 | 33 MB | 约 2 GB/s | 约 520 帧（8.7 秒） | 写入掉到约 0.5 GB/s，30 秒里只采到 59%（c67b742430a9） |
| 960x540 | 8.3 MB | 约 0.5 GB/s | 13,844 帧（3 分 50 秒） | 累计写了约 115 GB 后开始丢，5 分钟共丢 8%（1831d9c6aa11） |

  每段 300 帧在三档下都是 0 丢帧。要 1080p 更长的序列，就少拷几张附件，或者分成几百帧一段、段间让手机写完再开下一段。开段前层会按「每帧字节 × 帧数 + 4 GB」检查剩余空间，不够就拒绝并写 refused.json。
- token：脚本跑起来以后不耗 token。agent 采一个场景只需要提交一条命令、看 report.json、出接触表、登记，粗估 1 万 token 以内，折合每 100 帧约 1 千 token。换游戏、换版本要重新找 pass 和检验速度图，那一次的花费要大得多。
