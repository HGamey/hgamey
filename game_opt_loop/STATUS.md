# STATUS — 现状总览

这份文件是唯一的现状总页。其它文档里出现「现状 / 进行中 / 下一步」的段落，一律以这里为准；同事照这份更新 PPT，这里过时了他就没法跟进。

最后核对：2026-10-11（系统交接审计；新增功能仍以实际合入与对应验收为准）

各仓最后核对到的 PR：

| 仓库 | 最后核对的 PR |
|---|---|
| game_opt_loop | [#24](https://github.com/HGamey/game_opt_loop/pull/24)，审计基线 `d0807b1` |
| phonefarm | HGamey/main `e3b7c35`（[#6](https://github.com/HGamey/phonefarm/pull/6)）；上游核心状态核对到 [#73](https://github.com/BH3GEI/phonefarm/pull/73) |
| refbench | [#1](https://github.com/HGamey/refbench/pull/1) |
| .github（组织主页） | `4ad34ae`；会议 [0917](https://github.com/orgs/HGamey/discussions/1) / [0924](https://github.com/orgs/HGamey/discussions/10) |

---

## 口径

harness 本身就是产品——交付的不是某一个优化算子，而是从一句话任务到一份可核对报告的流水线。

- **game_opt_loop**：入口与决策。接任务、摸底、决定开哪几路、组织大模型出方案、维护优胜档案、出总报告与可视化、给 agent 的 skill。
- **phonefarm**：底座。设备连接（adb / hdc）、应用改动（`knobs/`）、测量、统计判定、现场还原。
- **refbench**：白盒测试程序。确定性 Vulkan 负载，每条优化路线一个开关，验机制。
- **sr_loop**：已结项的前序实验，不在当前主线。

---

## 系统交付差距（2026-10-11核对）

会议要求的目标是媒体算法寻优全流程，当前已经有可运行组件和实际案例，仍有以下结构断点。以下按 `d0807b1` / `e3b7c35` 源码核对；开放 issue 或 draft PR 不算主线完成。

| 目标 | 当前实际边界 | 追踪 |
|---|---|---|
| 自主 SR 进入黑盒游戏 | 独立 Shader 入口在工装演化；optimize 只驱动系统参数和有限 LoadOp；原神固定 upop 是另一路人工实验，动态候选未统一 | [系统 #14](https://github.com/HGamey/.github/issues/14) |
| 专家每代方向筛选与恢复 | 没有两入口共用的风险/收益/成本决定与 campaign checkpoint | [本仓 #25](https://github.com/HGamey/game_opt_loop/issues/25) |
| 完整实验/结论资格 | KGSL 提交间隔分组是代理节奏，不是 present/display 测量；缺必需指标、冷热/恢复失败需统一阻断 | [系统 #12](https://github.com/HGamey/.github/issues/12) |
| 连续帧质量 | 静态 PSNR/MAD 与单图平移+合成 HUD 机制已有；真实连续色调/鬼影门禁未完成 | [系统 #13](https://github.com/HGamey/.github/issues/13) |
| 真正 LR/HR/插帧配对 | SR 的 LR 仍由 HR 降采样；插帧输入仍为单图平移；不能作为原生渲染/运动配对证明 | [系统 #15](https://github.com/HGamey/.github/issues/15) |
| 设计/训练/量化/部署/调优 | 主线候选是 GLSL；sr_loop 历史 checkout 没有可直接部署的权重/TFLite/配对数组 | [系统 #16](https://github.com/HGamey/.github/issues/16) |
| 实际条件与自动租约 | Shader 发送/接收降级会丢失部分 v2 条件；requested 不能充当 observed；锁仍依赖手工 | [系统 #17](https://github.com/HGamey/.github/issues/17) |
| 独立确认与零成本声明 | 已有探索案例；不显著不能证明非劣效，最终案例还需与探索分开的确认数据 | [系统 #18](https://github.com/HGamey/.github/issues/18) |

原神灰层能力仅在已有设备/客户端/管线条件下有证据；一加 Ace 5 接回后需要重新验证资格、挂点、命中、持续控制、供电和恢复。国内模型通道已实现，但跨模型同预算效果/成本尚未验证。完整交接与迭代顺序见 [docs 交接计划 PR](https://github.com/HGamey/docs/pull/5)，总追踪 [系统 #11](https://github.com/HGamey/.github/issues/11)。

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
| 已完成 | ②③ 两层（画面流程改写、系统参数）的真机评测：③ 系统参数已端到端出报告（#66 真机 5v5 首候选 REJECT p=0.3143），② 灰档机械链路已通、命中口径已对齐（#69：hits 改算 effective[].begins 之和，真机实测命中 27 处 / begins 9195，登录页如实 ABORT） | [#8](https://github.com/HGamey/game_opt_loop/pull/8)、[#61](https://github.com/BH3GEI/phonefarm/pull/61)、[#66](https://github.com/BH3GEI/phonefarm/pull/66)、[#69](https://github.com/BH3GEI/phonefarm/pull/69) |
| 进行中 | 多代自主进化：本地闭环能跑多代，真机目前最多 2 代 | — |
| 计划 | 设备锁自动接入：`--plan` 的摸底现在靠 skill 里手工 `devlock run` 包一层，代码自己还没接锁 | — |
| 计划 | 停充带到真机请求：v2 计划里已填停充条件，eval 路（②③）已按契约接停充（#66 实际停充、节点记进 power_source.charging_suspended_via），① 画面算子的真机路现在还降级成 v1 `gpu-op`（不带停充） | — |

### phonefarm（底座）

| 状态 | 能力 | PR |
|---|---|---|
| 已完成 | 停充测量：测功耗期间 root 停充，供电状态进报告并参与判定 | [#27](https://github.com/BH3GEI/phonefarm/pull/27) |
| 已完成 | 性能数据来源抽象：安卓 sysfs + 鸿蒙 HiSmartPerf，上层拿同一份 JSON | [#18](https://github.com/BH3GEI/phonefarm/pull/18)、[#20](https://github.com/BH3GEI/phonefarm/pull/20) |
| 已完成 | loop_v1 最小可信闭环（五条判据实测通过） | [#24](https://github.com/BH3GEI/phonefarm/pull/24) |
| 已完成 | 灰档只读层 + LoadOp 改写 + passdump 描述符溯源（原 HGamey/knobs 并入） | [#31](https://github.com/BH3GEI/phonefarm/pull/31)、[#42](https://github.com/BH3GEI/phonefarm/pull/42)、[#47](https://github.com/BH3GEI/phonefarm/pull/47)、[#49](https://github.com/BH3GEI/phonefarm/pull/49) |
| 已完成 | 灰档超分算子可行性（只读观测确认可行：放大 = 帧内最后一个 pass 的第一笔 draw，采样图可读） | [#47](https://github.com/BH3GEI/phonefarm/pull/47) |
| 已完成 | 灰档超分机制全链路预演（copyprobe 1:1 拷贝探针）：已测客户端下运行未被阻断；采样未检出帧时差异；copy 算法构造上逐纹素等价（非通用反作弊或零成本保证） | [#51](https://github.com/BH3GEI/phonefarm/pull/51) |
| 已完成 | 灰档真超分算子投原神大世界（gen1_loc3，2×2 bilinear）：替换「渲染精度低」档下游戏自带放大，5 对 A/B 无指标显著变差；裁决 INCONCLUSIVE，未证明零成本/非劣效；静态局部截图 MAD 比游戏自带放大更接近极高参考约 3–4 倍 | [#64](https://github.com/BH3GEI/phonefarm/pull/64) |
| 已完成 | 农场只读快照 `fleet --json` | [#43](https://github.com/BH3GEI/phonefarm/pull/43) |
| 已完成 | 系统参数自动调参在真原神上跑通（两组候选走完统计判定，均被淘汰且有道理） | [#52](https://github.com/BH3GEI/phonefarm/pull/52) |
| 已完成 | 评测统一入口 `phonefarm eval --request`：v1 原样转交 gpu-op，v2 按 kind 路由 shader / sysparam / gray；离线 15 项单测 + 冒烟过，#66 对齐 optimize 驱动三处（serial 进请求体、workload 相对路径、停充按契约执行）并在原神真机验证通过 | [#61](https://github.com/BH3GEI/phonefarm/pull/61)、[#66](https://github.com/BH3GEI/phonefarm/pull/66) |
| 进行中 | loop_v1 往 Rust 内核收：判定口径（解析→归因→统计→判据收口→白名单→判定→画面判据→模型交互/本地变异器）、两通路对照、Vulkan-Samples 三道闸与两臂判读面、refbench/vks 报告、autoloop 编排（`phonefarm autoloop`）、灰档主机侧编排（`phonefarm gray-layer` / `gray-ab` / `shot-diff`）已收完，剩载体构建/路线工具（build_vks / make_route_a / shadercompiler_shim 等）、设备端 shell（按约定保持设备端）、`knobs/` 超分那批实验脚本与 test_loadop.sh（refbench 三臂对照配方） | [#39](https://github.com/BH3GEI/phonefarm/pull/39)、[#45](https://github.com/BH3GEI/phonefarm/pull/45)、[#48](https://github.com/BH3GEI/phonefarm/pull/48)、[#50](https://github.com/BH3GEI/phonefarm/pull/50)、[#54](https://github.com/BH3GEI/phonefarm/pull/54)、[#55](https://github.com/BH3GEI/phonefarm/pull/55)、[#57](https://github.com/BH3GEI/phonefarm/pull/57)、[#58](https://github.com/BH3GEI/phonefarm/pull/58)、[#59](https://github.com/BH3GEI/phonefarm/pull/59)、[#62](https://github.com/BH3GEI/phonefarm/pull/62)、[#71](https://github.com/BH3GEI/phonefarm/pull/71)、[#73](https://github.com/BH3GEI/phonefarm/pull/73) |
| 已完成 | ③ 系统参数白名单接收端真机探测（`--sysparam-probe`）：probe_only 白名单 11 项、探测零残留、status=REVERTED | [#66](https://github.com/BH3GEI/phonefarm/pull/66) |
| 已完成 | ② 灰档（loadop）命中口径对齐：hits 从 effective 数组长度改为 effective[].begins 之和（改写了但从未绑定的 pass 不算命中），每个 knob 臂跑完立刻采样取最大；真机实测原神 loadop 命中 27 处改写、begins 总和 9195，登录页静止如实判 0 ABORT | [#69](https://github.com/BH3GEI/phonefarm/pull/69) |
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

条件：NX809J；性能载体是 refbench，画质在 headless runner 用 19 张原神静态截帧另测；`--no-llm` 本地确定性变异器（固定种子可复现）；冷机门槛放宽到 42 °C；设备插 USB 充电，功耗不作判据。

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
- 已观测帧时差异很小（不等于证明非劣效）：探针臂 `frame_p50` 16.636–16.674 ms，与无层臂（16.661 / 16.680 ms）差 < 0.05 ms，顶满 60 帧档；每帧一次 2140×968 拷贝 + 一次克隆替换的开销淹没在噪声里。

### 灰档真超分算子 upop A/B（phonefarm knobs，原神大世界）

条件：原神 7.1.0 大世界站定 36 s 转视角负载（`workload_spin_touch_v1`）；渲染精度档位「低」（渲染 1134×514、送显 2141×969）；A 臂 = 挂层只读 passdump + 游戏自带放大，B 臂 = 挂层 upop（我们的算子，2×2 bilinear，imageLoad 改写为数学逐位等价的 texelFetch）；5 对交替 A/B，loop_v1 冻结口径（5v5 精确置换 252 枚举，α_win=0.0167 Bonferroni/3，α_lose=0.05）；帧时 + 停充功耗（battery rail）+ 电池温度。

| 指标 | A（游戏放大） | B（我们的算子） | Δ | p_lose |
|---|---|---|---|---|
| frame_p95 | 19.661 ms | 19.624 ms | −0.19% | 0.571 |
| fps_mean | 59.178 | 59.269 | +0.15% | 0.881 |
| power_watt | 2.789 W | 2.903 W | +4.07% | 0.278 |

无指标显著变差（p_lose 全 ≥ 0.28）→ `INCONCLUSIVE(无显著变差)`；没有预冻结非劣效界及相应区间判据，不能据此证明零成本。前四对温度缺测，补跑对电池温度 37→37 °C 平直。原 a5 臂误采加载画面作废，rerun 5 对补跑（表内已用补跑值）。

画质（同位置 · 同传送视角 · 同时辰 ~11:57–11:59 正午，璃月港，UID/角色/传送水晶排除）：逐像素 MAD 我们的算子 8.3–18.7 vs 游戏自带放大 26.9–42.8，这些静态局部测区的 MAD 更接近极高参考约 3–4 倍；跨 run 截图不是连续色调/鬼影或同帧 LR/HR 配对证据。

### 停充之后的功耗对照（phonefarm）

条件：原神固定场景两通路并排采 30 秒、全窗口停充。

- 整机功耗 HiSmartPerf 报 4.867 W，本仓库报 4.979 W，相对差 2.3%。

### 灰档命中口径（phonefarm eval gray，原神）

条件：devlock / rust-port 真机，预热游戏后。

- 原神 loadop 命中 27 处改写，begins 总和 9195，`require_hit` 正确放行。
- 游戏停在登录页（30 s 窗口仅 34 次 ftrace 提交）时如实判 0 并 ABORT——两个方向都符合契约。

### 载体的可重复性基线（frame_p95 离散度，越小越好）

refbench 0.45%，Megacity 0.987%，原神 1.37%（跨黄昏那一批 5.35%）。

---

## 已撤回 / 作废的结论

- **M1 功耗「6.26 W → 5.94 W」已撤回**。采样时设备插 USB 充电，量到的是 USB 输入轨，其中约 46% 在给电池充电并随电量漂移，跨候选比较不成立。现在功耗只在放电态（`power_source.on_battery == true`）下进入 Pareto 支配排序。
- **旧 DDR 下限结论（p95 −1.61%，p=0.0079）已不可比**。那是 30 fps 时代测的，而且当时根本没量功耗。60 fps 重测后同一条改动是净亏（多烧 16.5% 电，帧时改善不显著）。
- **「原神被厂商限帧 30fps」需要加注**。那是客户端 7.0.0、当时那套游戏设置下的观测；7.1.0、游戏内 60 帧设置下同一台机器实测就是 60 fps。两次差异的原因还没核实，不再当定论引用。

---

## 变更记录

- **2026-10-11（系统交接审计）**：按远端源码和两次会议核对业务交付，补结构断点与验收追踪。限定 M2 为 refbench 性能+静态原神帧画质；原神 upop 保留原数和 INCONCLUSIVE，撤回“零成本通过/功耗差属于噪声”的推论；copyprobe 未检出差异不能证明非劣效。下方历史记录保留为当时表述，旧零成本口径由本条及当前正文取代。未进行本轮真机或付费 API 测试。

- **2026-09-24（第八次核对）**：核对到 phonefarm #73（灰档主机侧编排搬进 Rust）。#73 把 `knobs/gray/` 的主机侧编排收成三个子命令：`phonefarm gray-layer`（← enable_layer.sh，probe / loadop / passdump / copyprobe / upop / target / status / off 全模式，挂层每步回读核验、off 兜底清扫）、`phonefarm gray-ab`（← genshin_loadop_ab.sh + genshin_copyprobe_ab.sh，loadop / copyprobe 两模式）、`phonefarm shot-diff`（← compare_shots.py，probe 与无层差异对照控制对基线而非字面 0）；删掉 compare_shots.py、genshin_loadop_ab.sh、genshin_copyprobe_ab.sh。顺手修 A/B 方法论：eval gray 的对照臂从「摘层」改成「两臂都挂层只切改写属性」，层本身的开销消掉后差值才干净对应改写这一个改动；eval gray 同步切到 GrayLayer（Rust）。真机（devlock/rust-port）gray-layer status / off / probe 走通，`cargo test` 504 项全过、clippy 零警告。留守 test_loadop.sh（refbench 三臂对照配方，依赖白盒靶子 knob.loadop，与 gray-ab 黑盒路线不同）、genshin_quality.sh / genshin_upop_ab.sh（超分那批）、载体构建/路线工具。把「loop_v1 往 Rust 内核收」里的灰档主机侧编排从剩余项划掉；同步 STATUS、game_opt_loop README、phonefarm AGENTS、loop_v1 README、knobs README 五处「剩 knobs/ 那批」说法（phonefarm 总 README 已在 #74 内同步、组织主页已由 lead 直接提交同步，无需再改）。
- **2026-09-24（第七次核对）**：核对到 phonefarm #71（autoloop 编排搬进 Rust）。#71 把 `loop_v1/auto/autoloop.py`（829 行）迁成 `src/autoloop.rs`，对外 `phonefarm autoloop`，`loop_v1/auto/` 只剩设备端 shell 与一份薄测试；判定口径零复制，复用 `src/sysparam.rs` / `src/llm.rs`，`cargo test` 500 项全过、clippy 零警告，autoloop.py 删除。至此 loop_v1 判定链（parse-trace / attribute / analyze / report / frames-moving / crosscheck-report / vks-* / eval / autoloop）全部在 Rust 内核。把「loop_v1 往 Rust 内核收」里的 autoloop 编排从剩余项划掉，剩载体构建/路线工具、设备端 shell、knobs/ 那批。真机 `--probe-only` + 一代闭环按 devlock 排队补跑（lead 指示后执行）。同步 STATUS、game_opt_loop README、phonefarm AGENTS 三处「剩 autoloop.py」说法（phonefarm README / loop_v1 README 已在 #71 内同步、组织主页已由 lead 直接提交同步，无需再改）。
- **2026-09-24（第六次核对）**：核对到 phonefarm #69（gray 命中口径对齐）。#69 修掉 eval gray 的两个问题：命中采样时机（原来在 ABBA 循环外读一次 marker，被 mount_layer 每次重写落点坑成空表，现改为每个 knob 臂跑完立刻采、各臂取最大）和命中口径（原来用 effective 数组长度，把「改写了但从未绑定」的 pass 也算了进去，现改为 effective[].begins 之和，readonly 探针退回 render_pass_begins）；顺手修 gray 初始挂层的 pkg 参数漏传。真机验证：原神 loadop 命中 27 处改写、begins 总和 9195，登录页静止如实判 0 ABORT。「② 画面流程改写命中口径」从待对齐划为已对齐；同步 STATUS ②③/命中口径、game_opt_loop README/DESIGN、phonefarm AGENTS、组织主页四处「命中计数待对齐」说法。
- **2026-09-24（第五次核对）**：核对到 phonefarm #63（docs 同步「等 eval --request」→已落地，无新状态）、#64（真超分算子投原神）、#66（eval 对齐 optimize 驱动三处 + 真机验证）。#64 把 game_opt_loop 真实帧优胜算子 gen1_loc3（2×2 bilinear）按 candidate.spv 契约装进灰档层，替换「渲染精度低」档下游戏自带放大，5 对 A/B 无指标显著变差（p95 −0.19%、fps +0.15%、功耗 +4.07% 但 p_lose=0.28 属噪声），零成本通过；画质 MAD 比游戏自带放大接近原生 3–4 倍（8.3–18.7 vs 26.9–42.8）；「灰档真正投算子」从计划划为已完成，后续计划改为算子迭代。#66 把 eval 的 serial（请求体兜底）、workload 相对路径、停充（charging_suspend 按契约执行）三处对齐 game_opt_loop 驱动，并在原神 NX809J 真机验证：③ 系统参数端到端出报告（optimize --budget 30m 走完 1 候选，5v5 REJECT p=0.3143）、② 灰档机械链路全通但命中计数恒 0 待对齐、③ 白名单探测 11 项零残留。「③ 白名单探测」从计划划为已完成；同步 STATUS 的②③/停充/评测入口/白名单探测四项，以及 game_opt_loop README/DESIGN、phonefarm AGENTS、组织主页的「真机未跑/还没跑通」说法。同步 knobs/README「下一步」里的分辨率候选说法。
- **2026-09-24（第四次核对）**：核对到 phonefarm #61、#62。新增「评测统一入口 `phonefarm eval --request`」这一项已完成能力（离线 15 项单测 + 冒烟过，真机路径尚未验证）——v1 原样转交 gpu-op，v2 按 kind 路由 shader / sysparam / gray，顺手把 brain.rs 密钥从 argv 撤到 `-K` 配置文件。#62 把 refbench 的 `RULES` 与 vks 的 `KNOBS`/`METRIC_MEANING` 抽成冻结的 `rules.json`，判读哈希随之切到这份文件的 sha256（切换点之前的归档哈希对应旧 .py，已记录在案，非回放损坏）。同步 STATUS 里的②③ 接收端说法、game_opt_loop README / DESIGN、phonefarm AGENTS、组织主页四处「等 eval --request」。
- **2026-09-24（第三次核对）**：核对到 phonefarm #57、#58、#59。loop_v1 的 Vulkan-Samples 三道闸与两臂判读面、refbench/vks 报告也收进 Rust，「报告类」从「还没收」里划掉；剩 autoloop.py（编排/设备驱动）、载体构建/路线工具（make_route_a.py / shadercompiler_shim.py）、设备端 shell、knobs/ 那批。#58 顺带修好 #45 引入的回放哈希断裂（归档证据重新可重放）。同步 README / AGENTS / loop_v1 README / 组织主页四处「进行中」说法。
- **2026-09-24（第二次核对）**：核对到 phonefarm #48、#50、#54、#51、#55。更新 loop_v1 收进 Rust 的进度（判定口径与两通路对照已收，剩 autoloop.py / 报告类 / 设备端 shell）；补上灰档 copyprobe 探针的已完成结论与帧时数字；同步五处其它文档里的「Rust 化」说法。
- **2026-09-24**：首次建立本页。核对到 game_opt_loop #14、phonefarm #52、refbench #1、.github #3；把四处过时的「现状 / 进行中 / 下一步」段落同步到本页口径（详见各仓当次提交）。
