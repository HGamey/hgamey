# 云端出方案（一页说明）

## 是什么

一次真机评测要 10–20 分钟，手机只有一台，所以出方案不该再占开发机或手机节点。
「云端出方案」就是在 GitHub Actions 里用 pi + Kimi 出候选改动，需要时经手机节点的 `/mcp` 只读查一眼手机。
对 harness 来说它只是又一个 proposer@1 外部进程：判定、白名单校验、上机都还在原来的流水线里。

## 链路

```
节点上的 optimize / goal run
   │  proposer@1 输入 (设备能力, 白名单, 历史判定, 人的指令 steer)
   ▼
cloud_actions.py ──gh workflow run──► GitHub Actions runner
   ▲                                   ├─ 配 Kimi：最多 3 个账号，403 额度用完或 429 限流就换下一个
   │                                   ├─ pi + Kimi：先经节点 /mcp (只读 token) 调 phonefarm_devices 确认手机在线
   │                                   └─ 出 1–3 个候选 + 用量，上传为 artifact
   └──gh run download── 候选 ──► harness 校验 (白名单 / 个数) ──► (可选) Jev 守门 ──► 真机 A/B 判定
```

用法：`optimize --proposer cmd:tools/proposers/cloud_actions.py`，或在 goal.md 里写 `proposer: cloud`。

## 已验证（2026-10-11）

- 整条链路跑通（Actions run 38193858130）：pi 调 `phonefarm_devices` 成功，出了 3 个候选，都在白名单里，没有重复历史上真机 REJECT 过的候选，退出码 0。
- 多账号轮换：前两个账号额度用完时，自动换到第 3 个。
- 人的指令会被听：给「只试总线频率，别动 CPU」，3 个候选全是 DDR/LLCC。
- 离线评测 18 个候选：全部在白名单内，不超个数（`docs/evidence/proposer_eval_pi_kimi_20261011.json`）。
- **这些候选的真机效果还没测。**

## 成本（每一代、每一层，出 3 个候选）

用 `pi --mode json` 的事件流统计，本机 5 次实测，开着 `/mcp` 工具：

| 项 | 实测 |
|---|---|
| 大模型调用 | 2 次（一次调工具查手机，一次出方案） |
| 输入 token | 6.0k–7.4k，其中命中缓存 2.6k–5.8k |
| 输出 token | 0.7k–1.6k |
| 用时 | pi 本身 21–46 秒；经 Actions 来回约 90 秒（触发、装 pi、取回结果） |
| 上机前守门 Jev（可选） | 每个候选 1 次，约 0.4k token，约 3 秒 |

对照：一个候选上真机 10–20 分钟。出方案的时间和 token 相对真机时间可以忽略，瓶颈仍然在手机。
用量会写进 report.json 的 `proposer.model_calls` / `proposer.usage`（外部出方案在输出里自报 `usage`）。

## 安全边界

- 节点 token 是只读的：只能调 `/mcp` 的只读工具，碰设备的 `run` / `benchmark` / `script`、`/jobs`、`/admin` 都会被拒。
- 触发 Actions 的一侧用环境变量 `GH_TOKEN`（只对本仓库、只能触发 workflow 和读 artifact 的受限 token），节点上不放 gh 登录态。
  这个 token 还没建，**目前节点上不能用 `proposer: cloud`**（goal run 会在冻结前拒绝）。第一次正式目标运行用 `proposer: llm`。
- 云端只决定「改什么」；白名单校验、冻结的判定规则、还原现场都在 harness 和 phonefarm 里，云端碰不到。

## 还没做的

- 受限 token 由 owner 在 GitHub 上建好，并配到节点的环境里。
- 云端出的候选还没在真机上评测过，「云端 vs 本地大模型 vs 本地变异器」谁更会出方案还没有数据。
