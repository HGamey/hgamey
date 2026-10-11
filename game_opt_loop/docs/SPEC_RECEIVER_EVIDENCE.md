# 接收端证据透传与结论资格

来源：系统交接 #11/#12/#13。phonefarm 新增的质量工件、资格、指标来源不能在 game_opt_loop 的 serde 解析中消失。

- 两种报告结构保留所有未知顶层 JSON 字段，序列化时逐值透传，不能将任意扩展字段解析成额外测量值或改写统计结果。
- `qualification` 若存在，只有 `stage=conclusion` 且 `eligible=true` 才允许晋级；missing/false/null/错误类型/探测阶段资格均不能覆盖此门槛。资格通过仍须通过原统计、画质和预算规则。
- `qualification_only=true` 是离线资格重放，没有新优化测量，即使回包包含 PASS 与合格资格也不得晋级。该标记若存在但类型错误同样拒绝晋级；显式 false 才表示非重放。
- 没有 `qualification` 的旧报告标为 `legacy_unqualified`，保留历史读写与探索比较能力，不升级为已资格化真实游戏收益。
- `artifacts`、`metric_provenance`、`metrics_provenance`、`sequence_quality`、规则/工件 hash 和未来扩展透传到算子档案与 optimize 的逐候选报告。报告展示明确资格状态与证据字段。
- 参数的 requested 意图不变成 observed。提交代理、合成输入及质量工具的指标 PASS 不在上游自行变成显示帧、真实配对或无鬼影结论。

验收：两个入口报告 round-trip、Archive 保存/重读、optimize Entry/report 保存，拒绝假 PASS + ineligible/malformed 资格晋级，并保留旧档案可读。
