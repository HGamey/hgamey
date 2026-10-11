#!/bin/zsh
# 节点自己下载 APK 并装到手机（开发机上行太慢，大安装包不经开发机）。作为 /jobs 的 fetch_apk 任务运行。
# 用法: fetch_apk.sh <https 下载地址> [期望字节数] [adb serial]
#   只接受 https；下到 ~/node/cache/apk/，大小对不上就不装；装完不删缓存，重装可复用。
set -eu
export PATH=/opt/homebrew/bin:$PATH
url=$1; want=${2:-}; serial=${3:-91253241019A}
[[ $url == https://* ]] || { echo "只接受 https 地址"; exit 2; }
mkdir -p ~/node/cache/apk
f=~/node/cache/apk/${${url:t}%%\?*}
[[ $f == *.apk ]] || f=$f.apk
if [[ ! -s $f ]] || [[ -n $want && $(stat -f %z $f) != $want ]]; then
  echo "下载 $url → $f"
  curl -fL --retry 5 -C - -o $f "$url"
fi
got=$(stat -f %z $f)
echo "大小 $got 字节"
[[ -z $want || $got == $want ]] || { echo "大小不符（期望 $want），不安装"; exit 3; }
adb -s $serial install -r -g $f
adb -s $serial shell cmd audio set-volume 3 0 >/dev/null 2>&1 || true   # 手机必须保持静音
echo "已安装；媒体音量已置 0"
