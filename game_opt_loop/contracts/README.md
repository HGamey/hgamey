# contracts — game_opt_loop 与 phonefarm 之间的接口

这些 JSON Schema 是两个仓库之间**唯一**的约定。game_opt_loop 按它们发请求,
phonefarm 按它们回报告；两边各自的单测对着同一份文件钉死，任何一边漂移都会红。

| 文件 | 谁产出 | 谁消费 |
|---|---|---|
| `eval_request.schema.json` | game_opt_loop | phonefarm |
| `eval_report.schema.json` | phonefarm | game_opt_loop |
| `candidate.schema.json` | game_opt_loop | 本仓库归档 |
| `pareto_archive.schema.json` | game_opt_loop | 本仓库归档 |

## v1 与 v2

v1 是只有「画面算子」一类时的扁平格式，由 `phonefarm gpu-op` 消费，**仍然有效**。

v2 把三类改动面收进同一个信封，由 `phonefarm eval --request <json> --json` 消费：

```
{
  "version": 2,
  "kind": "shader" | "sysparam" | "gray",
  "candidate_id": ..., "serial": ..., "package": ...,
  "workload":   { carrier / script / seconds / frames / intensity },
  "conditions": { 冻结的测试条件 },
  "protocol":   { 冻结的统计协议 },
  "payload":    { 各类特有的部分 }
}
```

共同的东西在信封上，各类特有的东西在 `payload` 里。这样接收端只有一个入口，
上层也只有一套证据格式与**一份**优胜档案 —— 同一个负载、同一套测试条件下量出来的
东西本来就该放在一起比。

三类各自的 payload：

- **shader** — 算子 GLSL + 预编译 SPIR-V + 耗时预算 + 画质真值 + 在位冠军耗时 + （黑盒时）注入方式
- **sysparam** — 一组参数 + 展开好的写入计划 + 白名单 + 温控保护关键词；`probe_only` 只探白名单不跑候选
- **gray** — 一组渲染流程改写开关 + `require_hit`（改写没命中就整轮作废）

## 三条贯穿全部字段的纪律

1. **量不到就是 `null`，绝不填 0。** 一个 `0.000 W` 看起来像个数字，其实是
   「这条轨此刻量不了」，会把 A/B 裁决直接带沟里。每个 `null` 在
   `metrics.unavailable` 里配一条人能读的原因。
2. **测量不算数与判定失败是两件事。** `status=ABORT` 是「这次没量准」（旋钮没全
   生效 / 超温度上限 / 轮末快照不一致 / 改写一次没命中），既不是正样本也不是
   负样本；`status=REJECT` 是「量到了，不好」，是有效的负样本。混为一谈会让上层
   把工装故障学成算法结论。
3. **数字要带着它的量法一起走。** `power_watt` 只在 `power_source.on_battery=true`
   时是整机功耗；`psnr_db` 的绝对值只在同一个 `quality_reference` 下可比；
   `conditions_observed` 记的是这一轮**实际**在什么热态、什么风扇状态、什么游戏内
   帧率设置下量的。不记下来，两批不可比的数据看起来就是可比的。

## 改这些文件的时候

加字段可以，改已有字段的语义要先对齐两边。上层对旧回包必须保持宽容：
新加的字段一律可空，缺了就按「接收端还没报」处理，不是解析错误。
