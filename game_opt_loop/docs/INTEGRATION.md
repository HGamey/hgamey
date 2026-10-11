# 第三方接入说明

harness（game_opt_loop + phonefarm）当裁判和流水线：摸底、冻结测试条件与判定口径、
把候选下发到真机、采样、统计判定、还原现场、归档出报告。**谁来出方案**可以换。

本页说明怎么写一个出方案的一方（proposer）。

---

## 1. 写一个出方案的一方（proposer）

### 1.1 它在流水线里的位置

```
optimize ─ 摸底 → 计划 → 逐层逐代:
                          ┌─────────────── harness ───────────────┐
                          │ 组输入 (proposer@1 Input)              │
                          │      │ stdin                           │
                          │      ▼                                 │
                          │  你的进程 ── stdout ──► Output         │
                          │      ▲                    │            │
                          │      │                    ▼            │
                          │      │        harness 校验 (白名单 /    │
                          │      │        开关目录 / 个数 / 字段)   │
                          │      │                    │            │
                          │   历史 + 真机判定 ◄── phonefarm eval     │
                          └────────────────────────────────────────┘
```

一代调一次你的进程。你**只**决定「这一代改什么」：候选的 `kind`、`why`、
`payload.params`（③ 系统参数）或 `payload.rewrites`（② 渲染流程改写）。
设备、负载、测试条件、统计协议、写入计划、白名单副本、`require_hit` 全部由 harness 填；
候选里带了这些字段，这条候选整条打回。判定在 phonefarm，协议在你看到输入之前就冻结了，
所以换谁来出方案都够不着判定。

① 画面算子层目前不走 `optimize`（独立的 `evolve` 赛道），这个接口暂时只覆盖 ③ 和 ②。

### 1.2 怎么接

```bash
game_opt_loop optimize --game 原神 --device <serial> --budget 1h \
    --proposer cmd:/path/to/your_proposer [参数...]   \
    --proposer-timeout-s 600
```

- `--proposer llm`：内置，大模型出方案，挂了退回本地变异器（不写 `--proposer` 时的缺省）。
- `--proposer local`：内置，本地确定性变异器（等于 `--no-llm`），同种子同结果。
- `--proposer cmd:<可执行文件> [参数...]`：外部进程。路径里不能有空格。

外部进程的约定：

| 项 | 约定 |
|---|---|
| 输入 | stdin 一份 JSON（写完即关闭），见 `contracts/proposer_input.schema.json` |
| 输出 | stdout **只能**有一份 JSON，见 `contracts/proposer_output.schema.json`；日志写 stderr |
| 退出码 | 0 = 正常；非 0 = 这一代出错 |
| 超时 | 缺省 600 秒（`--proposer-timeout-s`），超时进程被杀 |
| 环境变量 | `GOL_PROPOSER_PROTOCOL=game_opt_loop/proposer@1` |
| 出错时 | 退出码非 0 / 超时 / 整份解析不了 → 这一代一个候选都不上机，记进报告；**不会**悄悄换成内置的出方案补位（不然对比失真） |

### 1.3 输入里有什么

完整字段见 `contracts/proposer_input.schema.json`，要点：

- `protocol` / `version`：`game_opt_loop/proposer@1` / `1`。认错了协议请退出码非 0。
- `kind`：这一代要哪一层的候选（`sysparam` / `gray`）。回别的 kind 会被打回。
- `generation`、`n`：第几代、这一代最多要几个。多给的不上机。
- `seed`：确定性的实现应当只依赖（输入, seed）。
- `device`：平台、型号、root、能否停充、能否挂 Vulkan 层、温度（`null` = 没探出来，不是「否」）。
- `target`、`plan`：游戏、白盒/黑盒、负载、这一层的路线/代数/每臂轮数/估计耗时。
- `conditions`、`judging`：冻结的测试条件与判定口径（只读，看得到改不了）。
- `constraints`：
  - ③ `whitelist`：参数 id → `{values, current, ...}`。**只能**从 `values` 里原样挑字符串。
  - ③ `deny_keywords`：温控保护关键词，命中即拒。
  - ② `knobs`：改写开关目录，只有 `status = landed` 的能用。
- `history`：这一层之前上过机的候选和真机结论（含 ABORT，`measurement_valid=false`
  表示那一轮没量准，既不是正样本也不是负样本）。

### 1.4 输出长什么样

```json
{
  "protocol": "game_opt_loop/proposer@1",
  "version": 1,
  "proposer": "my-proposer 0.1",
  "candidates": [
    {"kind": "sysparam", "why": "提高 GPU 最低档, 看帧时尾部",
     "payload": {"params": {"gpu.min_pwrlevel": "6"}}},
    {"kind": "gray", "why": "去掉多余的 load",
     "payload": {"rewrites": [{"knob": "loadop_dont_care"}]}}
  ]
}
```

每个候选单独校验，不合格的单独打回（原因进报告的「本地打回」），不连累同一批里合格的。
回空列表是合法的：harness 记「这一代一个可用候选都没提出来」，这一层到此为止。

### 1.5 最小例子

`examples/proposers/fixed_one.py`：只用 Python 标准库，③ 第一代固定给一个候选
（白名单里的一项改成另一个合法值），其余回空。可以照着改。

离线调试不需要手机：每一代的输入和输出都落在 `<out>/proposer/<层>_g<代>.{input,output}.json`，
拿 input 文件直接喂你的进程就能复现：

```bash
./your_proposer < runs/optimize/proposer/sysparam_g1.input.json
```

### 1.6 版本

协议名带版本（`@1`）。加字段不升版本，上层和下层都要对新字段宽容；改已有字段的语义才升。
`tests/contract_conformance.rs` 把 Rust 序列化出来的输入、一份手写的第三方输出与契约两边钉死。

### 1.7 内置实现与接口的关系

内置的 `llm` / `local` 也走同一个接口：harness 组同一份输入，内置实现把它还原成改造前
各层吃的历史格式，出完方案再过同一道校验。`tests/proposer_golden.rs` 用改造**之前**
的代码录下整条链路下发给真机的请求，改造之后两个种子下逐字节一致。
