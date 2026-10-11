# 同帧 native LR / native HR 捕获（待 Android / 真机验收）

Refs [数据合同 #15](https://github.com/HGamey/.github/issues/15)、[系统 epic #11](https://github.com/HGamey/.github/issues/11)。主线 `sr_pipeline` 的 `imgHi` 是候选 compute 输出，不是 native HR 真值。

## 来源与范围

新增可选 `capture.native_pairs=on`，仅 sr_pipeline。独立 native HR 以同一个 alu.frag、相同 frame index、相位 float 位模式、intensity/iters、固定 fullscreen UV camera 与 exposure，直接在 1920×1080 栅格渲染。LR 直接在 1280×720 渲染，禁止从 HR 降采样。`imgHi` 只作为 postfx_candidate 另列；postfx=off 没有候选输出。

来源是 **native_rendered_controlled_whitebox**，内容仍为 **procedural_alu**。GPU 原生渲染不等于真实游戏内容，更不等于原神验证。不得把 CPU 合成 fixture 当 native 捕获。FrameGen 中间时刻真值、真实引擎相机/曝光、商业黑盒同步捕获均未交付。

## 配置与同步

- 固定 `capture.start` / `capture.count` / `capture.stride`，count 1..16、stride 1..16，完整索引必须落在固定 frames 内；不静默 clamp 捕获配置。run_id 必须为安全文件名且不是 unset。
- `capture.rules_path` 和 `capture.rules_sha256` 必须绑定外部事先冻结的质量规则。开始前读取实际文件、验证 SHA 和规则中的 frozen_at_unix_ms，实际 CLOCK_REALTIME start_ms 必须晚于冻结。候选 SHA 绑定实际装载 SPIR-V 字节。宿主文件 pull/import 不改写原始输入。
- LR 与独立 HR 使用同一 record_frame 的 PushC，不读时钟决定画面。在同一 VkCommandBuffer / submission 中画 native HR，按需执行候选，再复制三个不同的 image 到 HOST_VISIBLE|HOST_COHERENT staging buffer。
- Image 由 SHADER_READ_ONLY → TRANSFER_SRC，copy 完后恢复 SHADER_READ_ONLY；buffer 写入提供 TRANSFER_WRITE → HOST_READ 依赖。必须等该 submission fence 完成才 map/write，且完成写入前不提交下一帧；不复用尚在飞行的 staging。只在 capture 模式添加 transfer-src usage / HR render / staging / fence wait。
- 只写 tight RGBA8（row_bytes=width×4），检查 map / 写入 / fsync。拒绝覆盖已有捕获目录。所有文件 SHA-256 来自实际 readback 字节；候选、场景 shader SHA 都写入清单。输出尺寸保持横屏固定，不随 swapchain 旋转。

这是质量采集模式，额外 HR draw、readback、同步和 CPU IO 会改变负载。schema 显式 `capture_mode=quality_only`、`performance_eligible=false`；不能用这次 run 宣称正常管线帧时或功耗。refbench 仍只输出配置、内容身份、文件哈希和计数，不计算质量、帧率、功耗或统计结论。

## 清单

`refbench/native_paired_capture@1` 含 run_id、scene、content/source、规则 SHA、shader SHA、candidate artifact SHA、采集配置及完整性。每项 frame 有 host_frame_id（run_id:frame_index）、整数 frame_index、phase / phase_bits、iters、camera/exposure、原生 role、分辨率/格式/色彩约定、raw 文件名/字节数/SHA。

清单记录真实 CLOCK_REALTIME start_ms/end_ms；每帧记录提交前 command recording 的 CLOCK_MONOTONIC timestamp_ns，同一帧 LR/HR/candidate 共享该时间与 phase。它不是 GPU执行或呈现时间，不用于性能测量，画面仍只由 frame_index 决定。时间仅写入专用捕获清单，不将其添加到既有 refbench_out 测量契约。同帧身份来自同一次显式录制的 phase/PushC 与 submission，不将 timestamp 或 hash 等同采集来源认证。完整清单仅在全部登记帧写入且整个 run clean 结束时 `complete=true`；失败清单保留已写帧并明确不完整。

## Host 校验及 PNG 桥

标准库工具先严格校验 schema/source/role/完整性、固定序列、每帧 phase 位模式、shader/candidate绑定、尺寸/字节数和所有 raw SHA；不输出测量结论或把 hash 一致当来源认证。合成测试清单明确 source=synthetic_fixture，不能冒充 GPU native。

导入工具将三个 role 分别转 PNG，原始 RGBA 与 SHA 不改动；明确用共同 linear UNORM8→sRGB8 映射，保留捕获清单 SHA、原始 frame/phase/camera/role/raw SHA、颜色派生到独立 provenance sidecar。与 phonefarm #17 的同尺寸 sequence-quality 对接只比较 nativeHR/reference 和 postfx_candidate/candidate，nativeLR PNG 单独供模型输入。

当前 phonefarm source 枚举没有白盒 native 类型，且只接受 sRGB8。桥的 source.kind=unknown，保留真实 capture start_ms 与采集 timestamp_ns；不得以导入时间替换。质量命令应可读这些 manifest/ledger 并计算指标；最终来源 UNKNOWN，不可产生真实游戏资格。采集 IO 导致间隔超过冻结门槛时必须如实 FAIL/UNKNOWN，不人为压缩时间线。后续扩展 ControlledWhitebox 合同才能在白盒范围下声明冻结指标 PASS；现有安全门禁保持。rules SHA 必须与 producer 预填值一致才能导入；候选 SPIR-V 当前文件必须与 capture 清单 SHA 匹配。

## 本轮验证边界

本机无 NDK / glslc，不下载或接受许可证、不构建 APK、不运行设备。本机 C++ 编译器 + 已安装 DevEco 的 Vulkan 头（隔离 include，不能将 OH sysroot当Android toolchain）+ Android 声明 stub 只做源文件语法检查。SPIR-V stub 仅为编译占位，不是可执行 shader。纯 host SHA/清单/PNG/合同与 fake readback测试可以证明局部代码，不能证明 Vulkan 生命周期、实际 GPU 同帧输出或设备画质；真实验证在 Android 构建和设备捕获后补齐。若 host 发现同步疑点优先阻断验收，不宣称原生捕获已通过。

## 实现状态与剩余门禁

- Producer：`app/src/paired_capture.h` 管文件/哈希/实际采集时钟；`main.cpp` 持有独立 HR image 与 staging。`rp_alu` 初始 UNDEFINED / final SHADER_READ_ONLY；LR/HR 两个 draw 复用相同 `PushC.iters=intensity*64` 与同一 phase float。布局与 fence 只经静态审查、host 语法检查，未通过真实 GPU 验证。
- 仅新增捕获 readback fence 等待为固定 10 秒。超时先持久化 `failure_reason=submission_fence_timeout` 与 `complete=false`，然后走失败收尾。既有 general `vkWaitForFences(UINT64_MAX)` / acquire / `vkDeviceWaitIdle` 仍可能无限阻塞，不能宣称整个进程 bounded；独立 harness watchdog 是后续门禁。
- `failure_reason` 为 sticky；readback 写入、提交等待、run 中止都不能翻回完整。manifest 原子更新自身 checkpoint；raw 文件及捕获目录拒绝已有对象/符号链接，部分 raw 写入不加入成功 frame 行。进程崩溃时此前 checkpoint 保持不完整。
- Producer 有界 JSON 解析仅检验 top-level freeze 元数据与原始 rules SHA；host importer 继续严格检验完整 rules schema/数值。解析器有意拒绝 escaped Unicode object keys；使用通常 ASCII keys 的冻结规则。
- Host bridge 为 `tools/native_pairs.py validate/import`，只使用 Python 标准库，快照 raw 总字节上限 384 MiB。输出目录必须不存在，最后才写 `import_provenance.json` 完整 marker；中断输出不能据路径推断完成。所有 raw 原件、捕获清单、规则和候选字节复制到新目录，frame ID / actual timestamps 不重编号/压缩。
- Host test `synthetic_fixture` 内容另标 `scene_content=synthetic_fixture`；假候选不是有效 SPIR-V。原生输出内容固定 `procedural_alu`，桥映射 unknown。来源声明/哈希不自动证明 GPU 来源或真实配对。
- 既有 `output.schema.json` 的 compute pass `load_op=NONE` 枚举不一致是原有 contract backlog，本次不顺带修复。新增捕获 flag 单独加入 output schema；完整原生捕获合同由严格 host validator 检验。
- 待 Android 构建后运行 GPU validation layers、捕获真实 native raw、检查尺寸/相位/色彩/设备生命周期，并用预冻结规则经 phonefarm 评测。该阶段未交付，所以 issue #15 不关闭。

## 新 completion 执行合同

本分支增加 [SPEC_CAPTURE_COMPLETION](SPEC_CAPTURE_COMPLETION.md)。manifest@1
保留字段，但 strict 新导入必须有 `refbench/native_capture_completion@1` 的
`completion.json`，绑定本 run 的 manifest 与 run-scoped refbench_out。
旧 capture 无 marker 仅可原样归档/UNKNOWN；不得回填升级旧记录。
quality_only 不再覆盖 app-wide refbench_out；其输出在独占 capture_run_id
目录。磁盘失败 sticky，最终 marker 出版前逐一重核 raw size/hash；actual
fence wait→map/write 顺序保持。缺 marker 时即使 manifest complete=true
也不能当完整 capture；存储失败可能连负面 checkpoint 都无法写，receiver
仍须拒绝。BW 输出路径、性能语义和 UNKNOWN 来源映射不变。
