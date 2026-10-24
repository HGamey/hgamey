# harness-ops

外部 Agent 使用远程真机的节点服务、客户端和网页；研究任务可独立于内置优化 Agent。

| 路径 | 作用 |
|---|---|
| `node/node_service.py` | 节点上的 HTTP 服务：接任务、跑 phonefarm / game_opt_loop、回传输出 |
| `node/pf-node` | 开发机上的客户端，把命令派到一台节点执行 |
| `node/pf-fleet` | 共享节点目录；显式 legacy 注册表可选空闲节点，迁移入口暂不支持 |
| `node/pf-node-deploy`、`node/update_node.sh` | 让节点从 GitHub 拉代码、编译、更新 |
| `node/dash.py`、`node/dash_goals.py`、`node/dash_static/` | 网页任务、游戏、节点目录与控制；查看和操作分别授权 |
| `node/node_remote.py`、`skills/hgamey-remote/` | 远程调试：个人操作员令牌接管设备（HTTP `/remote`、MCP `/remote/mcp`、网页 `/dash/control`），用法见 skill |
| `node/run_node.sh` | 在节点上前台运行节点服务和 cloudflared 隧道 |
| `cloudflare/offline/` | Cloudflare Worker：节点连不上时返回离线提示页 |

## 常用命令

```bash
export PF_NODE_URL=https://node.example.invalid
export PF_NODE_TOKEN_FILE=/private/path/node.token     # 显式文件，不搜索其他目录
node/pf-node node                                      # 只读取得 node_id、宿主和数据位置
export PF_NODE_ID='填入上一步的node_id'                  # 后续写入和设备采样必须绑定
node/pf-fleet --registry /private/path/nodes.json status # 显式 legacy 目录；也可设置 PF_FLEET_REGISTRY
node/pf-fleet --registry /private/path/nodes.json run --any-free -- phonefarm eval ...
node/pf-node leases                                    # 只读设备所有权与恢复事实
node/pf-node jobs --query Doom                         # 任意任务检索、分页，含失败与历史任务
node/pf-node artifacts <job-id>                        # 列举输出；用 out <job-id> <path> 下载
node/pf-node-deploy --restart                          # 更新节点代码并重启服务
```

节点 URL/凭据必须显式设置；TOKEN 与 TOKEN_FILE 环境变量不可同时设置，`--token-file` 可显式覆盖。凭据不进仓库。
新客户端拒绝向缺少 `node.target-v1` 的旧节点写入；原 ID 续查保留 URL/node_id，网页旧未绑定请求不会自动转投。
只读 token 可查询 `/node`、`/jobs`、日志和输出；网页使用查看密码打开 `/dash/jobs`，不触发手机采样。
服务可用 `--data-dir` 指定独立数据根（`jobs/`）；不指定时继续读写原 `phonefarm/_jobs`，不自动迁移。

## 文档

长期文档放 [HGamey/docs](https://github.com/HGamey/docs)。本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
