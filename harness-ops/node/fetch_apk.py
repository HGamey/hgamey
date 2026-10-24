#!/usr/bin/env python3
"""Legacy locked install adapter. Prefer a host-only game prepare before installing.

Requires SOURCE SIZE SERIAL SHA256 and --cache-root DIR (or PF_GAME_CACHE_ROOT).
SOURCE is a public HTTP(S) URL or one basename in PF_APK_CACHE_DIR, which defaults
to ~/node/cache/apk for explicit legacy imports. No permission or volume changes.
"""
import argparse
import os
import re
import subprocess
import sys

from artifact_io import SafeRoot
from game_assets import AssetCache, AssetError, _public_url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", default=os.environ.get("PF_GAME_CACHE_ROOT"))
    parser.add_argument("source")
    parser.add_argument("size")
    parser.add_argument("serial")
    parser.add_argument("sha256")
    args = parser.parse_args(argv)
    # Finish validation before cache access, network or ADB.
    if (not args.cache_root or not os.path.isabs(args.cache_root)
            or not re.fullmatch(r"[0-9]+", args.size) or int(args.size) <= 0
            or not args.serial.strip() or any(ord(c) < 32 for c in args.serial)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256)):
        print("fetch_apk requires an absolute cache root, positive size, explicit serial and SHA-256", file=sys.stderr)
        return 2
    remote = args.source.startswith(("http://", "https://"))
    try:
        if remote:
            source = {"kind": "url", "url": _public_url(args.source)}
        else:
            if (args.source in {"", ".", ".."} or os.path.basename(args.source) != args.source
                    or args.source.endswith(".part") or "\x00" in args.source):
                raise AssetError("legacy source must be one complete cache basename")
            source = {"kind": "unavailable", "reason": "explicit legacy local cache import"}
        asset = {"name": "legacy-apk", "role": "apk", "sha256": args.sha256.lower(),
                 "size": int(args.size), "source": source}
        cache = AssetCache(args.cache_root)
        if remote or cache.observe(asset)["state"] == "verified":
            result = cache.prepare(asset, None)
        else:
            old = SafeRoot(os.environ.get("PF_APK_CACHE_DIR", os.path.expanduser("~/node/cache/apk")))
            try:
                with old.open_file(args.source) as stream:
                    result = cache.import_file(asset, stream)
            finally:
                old.close()
        if result["state"] != "verified":
            print(result.get("error") or "APK content is not verified; not installed", file=sys.stderr)
            return 3
        print("APK size and SHA-256 verified; installing on the explicitly selected device", flush=True)
        adb = os.environ.get("PF_APK_ADB_BIN", os.environ.get("ADB_BIN", "adb"))
        result = subprocess.run([adb, "-s", args.serial, "install", "-r", cache.object_path(asset["sha256"])], check=False)
        if result.returncode == 0:
            print("Installed; runtime permissions and media volume were not changed", flush=True)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(str(error) if isinstance(error, AssetError) else "APK preparation or installation could not complete", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
