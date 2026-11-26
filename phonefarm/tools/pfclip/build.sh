#!/bin/sh
# 重建 pfclip.dex(提交在仓库里, phonefarm 编译时 include_bytes 内嵌)。需要 JDK 与 Android SDK(build-tools 的 d8, platforms 的 android.jar)。
set -e
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:-$HOME/Library/Android/sdk}
BT=$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)
JAR=$(ls -d "$SDK"/platforms/android-* | sort -V | tail -1)/android.jar
rm -rf out && mkdir out
javac --release 11 -cp "$JAR" -d out PfClip.java
"$BT/d8" --min-api 26 --lib "$JAR" --output out out/PfClip.class
mv out/classes.dex pfclip.dex
rm -rf out
ls -l pfclip.dex
