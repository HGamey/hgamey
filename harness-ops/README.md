# harness-ops

外部 Agent 使用远程真机的节点服务、客户端和网页；研究任务可独立于内置优化 Agent。

| 路径 | 作用 |
|---|---|
| `node/node_service.py` | 节点上的 HTTP 服务：接任务、跑 phonefarm / game_opt_loop、回传输出 |
| `node/pf-node` | 开发机上的客户端，把命令派到一台节点执行 |
| `node/pf-fleet` | 跨节点调度：查看所有节点状态，派到空闲节点，宕机后迁移续跑 |
| `node/pf-node-deploy`、`node/update_node.sh` | 让节点从 GitHub 拉代码、编译、更新 |
| `node/dash.py`、`node/dash_goals.py`、`node/dash_static/` | 节点自带的只读实时监控面板（/dash） |
| `node/node_remote.py`、`skills/hgamey-remote/` | 远程调试：个人操作员令牌接管设备（HTTP `/remote`、MCP `/remote/mcp`、网页 `/dash/control`），用法见 skill |
| `node/run_node.sh` | 在节点上前台运行节点服务和 cloudflared 隧道 |
| `cloudflare/offline/` | Cloudflare Worker：节点连不上时返回离线提示页 |

## 常用命令

```bash
node/pf-fleet status                                   # 所有节点在线情况、设备锁、手机清单
node/pf-fleet run --any-free -- phonefarm eval ...     # 派到一台空闲节点
node/pf-node devlock status                            # 查看某节点的设备锁
node/pf-node node                                      # 节点身份、版本、执行宿主和数据位置
node/pf-node jobs --query Doom                         # 任意任务检索、分页，含失败与历史任务
node/pf-node artifacts <job-id>                        # 列举输出；用 out <job-id> <path> 下载
node/pf-node-deploy --restart                          # 更新节点代码并重启服务
```

节点令牌放在 `~/.config/hgamey/node.token` 或 `.hgamey/node.token`，不进仓库。
只读 token 可查询 `/node`、`/jobs`、日志和输出；网页使用查看密码打开 `/dash/jobs`，不触发手机采样。
服务可用 `--data-dir` 指定独立数据根（`jobs/`）；不指定时继续读写原 `phonefarm/_jobs`，不自动迁移。

## 文档

长期文档放 [HGamey/docs](https://github.com/HGamey/docs)。本仓库只放代码，规则见 [AGENTS.md](AGENTS.md)。
