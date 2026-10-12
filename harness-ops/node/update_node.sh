#!/bin/zsh
# 节点自更新，作为 /jobs 的 update_node 任务运行（cwd = ~/node）。两种用法：
#
#   update_node.sh --git [phonefarm[@ref]] [game_opt_loop[@ref]] [harness-ops[@ref]]
#       节点自己从 GitHub 拉代码（缺省 phonefarm 和 game_opt_loop，ref 缺省 origin/main）。
#       phonefarm 的二进制和灰档层 .so 由 BH3GEI/phonefarm 的 GitHub Actions 云端编译，发在 release
#       node-build；build.json 的 sha 等于要部署的提交就直接下载，不在节点上编译（节点内存/磁盘紧）。
#       部署的正好是 origin/main 最新而 CI 还没跑完，就最多等 10 分钟；仍对不上才退回本地编译。
#       game_opt_loop（私有、很轻）照旧在节点上编译。
#       开发机上行很慢，大文件一律不从开发机传；部署只发这一条小指令。
#       源码在 ~/node/src/<名字>（独立 clone，不碰 ~/projects），编译目录 ~/node/.target。
#       只把 git 跟踪的文件同步进 ~/node/<名字>，不删节点上的运行数据（_jobs、runs、证据）。
#
#   update_node.sh <上传目录>
#       旧方式：上传目录里的 phonefarm/、game_opt_loop/、harness-ops/ 逐个 rsync 到 ~/node/ 下同名目录。
set -eu
# 同一时间只允许一个部署（两个 agent 同时部署会互相覆盖 .new 文件）
LOCKD=$HOME/node/.deploy.lock
for i in {1..60}; do mkdir $LOCKD 2>/dev/null && break; [[ $i == 60 ]] && { echo "另一个部署 10 分钟没结束，放弃"; exit 4; }; echo "等另一个部署结束…"; sleep 10; done
trap 'rmdir $LOCKD 2>/dev/null' EXIT
export PATH=/opt/homebrew/bin:$HOME/.cargo/bin:$PATH
typeset -A REPO=(
  phonefarm     git@github.com:BH3GEI/phonefarm.git
  game_opt_loop git@github.com:HGamey/game_opt_loop.git
  harness-ops   git@github.com:HGamey/harness-ops.git
)

install_bin() {  # $1 新程序 $2 目标：先写临时文件再 mv 整体替换，正在跑的旧进程不受影响
  cp $1 $2.new && { [[ $(file -b $1) != Mach-O* ]] || codesign --force --sign - $2.new 2>/dev/null || true; } && mv -f $2.new $2
}

PF_REL=https://github.com/BH3GEI/phonefarm/releases/download/node-build
PF_WAIT=${PF_WAIT:-600}   # CI 还没出这个提交的产物时最多等多少秒

json_get() {  # $1 build.json $2 python 表达式（j 是解析后的 dict）
  python3 -c 'import json,sys; j=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' $1 $2
}

fetch_prebuilt() {  # $1 提交 sha $2 下载目录。0=下好且校验过 1=release 上不是这个 sha 2=下载/校验失败
  local sha=$1 d=$2 f want
  rm -rf $d && mkdir -p $d
  curl -fsSL --retry 3 --max-time 60 -o $d/build.json "$PF_REL/build.json?t=$(date +%s)" || return 2
  [[ $(json_get $d/build.json 'j["sha"]') == $sha ]] || return 1
  for f in $(json_get $d/build.json '" ".join(j["files"])'); do
    curl -fsSL --retry 3 --max-time 300 -o $d/$f "$PF_REL/$f" || return 2
    want=$(json_get $d/build.json "j['files']['$f']['sha256']")
    # CI 连着两次 push 时文件可能已被下一次覆盖而 build.json 还没换：按 1 处理，过会儿再看
    [[ $(shasum -a 256 $d/$f | cut -d' ' -f1) == $want ]] || { echo "  $f sha256 对不上（CI 正在覆盖？）"; return 1; }
  done
  return 0
}

prebuilt_phonefarm() {  # $1 源码目录 $2 下载目录。成功返回 0，需退回本地编译返回 1
  local src=$1 d=$2 sha main rc waited=0
  sha=$(git -C $src rev-parse HEAD); main=$(git -C $src rev-parse origin/main)
  while :; do
    fetch_prebuilt $sha $d && rc=0 || rc=$?
    (( rc == 0 )) && { echo "  用 CI 产物 $(json_get $d/build.json 'j["built_at"]+"  "+j["run_url"]')"; return 0; }
    (( rc == 2 )) && { echo "  下载 CI 产物失败"; return 1; }
    [[ $sha == $main ]] || { echo "  $sha 不是 origin/main 最新，CI 不会单独编它"; return 1; }
    (( waited >= PF_WAIT )) && { echo "  等了 ${waited}s，node-build 仍是 $(json_get $d/build.json 'j["sha"]')"; return 1; }
    (( waited == 0 )) && echo "  CI 还没出 ${sha:0:7} 的产物，最多等 $((PF_WAIT/60)) 分钟…"
    sleep 30; (( waited += 30 ))
  done
}

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
        pre=~/node/.prebuilt/phonefarm
        if prebuilt_phonefarm $src $pre; then
          sync_tracked $src ~/node/phonefarm
          chmod +x $pre/phonefarm   # curl 下下来没有执行位
          install_bin $pre/phonefarm ~/node/phonefarm/phonefarm
          # 灰档层 .so 不入库，phonefarm 挂层时找 knobs/gray/build/out/
          if [[ -f $pre/libVkLayer_refknobs.so ]]; then
            mkdir -p ~/node/phonefarm/knobs/gray/build/out
            install_bin $pre/libVkLayer_refknobs.so ~/node/phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so
            install_bin $pre/VkLayer_refknobs.json ~/node/phonefarm/knobs/gray/build/out/VkLayer_refknobs.json
            ls -la ~/node/phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so
          fi
          rm -rf $pre
          echo "updated ~/node/$name（CI 产物，未在节点编译）"
          continue
        fi
        echo "  退回节点本地编译（慢、吃内存）"
        (cd $src/src && cargo build --release -q)
        sync_tracked $src ~/node/phonefarm
        install_bin $CARGO_TARGET_DIR/release/phonefarm ~/node/phonefarm/phonefarm
        # 灰档层 .so 不入库，phonefarm 挂层时找 knobs/gray/build/out/；NDK 只装在节点上，所以在这里编
        if [[ -x ~/node/phonefarm/knobs/gray/build/build_layer.sh ]]; then
          (cd ~/node/phonefarm && knobs/gray/build/build_layer.sh 2>&1 | tail -3) || { echo "灰档层编译失败"; exit 1; }
          ls -la ~/node/phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so
        fi;;
      game_opt_loop)
        (cd $src && cargo build --release -q)
        sync_tracked $src ~/node/game_opt_loop
        install_bin $CARGO_TARGET_DIR/release/game_opt_loop ~/node/game_opt_loop/game_opt_loop;;
      harness-ops)
        rsync -a $src/node/ ~/node/harness-ops/ --exclude __pycache__
        echo "节点服务本身的改动要 /admin/restart 才生效";;
    esac
    echo "updated ~/node/$name"
  done
  exit 0
fi

src=$1
# 作战室的两个密码（查看 dash.pass / 掌舵 steer.pass）：上传目录里有 secrets/ 就装到 ~/.config/hgamey/（600）
for f in dash.pass steer.pass; do
  [ -f "$src/secrets/$f" ] || continue
  mkdir -p "$HOME/.config/hgamey" && install -m 600 "$src/secrets/$f" "$HOME/.config/hgamey/$f"
  rm -f "$src/secrets/$f"   # 上传目录在 _jobs/<id>/in 下，不留副本
  echo "updated ~/.config/hgamey/$f"
done
for d in phonefarm game_opt_loop harness-ops; do
  [ -d "$src/$d" ] || continue
  rsync -a "$src/$d/" "$HOME/node/$d/"
  echo "updated ~/node/$d"
done
[ -f "$HOME/node/phonefarm/phonefarm" ] && codesign --force --sign - "$HOME/node/phonefarm/phonefarm" 2>/dev/null || true
[ -f "$HOME/node/game_opt_loop/game_opt_loop" ] && codesign --force --sign - "$HOME/node/game_opt_loop/game_opt_loop" 2>/dev/null || true
echo "节点服务本身的改动要重启服务才生效（harness-ops/node_service.py）"
