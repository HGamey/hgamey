#!/usr/bin/env python3
"""A simulated phone for game installation tests, never real-device data.

`adb shell` runs the command with the host's shell, its device paths (/data/, /system/app/, /proc/) moved under
PF_FAKE_PHONE, and fakes for the Android tools a session uses. PF_FAKE_COMMIT picks how the package manager answers a
commit: ok; root_only (ColorOS 15: a shell commit waits on approval screens, a root one installs); waits (any commit
waits until abandoned); refused (a policy refusal); storage (a genuine failure). PF_FAKE_ROOT=1 gives su and Magisk;
PF_FAKE_MOUNT=broken makes modules fail to mount; PF_FAKE_STAY_OFFLINE=1 keeps the phone away after a reboot until
the test deletes PF_FAKE_PHONE/offline. A reboot gives a new boot ID, removes modules marked `remove` and
mounts the others' system/app.

Also: `aapt2 dump ...` for the fixture APK, and `make-apk OUT PACKAGE VERSION_CODE` to build one.
"""
import fcntl, fnmatch, hashlib, json, os, pathlib, re, shutil, subprocess, sys, time, uuid, zipfile

HERE = pathlib.Path(__file__).resolve()
TOOLS = ["pm", "su", "id", "getprop", "am", "magisk", "chown", "chcon", "dumpsys"]


def phone():
    return pathlib.Path(os.environ["PF_FAKE_PHONE"])


class State:
    """state.json under a lock: user packages, sessions, the commits waiting."""

    def __enter__(self):
        self.lock = open(phone() / "state.lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        path = phone() / "state.json"
        self.value = json.loads(path.read_text()) if path.exists() else {"packages": {}, "sessions": {}, "next": 1}
        return self.value

    def __exit__(self, *error):
        if error[0] is None:
            (phone() / "state.json").write_text(json.dumps(self.value))
        self.lock.close()


def apk_identity(path):
    with zipfile.ZipFile(path) as apk:
        info = json.loads(apk.read("fake.json"))
        abis = sorted({name.split("/")[1] for name in apk.namelist() if name.startswith("lib/") and name.count("/") == 2})
    return info["package"], info["version_code"], abis


def system_apps():
    apps = {}
    for apk in sorted((phone() / "system/app").glob("*/base.apk")):
        package, version, _ = apk_identity(apk)
        apps[package] = {"path": f"/system/app/{apk.parent.name}/base.apk", "version": version, "file": str(apk)}
    return apps


def init():
    root = phone()
    if (root / "bin").exists():
        return
    for directory in ["bin", "data/local/tmp", "data/app", "system/app", "proc/sys/kernel/random", "sessions"]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "proc/sys/kernel/random/boot_id").write_text(str(uuid.uuid4()) + "\n")
    for tool in TOOLS:
        script = root / "bin" / tool
        script.write_text(f'#!/bin/sh\nexec python3 {HERE} {tool} "$@"\n')
        script.chmod(0o755)
    # A background command runs on the phone, not in adb's process group on the host.
    (root / "bin/nohup").write_text('#!/bin/sh\nexec setsid "$@"\n')
    (root / "bin/nohup").chmod(0o755)
    if os.environ.get("PF_FAKE_ROOT") == "1":
        (root / "data/adb/modules").mkdir(parents=True)
        (root / "data/adb/magisk").mkdir(parents=True)
        busybox = root / "data/adb/magisk/busybox"
        busybox.write_text(f'#!/bin/sh\nexec python3 {HERE} busybox "$@"\n')
        busybox.chmod(0o755)


def device_paths(command):
    return re.sub(r"(?<![\w./-])/(data|system/app|proc)/", lambda m: f"{phone()}/{m.group(1)}/", command)


def shell(command, uid="2000"):
    env = {**os.environ, "PATH": f"{phone() / 'bin'}:/usr/bin:/bin", "FAKE_UID": uid}
    return subprocess.run(["sh", "-c", device_paths(command)], env=env).returncode


def reboot():
    root = phone()
    (root / "proc/sys/kernel/random/boot_id").write_text(str(uuid.uuid4()) + "\n")
    shutil.rmtree(root / "system/app")
    (root / "system/app").mkdir()
    for module in sorted((root / "data/adb/modules").glob("*")) if (root / "data/adb/modules").exists() else []:
        if (module / "remove").exists():
            shutil.rmtree(module)
        elif os.environ.get("PF_FAKE_MOUNT") != "broken":
            for app in (module / "system/app").glob("*"):
                shutil.copytree(app, root / "system/app" / app.name)
    with State() as state:
        state["sessions"] = {}
        state["reboots"] = state.get("reboots", 0) + 1
    if os.environ.get("PF_FAKE_STAY_OFFLINE") == "1":
        (root / "offline").touch()


def adb(args):
    init()
    with open(phone() / "adb.log", "a") as log:
        log.write(json.dumps(args) + "\n")
    if args[:1] == ["-s"]:
        args = args[2:]
    if (phone() / "offline").exists() and args[:1] in (["shell"], ["push"], ["get-state"]):
        print("error: device offline", file=sys.stderr)
        return 1
    if args == ["get-state"]:
        print("device")
        return 0
    if args[:1] == ["shell"] and len(args) == 2:
        return shell(args[1])
    if args[:1] == ["push"] and len(args) == 3:
        shutil.copyfile(args[1], device_paths(args[2]))
        print(f"{args[1]}: 1 file pushed, 0 skipped.")
        return 0
    if args == ["reboot"]:
        reboot()
        return 0
    if args == ["wait-for-device"]:
        return 0
    print(f"fake adb: unsupported {args}", file=sys.stderr)
    return 1


PM_HELP = """Package manager (package) commands:
  install-create [-lrtsfdg] [-i PACKAGE] [--user USER_ID|all|current]
  install-write [-S BYTES] SESSION_ID SPLIT_NAME [PATH|-]
  install-commit SESSION_ID
  install-abandon SESSION_ID
    -R: disallow replacement of existing application
"""


def called(tool, args):
    with open(phone() / f"{tool}.log", "a") as log:
        log.write(f"uid={os.environ.get('FAKE_UID', '2000')} {' '.join(args)}\n")


def pm(args):
    called("pm", args)
    uid = os.environ.get("FAKE_UID", "2000")
    mode = os.environ.get("PF_FAKE_COMMIT", "ok")
    if args == ["help"]:
        print(PM_HELP)
        return 0
    if args[:2] == ["list", "packages"]:
        with State() as state:
            names = ["android", "com.android.settings", *state["packages"], *system_apps()]
        print("\n".join(f"package:{name}" for name in names))
        return 0
    if args[:1] == ["install-create"]:
        package, size = args[args.index("--pkg") + 1], int(args[args.index("-S") + 1])
        with State() as state:
            number = state["next"]
            state["next"] += 1
            state["sessions"][str(number)] = {"package": package, "size": size, "uid": uid, "state": "open"}
        print(f"Success: created install session [{number}]")
        return 0
    if args[:1] == ["install-write"]:
        session, source = args[3], args[5]
        with State() as state:
            if state["sessions"].get(session, {}).get("uid") != uid:
                print("Error: session owned by another user", file=sys.stderr)
                return 1
        shutil.copyfile(source, phone() / "sessions" / f"{session}.apk")
        print(f"Success: streamed {os.path.getsize(source)} bytes")
        return 0
    if args[:1] == ["install-commit"]:
        session = args[1]
        waits = mode == "waits" or (mode == "root_only" and uid != "0")
        if mode == "refused":
            print("Failure [INSTALL_FAILED_ABORTED: -99]")
            return 1
        if mode == "storage":
            print("Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE: not enough space]")
            return 1
        if waits:
            with State() as state:
                state["sessions"][session]["state"] = "waiting"
            for _ in range(600):
                time.sleep(0.1)
                with State() as state:
                    if state["sessions"].get(session, {}).get("state") == "abandoned":
                        print("Failure [INSTALL_FAILED_ABORTED: Session was abandoned]")
                        return 1
            return 1
        apk = phone() / "sessions" / f"{session}.apk"
        package, version, _ = apk_identity(apk)
        target = phone() / "data/app" / package
        target.mkdir(parents=True)
        shutil.copyfile(apk, target / "base.apk")
        with State() as state:
            state["packages"][package] = {"version": version}
            state["sessions"].pop(session)
        print("Success")
        return 0
    if args[:1] == ["install-abandon"]:
        with State() as state:
            if args[1] not in state["sessions"] or state["sessions"][args[1]]["uid"] != uid:
                print("Error: no such session")
                return 1
            state["sessions"][args[1]]["state"] = "abandoned"
        print("Success")
        return 0
    if args[:1] == ["path"]:
        package = args[-1]
        with State() as state:
            installed = package in state["packages"]
        if installed:
            print(f"package:/data/app/{package}/base.apk")
            return 0
        if package in system_apps():
            print(f"package:{system_apps()[package]['path']}")
            return 0
        return 1
    print(f"fake pm: unsupported {args}", file=sys.stderr)
    return 1


def dumpsys(args):
    if args[:1] == ["package"]:
        with State() as state:
            installed = state["packages"].get(args[1])
        version = installed["version"] if installed else system_apps().get(args[1], {}).get("version")
        if version is not None:
            print(f"Packages:\n  Package [{args[1]}] (fixture):\n    userId=10123\n    versionCode={version} minSdk=23 targetSdk=35")
        return 0
    if args[:2] == ["activity", "activities"]:
        print("  topResumedActivity=ActivityRecord{1 u0 com.android.launcher/.Launcher t1}")
        return 0
    return 1


def busybox(args):
    # busybox unzip -q -o APK PATTERN -d DIR
    if args[:1] != ["unzip"]:
        return 1
    apk, pattern, target = args[3], args[4], args[args.index("-d") + 1]
    with zipfile.ZipFile(apk) as archive:
        for name in archive.namelist():
            if fnmatch.fnmatch(name, pattern):
                archive.extract(name, target)
    return 0


def aapt2(args):
    path = args[-1]
    package, version, abis = apk_identity(path)
    if args[:2] == ["dump", "badging"]:
        print(f"package: name='{package}' versionCode='{version}' versionName='fixture' platformBuildVersionName='15' "
              f"platformBuildVersionCode='35' compileSdkVersion='35' compileSdkVersionCodename='15'")
        print("minSdkVersion:'23'")
        if abis:
            print("native-code: " + " ".join(f"'{abi}'" for abi in abis))
        return 0
    if args[:2] == ["dump", "xmltree"]:
        print("N: android=http://schemas.android.com/apk/res/android (line=1)\n  E: manifest (line=1)\n"
              f"    A: http://schemas.android.com/apk/res/android:versionCode(0x0101021b)={version}\n"
              f"    A: package=\"{package}\" (Raw: \"{package}\")\n      E: uses-sdk (line=2)\n"
              "        A: http://schemas.android.com/apk/res/android:minSdkVersion(0x0101020c)=23\n"
              "      E: application (line=3)\n        A: http://schemas.android.com/apk/res/android:hasCode(0x0101000c)=false\n"
              "        A: http://schemas.android.com/apk/res/android:testOnly(0x01010272)=false")
        return 0
    return 1


def make_apk(out, package, version):
    with zipfile.ZipFile(out, "w") as apk:
        apk.writestr("fake.json", json.dumps({"package": package, "version_code": int(version)}))
        apk.writestr("AndroidManifest.xml", b"fixture")
        apk.writestr("lib/arm64-v8a/libgame.so", b"\x7fELF arm64 fixture")
        apk.writestr("lib/armeabi-v7a/libgame.so", b"\x7fELF arm fixture")
    data = pathlib.Path(out).read_bytes()
    print(json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}))
    return 0


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    if tool == "adb":
        return adb(args)
    if tool == "aapt2":
        return aapt2(args)
    if tool == "make-apk":
        return make_apk(*args)
    if tool == "pm":
        return pm(args)
    if tool == "dumpsys":
        return dumpsys(args)
    if tool == "busybox":
        return busybox(args)
    if tool == "su":
        if os.environ.get("PF_FAKE_ROOT") != "1":
            print("/system/bin/sh: su: inaccessible or not found", file=sys.stderr)
            return 127
        return shell(args[1], uid="0")
    if tool == "id":
        print(os.environ.get("FAKE_UID", "2000"))
        return 0
    if tool == "getprop":
        values = {"sys.boot_completed": "1", "ro.build.version.sdk": "35", "ro.product.cpu.abilist": "arm64-v8a,armeabi-v7a"}
        print(values.get(args[0], ""))
        return 0
    if tool == "am":
        called("am", args)
        if args == ["get-current-user"]:
            print("0")
        return 0
    if tool == "magisk":
        print("30700")
        return 0
    if tool in ("chown", "chcon"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
