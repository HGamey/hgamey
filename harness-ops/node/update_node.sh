#!/bin/zsh
# 节点自更新，作为 /jobs 的 update_node 任务运行（cwd = ~/node）。两种用法：
#
#   update_node.sh --git [phonefarm[@ref]] [game_opt_loop[@ref]] [harness-ops[@ref]]
#       节点自己从 GitHub 拉代码、自己编译（缺省 phonefarm 和 game_opt_loop，ref 缺省 origin/main）。
#       开发机上行很慢，大文件一律不从开发机传；部署只发这一条小指令。
#       源码在 ~/node/src/<名字>（独立 clone，不碰 ~/projects），编译目录 ~/node/.target。
#       只把 git 跟踪的文件同步进 ~/node/<名字>，不删节点上的运行数据（_jobs、runs、证据）。
#
#   update_node.sh <上传目录>
#       旧方式：上传目录里的 phonefarm/、game_opt_loop/、harness-ops/ 逐个 rsync 到 ~/node/ 下同名目录。
set -eu
export PATH=/opt/homebrew/bin:$HOME/.cargo/bin:$PATH
typeset -A REPO=(
  phonefarm     git@github.com:BH3GEI/phonefarm.git
  game_opt_loop git@github.com:HGamey/game_opt_loop.git
  harness-ops   git@github.com:HGamey/harness-ops.git
)

sync_tracked() {  # $1 源码目录 $2 目标目录
  git -C $1 ls-files -z | rsync -a --from0 --files-from=- $1/ $2/
}

if [[ ${1:-} == --git ]]; then
  shift
  targets=($@); (( $#targets )) || targets=(phonefarm game_opt_loop)
  mkdir -p ~/node/src
  export CARGO_TARGET_DIR=$HOME/node/.target
  for t in $targets; do
    name=${t%%@*}; ref=origin/main; [[ $t == *@* ]] && ref=${t#*@}
    [[ -n ${REPO[$name]:-} ]] || { echo "未知仓库 $name"; exit 2; }
    src=~/node/src/$name
    [[ -d $src/.git ]] || git clone -q ${REPO[$name]} $src
    git -C $src fetch -q origin
    git -C $src checkout -q --detach $ref
    echo "$name @ $(git -C $src log --oneline -1)"
    case $name in
      phonefarm)
        (cd $src/src && cargo build --release -q)
        sync_tracked $src ~/node/phonefarm
        cp $CARGO_TARGET_DIR/release/phonefarm ~/node/phonefarm/phonefarm
        codesign --force --sign - ~/node/phonefarm/phonefarm 2>/dev/null || true;;
      game_opt_loop)
        (cd $src && cargo build --release -q)
        sync_tracked $src ~/node/game_opt_loop
        cp $CARGO_TARGET_DIR/release/game_opt_loop ~/node/game_opt_loop/game_opt_loop
        codesign --force --sign - ~/node/game_opt_loop/game_opt_loop 2>/dev/null || true;;
      harness-ops)
        rsync -a $src/node/ ~/node/harness-ops/ --exclude __pycache__
        echo "节点服务本身的改动要 /admin/restart 才生效";;
    esac
    echo "updated ~/node/$name"
  done
  exit 0
fi

src=$1
for d in phonefarm game_opt_loop harness-ops; do
  [ -d "$src/$d" ] || continue
  rsync -a "$src/$d/" "$HOME/node/$d/"
  echo "updated ~/node/$d"
done
[ -f "$HOME/node/phonefarm/phonefarm" ] && codesign --force --sign - "$HOME/node/phonefarm/phonefarm" 2>/dev/null || true
[ -f "$HOME/node/game_opt_loop/game_opt_loop" ] && codesign --force --sign - "$HOME/node/game_opt_loop/game_opt_loop" 2>/dev/null || true
echo "节点服务本身的改动要重启服务才生效（harness-ops/node_service.py）"
