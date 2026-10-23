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

独立worker在直接命令退出后有界核查原进程组；仍存在或核查未知时保留真实命令退出码，但作业为interrupted/recovery.required，调度保留声明资源。Web与CLI展示同一owned_process_group记录，不能将absent解释为setsid脱组后代或设备恢复。合同与验收范围见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-GROUP-COMPLETION.md 。

请求可显式声明execution_policy；当前仅process_group可用，contained在上传/准备/启动前拒绝，不按OS推断支持。旧请求省略字段保持原幂等内容；请求策略与worker实际process_management分别记录，缺证据不回填。Web/CLI/API必须同步准入与范围。节点启用containment仍待生产权限与恢复边界验收，见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-CONTAINMENT.md 。

job_cgroup提供仅供管理员隔离调用的Linux特权worker后端，公共node能力仍不可用；不得给HTTP服务提权代替生产启动边界。配置使用空的专用cgroup根与专用非root身份；作业只在入域确认后放行，域空后封存job根再完成。未确认的启动门/遗留域阻断身份复用，worker丢失不宣称自动回收。真实内核验收由独立cgroup-kernel CI承担，普通协议测试不改系统cgroup。

install_worker仅由管理员显式安装到新的root-owned固定目录，输出sudo策略但不启用；不得提权执行node自更新树。固定入口只收job ID、有限reconcile/force选择及runner锁FD，拒绝非contained执行与旧job重放；管理JSON归node，输出在域空封存后交还。跨UID PF实时会话、失联恢复与实际部署尚未验收，public contained保持不可用。

## 作业保存边界

关键结束记录在worker释放flock、文件和目录同步后才提交SQLite终态。保存失败只保留该job资源并公开storage_blocked；retry-finalize仅重新保存，不重放命令，普通release不得绕过。准备失败/取消的未执行依据由SQL记录，查询不得通过缺文件猜执行结果。范围不含任意producer输出的断电持久性；合同见 https://github.com/HGamey/docs/blob/main/plans/2026-10-22-agent-platform/JOB-DURABILITY.md 。

显式--worker-prefix让node lease/session reader共用可信安装中的PF路径与共享组配置，不开启contained准入。contained的lease-receipts由worker核空out后创建；普通process_group保留原准备流程。finished后的session查询按封存后的私有输出权限读取；配置不含服务凭据。

显式PF共享profile也用于process_group的新out和lease-receipts，创建时设置同组2770；旧queued私有输出在启动前拒绝，不在线改权。未配置profile时仍使用原私有目录。

显式execution reconcile仅由声明能力的固定安装提供，普通核查不杀进程；force只作用于核实boot/inode的绑定cgroup。保存handoff义务后才能改输出身份，下一次复用UID先核持久记录。宿主核查完成保留原lost/outcome与设备恢复义务；queue-release/session-recover共用宿主资格，不能以lease clear代替域空和私有输出交还。跨boot判断模型不等于真实主机重启验收。
