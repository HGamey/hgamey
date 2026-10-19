# refbench

白盒测试载体：确定性的 Vulkan 渲染负载，每条优化路线一个外部开关，自带 render pass 标记，瓶颈类型可定向构造。本仓库只提供负载、开关和已知答案；驱动、采集、统计判定都在 [phonefarm](https://github.com/HGamey/phonefarm) 的 `loop_v1/refbench/`。

## 构建

```bash
bash build/build.sh          # 产物 build/out/refbench.apk（arm64-v8a）
bash selftest/build.sh       # 只在改了 selftest 的 .comp 时需要
```

## 手动冒烟（正式流程走 phonefarm loop_v1）

```bash
adb install -r build/out/refbench.apk
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene bw_pingpong --es run_id smoke --es frames 600 --es intensity 8 --es knob.loadop off
adb shell cat /storage/emulated/0/Android/data/io.github.hgamey.refbench/files/refbench_out.json
```

接口契约在 [`contract/`](contract/)，验收数据在 [`evidence/`](evidence/)。

## 文档

- 完整说明、场景与判据：[HGamey/docs · repos/refbench](https://github.com/HGamey/docs/tree/main/repos/refbench)

本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
