# AGENTS.md

## 代码仓库只放代码

- 不要在本仓库新增或修改说明文档、图片、PPT、PDF、实验报告。
- 这次改了什么、为什么改：写在 commit message 正文。
- 一个功能或修复的说明、验证与限制写进 commit 正文；长期记录归 HGamey/docs。
- 长期文档（设计、规格、架构图、进展汇报）：提交到 https://github.com/HGamey/docs ，本仓库的旧文档在那里的 `repos/<仓库名>/` 下。
- 允许的例外：根目录 README.md（40 行以内）、AGENTS.md、skills/、许可证、程序或测试要读的数据。CI 检查 `code-only` 会拦下其他文档类文件。

## 产品方向与提交

- 面向外部 Agent 的远程真机研究平台；网页与 CLI/API 同等优先，GOL 是可选客户端。
- 目标、方向、实验、证据和异步人工反馈独立于聊天工具；一个方向等人时其他方向继续。
- 产品准则与计划：https://github.com/HGamey/docs/tree/main/plans/2026-10-22-agent-platform 。维护者接续时先读准则、讨论、todo，并持续记录决定与验证范围。
- 用户已授权直接 commit/push，不新开 PR。每批围绕一个可验收变化；提交前核对当前 main，保留同事改动，不 force push。
- main 保留必需的 `code-only` 检查。先推同一提交到 `verify/<批次>` 等 CI 通过，再直接推 main；若 rebase 改变提交需重验。不关闭必需检查或强推保护。
- 修改节点协议后运行 `python3 -m unittest discover -s node/tests -v`。模拟节点通过不能写成真机验证通过。
- 测试聚焦关键合同和已发生故障，避免镜像实现或重复覆盖。优先简化状态与资源所有权；哈希用于传输完整性/内容标识，不对每层对象重复计算。

## 作业结束范围

results.cross-compare-v1从冻结目录目标有界读取详情，沿共享四槽/8秒截止，不代理原件。新schema显式每列node与基线，按(node_id,result_id)去重，复用原登记值算术；字段一致不等于同实验，旧比较/研究revision不升级。Web/CLI同级，合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/RESULT-CROSS-COMPARE.md 。

可选 host_slots 与管理员 --host-capacity 在原队列 SQLite 中原子计账；CPU/GPU/IO 是声明调度容量，不是操作系统硬限制。未知宿主执行保留占位，已有宿主完成收据持久提交后才归还；设备恢复屏障独立。旧请求省略保持原内容并按 cpu1/gpu0/io0 计账，恢复任务不借祖先计算容量。Web/CLI同级，合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-HOST-CAPACITY.md 。

可选 time_budget 由独立 worker 从实际命令启动后用单调时钟执行，复用原取消与升级链。排队不计时，HTTP重启不重置；期限触发与实际退出、设备恢复分别记录。旧请求省略保持原幂等内容，恢复任务暂不支持；Web/CLI同级，CLI可信exit0且期限触发返回124、未知125优先。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-TIME-BUDGET.md 。

活跃owner在管理记录读写失败后保留内存停止意图与原信号时钟；已有取消模式不因保存失败增强，无已知意图时以owner_record_error/escalate停止。execution.stop_intent仅为最近成功保存观察，实际信号仍看cancel.signals；最终保存/域空/设备恢复边界不变，旧固定prefix需新安装。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-OWNER-RECORD-FAILURE.md 。

显式 device_binding 仅支持固定 PF 灰层 session，来源为已 pin 节点的 devices-resolve。
省略字段的旧请求仍 global，不自动回填当前登记。设备范围、排队、恢复与 review release
共用 PF 的缓存 device_scopes；不同设备并行，同机别名与全域仍冲突。恢复沿原绑定，
不从新登记选另一连接。网页与 CLI 同级；隔离宿主验证不能写成真机或部署已完成。
合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/DEVICE-LANES.md 。

独立worker在直接命令退出后有界核查原进程组；仍存在或核查未知时保留真实命令退出码，但作业为interrupted/recovery.required，调度保留声明资源。Web与CLI展示同一owned_process_group记录，不能将absent解释为setsid脱组后代或设备恢复。合同与验收范围见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-GROUP-COMPLETION.md 。

请求可显式声明execution_policy；当前仅process_group可用，contained在上传/准备/启动前拒绝，不按OS推断支持。旧请求省略字段保持原幂等内容；请求策略与worker实际process_management分别记录，缺证据不回填。Web/CLI/API必须同步准入与范围。节点启用containment仍待生产权限与恢复边界验收，见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-CONTAINMENT.md 。

job_cgroup提供仅供管理员隔离调用的Linux特权worker后端，公共node能力仍不可用；不得给HTTP服务提权代替生产启动边界。配置使用空的专用cgroup根与专用非root身份；作业只在入域确认后放行，域空后封存job根再完成。未确认的启动门/遗留域阻断身份复用，worker丢失不宣称自动回收。真实内核验收由独立cgroup-kernel CI承担，普通协议测试不改系统cgroup。

install_worker仅由管理员显式安装到新的root-owned固定目录，输出sudo策略但不启用；不得提权执行node自更新树。固定入口只收job ID、有限reconcile/force选择及runner锁FD，拒绝非contained执行与旧job重放；管理JSON归node，输出在域空封存后交还。跨UID PF实时会话、失联恢复与实际部署尚未验收，public contained保持不可用。

## 作业保存边界

games.write-limits-v1覆盖资源URL/import/upload实际partial写入：声明size写前核验、FD空间复核和短写计账，沿原SHA锁/同步/校验发布。失败保本次前缀，原200忽略Range从零重来仍是明确例外；已接受字节不等于已同步offset。Web/CLI共用政策与错误，不自动续写。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/ASSET-WRITE-LIMITS.md 。

jobs.export-limits-v1只约束Node目录导出缓存生成：有界树预检/归档、同job稳定非阻塞flock、gzip实际写入字节与空间检查。失败清本次tmp，不删源或旧cache；发布后的元数据故障不伪称双文件事务。Web显式准备、CLI不自动重压；累计缓存/producer硬限额另列。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-EXPORT-LIMITS.md 。

可选disk_guard由实际worker在日志inode/job/out目录FD上预检和限频观察，达到日志长度/共享空间阈值或观察失败时沿原owner请求停止，不是任意输出硬配额。Web/CLI仅显示最后保存观察；旧请求省略不回填，恢复任务不继承，旧固定prefix缺显式cap在launch写入前拒绝。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-DISK-GUARD.md 。

jobs.input-limits-v1 只约束 Node 准备的输入文件：tar、同节点引用和派生 JSON 共用载荷预算，完整 gzip 流和命名空间另有限额。普通 file/dir 归档先预检再经 SafeRoot 写入，链接/稀疏/特殊成员拒绝；旧请求 ID 不重准备。input_preparation 保存采用限值、成功写入字节和失败原因；空间预检不是运行期配额，不删除失败 partial。Web/CLI同级，合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-INPUT-WRITE-LIMITS.md 。

job 的 status_revision 只覆盖 committed_status 小对象；原 SQLite 同事务保存状态、版本和事件，GET 不补历史。独立 worker 离线阶段可合并，dispatcher恢复后提交已观察事实；实时 worker/lease/waiting/controls 仍须独立读取。事件 cursor、列表 cursor、日志字节 offset 分开，不能以版本未变跳过安全资格核查。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-STATE-EVENTS.md 。

关键结束记录在worker释放flock、文件和目录同步后才提交SQLite终态。保存失败只保留该job资源并公开storage_blocked；retry-finalize仅重新保存，不重放命令，普通release不得绕过。准备失败/取消的未执行依据由SQL记录，查询不得通过缺文件猜执行结果。范围不含任意producer输出的断电持久性；合同见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-DURABILITY.md 。

显式--worker-prefix让node lease/session reader共用可信安装中的PF路径与共享组配置，不开启contained准入。contained的lease-receipts由worker核空out后创建；普通process_group保留原准备流程。finished后的session查询按封存后的私有输出权限读取；配置不含服务凭据。

显式PF共享profile也用于process_group的新out和lease-receipts，创建时设置同组2770；旧queued私有输出在启动前拒绝，不在线改权。未配置profile时仍使用原私有目录。

显式execution reconcile仅由声明能力的固定安装提供，普通核查不杀进程；force只作用于核实boot/inode的绑定cgroup。保存handoff义务后才能改输出身份，下一次复用UID先核持久记录。宿主核查完成保留原lost/outcome与设备恢复义务；queue-release/session-recover共用宿主资格，不能以lease clear代替域空和私有输出交还。跨boot判断模型不等于真实主机重启验收。
