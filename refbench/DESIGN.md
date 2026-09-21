# refbench — 白盒基准靶场 设计文档

> 立项 2026-09-21 · 驱动方: [BH3GEI/phonefarm](https://github.com/BH3GEI/phonefarm) 的 `loop_v1/`
> 状态: **M0 通过 (2026-09-21)** — 六判据全绿, 证据见 [`evidence/`](evidence/)

## 0. 定位

phonefarm 是尺子，refbench 是**可以被改、被量、被精确复现的被测物**。它同时解决两件事:

1. **可改的靶子** —— 第三方游戏改不了 render pass / shader / draw call，harness 只能评估不能优化。
   refbench 的每条优化路线是一个外部开关。
2. **已知答案的考场** —— loop_v1 的归因结论（"限帧器封顶"、"带宽没饿着 GPU"）此前无法证伪。
   refbench 可以定向构造已知瓶颈的场景，考 harness 说不说得对。

## 1. 边界（跑测分离）

| 谁 | 负责 |
|---|---|
| phonefarm / loop_v1 | 驱动设备、采集、归因、统计判定、回滚、证据归档 |
| refbench（本仓库） | 提供负载、提供旋钮、提供 ground truth |

**本仓库内不得出现任何测量或判定逻辑。** 不自报帧率、不读时钟决定内容、不自算统计量。
输出 JSON 里只有配置事实（生效的旋钮、pass 结构、自报瓶颈类型、提交帧数），没有任何时间量。

## 2. 接口契约

机读定义在 `contract/`（`launch.json` 进、`output.schema.json` 出）。要点:

**进** —— `am start` intent 字符串 extras（全部用 `--es`，应用内解析，避免类型踩坑）:

```
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene bw_pingpong --es run_id knob1 --es frames 3600 \
  --es intensity 8 --es knob.loadop off
```

**出** —— 进程退出前写 `/storage/emulated/0/Android/data/io.github.hgamey.refbench/files/refbench_out.json`
（无需 root 可 pull）。内容: contract 版本、scene、`declared_bottleneck`、`effective_knobs`、
每个 render pass 的 marker（名字/目标/loadOp/storeOp/尺寸/格式）、swapchain 实际配置
（present mode 等）、`frames_submitted`、`clean_exit`。

**进程生命周期**: 渲染完固定帧数后自行 `exit(0)`。harness 用 `pidof` 存活轮询判断结束，
不用时长盲等。JSON 在退出前落盘并 fsync。

**trace 过滤线程名**: `RefbenchDrv`。实测（NX809J / Adreno 840）Vulkan 驱动不在调用线程发
kgsl 提交，而是驱动自己的进程内匿名线程（`Thread-N`）——所以调用线程名当不了过滤契约。
refbench 在帧循环内把本进程所有 `Thread-*` 线程经 `/proc/self/task/<tid>/comm` 改名为
`RefbenchDrv`（同线程组可写，幂等，周期重扫防驱动延迟建线程）。loop_v1 的
`parse_trace.py --comm RefbenchDrv` 按此过滤。每帧恰好一次 `vkQueueSubmit`，
提交间隔无周期结构时 `detect_submits_per_frame` 自然回退 N=1。

## 3. M0 场景与旋钮

M0 路线 = 路线图 A 类第 2 条 **LoadOp / StoreOp 正确性（`DONT_CARE` vs `LOAD`）**
（`phonefarm/docs/MOBILE_GPU_OPT_ROUTES.md`: "一个多余的 LOAD = 整帧读带宽。最典型的免费的钱。
差异应直接反映在总线投票上"）。

| scene | declared_bottleneck | 构造 | intensity 语义 | present mode |
|---|---|---|---|---|
| `bw_pingpong` | `bandwidth` | 2×RGBA16F 2048² 离屏 RT 乒乓全屏 pass ×N + swapchain blit。着色器 4 tap 采样 + 少量 ALU: 流量重、计算轻 | N = 乒乓 pass 数（默认 8） | MAILBOX 优先 |
| `frag_alu` | `fragment` | 1024² RGBA8 单离屏 pass + blit。片元着色器跑 `intensity×64` 次 fma/sin 循环（循环上界走 push constant，编译器不可展开消除）: 计算重、流量轻。实测 NX809J: 512 iter ≈ 17.7ms | ALU 循环规模 | MAILBOX 优先 |
| `idle_cap` | `none` | 单 pass 一个小三角形，强制 FIFO: 已知被 vsync 钉住 | 忽略 | FIFO 强制 |

**旋钮 `knob.loadop`**（只作用于 `bw_pingpong`）:

- `off`（未优化）: 每个乒乓 pass 的 color attachment `loadOp=LOAD` —— TBDR 上每 pass 多一次
  整 RT 读回 GMEM（2048²×8B ≈ 33.5MB/pass，×8 pass ≈ 268MB/帧的纯多余流量）
- `on`（优化）: `loadOp=DONT_CARE`（全屏覆写，内容语义不变，两臂输出像素一致）

两臂**唯一**差异是这个多余的 load，其余字节不变 —— 这就是 ground truth。

## 4. 判据 4 的归因映射（考题与标准答案）

loop_v1 的 `attribute.py` 判定树没有"带宽受限"主因 —— 带宽是正交维（`bw_median` 总线投票 +
DDR 频率）。访存停顿在 kgsl `active` 里同样计为 GPU 忙。所以映射是:

| declared_bottleneck | loop_v1 应给出 |
|---|---|
| `bandwidth` | 主因 =「GPU 计算受限」**且** 带宽维显著高（`bw_median` 远高于 `frag_alu` 场景，≥3×） |
| `fragment` | 主因 =「GPU 计算受限」**且** 带宽维显著低（上式的另一端） |
| `none` | 主因 =「限帧器封顶 @{面板刷新率}fps」（vsync 钉住，harness 不得无中生有别的主因） |

映射的判定代码属于 loop_v1 侧（`phonefarm/loop_v1/refbench/`），本仓库只声明标准答案。

**两个已知陷阱，负载强度设计必须避开:**

1. `attribute.py` 把帧周期落在 30/45/60/90/120/144 fps ±2% 带内判为「限帧器封顶」。
   重负载场景的 intensity 要选到带外（如 ~13ms ≈ 77fps），否则会被误判 —— 这不是 harness
   错，是考题出瞎了。
2. 重负载可能触发热事件，主因会变「热降频受限」。对策: 采集窗口短（30s）、轮间用温度
   回落轮询（不盲等）、intensity 不顶满。

## 5. 确定性设计

- 内容只是 `frame_index` 的函数: uniform 相位 = 2π×frame/frames。不读时钟、不用随机数
  （程序化数据固定种子）、无物理、无昼夜。
- 固定帧数（harness 显式传入并记录），跑完自退。
- 离屏 RT 尺寸固定（2048²/1024²），与设备分辨率解耦 —— 跨设备负载字节可比。
- 交换链格式固定优先级选取，实际选中值写进输出 JSON。
- 包名 `io.github.hgamey.refbench` 与任何商业游戏无相似性，不落入厂商限帧/驱动白名单。
- 不依赖 root（采集侧的 root 是 loop_v1 的事）。

## 6. 判据 1 的预算依据

原神深夜窗口 frame_p95 离散度 1.37%（含游戏逻辑、流式加载、AI）。refbench 无逻辑无 IO
无分支，剩余噪声只有 DVFS 与温度，目标 < 0.5%。若实测不达，先查 DVFS 稳定窗口
（MAILBOX 满载下 GPU governor 应贴顶），再谈放宽。

## 7. 构建

`build/build.sh` 零交互出 `build/out/refbench.apk`（arm64-v8a）。不用 Gradle:
NDK clang 直接编 `NativeActivity`（`hasCode=false`，零 Java 源码），glslc 编 SPIR-V
嵌进 .so，aapt2 打包，apksigner 用脚本自生成的调试密钥签名。依赖只有
brew 的 `android-commandlinetools`（build-tools;35.0.0 + ndk;28.2.13676358）与 openjdk。

## 8. M0 退出判据

| # | 判据 | 断言 |
|---|---|---|
| 1 | 负载可重复 | analyze.py 报 frame_p95 离散度 < 0.5% 且单调比 < 0.75 |
| 2 | 天花板是真的 | 同场景改 intensity，帧时间必须跟着变；钉死在节拍上不动 = 不通过 |
| 3 | 旋钮生效可验证 | 两臂 `effective_knobs` 不同，且精确置换检验主指标 p < 0.05 |
| 4 | 归因可证伪 | ≥2 个瓶颈类型不同的变体，loop_v1 归因与 `declared_bottleneck` 按 §4 映射一致 |
| 5 | 不留痕 | 进出设备快照逐行相等（loop_v1 判据 4 原样适用） |
| 6 | 可离线回放 | 同一份 trace 重算逐字节一致（loop_v1 判据 5 原样适用） |

## 9. Backlog（M0 之后）

- B 类顶点流分离场景（`vert_bin`，打 binning pass）→ 补齐第三类瓶颈变体
- A 类 render pass 合并（subpass）旋钮
- C 类 FP16/mediump 旋钮
- OpenHarmony 出包（Vulkan 两边都有，`build/` 加一条链）
- Unity 场景（证明真实引擎接得上，半黑盒定位）
