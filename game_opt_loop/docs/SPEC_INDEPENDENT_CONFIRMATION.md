# 独立确认与非劣效合同

Refs https://github.com/HGamey/.github/issues/18 。这是离线确认注册/裁决入口，不调设备或模型，不修改前沿。

## 数据合同与冻结

`confirm freeze --plan plan.json --out frozen.json` 以 create_new 注册：基线与候选工件 SHA-256、条件 SHA-256、基线准入工件 `baseline_admission_sha256`、探索 run IDs、预先分配的确认pair/run IDs与平衡AB/BA次序、完整确认 pair 数、指标方向/单位、最小收益、最大允许退化、家族 alpha 与统计假设。至少5对独立运行；至少1个指标必须有正的收益目标。sha绑定规则和注册时间。已有文件不得覆盖。基线准入发生在注册前，其原始 trace/ENV 也列入 exploration_artifact_sha256，不能再作为确认臂。

`confirm judge --frozen frozen.json --samples pairs.json --out report.json` 必须一次提供确切冻结轮数；未齐、超额、重复 run/pair/report 字节、与探索数据重叠、工件/条件/规则改变均 BLOCKED。每臂报告完整字节以sha绑定。confirmation_binding.raw_artifacts必须含trace_sha256及env_sha256，原始工件hash也须与其它确认臂及探索工件互斥；freeze必须列exploration_artifact_sha256。receiver必须提供单轮 `confirmation_binding`：run_id、sample_role baseline/candidate、artifact_sha256、conditions_sha256、frozen_plan_sha256、pair_id、started_at/finished_at、aggregation_level=single_run。运行时间必须在注册后，单轮不能重叠，必须符合冻结的AB/BA与pair顺序；报告不具备绑定则明确阻断。现有聚合 eval 报告没有此合同，不能直接当独立运行重复计数；后续执行driver需产出单轮绑定。

接收端必须是版本化单轮报告 `phonefarm/confirmation_observation@1`、source=phonefarm、status=VALID_OBSERVATION。单轮 `qualification.stage=experiment`、`eligible=false`，不作统计结论；来源明确 live_device/recorded_device。独立 `sample_qualification` 的 schema 为 `phonefarm/confirmation_sample_qualification@1`，valid_observation=true，baseline_admission_sha256 与计划相等，gates 中 root_verified、control_verified、cold_verified、trace_window_verified、workload_succeeded、restore_verified、baseline_admission_verified 必须全部为布尔 true。这些 gate 应由 receiver 从原始 bundle 重建，不能由候选自行填报。现有完整 A/B 的 conclusion/eligible 报告即使贴 single_run binding 仍拒绝；单轮有效性与完整确认集的统计裁决分开。

每臂还必须包含 `measurement_window`：schema=`phonefarm/measurement_window@1`，
clock_scope=`capture_metadata_and_env_proc_uptime`，device_serial/boot_id，
capture_start_uptime_s/capture_end_uptime_s、env_start_uptime_s/env_end_uptime_s、
env_duration_s/env_interval_s。这些设备时间均来自 `/proc/uptime` 记录声明：有限、
非负、start<end，ENV 覆盖 capture，采样 duration 及实际 ENV 跨度不得大于 host
单轮时长；不能把 ftrace event clock 当成 uptime。按冻结顺序检查所有臂：serial/boot
不变，整 ENV 窗及 capture 窗分别严格不重叠、不可倒退。raw hash 不同不足以证明
遥测窗独立。phonefarm 另外检查原始基线准入窗互斥、确认窗晚于准入窗。上述校验
不认证设备时钟或采集身份，统计独立性仍是预注册实验假设。

qualification_only 或坏类型不可使用；BLOCKED/ABORT 不属于确认数据。必需指标数值/来源缺失不删除指标，不补零。当前仅支持power_watt、fps_p95_ms、frame_p95_ms、fps_mean及其正确方向，数值必须正且有限。功耗要求source=battery_sysfs、rail=battery、discharging=true、window_verified=true；帧代理要求source=raw_ftrace_kgsl、confidence=inferred、semantics=gpu_submit_proxy、display_verified=false。来源声明、sha及时间是完整性检查，不能证明采集真实性或外部注册 chronology。GOL 校验 receiver 报告与冻结绑定，不重新解析准入/采样原始 bundle；receiver 的门禁真实性仍待采集认证与真机验收。合成/未知来源不能确认手机指标。

## 统计与结论

以每对独立运行的B−A差作为样本，统一正方向为更差；百分比用每对正基线归一化，absolute保留原指标单位。声明的假设为独立运行对与近似正态差；绝不把一段运行中的帧当独立样本。若该设计不满足假设，应冻结另一协议，不能用此结果推广。

对m个冻结指标分别使用单侧上置信界 `mean + t(1-alpha/m,n-1)*SE`，采用Bonferroni控制家族误差。上界严格小于最大退化界才支持非劣效；上界严格小于负的最小收益界才支持改善。所有必需门槛成立才PASS，否则INCONCLUSIVE；零方差/浮点退化方差/非有限值/坏数据为BLOCKED。不将p≥alpha或平均值接近0解释为零成本。

算法参考：[NIST 单侧均值置信界](https://itl.nist.gov/div898/handbook/prc/section2/prc221.htm)。分位值实现使用 [statrs StudentsT](https://docs.rs/statrs/0.18.0/statrs/distribution/struct.StudentsT.html)。

任何PASS仅限冻结运行指标及声明统计假设；`real_game_optimization_proven=false`、`promotion_allowed=false` 始终保留。连续质量、真实来源、display/present、作用点及恢复证据需要其对应模块，未在此离线入口自行补出。

示例 [confirmation_plan.json](../examples/confirmation_plan.json) 的hash仅为占位，必须替换为实际工件hash。本工具不采集、不会生成合格单轮receiver绑定；与phonefarm采样driver接通和真机验收仍由#18追踪。
