# STATUS — 现状总览

这份文件是唯一的现状总页。其它文档里出现「现状 / 进行中 / 下一步」的段落，一律以这里为准；同事照这份更新 PPT，这里过时了他就没法跟进。

最后核对：2026-09-24（北京时间）

各仓最后核对到的 PR：

| 仓库 | 最后核对的 PR |
|---|---|
| game_opt_loop | [#14](https://github.com/HGamey/game_opt_loop/pull/14) |
| phonefarm（对外 BH3GEI/phonefarm） | [#64](https://github.com/BH3GEI/phonefarm/pull/64) |
| refbench | [#1](https://github.com/HGamey/refbench/pull/1) |
| .github（组织主页） | [#3](https://github.com/HGamey/.github/pull/3) |

---

## 口径

harness 本身就是产品——交付的不是某一个优化算子，而是从一句话任务到一份可核对报告的流水线。

- **game_opt_loop**：入口与决策。接任务、摸底、决定开哪几路、组织大模型出方案、维护优胜档案、出总报告与可视化、给 agent 的 skill。
- **phonefarm**：底座。设备连接（adb / hdc）、应用改动（`knobs/`）、测量、统计判定、现场还原。
- **refbench**：白盒测试程序。确定性 Vulkan 负载，每条优化路线一个开关，验机制。
- **sr_loop**：已结项的前序实验，不在当前主线。

---

## 能力清单

### game_opt_loop（入口与决策）

| 状态 | 能力 | PR |
|---|---|---|
| 已完成 | 本地静态检查：红队语料 16/16 拦下且拦对原因，合规算子 48/48 放行，单次 < 10 ms | [#1](https://github.com/HGamey/game_opt_loop/pull/1) |
| 已完成 | 评测请求/报告契约扩到 v2（三类改动面走同一个信封，含停充、风扇、游戏内帧率设置等条件字段） | [#6](https://github.com/HGamey/game_opt_loop/pull/6) |
| 已完成 | 画质真值改用真机参考帧（不再用程序化合成图案） | [#2](https://github.com/HGamey/game_opt_loop/pull/2)、[#3](https://github.com/HGamey/game_opt_loop/pull/3) |
| 已完成 | `optimize` 总入口：摸底（只读）→ 按能力开路 → 排计划 → 出方案 → 统一档案 + 总报告；`--plan` 只读跑通 | [#8](https://github.com/HGamey/game_opt_loop/pull/8)、[#14](https://github.com/HGamey/game_opt_loop/pull/14) |
| 已完成 | 摸底修正：SoC 结温只认 `cpu-` / `cpullc` / `gpuss` 前缀，停充节点存在性在 `su` 里测 | [#12](https://github.com/HGamey/game_opt_loop/pull/12) |
| 已完成 | 可视化面板（只读、离线）：总览 / 轮次 / 图 / 手机四页 | [#10](https://github.com/HGamey/game_opt_loop/pull/10)、[#11](https://github.com/HGamey/game_opt_loop/pull/11)、[#13](https://github.com/HGamey/game_opt_loop/pull/13) |
| 已完成 | agent skill 说明书（按全新会话实跑结果补齐） | [#14](https://github.com/HGamey/game_opt_loop/pull/14) |
| 进行中 | ②③ 两层（画面流程改写、系统参数）的真机评测：计划会生成并下发，接收端 `phonefarm eval --request` 已落地（离线验证，真机未跑），真机评测待跑 | [#8](https://github.com/HGamey/game_opt_loop/pull/8)、[#61](https://github.com/BH3GEI/phonefarm/pull/61) |
| 进行中 | 多代自主进化：本地闭环能跑多代，真机目前最多 2 代 | — |
| 计划 | 设备锁自动接入：`--plan` 的摸底现在靠 skill 里手工 `devlock run` 包一层，代码自己还没接锁 | — |
| 计划 | 停充带到真机请求：v2 计划里已经填了停充条件，但 ① 画面算子的真机路现在还降级成 v1 `gpu-op`（不带停充），②③ 的接收端已落地、真机评测还没跑 | — |

### phonefarm（底座）

| 状态 | 能力 | PR |
|---|---|---|
| 已完成 | 停充测量：测功耗期间 root 停充，供电状态进报告并参与判定 | [#27](https://github.com/BH3GEI/phonefarm/pull/27) |
| 已完成 | 性能数据来源抽象：安卓 sysfs + 鸿蒙 HiSmartPerf，上层拿同一份 JSON | [#18](https://github.com/BH3GEI/phonefarm/pull/18)、[#20](https://github.com/BH3GEI/phonefarm/pull/20) |
| 已完成 | loop_v1 最小可信闭环（五条判据实测通过） | [#24](https://github.com/BH3GEI/phonefarm/pull/24) |
| 已完成 | 灰档只读层 + LoadOp 改写 + passdump 描述符溯源（原 HGamey/knobs 并入） | [#31](https://github.com/BH3GEI/phonefarm/pull/31)、[#42](https://github.com/BH3GEI/phonefarm/pull/42)、[#47](https://github.com/BH3GEI/phonefarm/pull/47)、[#49](https://github.com/BH3GEI/phonefarm/pull/49) |
| 已完成 | 灰档超分算子可行性（只读观测确认可行：放大 = 帧内最后一个 pass 的第一笔 draw，采样图可读） | [#47](https://github.com/BH3GEI/phonefarm/pull/47) |
| 已完成 | 灰档超分机制全链路预演（copyprobe 1:1 拷贝探针）：反作弊放行、帧时无损、画面构造性逐位不变 | [#51](https://github.com/BH3GEI/phonefarm/pull/51) |
| 已完成 | 灰档真超分算子投原神大世界（gen1_loc3，2×2 bilinear）：替换「渲染精度低」档下游戏自带放大，5 对 A/B 无指标显著变差（零成本通过），画质比游戏自带放大接近原生 3–4 倍 | [#64](https://github.com/BH3GEI/phonefarm/pull/64) |
| 已完成 | 农场只读快照 `fleet --json` | [#43](https://github.com/BH3GEI/phonefarm/pull/43) |
| 已完成 | 系统参数自动调参在真原神上跑通（两组候选走完统计判定，均被淘汰且有道理） | [#52](https://github.com/BH3GEI/phonefarm/pull/52) |
| 已完成 | 评测统一入口 `phonefarm eval --request`：v1 原样转交 gpu-op，v2 按 kind 路由 shader / sysparam / gray；离线 15 项单测 + 冒烟过，真机路径尚未验证 | [#61](https://github.com/BH3GEI/phonefarm/pull/61) |
| 进行中 | loop_v1 往 Rust 内核收：判定口径（解析→归因→统计→判据收口→白名单→判定→画面判据→模型交互/本地变异器）、两通路对照、Vulkan-Samples 三道闸与两臂判读面、refbench/vks 报告已收完，剩 autoloop.py（编排/设备驱动）、载体构建/路线工具（make_route_a.py / shadercompiler_shim.py）、设备端 shell、knobs/ 那批 | [#39](https://github.com/BH3GEI/phonefarm/pull/39)、[#45](https://github.com/BH3GEI/phonefarm/pull/45)、[#48](https://github.com/BH3GEI/phonefarm/pull/48)、[#50](https://github.com/BH3GEI/phonefarm/pull/50)、[#54](https://github.com/BH3GEI/phonefarm/pull/54)、[#55](https://github.com/BH3GEI/phonefarm/pull/55)、[#57](https://github.com/BH3GEI/phonefarm/pull/57)、[#58](https://github.com/BH3GEI/phonefarm/pull/58)、[#59](https://github.com/BH3GEI/phonefarm/pull/59)、[#62](https://github.com/BH3GEI/phonefarm/pull/62) |
| 计划 | ③ 系统参数白名单的接收端真机探测（`--sysparam-probe`） | — |
| 计划 | 灰档超分算子迭代：2×2 bilinear 只是占位优胜者，换 game_opt_loop 后续赛道更强的算子（同 candidate.spv 契约换装） | — |

### refbench（白盒靶子）

| 状态 | 能力 | PR |
|---|---|---|
| 已完成 | M0：六判据全绿的 report.json + 验收证据 | — |
| 已完成 | `sr_pipeline` 场景 + compute 后处理，支持外部动态装入 candidate.spv | — |
| 已完成 | 按新口径改写「在整体里的位置」 | [#1](https://github.com/HGamey/refbench/pull/1) |

---

## 已验证的数字（只抄证据里的原数，看数字先看条件）

### 画面算子画质闭环（game_opt_loop M2）

条件：NX809J，原神，19 张实拍帧当画质真值；`--no-llm` 本地确定性变异器（固定种子可复现）；冷机门槛放宽到 42 °C；设备插 USB 充电，功耗不作判据。

- 赛道基线模板逐帧 29.67–35.68 dB，均值 34.521 dB。
- 前沿留下 `gen0_baseline 0.264 ms / 34.521 dB` 与 `gen1_loc3 0.479 ms / 37.796 dB`——多花 0.22 ms 换 +3.3 dB。

### 系统参数自动调参（phonefarm loop_v1，原神 60 fps）

条件：NX809J / Android 16 / Adreno 840v2，root；原神 7.1.0 大世界探索态；触控拖拽负载（自检画面变化 81.8% / 85.6%，门槛 20%）；内置风扇开（`fan_enable=1, fan_speed_level=5`）；测量期间停充、全程放电态；每组 5 对 A/B ABBA 交替，精确置换检验全枚举 252 种，`alpha_win = 0.05/3 = 0.0167`，最小可达 p = 0.0079；温度上限 70 °C，实测峰值 63.7 °C。

重测基线：`fps_mean` 59.1–59.4、`frame_p50` 16.65 ms、`frame_p95` 19.3–19.6 ms、整机功耗 4.76 W、SoC 55.2 °C。

| 参数 | frame_p95 | fps_mean | power | 判定 |
|---|---|---|---|---|
| `bus.DDR.boost_freq=5333000` + `bus.LLCC.boost_freq=1350000` | −2.93%（p=0.135） | +0.28%（p=0.262） | +16.5%（p=0.0079） | REJECT：功耗显著变差 |
| `gpu.min_pwrlevel` 17→12 | +2.68%（p=0.079） | −0.571%（p=0.0159） | −2.56%（p=0.429） | REJECT：帧率显著变差 |

### 灰档 LoadOp 改写（phonefarm knobs，原神大世界）

条件：原神 7.1.0，5 对交替 A/B，精确置换检验。

- LoadOp `LOAD→DONT_CARE` 改写命中 4.4–5.2 万次（确实绑上了），主指标 `frame_p95` 差值 −0.51%，置换检验 p = 0.325，判无效。

### 灰档 copyprobe 1:1 拷贝探针（phonefarm knobs，原神大世界）

条件：原神 7.1.0，5 个探针臂（两批）跑满 36 s 转视角负载，`debug.knobs.copyprobe=1`；算子是 texelFetch 逐纹素 1:1 拷贝（无过滤无舍入），画面应逐像素不变。

- 反作弊放行：5 臂全过，层自报 copies ≈ frames（4791–5392），从未停用。
- 帧时无损：探针臂 `frame_p50` 16.636–16.674 ms，与无层臂（16.661 / 16.680 ms）差 < 0.05 ms，顶满 60 帧档；每帧一次 2140×968 拷贝 + 一次克隆替换的开销淹没在噪声里。

### 灰档真超分算子 upop A/B（phonefarm knobs，原神大世界）

条件：原神 7.1.0 大世界站定 36 s 转视角负载（`workload_spin_touch_v1`）；渲染精度档位「低」（渲染 1134×514、送显 2141×969）；A 臂 = 挂层只读 passdump + 游戏自带放大，B 臂 = 挂层 upop（我们的算子，2×2 bilinear，imageLoad 改写为数学逐位等价的 texelFetch）；5 对交替 A/B，loop_v1 冻结口径（5v5 精确置换 252 枚举，α_win=0.0167 Bonferroni/3，α_lose=0.05）；帧时 + 停充功耗（battery rail）+ 电池温度。

| 指标 | A（游戏放大） | B（我们的算子） | Δ | p_lose |
|---|---|---|---|---|
| frame_p95 | 19.661 ms | 19.624 ms | −0.19% | 0.571 |
| fps_mean | 59.178 | 59.269 | +0.15% | 0.881 |
| power_watt | 2.789 W | 2.903 W | +4.07% | 0.278 |

无指标显著变差（p_lose 全 ≥ 0.28）→ 零成本通过；补跑对电池温度 37→37 °C 平直。原 a5 臂误采加载画面作废，rerun 5 对补跑（表内已用补跑值）。

画质（同位置 · 同传送视角 · 同时辰 ~11:57–11:59 正午，璃月港，UID/角色/传送水晶排除）：逐像素 MAD 我们的算子 8.3–18.7 vs 游戏自带放大 26.9–42.8，所有测区接近原生（极高）3–4 倍。

### 停充之后的功耗对照（phonefarm）

条件：原神固定场景两通路并排采 30 秒、全窗口停充。

- 整机功耗 HiSmartPerf 报 4.867 W，本仓库报 4.979 W，相对差 2.3%。

### 载体的可重复性基线（frame_p95 离散度，越小越好）

refbench 0.45%，Megacity 0.987%，原神 1.37%（跨黄昏那一批 5.35%）。

---

## 已撤回 / 作废的结论

- **M1 功耗「6.26 W → 5.94 W」已撤回**。采样时设备插 USB 充电，量到的是 USB 输入轨，其中约 46% 在给电池充电并随电量漂移，跨候选比较不成立。现在功耗只在放电态（`power_source.on_battery == true`）下进入 Pareto 支配排序。
- **旧 DDR 下限结论（p95 −1.61%，p=0.0079）已不可比**。那是 30 fps 时代测的，而且当时根本没量功耗。60 fps 重测后同一条改动是净亏（多烧 16.5% 电，帧时改善不显著）。
- **「原神被厂商限帧 30fps」需要加注**。那是客户端 7.0.0、当时那套游戏设置下的观测；7.1.0、游戏内 60 帧设置下同一台机器实测就是 60 fps。两次差异的原因还没核实，不再当定论引用。

---

## 变更记录

- **2026-09-24（第五次核对）**：核对到 phonefarm #63（docs 同步「等 eval --request」→已落地，无新状态）、#64（真超分算子投原神）。#64 把 game_opt_loop 真实帧优胜算子 gen1_loc3（2×2 bilinear）按 candidate.spv 契约装进灰档层，替换「渲染精度低」档下游戏自带放大，5 对 A/B 无指标显著变差（p95 −0.19%、fps +0.15%、功耗 +4.07% 但 p_lose=0.28 属噪声），零成本通过；画质 MAD 比游戏自带放大接近原生 3–4 倍（8.3–18.7 vs 26.9–42.8）。「灰档真正投算子」从计划划为已完成，后续计划改为算子迭代。同步 knobs/README「下一步」里的分辨率候选说法。
- **2026-09-24（第四次核对）**：核对到 phonefarm #61、#62。新增「评测统一入口 `phonefarm eval --request`」这一项已完成能力（离线 15 项单测 + 冒烟过，真机路径尚未验证）——v1 原样转交 gpu-op，v2 按 kind 路由 shader / sysparam / gray，顺手把 brain.rs 密钥从 argv 撤到 `-K` 配置文件。#62 把 refbench 的 `RULES` 与 vks 的 `KNOBS`/`METRIC_MEANING` 抽成冻结的 `rules.json`，判读哈希随之切到这份文件的 sha256（切换点之前的归档哈希对应旧 .py，已记录在案，非回放损坏）。同步 STATUS 里的②③ 接收端说法、game_opt_loop README / DESIGN、phonefarm AGENTS、组织主页四处「等 eval --request」。
- **2026-09-24（第三次核对）**：核对到 phonefarm #57、#58、#59。loop_v1 的 Vulkan-Samples 三道闸与两臂判读面、refbench/vks 报告也收进 Rust，「报告类」从「还没收」里划掉；剩 autoloop.py（编排/设备驱动）、载体构建/路线工具（make_route_a.py / shadercompiler_shim.py）、设备端 shell、knobs/ 那批。#58 顺带修好 #45 引入的回放哈希断裂（归档证据重新可重放）。同步 README / AGENTS / loop_v1 README / 组织主页四处「进行中」说法。
- **2026-09-24（第二次核对）**：核对到 phonefarm #48、#50、#54、#51、#55。更新 loop_v1 收进 Rust 的进度（判定口径与两通路对照已收，剩 autoloop.py / 报告类 / 设备端 shell）；补上灰档 copyprobe 探针的已完成结论与帧时数字；同步五处其它文档里的「Rust 化」说法。
- **2026-09-24**：首次建立本页。核对到 game_opt_loop #14、phonefarm #52、refbench #1、.github #3；把四处过时的「现状 / 进行中 / 下一步」段落同步到本页口径（详见各仓当次提交）。
