# refbench — 白盒基准靶场

给 [phonefarm](https://github.com/BH3GEI/phonefarm) 的 loop_v1 harness 一个**可改的靶子**和
一个**已知答案的考场**：确定性极高的 Vulkan 渲染负载，每条优化路线一个外部开关，
自带 render pass 标记，瓶颈类型可定向构造。

- 为什么存在、边界、场景与判据 → [`DESIGN.md`](DESIGN.md)
- 接口契约（进: intent extras；出: JSON）→ [`contract/`](contract/)
- 出包 → `bash build/build.sh`（零交互，产物 `build/out/refbench.apk`，arm64-v8a）

跑测分离：本仓库只提供负载、旋钮、ground truth。驱动、采集、归因、统计判定全部在
phonefarm 侧（`loop_v1/refbench/`）。**本仓库内没有也不允许有任何测量或判定逻辑。**

## 快速验证（手动冒烟，正式流程走 loop_v1）

```bash
adb install -r build/out/refbench.apk
adb shell am start -n io.github.hgamey.refbench/android.app.NativeActivity \
  --es scene bw_pingpong --es run_id smoke --es frames 600 --es intensity 8 --es knob.loadop off
# 进程自退后:
adb shell cat /storage/emulated/0/Android/data/io.github.hgamey.refbench/files/refbench_out.json
```
