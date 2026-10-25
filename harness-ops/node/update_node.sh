#!/bin/zsh
# 节点自更新，作为 /jobs 的 update_node 任务运行（cwd = ~/node）。两种用法：
#
#   update_node.sh --git [phonefarm[@ref]] [game_opt_loop[@ref]] [harness-ops[@ref]]
#       节点自己从 GitHub 拉代码（缺省 phonefarm 和 game_opt_loop，ref 缺省 origin/main）。
#       phonefarm 的二进制和灰档层 .so 由 BH3GEI/phonefarm 的 GitHub Actions 云端编译，优先用 git 从隐藏 ref
#       refs/node-build/sha/<提交> 取（走 ssh github.com，稳），取不到再从 release node-build 下载；
#       提交对得上就直接用，不在节点上编译（节点内存/磁盘紧）。
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
LOCKD=$HOME/node/.deploy.lock; mkdir -p $HOME/node
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

# 节点到 GitHub 的网络会断：失败自动重试、断点续传（-C -），30 秒低于 10KB/s 算卡住重来
CURL=(curl -fsSL --connect-timeout 20 --retry 5 --retry-all-errors --retry-delay 3 --speed-limit 10240 --speed-time 30)

fetch_prebuilt() {  # $1 提交 sha $2 下载目录。0=下好且校验过 1=release 上不是这个 sha 2=下载/校验失败（可重试）
  local sha=$1 d=$2 f asset want got
  mkdir -p $d && rm -f $d/build.json
  $CURL --max-time 60 -o $d/build.json "$PF_REL/build.json?t=$(date +%s)" || { echo "  取 build.json 失败"; return 2; }
  [[ $(json_get $d/build.json 'j["sha"]') == $sha ]] || return 1
  for f in $(json_get $d/build.json '" ".join(j["files"])'); do
    # 新版 CI 给每个文件一个带提交号的资产名（永不覆盖，不会撞上 CI 覆盖时的 404）；老 build.json 没有就用原名
    asset=$(json_get $d/build.json "j['files']['$f'].get('asset', '$f')")
    want=$(json_get $d/build.json "j['files']['$f']['sha256']")
    [[ -f $d/$f && $(shasum -a 256 $d/$f | cut -d' ' -f1) == $want ]] && continue   # 上一轮已下好
    # 下到 .part.<资产名>，断了下一轮接着续；资产名带提交号，不会把两个版本续到一起
    $CURL -C - --max-time 600 -o $d/.part.$asset "$PF_REL/$asset" || { echo "  下载 $asset 失败（已下 $(stat -f %z $d/.part.$asset 2>/dev/null || echo 0) 字节，下一轮续传）"; return 2; }
    got=$(shasum -a 256 $d/.part.$asset | cut -d' ' -f1)
    if [[ $got != $want ]]; then
      rm -f $d/.part.$asset
      # 老 build.json（无 asset 字段）时同名文件可能正被 CI 覆盖：等新 build.json
      [[ $asset == $f ]] && { echo "  $f sha256 对不上（CI 正在覆盖？）"; return 1; }
      echo "  $asset sha256 对不上，删掉重下"; return 2
    fi
    mv -f $d/.part.$asset $d/$f
  done
  return 0
}

fetch_prebuilt_git() {  # 同 fetch_prebuilt，但走 git：CI 把产物做成孤立提交推到 refs/node-build/sha/<提交>。
  # 节点访问 release 资产要跳到 release-assets.githubusercontent.com（Fastly），国内时好时坏（同网段的
  # raw.githubusercontent.com 直接被重置）；拉源码用的 ssh github.com 一直稳，所以优先走它。
  local sha=$1 d=$2 g=$2/.git-mirror ref=refs/node-build/sha/$1 f want lrc=0
  mkdir -p $d
  git ls-remote --exit-code ${REPO[phonefarm]} $ref >/dev/null 2>&1 || lrc=$?
  (( lrc == 2 )) && return 1                       # 没有这个 ref：CI 还没推（或是老构建）
  (( lrc )) && { echo "  git ls-remote 失败（$lrc）"; return 2; }
  rm -rf $g && git init -q --bare $g
  git -C $g fetch -q --depth 1 ${REPO[phonefarm]} "+${ref}:refs/b" || { echo "  git 取产物失败"; rm -rf $g; return 2; }
  git -C $g cat-file blob refs/b:build.json > $d/build.json
  [[ $(json_get $d/build.json 'j["sha"]') == $sha ]] || { echo "  git 产物里的 sha 不对"; rm -rf $g; return 2; }
  for f in $(json_get $d/build.json '" ".join(j["files"])'); do
    want=$(json_get $d/build.json "j['files']['$f']['sha256']")
    git -C $g cat-file blob refs/b:$f > $d/$f
    [[ $(shasum -a 256 $d/$f | cut -d' ' -f1) == $want ]] || { echo "  git 产物 $f sha256 不对"; rm -rf $g; return 2; }
  done
  rm -rf $g
  return 0
}

prebuilt_phonefarm() {  # $1 源码目录 $2 下载目录。成功返回 0，需退回本地编译返回 1
  local src=$1 d=$2 sha main rc grc waited=0 fails=0 from now
  sha=$(git -C $src rev-parse HEAD); main=$(git -C $src rev-parse origin/main)
  rm -rf $d
  while :; do
    from=git; fetch_prebuilt_git $sha $d && rc=0 || rc=$?
    if (( rc )); then  # git 上没有（老构建 / CI 还没推）或 git 网络失败：再试 release 下载
      grc=$rc; from=release; fetch_prebuilt $sha $d && rc=0 || rc=$?
      (( rc && grc == 1 )) && rc=1   # git 明确说还没有，就当没好，不管 release 那边是不是网络错误
    fi
    (( rc == 0 )) && { echo "  用 CI 产物（经 $from）$(json_get $d/build.json 'j["built_at"]+"  "+j["run_url"]')"; return 0; }
    if (( rc == 2 )); then  # git 和 release 都取不到：连续 5 轮才放弃
      (( ++fails >= 5 )) && { echo "  git 和 release 都连续 $fails 轮取不到 CI 产物，放弃"; return 1; }
      sleep 15; (( waited += 15 )); continue
    fi
    fails=0
    [[ $sha == $main ]] || { echo "  ${sha:0:7} 没有 CI 产物，也不是 origin/main 最新（CI 不会再编它）"; return 1; }
    (( waited >= PF_WAIT )) && { echo "  等了 ${waited}s，CI 仍没出 ${sha:0:7} 的产物"; return 1; }
    now=$(json_get $d/build.json 'j["sha"][:7]' 2>/dev/null || echo "?")
    echo "  CI 还没出 ${sha:0:7} 的产物（release 上现在是 $now），30 秒后再看（最多等 $((PF_WAIT/60)) 分钟）"
    sleep 30; (( waited += 30 ))
  done
}

sync_tracked() {  # $1 源码目录 $2 目标目录
  git -C $1 ls-files -z | rsync -a --from0 --files-from=- $1/ $2/
}

write_source_build() {  # $1 目标目录 $2 repository $3 所选源码 revision $4 dirty
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, os, pathlib, sys, tempfile
directory = pathlib.Path(sys.argv[1])
record = {"schema": "hgamey/source-build@1", "repository": sys.argv[2],
          "revision": sys.argv[3], "dirty": sys.argv[4] == "true"}
fd, temporary = tempfile.mkstemp(prefix=".runtime-build-", dir=directory)
try:
    with os.fdopen(fd, "w") as output:
        json.dump(record, output, ensure_ascii=False)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, directory / "runtime-build.json")
    parent = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
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
    source_revision=$(git -C $src rev-parse HEAD)
    source_status=$(git -C $src status --porcelain --untracked-files=normal)
    source_dirty=false
    [[ -z $source_status ]] || source_dirty=true
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
          # Original downloaded asset provenance, before local signing. It is
          # not a digest of the installed executable and is retained by source.
          mkdir -p ~/node/phonefarm/download-builds
          cp $pre/build.json ~/node/phonefarm/download-builds/$source_revision.json
          rm -rf $pre
          echo "updated ~/node/$name（CI 产物，未在节点编译）"
          continue
        fi
        echo "  退回节点本地编译（慢、吃内存）"
        (cd $src/src && HGAMEY_BUILD_REVISION=$source_revision HGAMEY_BUILD_DIRTY=$source_dirty cargo build --release -q)
        sync_tracked $src ~/node/phonefarm
        install_bin $CARGO_TARGET_DIR/release/phonefarm ~/node/phonefarm/phonefarm
        # 灰档层 .so 不入库，phonefarm 挂层时找 knobs/gray/build/out/；NDK 只装在节点上，所以在这里编
        if [[ -x ~/node/phonefarm/knobs/gray/build/build_layer.sh ]]; then
          (cd ~/node/phonefarm && knobs/gray/build/build_layer.sh 2>&1 | tail -3) || { echo "灰档层编译失败"; exit 1; }
          ls -la ~/node/phonefarm/knobs/gray/build/out/libVkLayer_refknobs.so
        fi;;
      game_opt_loop)
        (cd $src && HGAMEY_BUILD_REVISION=$source_revision HGAMEY_BUILD_DIRTY=$source_dirty cargo build --release -q)
        sync_tracked $src ~/node/game_opt_loop
        install_bin $CARGO_TARGET_DIR/release/game_opt_loop ~/node/game_opt_loop/game_opt_loop;;
      harness-ops)
        rsync -a $src/node/ ~/node/harness-ops/ --exclude __pycache__
        write_source_build ~/node/harness-ops HGamey/harness-ops $source_revision $source_dirty
        echo "节点服务本身的改动要 /admin/restart 才生效";;
    esac
    echo "updated ~/node/$name"
  done
  exit 0
fi

src=$1
# 上传密码沿服务的外置配置根；相对 HG_CONFIG_DIR 与 launcher 一样按 ~/node 解释。
config_dir=$(python3 - <<'PY'
import os
path = os.path.expanduser(os.environ.get("HG_CONFIG_DIR") or "~/.config/hgamey")
print(path if os.path.isabs(path) else os.path.join(os.path.expanduser("~/node"), path))
PY
)
for f in dash.pass steer.pass; do
  [ -f "$src/secrets/$f" ] || continue
  (umask 077; mkdir -p "$config_dir" && install -m 600 "$src/secrets/$f" "$config_dir/$f")
  rm -f "$src/secrets/$f"   # 上传目录在 _jobs/<id>/in 下，不留副本
  echo "updated $config_dir/$f"
done
for d in phonefarm game_opt_loop harness-ops; do
  [ -d "$src/$d" ] || continue
  # An upload without a declaration must not inherit the previous install's
  # source identity. A declaration supplied with this upload is copied below.
  [[ $d != harness-ops ]] || rm -f "$HOME/node/$d/runtime-build.json"
  rsync -a "$src/$d/" "$HOME/node/$d/"
  echo "updated ~/node/$d"
done
[ -f "$HOME/node/phonefarm/phonefarm" ] && codesign --force --sign - "$HOME/node/phonefarm/phonefarm" 2>/dev/null || true
[ -f "$HOME/node/game_opt_loop/game_opt_loop" ] && codesign --force --sign - "$HOME/node/game_opt_loop/game_opt_loop" 2>/dev/null || true
echo "节点服务本身的改动要重启服务才生效（harness-ops/node_service.py）"
