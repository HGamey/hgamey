# refbench

白盒测试载体：确定性的 Vulkan 渲染负载，每条优化路线一个外部开关，自带 render pass 标记，瓶颈类型可定向构造。本仓库只提供负载、开关和已知答案；由 [phonefarm](https://github.com/HGamey/phonefarm) 的方法与合同驱动，`loop_v1/refbench/` 保留既有方法入口；共享节点通过 [harness-ops](https://github.com/HGamey/harness-ops) 的作业入口执行。

## 构建

```bash
bash build/build.sh          # 产物 build/out/refbench.apk（arm64-v8a）
bash selftest/build.sh       # 只在改了 selftest 的 .comp 时需要
```

## 本地开发冒烟（仅限明确独占的测试设备）

下列 raw adb 命令说明开发动作；共享节点通过已定义的任务与租约路径执行，不能用本段绕过 owner 或恢复屏障。

```bash
adb install -r build/out/refbench.apk
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene bw_pingpong --es run_id smoke --es frames 600 --es intensity 8 --es knob.loadop off
adb shell cat /storage/emulated/0/Android/data/io.github.hgamey.refbench/files/refbench_out.json
```

接口契约在 [`contract/`](contract/)，验收数据在 [`evidence/`](evidence/)。

## 文档

- [产品准则](https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/PRODUCT-CHARTER.md)：外部 Agent 通过网页/CLI/API 组织研究，人异步参与；白盒机制与宿主验收不能外推到商业游戏收益。
- 完整说明、场景与判据：[HGamey/docs · repos/refbench](https://github.com/HGamey/docs/tree/main/repos/refbench)
- Native LR/HR 质量采集（`capture.native_pairs=on`，尚未真机验收）：[NATIVE_PAIRED_CAPTURE.md](https://github.com/HGamey/docs/blob/main/repos/refbench/NATIVE_PAIRED_CAPTURE.md)

本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
