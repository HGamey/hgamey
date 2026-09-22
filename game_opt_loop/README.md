# game_opt_loop

移动游戏自主优化与画质跃升闭环 (Rust 原生)

> **让游戏的性能与画面表现，如同魔法一般跃升。**
> 游戏内部降载到 720p 渲染（省下 GPU 算力与发热），在送显前用一个极轻的 Compute Shader
> 就地重构成锐利的 1080p 画面 —— 全程不离开显存。算子由大模型在真机物理约束下自主变异演化。

**纯 Rust 单二进制**：GLSL 前端与 SPIR-V 后端都走 [`naga`](https://crates.io/crates/naga)，
不依赖 glslang / shaderc / Python / Shell。

---

## 📌 快速导航

| 文档 | 是什么 | 什么时候读 |
|---|---|---|
| **[DESIGN.md](DESIGN.md)** | 系统架构、赛道硬约束、数据流与验收标准 | **开展具体开发前必读** |
| **[contracts/](contracts/)** | 算子、评测请求、判定报告、优胜档案的 JSON Schema | **模块间联调** |
| **[src/tracks/](src/tracks/)** | 两条赛道的零拷贝槽位契约与预算 | **新增或调试算法时** |

---

## 架构分工：与 phonefarm 的边界

- **phonefarm（外部 Rust 物理底座）**：真机硬件的脏活累活 —— adb/hdc 通信、外挂风扇控温冷机基准、
  60s 确定性脚本回放、SmartPerf 功耗与帧时采样、冻结规则两样本 t 检验 (p < 0.01)、100% 原子还原。
- **game_opt_loop（本仓库）**：算法进化与管线编排。

---

## 跑起来

```bash
# 1. 防爆门禁自检：红队语料拦截率 + 合规算子放行率
cargo run -- --self-check

# 2. 离线冒烟：不连真机、不联网，跑通完整多代闭环
cargo run -- --track sr --generations 3 --children 4 --dry-run --no-llm

# 3. 完整 dry-run：接大模型变异，指标仍由静态成本模型估计
#    （一次多算子变异请求要几分钟，属正常）
cargo run -- --track sr --generations 3 --children 4 --dry-run

# 4. 把一份可疑产物（裸 GLSL 或 LLM 原始回包）丢进门禁看判定
cargo run -- --gate-file /path/to/reply.md --track sr

# 5. 全部测试
cargo test
```

产物：`runs/evo/archive.json`（Pareto 优胜档案）、`runs/evo/gen_N.json`（逐代记录）、
`runs/evo/candidates/*.comp`（**只有过了门禁的算子才落盘**）、`evidence/M0_report.json`（验收报告）。

---

## 防爆编译门禁：四道闸

非法算子在本地就被打回，**绝不推入真机**。

| 闸 | 查什么 | 由谁执行 |
|---|---|---|
| **Source** | `#version 450/460` 标头、工作组声明、源码规模 | 本仓库 |
| **Parse** | GLSL 语法、类型、`readonly`/`writeonly` 违规、递归 | naga `glsl-in` |
| **Policy** | 零拷贝 binding 契约、**循环有界性**、barrier 一致性、写回越界守卫 | 本仓库 |
| **Codegen** | 真吐出 SPIR-V 二进制 —— 能落地成设备字节码才算数 | naga `spv-out` |

### 为什么 Policy 这一闸必须自己写

naga 只管「这段 GLSL 合不合法」。`while(true)`、`for(;;)`、`do{}while(true)`
**全部能通过它的 parse + validate** —— 而这类算子在真机上会把合成线程挂死。

所以循环有界性由本仓库在 naga IR 上做**归纳变量分析**自己证：
只有能同时给出「常量初值 + 常量步长 + 常量上界」三件套的循环才放行。
证不出来的一律打回，包括上界来自 `imageSize()` 这类运行期值的。
保守方向是对的 —— 宁可错杀一个能跑的算子，不可放过一个挂死真机的算子。

字符串匹配式的门禁在这里一定漏：`while (1 > 0)` 源码里没有 `while(true)` 字样，
但它就是死循环。只有在 IR 上做常量求值才抓得到。

### 零拷贝是可执行的静态契约，不是口号

前车之鉴：离线神经网络路线里，模型算力只要 2.9ms，但画面在内存与显存之间
**拷来拷去花了 7ms**，直接吃掉 60fps 预算的 60%。

所以赛道契约把允许的资源钉死成一张表：只准声明表里那几个 storage image，
变量名都不许改（合成层按名字挂载）。声明 SSBO、多一个 binding、或者改了槽位名字，
一律 `NON_ZERO_COPY_RESOURCE` / `UNKNOWN_BINDING` / `BINDING_NAME_MISMATCH` 打回。
片上 `shared` 与 `push_constant` 允许（不离开 GPU），但有容量上限。

### 开销统计必须跟着函数调用走

实测大模型很爱把采样抽成 `vec4 fetchTexel(...) { return imageLoad(...); }`，
再在 4×4 循环里调。逐函数各算一遍的话，这种算子的 **21 次访存会被记成 1 次**，
成本模型直接低估一个数量级。所以调用点要带着所在循环的迭代倍率递归进被调函数。

---

## 演化：一代的状态机

```text
  选亲本 (档案 Pareto 前沿 + 拥挤距离)
       ↓
  变异 (LLM 优先，失败降级到本地确定性变异器)
       ↓
  去重 (指纹：折叠注释与空白后的 sha256)
       ↓
  防爆门禁 ← 打回的绝不上真机
       ↓
  评测 (真机 phonefarm  或  dry-run 静态成本模型)
       ↓
  归档 (三目标 Pareto 支配排序，原子写)
```

**三目标**：`[延迟 ms ↓, 整机功耗 W ↓, 画质 dB ↑]`。

**PSNR 既是约束也是目标，但作用位置不同**：低于基线的个体在排序之前就被剔除
（它是不合格品，不是"差一点的解"），否则一个 0.1ms 但糊成一片的算子会永远霸占前沿一角，
还会把变异器往牺牲画质换帧率的方向带。合格品之间，更高的 PSNR 才值得用延迟去换。

**LLM 挂了整代照样推进**：闭环零人工介入不能建立在某个外部服务一定可用之上。
本地确定性变异器是兜底通道，它产出的每个算子都必须能过自家门禁（有测试钉死这条）。

---

## ⚠️ 数据来源：估计值与实测值绝不混淆

每一条评测记录都带 `source` 字段：

| source | 含义 | 能用来做什么 |
|---|---|---|
| `phonefarm` | 真机物理采样 + 冻结规则 t 检验 | 可作为性能结论 |
| `static_cost_model` | 本地静态估计 | **只能**用于本地排序与冒烟 |

`--dry-run` 产出的 latency / power / psnr **全部是静态估计值，不是测量值**，
也不带统计显著性（`p_value` 固定记 `1.0` 这个"未检验"哨兵，不伪造漂亮的小 p 值）。
命令行输出与 `evidence/M0_report.json` 都会显式声明这一点。

---

## 当前状态

**已验证（纯本地，可复现）**

- 防爆门禁：16 条红队语料 **16/16 拦下且拦对原因**；48 个合规算子 **48/48 放行**；单次门禁 < 10 ms。
- LLM 通道：对 glm-4.7 真实产出的 4 个候选算子做过端到端校验，**4/4 过门禁并成功生成 SPIR-V**。
- 多代闭环：固定种子可复现，档案无重复指纹，前沿内部互不支配，累计最优画质单调不倒退。
- 契约一致性：Rust 结构体序列化结果对照 `contracts/*.schema.json` 逐字段校验。

**未完成（真机数字还拿不到）**

- **M1 阻塞在 phonefarm 侧**：本仓库的驱动、评测请求契约 (`contracts/eval_request.schema.json`)
  与回包解析都已就绪，但 `phonefarm` 目前**没有算子评测子命令**（`phonefarm gpu-op` 返回"未知子命令"）。
  需要 phonefarm 实现接收端：冷机 → 挂载算子 → 60s 回放 → A/B/A/B → t 检验 → 回 `eval_report`。
  设备侧是通的（`phonefarm devices` 能看到在线机器），接上就能跑。
- **零拷贝挂载点**尚未接入真实合成层（RenderService / DPU 后处理插槽）。
  在此之前，所有 latency / power / psnr 都只有成本模型的估计值。
