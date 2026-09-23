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
