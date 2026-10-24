# 命令与接续示例

以下为占位示例，变量由当前任务提供。不要照抄示例 ID 复用到不相关研究；固定 ID 在同一次未知结果接续中保持不变。全局参数位于子命令前。`pf-node` 指当前仓库 `node/pf-node`；文件只包含所述 data，不能多包一层 envelope。

## 读取

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" node
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research show research "$RESEARCH_ID"
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research list direction --research-id "$RESEARCH_ID" --limit 100
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research list request --research-id "$RESEARCH_ID" --limit 100
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research list feedback --research-id "$RESEARCH_ID" --limit 100
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research events --research-id "$RESEARCH_ID" --after "$AFTER" --limit 100
```

列表续页用原过滤条件和 `--cursor next_cursor`；事件用 `next_after`，看 `has_more`。若本回合页数/时间有限，保存尚未处理的分页位置，不能将第一页当全部。不同 kind 的列表没有共同事务快照；决策前重读目标对象。首次可从 after=0 分页重建，已处理事实以方向中的反馈决定和对象版本核对。

## A 等待具体协助

例如 `request-data.json`（替换 study/A 及版本）：

```json
{
  "research_id": "study",
  "type": "assistance",
  "title": "A 的场景输入",
  "need": "提供可复现的场景材料和使用范围",
  "why": "A 的实验尚缺输入；B 的离线分析不依赖它",
  "resume_hint": "读回复并核输入可用后，明确解除 A 的依赖",
  "context": {"research_revision": 1, "directions": [{"id": "A", "revision": 1}]},
  "evidence_refs": []
}
```

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research create request "$REQUEST_ID" request-data.json --operation-id "$OP_REQUEST"
```

从 A 最新 `.data` 编辑完整文件，增加依赖，state 可设 waiting_material，写明 state_reason：

```json
{"id":"scene-input","kind":"request","target_id":"request-a","target_revision":1,"blocking":true,"reason":"A 缺场景材料","resolution":null}
```

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research update direction "$DIR_A" direction-a-data.json --revision "$DIR_A_REV" --reason "只阻断缺材料的 A" --operation-id "$OP_A"
```

创建请求与挂依赖是两次写入；中断后分别找回，不能认为请求存在就已阻断方向。A/B 主观评价另需 `research.evaluation-v1`，固定已注册 result_source 的槽位和请求 revision；media 下载及 client_display 声明都不证明人已经看过。

## B 使用已有离线 producer

先保存完整 B data；next_experiment 的精确结构：

```json
{"title":"分析已有小 frame audit 原件","plan":"job request_id=study-b-analysis-1；phonefarm frame-audit analyze；记录实际输入/producer版本；输出 analysis.json/analysis.md；本轮只检验分析产物，无实机收益结论。","research_revision":1}
```

输入须符合当前 PF frame-audit 格式；不能用任意 JSON 或缩图假冒实机原件。只有已明确允许 host 离线操作才采用此例，设备实验换成真正支持且获授权的 recipe。

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research update direction "$DIR_B" direction-b-data.json --revision "$DIR_B_REV" --reason "冻结输入、判据及 job request ID" --operation-id "$OP_B"
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" --detach --request-id "$JOB_REQUEST" --device-access none --execution-policy process_group --resource host:offline-analysis phonefarm frame-audit analyze frameaudit-small.json --json @out/analysis.json --md @out/analysis.md
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" request "$JOB_REQUEST"
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" status "$JOB_ID"
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" artifacts "$JOB_ID" --limit 100
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" -o "$LOCAL_OUT" out "$JOB_ID" analysis.json
```

`host:offline-analysis` 是合作客户端约定的独占资源名，不是 CPU 配额。none 只适用真正不接触设备的命令。process_group 只管原进程组；不能从源码存在推断节点已开放 contained。现存本机路径会自动打包上传，传指定小输入，别把素材/凭据大目录作为参数。

job 客户端可在网络/5xx 后同 ID/同 body 重试，依赖节点幂等；不在外层换 ID 重试。研究/结果写入是单次应用层 POST，未知时只查原 ID：

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" research operation "$OP_B"
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" results show "$RESULT_ID"
```

结合 phase/outcome/exit/execution/recovery/queue 检查原任务。storage_blocked/lost/恢复屏障均保留具体义务；不会因分析出 JSON 而消失。仅下载需要的输出；缺少 job ID 时先查 request，不能伪造 evidence。

## 显式结果与方向引用

极小 synthetic 示例的结果清单如下，替换实际 ID/producer_version。真实原件须如实改 origin；分析失败则 outcome=failed，保留原错误和可用来源。

```json
{
  "schema": "phonefarm/result@1",
  "result_id": "study-b-analysis-1",
  "title": "B 的离线分析流程验证",
  "job_id": "0123456789ab",
  "labels": {"research_id": "study", "direction_id": "B"},
  "origin": {"producer": "phonefarm frame-audit analyze", "producer_version": null, "evidence_origin": "synthetic", "observed_at": null},
  "outcome": "unknown",
  "sources": [{"name": "analysis", "kind": "job_output", "job_id": "0123456789ab", "path": "analysis.json"}],
  "metrics": [],
  "claims": ["保留合成输入的离线分析报告"],
  "limitations": ["没有实机候选对照、显示 FPS、功耗或优化收益结论"]
}
```

```bash
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" results register result-manifest.json
pf-node --url "$NODE_URL" --node-id "$NODE_ID" --token-file "$TOKEN_FILE" results list --research-id "$RESEARCH_ID" --direction-id "$DIR_B"
```

原件在 job/out，登记不复制大文件或授予测量资格。登记确认后，把真实引用加到 B 最新完整 data 的 evidence_refs：

```json
[
  {"kind":"job","node_id":"<32位原node_id>","job_id":"<原job_id>"},
  {"kind":"result","node_id":"<同node_id>","result_id":"<原result_id>"},
  {"kind":"result_source","node_id":"<同node_id>","result_id":"<原result_id>","source_name":"analysis"}
]
```

更新 state/reason/priority/next_experiment 时保留旧证据；报告齐全不自动 verified。研究与结果目前同节点引用，换 URL 不会迁移对象。

## 另一客户端处理反馈

从原 target/research 读事件、feedback 和原 request revision，再读 A 当前版本。`feedback_decisions` 示例：

```json
{"feedback_id":"reply-a-1","decision":"adopt","reason":"已核对应原问题，材料满足本方向当前前提"}
```

确实满足依赖时，在原 dependency 的 resolution 中另存：

```json
{"decision":"satisfied","reason":"已核材料可读取且范围满足实验计划","feedback_ids":["reply-a-1"]}
```

若尚缺材料，保留 resolution=null，decision 可 defer 并写原因。反馈改变了方法时冻结新的计划版本，旧实验仍按其原 research_revision 解释。当前授权不允许的建议只登记，不能通过 adopt 扩权。

## 可携带交接块

保存为客户端小文件或交接消息；不是 research data 的新字段，也不上传聊天历史：

```text
目标：node URL + node ID；research ID/当前 revision
授权与回合边界：复用哪些用户许可；哪些方向尚无前提
游标：node/research 过滤 + 本客户端已处理 after；未完列表 cursor
未决元数据：kind/id/operation_id/原 revision，下一原 operation 查询
任务：direction/冻结 research revision/request_id/job_id/原输出路径
结果：result_id/import_id，已登记但未回写方向的引用
反馈：原 request ID/revision、已处理决定及仍未解除的局部依赖
下一步：可行方向、待运行完成的原 job 读取、阻断原因
```

凭据用既有配置或本地文件路径提供给接续者，不写 token 值；不能假定另一机器有同一路径。所有方向暂不可做时保存此块，按外部运行器约定返回。平台当前不会自动唤醒，也不能找回从未保存的客户端 ID。
