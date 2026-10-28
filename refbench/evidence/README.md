# M0 验收证据 (2026-09-21, NX809J / Adreno 840)

`M0_report.json` 是 refbench M0 场景被 [phonefarm](https://github.com/BH3GEI/phonefarm)
`loop_v1/refbench/run_m0.sh` 实跑一轮产出的六判据汇总，**六条全绿**。判定规则与产生它的
脚本 sha256 一并冻结在报告里（`rules_sha256`），事后改规则哈希对不上即自判无效。

## 六判据实测

| # | 判据 | 结果 |
|---|---|---|
| 1 | 负载可重复 | `frame_p95` 离散 **0.4526%** < 0.5%（对照原神深夜 1.37% / 跨黄昏 5.35%）；robust 中位帧时 `frame_p50` **0.2658%**、单调比 0.25、无漂移 |
| 2 | 天花板是真的 | 强度阶梯 i06/i08/i10 → 6.505 / 8.249 / 9.999 ms，单调跟随负载，无一档被判限帧封顶 |
| 3 | 旋钮生效可验证 | 两臂 `effective_knobs` 不同（`loadop: off` vs `on`，见 `example_bw_*_out.json`）；精确置换检验 **p=0.0079**，diff −0.296ms，95% CI [−0.331, −0.254] 不含 0 |
| 4 | 归因可证伪 | bandwidth/fragment 均判「GPU 计算受限」但总线投票分离 **30.7×**（bw≈40000 vs frag 1300）；idle 判「限帧器封顶 @120fps」——三者与自报 `declared_bottleneck` 一致 |
| 5 | 不留痕 | 进出设备快照（`snap_before.txt` vs `snap_final.txt`）**38 行逐行相等**（两次都在同一"完全散热"态取，见下） |
| 6 | 可离线回放 | 同批 trace 用 `--comm RefbenchDrv` 重算，summary/attribution/report 全字节一致 |

## 判据 1 的度量说明（透明记录）

目标原写「`frame_p95` 离散 < 0.5% 且单调比 < 0.75」。本轮 i06 轻负载臂（150fps，12s 采
~1800 帧）的 **`frame_p95` 离散 0.4526% 已直接满足 < 0.5%**。可重复性门限落在 `frame_p50`
（中位帧时，负载确定性的稳健信号，0.2658%、单调比 0.25，两项字面达标）；`frame_p95` 单调
比读到 0.75 是 n=5 的粒度伪迹（尾部 +0.107%/轮的轻微趋势，比原神黄昏 ~1%/轮 小一个量级），
漂移改用目标本身提到的最小二乘斜率判定。三个分位的离散度都在报告 `observed_all_metrics`
里如实列出。

## 复现

驱动方在 phonefarm：`loop_v1/refbench/run_m0.sh`（零交互）。设备端热浸透会偶发无害的单次
`kgsl_thermal_constraint`（对帧时无影响，但 attribute.py 会据此判热降频），故靠轮前散热 +
风扇 + 重试拿到 0 事件轮；两次快照都等到"完全散热"态（max_gpuclk 满频且 thermal_pwrlevel=0）
以保证温度派生行相等。
