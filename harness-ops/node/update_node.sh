#!/bin/zsh
# Code preparation remains compatible with the existing git/prebuilt workflow.
# The Python supervisor owns locking, guarded publication, receipts and rollback.
set -euo pipefail
SCRIPT_DIR=${0:A:h}
if [[ ${1:-} != --prepare-internal ]]; then
  exec "${HG_NODE_PYTHON:-python3}" "$SCRIPT_DIR/node_update.py" "$@"
fi
shift
[[ $# == 4 && -n ${HG_UPDATE_INTERNAL_STAGE:-} && $3 == $HG_UPDATE_INTERNAL_STAGE ]] || { echo "invalid internal staging invocation" >&2; exit 2; }
name=$1; ref=$2; stage=$3; work=$4
mkdir -p "$stage" "$work/src" "$work/target"
export CARGO_TARGET_DIR=$work/target
export PATH=/opt/homebrew/bin:$HOME/.cargo/bin:$PATH
typeset -A REPO=(
  phonefarm     git@github.com:HGamey/phonefarm.git
  game_opt_loop git@github.com:HGamey/game_opt_loop.git
  harness-ops   git@github.com:HGamey/harness-ops.git
)

PF_REL=https://github.com/HGamey/phonefarm/releases/download/node-build
PF_WAIT=${PF_WAIT:-600}   # CI 还没出这个提交的产物时最多等多少秒

json_get() {  # fixed fields from a bounded CI manifest; no executable expressions
  python3 - "$@" <<'PYJSON'
import hashlib, json, pathlib, re, sys
with pathlib.Path(sys.argv[1]).open("rb") as stream:
    raw = stream.read(65537)
if len(raw) > 65536:
    raise SystemExit("build manifest too large")
j = json.loads(raw)
files = j.get("files") if isinstance(j, dict) else None
allowed = {"phonefarm", "libVkLayer_refknobs.so", "VkLayer_refknobs.json"}
if (not isinstance(files, dict) or "phonefarm" not in files or not set(files) <= allowed
        or not isinstance(j.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", j["sha"])):
    raise SystemExit("invalid CI build manifest")
for name, entry in files.items():
    if (not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or not isinstance(entry.get("asset", name), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", entry.get("asset", name))
            or ("size" in entry and (type(entry["size"]) is not int or entry["size"] < 0))):
        raise SystemExit("invalid CI asset metadata")
op = sys.argv[2]
if op == "sha": print(j["sha"])
elif op == "files": print(" ".join(files))
elif op == "asset": print(files[sys.argv[3]].get("asset", sys.argv[3]))
elif op == "sha256": print(files[sys.argv[3]]["sha256"])
elif op == "verify":
    entry = files[sys.argv[3]]
    digest, size = hashlib.sha256(), 0
    with pathlib.Path(sys.argv[4]).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"] or ("size" in entry and size != entry["size"]):
        raise SystemExit("CI asset size/sha256 mismatch")
elif op == "provenance": print(str(j.get("built_at", "unknown")) + " " + str(j.get("run_url", "unknown")))
else: raise SystemExit("invalid manifest field")
PYJSON
}

# 节点到 GitHub 的网络会断：失败自动重试、断点续传（-C -），30 秒低于 10KB/s 算卡住重来
CURL=(curl -fsSL --connect-timeout 20 --retry 5 --retry-all-errors --retry-delay 3 --speed-limit 10240 --speed-time 30)

fetch_prebuilt() {  # $1 提交 sha $2 下载目录。0=已核验 1=版本尚无 2=下载/校验失败
  local sha=$1 d=$2 f asset reported http rc=0 legacy=0
  mkdir -p "$d" && rm -f "$d/build.json"
  # 精确版本优先；仅明确 404 允许旧 manifest 兼容，网络/格式失败不改取 latest。
  http=$($CURL --retry 0 --max-time 60 -w '%{http_code}' -o "$d/build.json" "$PF_REL/$sha-build.json") || rc=$?
  if (( rc == 22 )) && [[ $http == 404 ]]; then
    legacy=1; rc=0
    http=$($CURL --retry 0 --max-time 60 -w '%{http_code}' -o "$d/build.json" "$PF_REL/build.json?t=$(date +%s)") || rc=$?
    (( rc == 22 )) && [[ $http == 404 ]] && return 1
  fi
  (( rc == 0 )) && [[ $http == 200 ]] || { echo "  取所选构建 manifest 失败"; return 2; }
  reported=$(json_get "$d/build.json" sha) || return 2
  if [[ $reported != $sha ]]; then
    (( legacy )) && return 1
    echo "  精确版本 manifest SHA 不符"; return 2
  fi
  for f in $(json_get "$d/build.json" files); do
    # 新版 CI 给每个文件一个带提交号的资产名（永不覆盖，不会撞上 CI 覆盖时的 404）；老 build.json 没有就用原名
    asset=$(json_get "$d/build.json" asset "$f")
    [[ -f $d/$f ]] && json_get "$d/build.json" verify "$f" "$d/$f" 2>/dev/null && continue
    # 下到 .part.<资产名>，断了下一轮接着续；资产名带提交号，不会把两个版本续到一起
    $CURL -C - --max-time 600 -o "$d/.part.$asset" "$PF_REL/$asset" || { echo "  下载 $asset 失败（已下 $(stat -f %z "$d/.part.$asset" 2>/dev/null || echo 0) 字节，下一轮续传）"; return 2; }
    if ! json_get "$d/build.json" verify "$f" "$d/.part.$asset"; then
      rm -f "$d/.part.$asset"
      # 老 build.json（无 asset 字段）时同名文件可能正被 CI 覆盖：等新 build.json
      [[ $asset == $f ]] && { echo "  $f sha256 对不上（CI 正在覆盖？）"; return 1; }
      echo "  $asset size/sha256 对不上，删掉重下"; return 2
    fi
    mv -f "$d/.part.$asset" "$d/$f"
  done
  return 0
}

fetch_prebuilt_git() {  # 同 fetch_prebuilt，但走 git：CI 把产物做成孤立提交推到 refs/node-build/sha/<提交>。
  # 节点访问 release 资产要跳到 release-assets.githubusercontent.com（Fastly），国内时好时坏（同网段的
  # raw.githubusercontent.com 直接被重置）；拉源码用的 ssh github.com 一直稳，所以优先走它。
  local sha=$1 d=$2 g=$2/.git-mirror ref=refs/node-build/sha/$1 f lrc=0
  mkdir -p $d
  git ls-remote --exit-code ${REPO[phonefarm]} $ref >/dev/null 2>&1 || lrc=$?
  (( lrc == 2 )) && return 1                       # 没有这个 ref：CI 还没推（或是老构建）
  (( lrc )) && { echo "  git ls-remote 失败（$lrc）"; return 2; }
  rm -rf $g && git init -q --bare $g
  git -C $g fetch -q --depth 1 ${REPO[phonefarm]} "+${ref}:refs/b" || { echo "  git 取产物失败"; rm -rf $g; return 2; }
  git -C $g cat-file blob refs/b:build.json > $d/build.json
  [[ $(json_get $d/build.json sha) == $sha ]] || { echo "  git 产物里的 sha 不对"; rm -rf $g; return 2; }
  for f in $(json_get $d/build.json files); do
    git -C $g cat-file blob refs/b:$f > $d/$f
    json_get "$d/build.json" verify "$f" "$d/$f" || { echo "  git 产物 $f size/sha256 不对"; rm -rf $g; return 2; }
  done
  rm -rf $g
  return 0
}

prebuilt_phonefarm() {  # $1 源码目录 $2 下载目录。成功返回 0，需退回本地编译返回 1
  local src=$1 d=$2 sha main rc grc waited=0 fails=0 from now
  # The existing CI artifact is macOS arm64 only. Other nodes build locally.
  [[ $(uname -s) == Darwin && $(uname -m) == arm64 ]] || return 1
  sha=$(git -C $src rev-parse HEAD); main=$(git -C $src rev-parse origin/main)
  rm -rf $d
  while :; do
    from=git; fetch_prebuilt_git $sha $d && rc=0 || rc=$?
    if (( rc )); then  # git 上没有（老构建 / CI 还没推）或 git 网络失败：再试 release 下载
      grc=$rc; from=release; fetch_prebuilt $sha $d && rc=0 || rc=$?
      (( rc && grc == 1 )) && rc=1   # git 明确说还没有，就当没好，不管 release 那边是不是网络错误
    fi
    (( rc == 0 )) && { echo "  用 CI 产物（经 $from）$(json_get $d/build.json provenance)"; return 0; }
    if (( rc == 2 )); then  # git 和 release 都取不到：连续 5 轮才放弃
      (( ++fails >= 5 )) && { echo "  git 和 release 都连续 $fails 轮取不到 CI 产物，放弃"; return 1; }
      sleep 15; (( waited += 15 )); continue
    fi
    fails=0
    [[ $sha == $main ]] || { echo "  ${sha:0:7} 没有 CI 产物，也不是 origin/main 最新（CI 不会再编它）"; return 1; }
    (( waited >= PF_WAIT )) && { echo "  等了 ${waited}s，CI 仍没出 ${sha:0:7} 的产物"; return 1; }
    now=$(json_get $d/build.json sha 2>/dev/null || echo "?")
    echo "  CI 还没出 ${sha:0:7} 的产物（release 上现在是 $now），30 秒后再看（最多等 $((PF_WAIT/60)) 分钟）"
    sleep 30; (( waited += 30 ))
  done
}

filter_code_paths() {
  python3 -c 'import pathlib,sys; sys.path.insert(0,sys.argv[1]); from node_update import RESERVED
prefix=sys.argv[2].encode()
for raw in sys.stdin.buffer.read().split(b"\0"):
    if not raw or (prefix and not raw.startswith(prefix)): continue
    raw=raw[len(prefix):]
    path=pathlib.PurePosixPath(raw.decode("utf-8"))
    if not path.is_absolute() and not any(p in RESERVED or p==".." for p in path.parts):
        sys.stdout.buffer.write(raw+b"\0")' "$SCRIPT_DIR" "$1"
}

sync_tracked() {  # $1 源码目录 $2 目标目录
  git -C "$1" ls-files -z | filter_code_paths "" | rsync -a --from0 --files-from=- "$1/" "$2/"
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

[[ -n ${REPO[$name]:-} ]] || { echo "unknown repository" >&2; exit 2; }
src=$work/src/$name
[[ -d $src/.git ]] || git clone -q ${REPO[$name]} "$src"
git -C "$src" remote set-url origin ${REPO[$name]}
git -C "$src" fetch -q origin
git -C "$src" checkout -q --detach "$ref"
source_revision=$(git -C "$src" rev-parse HEAD)
source_status=$(git -C "$src" status --porcelain --untracked-files=normal)
[[ -z $source_status ]] || { echo "updater source checkout is not clean" >&2; exit 2; }
source_dirty=false
case $name in
  phonefarm)
    pre=$work/prebuilt/phonefarm
    if prebuilt_phonefarm "$src" "$pre"; then
      sync_tracked "$src" "$stage"
      cp "$pre/phonefarm" "$stage/phonefarm"
      chmod 755 "$stage/phonefarm"
      if [[ -f $pre/libVkLayer_refknobs.so ]]; then
        mkdir -p "$stage/knobs/gray/build/out"
        cp "$pre/libVkLayer_refknobs.so" "$pre/VkLayer_refknobs.json" "$stage/knobs/gray/build/out/"
      fi
      mkdir -p "$stage/download-builds"
      cp "$pre/build.json" "$stage/download-builds/$source_revision.json"
    else
      (cd "$src/src" && HGAMEY_BUILD_REVISION=$source_revision HGAMEY_BUILD_DIRTY=false cargo build --release -q)
      sync_tracked "$src" "$stage"
      cp "$CARGO_TARGET_DIR/release/phonefarm" "$stage/phonefarm"
      chmod 755 "$stage/phonefarm"
      if [[ -x $stage/knobs/gray/build/build_layer.sh ]]; then
        (cd "$stage" && knobs/gray/build/build_layer.sh)
      fi
    fi;;
  game_opt_loop)
    (cd "$src" && HGAMEY_BUILD_REVISION=$source_revision HGAMEY_BUILD_DIRTY=false cargo build --release -q)
    sync_tracked "$src" "$stage"
    cp "$CARGO_TARGET_DIR/release/game_opt_loop" "$stage/game_opt_loop"
    chmod 755 "$stage/game_opt_loop";;
  harness-ops)
    # Only tracked source is staged; runtime data and untracked credentials never enter publication.
    git -C "$src" ls-files -z node/ | filter_code_paths "node/" | rsync -a --from0 --files-from=- "$src/node/" "$stage/"
    write_source_build "$stage" HGamey/harness-ops "$source_revision" false;;
esac
printf '%s\n' "$source_revision" > "$work/revision-$name"
