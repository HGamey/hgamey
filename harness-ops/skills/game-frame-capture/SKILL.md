---
name: game-frame-capture
description: 在 HGamey 的真机上采游戏的连续序列帧：每帧同时拿到带 UI 的画面、不带 UI 的画面、深度和速度图（motion vector），可选几档分辨率，逐帧检查后登记成数据集。适用于原神这类 Unity Vulkan 手游；换游戏或换手机时按本文先找 pass 再采。
---

# 游戏序列帧采集

以原神 7.2（红魔 NX809J）为例，整套流程已经跑通。换场景、换手机只改参数；换游戏要先做「找 pass」那一步。
脚本都在 HGamey/experiments main 的 `dataset/` 下，平台上用 `pf-node --experiment <experiments main 的完整提交号> experiment dataset/<脚本>` 跑，参数里写的本机文件会自动上传。

## 能拿到什么

| 内容 | 怎么拿 | 原神 7.2 实测 |
|---|---|---|
| 带 UI 的画面 | 平台 framedump（送显时读回） | RGBA8，尺寸见下一节 |
| 不带 UI 的画面 | 灰层 framedump_att 拷 tonemap 输出 | RGBA8；16:9 各档和送显同尺寸，native 下比送显少 1 像素（2140x968 对 2141x969） |
| 深度 | framedump_att 拷 G-buffer 的深度附件 | D24S8，reverse-Z（近处值大） |
| 速度图 | framedump_att 拷 A2R10G10B10 的速度 pass | 见「深度和速度图」 |
| 分辨率 | `wm size` | 1920x1080、1280x720、960x540 精确；2K 拿不到（封顶 1080p） |

## 准备

1. 手机只通过 `pf-node` 用，每次提交都带 `--resource device:<串号>`。`pf-node phones` 看有哪几台、在不在线、有没有 root、前台是什么；它不列已装的应用，游戏装没装看第 2 步。
2. 游戏版本：`pf-node --resource device:<串号> adb -s <串号> shell "dumpsys package <包名> | grep versionName"`。整条命令加引号，管道才在手机上执行；没装就没有输出。`gi_capture.py` 也会把版本记进 report.json。需要更新时在节点上下载官方 APK，`pm install -r`。不用私服、不用改过的 APK。
3. 账号要已经登录。登录、验证码、实名、维护公告都要人来处理：用 `research note --ask` 提出来，同时 ping lead，然后去做别的事。账号密码不写进留言、脚本和日志。
4. 第一次启动会下约 500 MB 资源，要几分钟。先单独跑一次进门，确认能进到世界里，再开始采。
5. 进世界用标准的 `phonefarm onboard`（Linux 节点上用 RapidOCR，第一次运行会自动安装）。onboard 进不去时的备用办法见「已知的坑」。
6. 采之前确认手机上没有别人留下的层、knobs 和 wm size 覆盖。`gi_capture.py` 发现这些会直接退出，不去动它们。`debug.knobs.*` 必须全部为空：值为 `0` 也算残留（`phonefarm gray-layer off` 会把它们设成 `0`），这时先用 `dataset/phone_state.py <串号> --restore` 清空。
7. 层：直接用平台自带的灰层（hgamey main，中心部署时用 NDK 编好；framedump_att 和 960x540、summary.json 的修复都在里面），不传 `--so`。层只在进程名和 `debug.knobs.target` 完全相同的进程里工作，别的进程（包括游戏自己的 `:子进程`）加载到它也是原样转发。会话按 `--pkg` 设这个属性、结束时恢复原值，所以 `--pkg` 要写游戏主进程的名字。只有要试还没合入 main 的灰层改动时，才在 hgamey 里 `REFBENCH_NDK=<NDK 路径> bash phonefarm/knobs/gray/build/build_layer.sh` 编层，用 `--so phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so` 传上去。

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
- 2K（2560x1440）拿不到：`wm size 1440x2560` 时送显仍是 1920x1080。游戏按固定像素预算（约 207 万像素，即 1920×1080）定交换链尺寸，窗口更大就等比缩小：原生 2688x1216 → 2141x969，2560x1440 → 1920x1080，都是 √(预算/窗口像素) 倍。所以让灰层把 surface 尺寸报大也没用，对游戏来说和 wm size 调大是同一件事。游戏内「渲染精度」调到最高会不会抬高这个预算，要等有进度的号能打开设置菜单才能查（新号派蒙菜单没解锁）。
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
pf-node --experiment <提交> --research <研究> --resource device:<串号> --detach --max-runtime-seconds 7200 \
  experiment dataset/gi_capture.py <串号> --scene route_a \
  --att noui=ac24d328:0,depth=fmt129:3,mv=fmt58:0 --tiers 1280x720,1920x1080,1920x1080 \
  --motion nav --circle --circle-dps 110 --seg-watch --fan --use-return \
  --segments 4 --frames 330 --gap-s 15 --heading -1
```

每档的做法：wm size → 设 framedump 属性 → `phonefarm gray-layer session` 冷启动游戏（给了 `--so` 才换成自己的层）→ onboard 进世界 → 按 `--motion` 给输入 → `framedump_go` 开一段（`--segments K` 时同一会话里每段换一个标签，连开 K 段）→ 停会话、停游戏 → 每段 tar 流到节点 → `fd_convert.py` 转 PNG、逐帧检查、量运动 → 删掉手机上的段。
- `--motion nav`（数据集用这个）：`dataset/gi_nav.py` 多指导航，见下一节。段与段之间跑 `--gap-s` 秒修方向、换地方，段内不截图、照跑并左右摆镜头。下一档接着上一档最后的方向跑（冷启动后角色还在原地）。
- `--motion run/turn/static` 是旧的单指输入：run 按住摇杆往 `--angle` 方向跑，turn 原地拖镜头，static 不动。单指做不到边跑边转，跑出来的段多半达不到运动标准，只用来测性能。
- 需求是每段连续 240 帧以上（最好 300 以上），每档总共 600 帧以上。1080p 全附件连续不丢帧只有约 500 帧，所以一档采几段、每段 330 帧：`--segments 5 --frames 330`，剔掉不达标或漏帧的段后，留下 2 段以上就够 600。不要为了凑达标把段切短。1080p 实测只有每次会话的第一段 0 漏帧，后面几段每隔 5–8 帧漏一帧，段间等 12 分钟也一样，像是发热降频；`--fan` 采集期间打开红魔风扇（收尾恢复原值），还不够就把 1080p 档重复几次（`--tiers 1920x1080,1920x1080`，目录记成 `1920x1080_2`），每次会话只指望第一段。`--heading -1` 让导航第一次看大地图时自己挑最开阔的方向。
- `--circle`：段内绕圈跑。整段按住冲刺，镜头匀速转，角色跟着镜头跑出弧线，左右交替。`--circle-dps 110` 按标定增益换成每秒 110° 的拖动（不给是每秒 0.055 个画面宽，约 50°/秒）；圈越小，越少撞上树和岩壁，撞上了也转得开。直线跑 5 秒在崖边、林子里很容易撞墙或爬墙，后半段就停了（11-17 实测：段开头 run_px 8–15，后半段掉到 1–4）。
- 体力：段内要一直冲刺（run_px 冲刺 13–20，普通跑只有 3–5，过不了 4 像素的线）。体力只在不冲刺、不爬墙时回。所以 gap 默认不冲刺（`--gap-sprint 0`），gap 收尾前要连续 8 秒没爬墙/游泳；段内每 2 秒、每次「落下」之后把冲刺键松开再按，被打断的冲刺按着不放是不会自己恢复的。没这几条时，只有会话的第一段（体力满）达标。
- `--use-return`：每段前点左侧的「返回任务点」传回任务点（开场是崖顶草地），原地转 180° 再加 0/±40/±70°（每段换一个），再跑。传回后 12 秒不触发「躲任务点」。偶尔传回时摔一下进「坠落」页，会自动点「复苏」。不传回的话，gap 会越跑越远，跑进岩石夹道（「望风山地 高危险度」），一路都在爬墙。
- 11-17 实测达标率（540p，坠星山谷崖顶，--circle-dps 110 + 上面几条）：4 段中 2 段达标，另 1 段 69%。没过的都是段中途撞树或撞岩壁：HUD 还是正常，seg-watch 认不出。拿小地图做块匹配判「撞停」也试过：转动的视锥会把小地图刷掉一半，跑动和撞停分不开，所以没用。按 50% 达标率排段数：每档 4 段，1080p 分两次会话。
- 天色：nav 默认 `--min-luma 80`，画面中央平均灰度低于 80（夜里 40–65，白天 90–150）就停下等天亮，最多 12 分钟。
- `--pull meta` 只拉元数据和首、中、尾三帧，用来测很长的序列（见「每百帧耗时」）。

产物在 `gi_<场景>_<时间>/<档>/`（分段时 `<档>/s<段>/`）：`ui/ noui/ depth16/ depth24/ mv/` 下的 PNG，`index.csv`（每帧时间戳和间隔），`checks.csv`，`summary.json`，开采前后的截图，`report.json`（尺寸、帧数、完整帧数、每个附件的格式和尺寸、耗时、温度、每段的运动结论、导航记录、收尾回读）。

## 导航（--motion nav）

`input` 只有一个触点，按住摇杆的同时拖镜头会把摇杆顶掉。`gi_nav.Touch` 直接往触摸屏的 evdev 节点写多点触控协议 B 事件：
- 设备从 `getevent -lp` 里找（带 `ABS_MT_POSITION_X` 和 `INPUT_PROP_DIRECT` 的那个；红魔是 `/dev/input/event9`，X 0–12159、Y 0–26879）。
- shell 用户虽在 input 组，SELinux 不让它写，所以经常驻的 `adb shell su -c sh`，每次写一句 `printf '\NNN…' > /dev/input/eventN`。
- 坐标用横屏比例坐标，按 `dumpsys input` 里 `Viewport INTERNAL` 的方向和 `physicalFrame` 换算（wm size 下逻辑画面有黑边，物理框不是整屏）。用高位槽（6–9），不和真手指撞。
- 验证：`settings put system pointer_location 1` 后截图，顶栏应显示 `P: 2/2`；用完还原成 0。

`gi_nav.Navigator` 的做法（原神 7.2 手机 HUD，坠星山谷新号实测）：
- 摇杆一直正推；gap 里默认不冲刺（`--gap-sprint 2.5` 是每 5 秒按住 2.5 秒），段内一直冲刺；转向全靠拖镜头，角色跟着镜头走。摇杆斜推会让镜头慢慢跟到角色身后，人就绕圈。
- 小地图北朝上，青色箭头是角色朝向（箭尖 = 离箭头重心最远的点）。小地图和按钮都是半透明的，白天背后的海和天也是青蓝色：箭头只认饱和纯青（r<80、g>180、b>225），并且只取和圆心连着的那块。
- 拖镜头的增益每次会话开头站着标定：拖 +0.1 / −0.2 / +0.1 个画面宽，每次轻推摇杆 0.5 秒让角色转向镜头，再读箭头。向右拖朝向变大；斜坡、树边的样本会乱，所以符号固定为正，取正样本中位数，夹在 450–1100。
- 段与段之间每 1.2 秒截一次图：
  - 朝向和目标差 15° 以上才修；等上一次拖动做完、角色转稳 0.9 秒后再修，每次只修 70%，不然会来回过冲。
  - 状态看按钮图标，在 24×24 网格上和模板比：攻击键是剑才算正常；剑没了、冲刺键变成波浪是游泳；剑没了、也不是波浪多半在攀爬（自动爬墙），先点攻击键位置（这时是「落下」）。
  - 游泳或攀爬之后往左或右转 100°（交替），那个方向 40 秒内不走。不要掉头 180°：从出生海滩往里走，掉头就回海滩。
  - 小地图里的任务标记（黄菱形）两次截图都不动，就是卡住了：跳一下，再转 70°。离任务标记不到 0.2 个小地图半径且正朝它跑时转开，碰到它会触发剧情对话。
- 每个间隔开头打开一次大地图（点小地图，BACK 关）。地图以角色为中心，可活动区的红色边界偏粉（b>g），任务标记的描边偏橙（b<g）。从中心沿 16 个方向找边界，打分 = 离边界距离 − 偏离偏好方向的角度/600。偏好方向离边界不到 0.12 屏高时，换成最开阔、又不朝来路的方向。
- 段内不截图（截图会打断合成）。不加 `--circle` 时段内自动摆镜头：+a、−2a、+2a…，朝向在均值两侧对称摆（a=0.05 个画面宽，约 ±40°），段末拖回均值。
- 开段前确认在动：隔 0.4 秒两张截图，画面中央平均灰度差小于 6 就再跑、换个方向，最多 10 秒。没这一步时，有的段开头角色还站着。再截一张确认是正常状态（攻击键是剑）：爬墙时画面也在动，只看画面会把爬墙放过去（11-17 有两段从第 30 帧起全程在爬墙）。
- `--seg-watch`：段内每秒截一次图，认出爬墙就点「落下」并转开 120°，游泳也转开；这时绕圈拆成每秒一小段拖动，转开的那一下才插得进来。截图可能让个别帧间隔变长（index.csv 的 dt 里看得到），帧仍是连续的 present。
- 同样比例的拖动，转角按逻辑像素算：1080p 档约 600–800 度/画面宽，540p 档约 300。所以每档会话开头都要重新标定。
- `gi_nav.py <串号> --tier 1280x720 --probe-return`：传回任务点，站着读 6 次朝向，每转 45° 存一张图，开大地图存一张，再直跑 6 秒、每 0.6 秒读一次朝向。用来看传回去的地方能不能跑。
- 先试跑再采：`gi_nav.py <串号> --trial 180 --heading 180` 只跑不采，每轮存一张截图，`nav.json` 里有每次检查的朝向、状态和动作，以及大地图规划。

## 会话前后核对

验收或排查时，在采集前后各跑一次，对比：

```
pf-node --research <研究> --resource device:<串号> adb -s <串号> shell \
  "su -c 'ls -l --full-time /data/tombstones'; echo TARGET=[\$(getprop debug.knobs.target)]; echo LAYERS=[\$(getprop debug.vulkan.layers)]"
```

- 输出别用 `tail` 截短：墓碑按 tombstone_00…31 循环编号，要按文件名加时间比，只看最后几行会把旧文件当成新的。
- 每个新墓碑看是哪个进程：`su -c "grep -m1 ^Cmdline: /data/tombstones/tombstone_<号>"`，再 grep 一下 refknobs，看崩的时候层在不在 maps 里。游戏进程不能有新墓碑；gameassist 和 gamefloat 各一个，属于上一节说的已知现象。
- `debug.knobs.target` 和 `debug.vulkan.layers` 要回到会话前的值（红魔上原值都是空）。

## 逐帧检查

`checks.csv` 每帧一行：
- `gap_before`：和上一帧之间漏掉几个 present，应全为 0。
- 各附件一列：ok / MISSING。每段第 1 帧没有附件，这是正常的：附件挂在开段之后的下一次 present 上。
- `motion_med`：画面中央和上一帧的差值中位数，只能说明画面没冻住；有没有在跑看下面的运动量。
- `noui_vs_ui_med`：不带 UI 和带 UI 的差值，应在 0 到 1。
- `depth_hi_range`：深度高 8 位的范围，全 0 说明没拷到。
- `speed_med / moving_frac / run_px / center_lag30`：每帧的运动量，定义见「运动标准」。
- 附件缺帧：看 summary.json 里每条规则的 `sig_matched` 和 `sigs_seen`。sig_matched 比帧数少，多半是 pass 换了 sig（见「已知的坑」）。

再用 `fd_sheet.py <档目录> --out sheet.png --mv-check 3` 出接触表，看首、中、尾几帧，确认不带 UI 的那张确实没有 UI、深度轮廓和画面对得上、速度图跟着镜头转。

## 运动标准

数据要角色在跑、画面在变。`dataset/fd_motion.py` 用速度图逐帧量（转换时自动做，结论在 `convert.motion`；审计别的任务的数据用 `fd_motion.py <档目录>… --every 2 --json-out @out`，不写进原目录）。位移一律折成 1080p 像素、每 1/60 秒：
- `run_px`：近处地面（画面 y 0.80–0.95）和远处（y 0.10–0.30）竖直位移中位数之差的绝对值（都取 x 0.25–0.75，角色掩码除外）。角色往前跑，脚下地面上下流过、远处几乎不动；原神的镜头绕角色转，左右转镜头只有水平分量，竖直差为 0；原地顶着石头也是 0。
- `speed_med`：位移中位数；`moving_frac`：位移超过 1 像素的点的占比。
- `center_lag30`：画面中央和 0.5 秒前的灰度差中位数，两帧任一帧是角色的点不算。

登记的段还要连续：0 漏帧、完整帧 ≥240（`gi_manifest.py` 自动筛）。一段运动达标要同时满足：`run_px ≥ 4` 的帧占 70% 以上（在跑）；`speed_med ≥ 2` 且 `moving_frac ≥ 0.6` 的帧占 70% 以上，并且 `center_lag30` 的中位数 ≥ 3（在变）。阈值来自 11-13 三个旧数据集的实测：正常跑 `run_px` 10–11；游泳 2.2；慢走、后退 2.4–3.2；只转镜头和顶着礁石都是 0。跑步段 `speed_med` 3–5、`moving_frac` 0.7–1.0；夜里游泳 1.0、0.5。`center_lag30` 受亮度和纹理影响大（跑步段只有 4–10，冻住的 1–2），只用来拦冻住的画面。帧率不是 60 时，位移阈值按实测帧间隔等比折算。不达标的段不登记。
局限：攀爬时近处岩壁也在上下流动，`run_px` 会把爬墙算成在跑。导航会尽量不爬墙；看接触表时发现整段在爬墙的，手动剔掉。

## 登记数据集

每个场景登记一条。`dataset/gi_manifest.py <任务> <report.json> "<场景名>" "<场景说明>" ["<另外的局限>"…] [--prefix gi-route] [--supersedes <旧 result_id>…] > m.json` 生成清单（每段的帧数、完整帧数、漏掉的 present、每百帧耗时、运动量都作为指标指回 report.json，中心登记时逐项核对），再 `pf-node results register m.json`。分段采的只收运动达标的段，没收的段和原因写进 claims；新数据取代旧数据时给 `--supersedes`，旧的不删。清单的样子：

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
- 不要自己设全局 `debug.vulkan.layers`，层只通过 `gray-layer session` 挂到游戏上。会话启动游戏前会短暂设上这个全局属性，游戏加载后收窄，结束时恢复原值（下一条的崩溃就来自这段时间）。
- 红魔的游戏助手（`cn.nubia.gameassist`）和游戏悬浮窗（`cn.zte.gamefloat`）每次会话开头各崩一次（同一秒两个墓碑），属正常。会话启动游戏前要短暂设上全局 `debug.vulkan.layers`，游戏助手跟着游戏一起启动，它是系统应用，库目录里没有层，vkCreateInstance 失败后 hwui 断言（墓碑里是 `Assertion failed: err < 0`，maps 里没有 refknobs）。崩完它可能抢一下前台，所以进门和点击前先确认前台是游戏。不要停用它：它可能管着游戏性能模式，停用会改变性能实验的条件。
- 红魔上 logcat 是空的，排查看层自己写的 summary.json 和 session 日志。
- 采集期间屏幕显示只有约 27 fps（红魔的 SF waitframe 会跳帧），但游戏自己仍按 60 帧 present，序列是连续的。看 `index.csv` 的 `dt_ns`，不要看屏幕帧率。
- 不碰屏幕时原神会降到 30 帧。static 段要隔几秒点一下空白处。
- `input` 只有一个触点：按住摇杆跑的同时再 `input swipe` 拖镜头，会把摇杆顶掉，角色就站住了。要边跑边转用 `gi_nav` 的多指注入（见「导航」）；确认真的在跑看 `run_px`，不要只看帧间差。
- 新号在坠星山谷开场海滩：北面是海，南面是岩壁，往南跑会自动爬墙（攻击键变「落下」，体力圈出现）。进山谷的路口就是派蒙（开场任务「到派蒙附近」），靠近会触发剧情。`gi_nav.py <串号> --story 420` 能推进开场剧情：朝任务标记跑、点对话和「复苏」、到点站住点交互、跑的时候隔一会儿点跳跃（跳崖时能开风之翼），离开任务区域就点左侧的「返回任务点」。它能推进到「鸟瞰风物：跟随派蒙」，跟着派蒙下崖的那段小路跟不住（11-17 实测），要人手动走一段。
- 血少时从崖上摔下会死，出现「坠落 / 复苏」页。Navigator 不在世界里（血条不见了）时会松手，交替点「复苏」和对话区；复苏后角色在最近的锚点。
- 720p 那档有时会被「游戏空间」的悬浮界面抢走前台，盲点会点开相册。`gi_enter` 只在游戏有焦点时才点；收尾一律按 HOME。
- `wm size` 覆盖时，`screencap` 返回的是逻辑尺寸，不用再做黑边校正；`input tap/swipe/motionevent` 用的也是逻辑坐标。按物理分辨率算的触摸点到了 1280x720、960x540 会落到屏幕外，角色和镜头都不动（`checks.csv` 的 `motion_med` 整段为 0）。脚本里的输入一律按当前逻辑尺寸算（`gi_capture.py` 已经这样做）。
- 第一次在 Linux 节点上跑 `phonefarm onboard` 会先装 RapidOCR，实测那一次卡了 13 分钟才失败，之后就正常了。赶时间时先单独跑一次 onboard 预热。
- 中心部署会强制打断正在跑的任务（worker ownership lost，状态 interrupted），收尾不会跑。这时灰层 .so 可能留在游戏库目录里：`phone_state.py --restore --recover-job <任务>` 的 session-recover 找不到会话文件时，用 `phonefarm gray-layer off <包名> --serial <串号> --json`（verified）摘层，再跑一次 `phone_state.py --restore` 清掉它置成 0 的 knobs。已完成的档的数据和 report.json 还在，从没采完的档接着交。
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
