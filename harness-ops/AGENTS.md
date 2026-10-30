# AGENTS.md

## 代码仓库只放代码

见根目录 [AGENTS.md](../AGENTS.md)：提交、文档、测试与部署规则对全仓库统一。

## 产品方向与提交

- 面向外部 Agent 的远程真机研究平台；网页与 CLI/API 同等优先，GOL 是可选客户端。
- 目标、方向、实验、证据和异步人工反馈独立于聊天工具；一个方向等人时其他方向继续。
- 产品准则与计划：https://github.com/HGamey/docs/tree/main/plans/2026-10-22-agent-platform 。维护者接续时先读准则、讨论、todo，并持续记录决定与验证范围。
- 每批围绕一个可验收变化；提交与部署按根目录 AGENTS.md。
- 用户于2026-10-26要求快速交付、减少校验和防御编程：只运行直接相关的最小检查，不默认本地全套或重复CI；共享协议的必要回归交由必需CI。模拟节点通过不能写成真机验证通过。
- 测试聚焦关键合同和已发生故障，避免镜像实现或重复覆盖。优先简化状态与资源所有权；哈希用于传输完整性/内容标识，不对每层对象重复计算。

## 作业结束范围

新灰层session要求配置PF声明gray-control-gateway@1，沿唯一启动/显式刷新程序缓存核兼容、拒新exe替换并在dispatch复核。旧ID优先找回，旧status/receipt/stop/recovery不受新建门禁影响；声明不授予owner动作或设备资格。Web/CLI同级，合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/BUSINESS-COMPATIBILITY.md 。

results.cross-compare-v1从中心结果索引读取详情，每列显式选择基线，按result_id去重，复用原登记值算术；execution_node_id保留实际执行出处，原件归集后经中心读取。字段一致不等于同实验，旧比较/研究revision不升级。Web/CLI同级。

可选 host_slots 与管理员 --host-capacity 在原队列 SQLite 中原子计账；CPU/GPU/IO 是声明调度容量，不是操作系统硬限制。仅claimed扣运行名额和slots；worker结束而原进程组未知的blocked保留原排他范围，已确认结束done释放全部。waiting与claim共用冲突判断；恢复required和保存错误不另占资源。旧请求省略保持原内容并按cpu1/gpu0/io0计账，恢复任务不借祖先计算容量。Web/CLI同级。

可选 time_budget 由独立 worker 从实际命令启动后用单调时钟执行，复用原取消与升级链。排队不计时，HTTP重启不重置；期限触发与实际退出、设备恢复分别记录。旧请求省略保持原幂等内容，恢复任务暂不支持；Web/CLI同级，CLI可信exit0且期限触发返回124、未知125优先。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-TIME-BUDGET.md 。

活跃owner在管理记录读写失败后保留内存停止意图与原信号时钟；已有取消模式不因保存失败增强，无已知意图时以owner_record_error/escalate停止。execution.stop_intent仅为最近成功保存观察，实际信号仍看cancel.signals；最终保存/域空/设备恢复边界不变，旧固定prefix需新安装。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-OWNER-RECORD-FAILURE.md 。

显式 device_binding 支持固定 PF 灰层 session、声明该能力的 typed game-deploy 和 remote owner，来源为已 pin 节点的 devices-resolve。远控 acquire 保存原 binding，恢复不覆盖；独立设备可并行。
显式 serial 的请求按单设备范围，未声明设备的才是 global。设备范围、排队与恢复
共用 PF 的缓存 device_scopes；不同设备并行，同机别名与全域仍冲突。登记 revision 只用于管理 CAS，不是运行准入；恢复沿原绑定，
不从新登记选另一连接。网页与 CLI 同级；隔离宿主验证不能写成真机或部署已完成。
合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/DEVICE-LANES.md 。

独立worker在直接命令退出后清理同组残留（TERM→KILL），再用ps核查原进程组活成员，zombie不算执行中；dispatcher在worker退出后刷新旧的present/unknown，确认组消失即自动释放，真实退出码和恢复结果不改。每轮共享一次组快照，仅blocked时仍继续观察；已done不重新探测旧PGID。Web与CLI展示同一owned_process_group记录，不能将absent解释为setsid脱组后代或设备恢复。合同与验收范围见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-GROUP-COMPLETION.md 。

请求可显式声明execution_policy；当前仅process_group可用，contained在上传/准备/启动前拒绝，不按OS推断支持。旧请求省略字段保持原幂等内容；请求策略与worker实际process_management分别记录，缺证据不回填。Web/CLI/API必须同步准入与范围。节点启用containment仍待生产权限与恢复边界验收，见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-CONTAINMENT.md 。

job_cgroup提供仅供管理员隔离调用的Linux特权worker后端，公共node能力仍不可用；不得给HTTP服务提权代替生产启动边界。配置使用空的专用cgroup根与专用非root身份；作业只在入域确认后放行，域空后封存job根再完成。未确认的启动门/遗留域阻断身份复用，worker丢失不宣称自动回收。真实内核验收由独立cgroup-kernel CI承担，普通协议测试不改系统cgroup。

install_worker仅由管理员显式安装到新的root-owned固定目录，输出sudo策略但不启用；不得提权执行node自更新树。固定入口只收job ID、有限reconcile/force选择及runner锁FD，拒绝非contained执行与旧job重放；管理JSON归node，输出在域空封存后交还。跨UID PF实时会话、失联恢复与实际部署尚未验收，public contained保持不可用。

## 作业保存边界

games.write-limits-v1覆盖资源URL/import/upload实际partial写入：声明size写前核验、FD空间复核和短写计账，沿原SHA锁/同步/校验发布。失败保本次前缀，原200忽略Range从零重来仍是明确例外；已接受字节不等于已同步offset。Web/CLI共用政策与错误，不自动续写。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/ASSET-WRITE-LIMITS.md 。

jobs.export-limits-v1只约束Node目录导出缓存生成：有界树预检/归档、同job稳定非阻塞flock、gzip实际写入字节与空间检查。失败清本次tmp，不删源或旧cache；发布后的元数据故障不伪称双文件事务。Web显式准备、CLI不自动重压；累计缓存/producer硬限额另列。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-EXPORT-LIMITS.md 。

可选disk_guard由实际worker在日志inode/job/out目录FD上预检和限频观察，达到日志长度/共享空间阈值或观察失败时沿原owner请求停止，不是任意输出硬配额。Web/CLI仅显示最后保存观察；旧请求省略不回填，恢复任务不继承，旧固定prefix缺显式cap在launch写入前拒绝。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-DISK-GUARD.md 。

jobs.input-limits-v1 只约束 Node 准备的输入文件：tar、同节点引用和派生 JSON 共用载荷预算，完整 gzip 流和命名空间另有限额。普通 file/dir 归档先预检再经 SafeRoot 写入，链接/稀疏/特殊成员拒绝；旧请求 ID 不重准备。input_preparation 保存采用限值、成功写入字节和失败原因；空间预检不是运行期配额，不删除失败 partial。Web/CLI同级，合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-INPUT-WRITE-LIMITS.md 。

job 的 status_revision 只覆盖 committed_status 小对象；原 SQLite 同事务保存状态、版本和事件，GET 不补历史。独立 worker 离线阶段可合并，dispatcher恢复后提交已观察事实；实时 worker/lease/waiting/controls 仍须独立读取。事件 cursor、列表 cursor、日志字节 offset 分开，不能以版本未变跳过安全资格核查。合同：https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-STATE-EVENTS.md 。

关键结束记录在worker释放flock后同步文件和目录，保存错误通过storage_error及SQL事件公开，不把已结束任务伪装running或另扣资源；重新保存不重放命令，SQL提交失败仍报错。准备失败/取消的未执行依据由SQL记录，查询不得通过缺文件猜执行结果。范围不含任意producer输出的断电持久性。

显式--worker-prefix只选择独立 contained worker 的可信安装和 execution reconcile 入口，不再配置 PF 路径或共享组。普通 process_group 的 lease-receipts 由 Node 创建；finished 后的 session 查询读取原输出。

PF 与 Node 使用同 UID 的普通输出和 lease-receipts；跨 UID/GID 的 PF 共享 profile 已退役。独立 contained worker 的 cgroup 隔离、私有输出交还和 HTTP 路径边界保留。

显式execution reconcile仅由声明能力的固定安装提供，普通核查不杀进程；force只作用于核实boot/inode的绑定cgroup。保存handoff义务后才能改输出身份，下一次复用UID先核持久记录。宿主核查完成保留原lost/outcome与设备恢复义务；contained的reset/session-recover共用宿主资格，不能以lease clear代替域空和私有输出交还。跨boot判断模型不等于真实主机重启验收。

普通作业只用reset：负责人或管理员可停止活任务、明确释放未知范围、重试保存；重复调用返回原释放事实，不重放命令，不把设备恢复写成成功。运行中复用cancel的INT→15s→TERM→15s→KILL；等待时不占intake。恢复任务按普通范围/优先级排队，不借祖先预留；来源原件和恢复资格读同一done事实（含显式reset）。独立contained后端仍要求域空和私有输出交还。

签到、页面与CLI按queue.state展示占用；blocked提示reset，done保存错误提示修盘后reset重存，done且recovery.required按已有签到游标提示设备还原，不再要求二次释放。版本不同只记录版本事实，不能据此推断部署中断；旧原件和失败历史保留。
