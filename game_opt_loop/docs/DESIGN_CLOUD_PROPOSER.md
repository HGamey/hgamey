# 阶段 3 提效：云端出方案 + 上机前守门（设计与最小实现）

## 为什么

一次真机评测（③ 一个候选）大约要 22 分钟，手机只有一台。阶段 3 的瓶颈是真机时间，不是出方案的算力。
所以提效从两头做：

1. **云端出方案**：出方案不占开发机、也不占节点。在 GitHub Actions 里跑 pi + Kimi，需要设备现状时
   经节点的 `/mcp` 只读查。它对 harness 来说只是又一个 `proposer@1` 外部进程。
2. **上机前守门**：候选过了 harness 校验、上真机之前，先问一个便宜的判断（TypeSafe Jev，
   一次约 0.1–3 秒、几百 token）「值不值得花这 22 分钟」。挡掉明显重复或明显没指望的候选，
   把真机时间留给值得的那些。

两件事都不碰判定：判定仍然在 phonefarm 里，用冻结的规则做。

## 1. 上机前守门（已实现，`src/optimize/gate.rs`）

```
出方案的一方 ──► harness 校验 ──► 守门 Jev (每个候选问一次) ──► p ≥ 阈值: 上真机
                                                          └─► p < 阈值: 记进「本地打回」，不上机
```

- 用法：`optimize ... --gate jev [--gate-threshold 0.3] [--gate-shadow] [--gate-key-file <path>]`。
  缺省不开，不加这些参数时行为和以前一样。
- 问法：System One 接口 `POST https://api.typesafe.ai/v1/systemone`，bool 问题的线上类型是 `noul`，
  回包 `answers.worth_device_time.noul` 就是 p(值得上机)。给它看的 state 只取出方案输入里已有的东西：
  设备、层、判定口径、这个候选改了什么和理由、这一层最近 12 条上过机的候选与真机结论。
- 只问过得了 harness 校验的候选：校验本来就会挡掉的，不必花守门的钱。
- **影子模式** `--gate-shadow`：只问、只记、不挡。每个判断（候选、p、动作）都写进 report.json 的
  `proposer.gate`。等真机结论回来，就能算出守门准不准：被判「不值得」的候选里，有几个其实
  PASS 了。**建议先用影子模式攒数据，再决定开不开挡、阈值定多少**。
- 守门出错（网络、key、回包格式）时一律放行，原因写进证据。守门坏了不该让流水线停。
- 实现成 `Proposer` 的装饰器：层跑器一行没改；不管谁来出方案，都要过同一道门。
- key：显式的 `--gate-key-file` 优先，其次环境变量 `TYPESAFE_API_KEY`，最后
  `~/.config/hgamey/typesafe.key`。optimize 在节点上跑时节点上没有 key，可以用
  `--gate-key-file <本机路径>`（pf-node 会把本机文件传上去）。**这等于把 key 拷到节点上，
  要不要这么做请 owner 定**。

验证：单测覆盖低于阈值的挡掉、影子模式不挡、守门出错时放行、回包解析、state 内容。真打了一次 Jev：
`GOL_LIVE_JEV=1 cargo test live_jev` 返回 p=0.36。另外手工问过一个「和已被拒候选几乎一样」的候选，
p=0.39。**守门准不准还没有数据**，要靠影子模式攒。

## 2. 云端出方案（最小实现：`tools/proposers/`、`.github/workflows/proposer.yml`）

```
节点上的 optimize ──proposer@1 Input──► cloud_proposer (cmd: 适配器)
                                             │ gh workflow run proposer.yml (输入: gzip+base64 的 Input, nonce)
                                             ▼
                                   GitHub Actions runner
                                     pi -p (Kimi)  ──只读──► 节点 /mcp (phonefarm_fleet / phonefarm_caps ...)
                                     产出 proposer@1 Output，本地按契约先校验一遍
                                     上传为 artifact proposer-<nonce>
                                             │
cloud_proposer ◄── gh run download ──────────┘ ──stdout──► harness 校验 / 守门 / 上机
```

- 对 harness 来说它就是 `--proposer cmd:tools/proposers/cloud_actions.py`，契约不变，可以和别的
  出方案方式进对比模式。
- 出方案的逻辑（提示词、Kimi 调用、回包整形）放在一个能在本机直接跑的 `pi_kimi.py` 里；Actions
  只是换个地方跑它。这样本机能调试，云端和本地出的方案同源。
- 延迟：dispatch → runner 起来 → pi 跑完 → artifact 可下载，估计 1–3 分钟，相对 22 分钟的真机评测
  可以接受。超时沿用 `--proposer-timeout-s`。
- `/mcp` 只开只读工具（节点白名单里还有 `phonefarm_run` / `phonefarm_benchmark` /
  `phonefarm_script` 这几个会碰设备的工具，云端出方案**不应该**用）。pi 用 `--tools` 白名单把
  这几个排除掉。
- 失败语义与外部进程一致：出错时这一代不上机，不换成别的出方案方式补位。

### 现状

- `tools/proposers/pi_kimi.py`：出方案本体，本机可直接用 `--proposer cmd:tools/proposers/pi_kimi.py`。
  在本机用 Kimi（kimi-for-coding）对一份手写输入跑过：约 13 秒出 2 个候选，都在白名单内，
  避开了历史上已 REJECT 的方向。开了 `PI_MCP_TOOLS` 时可经节点 `/mcp` 查设备
  （本机验证过 pi 能调到 `phonefarm_devices` 并返回 91253241019A；MCP 服务器在 pi 的 mcp.json 里要设
  `"exposure": "direct"`，不能带 `-ne`）。
- `tools/proposers/cloud_actions.py` + `.github/workflows/proposer.yml`：Actions 那一跳。
  **仓库还没有 secrets，云端整条链路没跑通过**，只能验证到「触发 → 找到 run → 在缺 key 那一步失败 →
  取回结果」。
- 跑 `cloud_actions.py` 的机器（通常是节点）上要有登录了的 `gh`。

### 需要 owner 决定 / 动手的

1. 在 HGamey/game_opt_loop 配 Actions secrets：`KIMI_API_KEY`（或 pi 认的 Kimi 凭据）、
   `PF_NODE_TOKEN`（节点 `/mcp` 的 token）。我没有仓库 secrets 的权限。
2. 节点 token 放进 GitHub 等于让云端 runner 能调节点 `/mcp`，包括白名单里会碰设备的那几个工具。
   要不要给云端单独发一个只读 token，或者在节点上按 token 区分只读白名单。
3. 守门 key 要不要放到节点上（见上）。
