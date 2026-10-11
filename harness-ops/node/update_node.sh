#!/bin/zsh
# 节点自更新（作为 /jobs 的 update_node 任务运行，cwd = ~/node）：
#   update_node.sh <上传目录> ；上传目录里有 phonefarm/、game_opt_loop/、harness-ops/ 中的任意几个，
#   逐个 rsync 到 ~/node/ 下同名目录。不删节点上的运行数据（_jobs、runs、证据）。
set -eu
src=$1
for d in phonefarm game_opt_loop harness-ops; do
  [ -d "$src/$d" ] || continue
  rsync -a "$src/$d/" "$HOME/node/$d/"
  echo "updated ~/node/$d"
done
[ -f "$HOME/node/phonefarm/phonefarm" ] && codesign --force --sign - "$HOME/node/phonefarm/phonefarm" 2>/dev/null || true
[ -f "$HOME/node/game_opt_loop/game_opt_loop" ] && codesign --force --sign - "$HOME/node/game_opt_loop/game_opt_loop" 2>/dev/null || true
echo "节点服务本身的改动要重启服务才生效（harness-ops/node_service.py）"
