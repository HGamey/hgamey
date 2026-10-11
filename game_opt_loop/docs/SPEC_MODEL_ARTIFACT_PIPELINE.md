# 模型工件阶段协议与可恢复账本

Refs [会议需求 #16](https://github.com/HGamey/.github/issues/16)、[系统 epic #11](https://github.com/HGamey/.github/issues/11)。本功能把 design → train → quantize → deploy → evaluate 的外部驱动串起来；不内置训练器、量化器、模型 API 或设备部署。五阶段完成只表示绑定的驱动协议完成，不能证明模型确实训练、可在游戏使用、评测合格或应推广。

## 冻结计划

`game_opt_loop/model_pipeline_plan@1` 必须包含唯一 `pipeline_id`、明确 `mode`（expert/autonomous）、候选说明、数据文件与来源、五个按顺序列出的 stage。

- 每个输入文件保存路径、SHA-256、格式、来源；来源缺省 unknown。`real_data_declared` 只是来源声明，无采集认证。合成或 unknown 来源不能变成真实游戏证明。
- `model` 明确声明 framework/runtime/ABI 及量化 method/precision/calibration_data_sha256；校准摘要只能引用冻结 data 输入。unknown 必须明确写出，不从扩展名推断能力。输出 role 按阶段依次要求 architecture、weights、quantized_model、deployment_receipt、evaluation_report，也可另附 metadata；role/format 都是声明，不等于模型格式或部署成功的语义验证。
- 驱动使用用户显式配置的 `argv: ["/absolute/executable", "arg", ...]` 和 `cwd`，`Command` 直接传参数，不拼接 shell。显式 shell 程序也属于用户配置，框架不生成 shell 指令。
- 可执行文件和显式 `driver_artifacts`（脚本、配置等）在冻结时哈希；已有文件形式的 argv 参数必须列入绑定。解释器依赖、动态库、网络服务不自动锁定；本地 SHA 只验证完整性，不证明驱动可信或没有其他副作用。
- 每个阶段输出名称、格式、来源、预算必须事先列明。输出路径由宿主给出，禁止 `../`、符号链接或沿用前一阶段输出；不得通过格式字符串宣称有效 ONNX/TFLite。实际格式解析由未来适配器负责。
- 宿主强制单进程执行、墙钟超时、日志+输出字节限制。steps/tokens/device_ms 是合作驱动在回执中声明的用量，只有声明超限门禁；报告恒为 `driver_declared_unverified`，不冒充可信计量或 API/设备预算强制。

## Driver 协议

宿主在阶段目录写 `request.json`，用保留环境变量 `GOL_PIPELINE_REQUEST` / `GOL_PIPELINE_RECEIPT` / `GOL_PIPELINE_STAGE_DIR` 传路径。驱动环境清空，只设这些变量；可执行程序应使用绝对路径。驱动读取请求，写出事先登记的输出，并最后原子写回执。

请求 `game_opt_loop/model_stage_request@1` 含 pipeline/plan SHA、stage、model 声明、实际输入文件及 SHA、预定输出路径/role/格式/来源、预算。输入包含原始 data 和所有已完成上游工件，评测驱动可明确引用量化模型及部署回执，不只依赖上一个路径名。`request_sha256` 是请求 JSON 的规范化摘要（回执绑定这个摘要）。驱动回执 `game_opt_loop/model_stage_receipt@1` 必須精确回显 `pipeline_id`、`plan_sha256`、`stage`、`request_sha256`；包含 status succeeded/failed、usage 的 steps/tokens/device_ms、输出名称与 SHA-256、可解释 note。失败也必须留下负结果，不加入性能 Pareto 档案。

输出全部按当前字节重新哈希，必须与回执匹配，集合也必须精确匹配计划。宿主保存完整回执、回执 SHA、stdout/stderr SHA、实际墙钟和输出工件 SHA；回执/输入/输出更改会阻断恢复。

## 状态与副作用

初始 pending。autonomous 自动 ready，但明确没有专家审查；expert 的每阶段 decide 绑定当前请求（包括前一阶段实际输出），要求 reviewer/reason，accept→ready、reject→rejected、defer→deferred，保留历史；未来阶段不预批。恢复不重新生成或替换已批准请求。

启动前先原子保存 in_flight，后才 spawn。成功且回执/输出完整→completed；非零退出或无效/缺失回执→failed，并保存协议失败与日志。超时/输出超限导致进程组终止→unknown，因外部副作用无法确定。无效输入等启动前失败也保存 failed。failed/rejected 不自动重试；重试需新实验目录。

账本以独占文件锁串行操作；临时文件 fsync、rename、父目录 fsync 实现原子 checkpoint。`resume` 只复用完整 completed/failed，in_flight/unknown 一律阻断，不因进程消失重复下发。操作者核对驱动现场、所有子进程已停止和外部还原后，可以 `reconcile --workers-stopped` 导入绑定回执；核对人/理由记录在账本，来源仍是人工声明。Unix 同时检查记录的进程组必须不存在；若启动后保存 PID 前宿主崩溃，必须依赖人工停止确认。PID 复用或僵尸进程导致保守阻断，不自动忽略。超时或输出超限后终止整个进程组；驱动启动脱离该组的远程/后台任务不受宿主控制，必须在适配器中禁止或另外核对。主机已记录的墙钟超限不能导成成功回执；未知中断墙钟不冒充实测预算。只验证回执身份和工件完整性，不能自动证明远端副作用发生一次或数据为真。

输出大小先检查再流式哈希。输出/日志预算以运行期间 10ms 轮询和完成时检查实施，驱动可在两次检查间越界；没有 OS 磁盘配额，不承诺写入字节绝不瞬时超额。

当前协议是保守 at-most-once 下发；没有跨本地账本/设备/API 的分布式事务。用户要对驱动可信度、设备租约、还原以及预算计量负责。本次实现和验收只调用 fake driver，不调用真实模型或设备。

## Expert / campaign 契约

工件 pipeline 是独立入口，不调用 `optimize`/phonefarm，不把其回执导成现有 EvalReport 或自授 campaign 权限。外层 campaign 可将 `plan_sha256` + 最终模型工件 SHA + 评测回执 SHA 作为冻结输入；必须继续走自身专家审批、来源/资格、独立确认和部署还原门禁。pipeline 的 expert 决定仅授权一个阶段，不等于 campaign 专家接受候选或质量合格。

账本/CLI 始终输出 `promotion_allowed=false`、`real_game_optimization_proven=false`、`model_semantics_verified=false`。fake 工件明确 synthetic，完成五阶段仍不进入真实游戏结论或推广。

## CLI 与离线验收

```text
game_opt_loop model-pipeline start --plan plan.json --out runs/model
game_opt_loop model-pipeline status --out runs/model
game_opt_loop model-pipeline decide --out runs/model --stage design --action accept --reviewer engineer --reason ...
game_opt_loop model-pipeline resume --out runs/model
game_opt_loop model-pipeline reconcile --out runs/model --stage train --receipt verified.json --reviewer engineer --reason ... --workers-stopped
```

真实外部 fake driver 集成测试必须覆盖五阶段 CLI、输入链与格式/合成标签、completed/failed 恢复不重复、专家逐阶段等待/拒绝/延期、驱动/计划/回执/输出篡改、缺回执/非零退出/预算超限、杀宿主后 in_flight 阻断及明确 reconcile。测试没有训练、Token、真机调用。后续再实现可信训练/量化适配、真实来源认证、可信预算计量、设备租约与还原、实际模型格式解析、phonefarm 资格/连续质量和独立确认接入。
