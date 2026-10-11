# 只读证据展示合同

系统 #18 的展示层需要保留候选的完整评测回包、资格、来源与否定结果。
依赖 evidence/archive PR27；本合同不改变评测、档案选择或推广规则。

## 显示规则

- 每个有报告的算子候选保留完整 `EvalReport` JSON，包括 flatten receiver 扩展。
  UI 可展开查看资格、指标 provenance、requested/resolved/observed 条件、恢复和限制。
- source=phonefarm 只说明报告入口，显示为“phonefarm 报告（未认证采集）”。
  evidence_origin 仅显示报告实际声明；缺失保持未知。
- 调用现有 qualification_state，拒绝 malformed/null/replay/experiment eligible=false。
  旧报告 LegacyUnqualified 显示仅证据；静态估计也仅证据。
  显式 Ineligible 显示无效；通过 conclusion 资格后才显示本次前沿/淘汰结果。
- 已资格化但档案读不到时，不推断被前沿支配，显示无效与“前沿未知”。
- 档案前沿成员关系仍保留，显示“档案前沿”；该关系不授予真实游戏证明。
  阶段、原始 status、数值与失败记录保持可追溯，报告缺失不能补字段。
- 代汇总分别统计 qualified/eliminated/invalid/evidence。
  含仅证据候选且没有已资格化结果时，整代为仅证据，不显示合格。
- 全部展示条目声明 view_grants_game_proof=false/view_allows_promotion=false。
  报告自行声明的 proof/promotion 字段留在原始 JSON，不转换为面板许可。
- 不认识的 quality_reference.kind 不显示成程序化图案。
- legacy A/B 报告完整 JSON 保留，原判据与否定结果可读；无资格记录为仅证据。
  缺失/异常/replay 资格阻断；conclusion 合格来源声明才展示原判据结果。
  A/B 全部通过还要求实际判据齐全、逐项通过且计数一致；汇总与逐项判据矛盾为无效。
- 总览说明合格的资格前提；载体重复性判定仅描述该表的离散度阈值。
  图表中的前沿也标为“档案前沿”；静态估计的 A/B 回包只能作为证据。
- 原始回包展开状态按轮次和候选保留，定时刷新不打断读取。

## 验收

既有历史真实文件可读，旧前沿归类仅证据。用这些记录明确变异的合同夹具覆盖
conclusion eligible=true、false/null/malformed/replay、synthetic、静态估计与缺报告，
并核对完整扩展的 dashboard JSON 保留。来源声明不足以认证采集。

本地 HTTP 只读检查使用 --no-fleet；不调用设备或模型。检查实际页面中的来源标签、
资格说明、原始回包与未知字段。保持已有路径白名单及文件读取限制。

当前边界：此单元覆盖算子候选与 legacy A/B 展示；完整确认报告、模型阶段账本的统一
案例展示仍待接入。没有实际游戏案例认证，不能关闭系统展示验收项。
