#!/bin/sh
# 在手机节点上前台运行：节点服务（崩了/被 /admin/restart 后自动重新拉起）+ cloudflared 隧道。
# 用法（在节点上）: nohup ~/node/harness-ops/run_node.sh > ~/node/run_node.log 2>&1 &
export PATH=/opt/homebrew/bin:$PATH
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
exec "${HG_NODE_PYTHON:-python3}" "$SCRIPT_DIR/node_launch.py" "$@"
