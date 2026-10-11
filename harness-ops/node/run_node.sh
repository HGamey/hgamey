#!/bin/zsh
# 在手机节点上前台运行：节点服务（崩了/被 /admin/restart 后自动重新拉起）+ cloudflared 隧道。
# 用法（在节点上）: nohup ~/node/harness-ops/run_node.sh > ~/node/run_node.log 2>&1 &
export PATH=/opt/homebrew/bin:$PATH
export PHONEFARM_NODE_TOKEN=$(cat ~/.config/hgamey/node.token)
cd ~/node
(while true; do /opt/homebrew/bin/python3 ~/node/harness-ops/node_service.py >> ~/node/node_service.log 2>&1; sleep 2; done) &
while true; do ~/node/bin/cloudflared tunnel --no-autoupdate --config ~/.cloudflared/hgamey-node.yml run hgamey-node >> ~/node/cloudflared.log 2>&1; sleep 5; done
