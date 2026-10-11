# refbench — 白盒测试载体

确定性极高的 Vulkan 渲染负载，每条优化路线一个外部开关，自带 render pass 标记，
瓶颈类型可定向构造。给 harness 一个可改的靶子，和一个已知答案的考场。

## 在整体里的位置

整套东西是一个 harness：用户只提一种任务——「把某个游戏在某台手机上优化一下」，
内部自动决定用哪几层。

- [game_opt_loop](https://github.com/HGamey/game_opt_loop) 🔒 是入口与决策层：接任务、摸底、
  决定开哪几路、出方案、归档优胜、出报告。
- [phonefarm](https://github.com/BH3GEI/phonefarm) 是底座：驱动设备、施加改动、测量、
  统计判定、还原。判定留在底座这一侧，出主意的一方改不了判定。
- 本仓库是其中一个测试载体。`phonefarm/loop_v1/carriers/` 下还有 Megacity、Vulkan-Samples、
  AnKi，各自覆盖不同的场景规模与可改程度。

优化分三个层级：① 游戏代码 ② 图形接口 Vulkan 层 ③ 系统参数。本仓库源码在手，
三层都能改，所以适合验机制——机制在这里不成立，就不必拿去真游戏上试。
闭源商用游戏只剩 ②③，效果要在那边验。

状态：M0 通过（2026-09-21），被 loop_v1 实跑一轮，六条判据全绿。
证据（含 `report.json`）在 [`evidence/`](evidence/)：可重复 frame_p95 离散 0.45%、
旋钮 p=0.008、归因与自报瓶颈一致（带宽/片元总线投票分离 30.7×）、快照 38 行零差异、
离线回放字节一致。离散度这一项在各载体间的位置是：本仓库 0.45%，Megacity 0.987%，
原神 1.37%（跨黄昏那一批 5.35%）。

- 为什么存在、边界、场景与判据 → [`DESIGN.md`](DESIGN.md)
- 接口契约（进: intent extras；出: JSON）→ [`contract/`](contract/)
- 出包 → `bash build/build.sh`（零交互，产物 `build/out/refbench.apk`，arm64-v8a）

跑测分离：本仓库只提供负载、旋钮、ground truth。驱动、采集、归因、统计判定全部在
phonefarm 侧（`loop_v1/refbench/`）。**本仓库内没有也不允许有任何测量或判定逻辑。**

## 场景 sr_pipeline：算子挂进真实管线

```bash
# A 臂: 不挂算子, present blit 直接放大低分辨率 RT
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene sr_pipeline --es run_id a_off --es frames 3000 --es intensity 6 --es knob.postfx off

# B 臂: 管线尾部挂外部 candidate.spv
adb push candidate.spv /data/local/tmp/postfx/cand.spv
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene sr_pipeline --es run_id b_on --es frames 3000 --es intensity 6 \
  --es knob.postfx on --es postfx.shader /data/local/tmp/postfx/cand.spv
```

两臂唯一的差别就是那个算子。算子装不上时**退出码 2 且 clean_exit=false**，
绝不静默退回 off —— 否则 A/B 会悄悄变成 A 和 A。

## 快速验证（手动冒烟，正式流程走 loop_v1）

```bash
adb install -r build/out/refbench.apk
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene bw_pingpong --es run_id smoke --es frames 600 --es intensity 8 --es knob.loadop off
# 进程自退后:
adb shell cat /storage/emulated/0/Android/data/io.github.hgamey.refbench/files/refbench_out.json
```

## Native LR / HR 质量采集（尚未 Android / GPU 验收）

[设计与边界](docs/SPEC_NATIVE_PAIRED_CAPTURE.md)；数据缺口 [#15](https://github.com/HGamey/.github/issues/15)。
新增 `capture.native_pairs=on`，在相同 `frame_index` / `phase` / `iters` 下分别
直接渲染 1280×720 native LR 和独立 1920×1080 native HR；`imgHi` 始终只是候选
compute 输出。内容是程序化白盒 ALU 场景，不能证明原神或其他真实游戏优化。

采集额外加入 HR draw、readback、同步、写盘，输出标明 `quality_only` /
`performance_eligible=false`。质量采集与正常性能/功耗实验必须分开运行。
规则必须在采集前冻结，采集记录实际时钟，导入不替换时间或阈值。

```bash
# 需先用正常 Android 工具链构建，并在设备端放置既有 candidate.spv 和冻结规则。
# 此命令是待真机验收用法，本 PR 未执行设备操作。
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene sr_pipeline --es run_id native_quality_001 --es frames 60 --es intensity 6 \
  --es knob.postfx on --es postfx.shader /data/local/tmp/postfx/candidate.spv \
  --es capture.native_pairs on --es capture.start 0 --es capture.count 4 --es capture.stride 1 \
  --es capture.rules_path /data/local/tmp/postfx/frozen_rules.json \
  --es capture.rules_sha256 "$RULES_SHA256"
# 进程结束后，pull 整个 files/capture_native_quality_001 目录；不要只拉 manifest。

python3 tools/native_pairs.py validate --manifest capture/manifest.json \
  --rules frozen_rules.json --candidate-shader candidate.spv
python3 tools/native_pairs.py import --manifest capture/manifest.json \
  --rules frozen_rules.json --candidate-shader candidate.spv --out imported
phonefarm sequence-quality --reference imported/reference.json \
  --candidate imported/candidate.json --rules imported/frozen_rules.json \
  --out quality_report.json
```

桥保留原始 RGBA、raw/capture manifest/candidate SHA、帧号/相位/原生 role，
共同执行 linear UNORM8→sRGB8 转换。phonefarm 只比较 native HR 与候选；native LR
PNG 另存为模型输入。当前来源枚举没有 controlled whitebox，桥保守输出 `unknown`：
有限指标可为 PASS，但总体 UNKNOWN、真实游戏资格和无鬼影证明均为 false。
合成夹具可得到明确限定的指标 PASS；这不是实际 GPU 或真实游戏验收。

```bash
# 标准库 Python + 本机 C++；PHONEFARM_SEQUENCE_BINARY 可选，指向已有消费方。
PHONEFARM_SEQUENCE_BINARY=/path/to/phonefarm python3 -m unittest discover -s tests -v
python3 tests/host_syntax.py --vulkan-include /path/to/existing/include
```

本轮只验证 host SHA/严格合同/PNG/现有消费方接口与语法。Android/JNI stub 和 SPIR-V
占位不能证明 Android ABI、有效 shader、Vulkan 生命周期或 GPU 输出。没有下载 NDK、
构建 APK 或操作设备。FrameGen 中间时刻真值仍未实现。
