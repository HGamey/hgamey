"""Host observations only: no program refresh, phone probes or repair writes."""
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import time

from runtime_identity import program_file

SCHEMA = "hgamey/node-doctor@1"
STATUSES = ("ok", "warning", "error", "unknown", "not_applicable")
_UNSET = object()
MACHO = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
         b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
         b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


def report(node):
    jobs, checked_at, checks = node.jobs, time.time(), []

    def add(id, category, status, title, message, evidence=None, remedy=None, observed_at=_UNSET):
        actions = remedy if isinstance(remedy, list) else [remedy] if remedy else []
        checks.append(dict(id=id, category=category, status=status, title=title, message=message,
                           observed_at=checked_at if observed_at is _UNSET else observed_at,
                           evidence=evidence or {}, remediation=[{"text": text} for text in actions]))

    add("node.connection", "connection", "ok", "节点连接", "已在明确绑定的节点读取诊断。",
        {"node_id": node.node_id, "instance_id": node.service["instance_id"], "platform": platform.system()})
    runtime = node.service.get("runtime", {})
    add("service.runtime", "runtime", "ok", "运行中的服务", "这是服务启动时记录的入口和解释器身份。",
        runtime, observed_at=node.started)
    build = runtime.get("build") or {}
    add("service.build", "runtime", "ok" if build.get("revision") else "unknown", "服务构建来源",
        "已记录构建来源；不据此推断其他组件版本。" if build.get("revision") else "运行服务没有可确认的构建来源记录。",
        build, "部署时保留 runtime-build.json 和实际构建来源；不要用当前源码版本回填历史。" if not build.get("revision") else None,
        node.started)
    env = jobs.pf_env if jobs.pf_env is not None else os.environ
    observations = jobs.programs.snapshot()
    for name in ("phonefarm", "game_opt_loop"):
        record = observations.get(name, {})
        if name not in jobs.bins:
            add(name + ".identity", "runtime", "warning", name + " 配置", "此节点未配置该程序。",
                remedy="若此节点承担该程序的任务，请配置并部署匹配宿主系统的产物。")
            continue
        executable, cwd = jobs.bins[name]
        path, fingerprint = program_file(executable, cwd, env)
        state = record.get("state", "unknown")
        status = "ok" if state == "observed" else "warning" if state == "stale" else "unknown"
        add(name + ".identity", "runtime", status, name + " 身份",
            {"observed": "使用已有的程序身份观察。", "stale": "文件已变化，已有程序身份过期。"}.get(state, "尚无可信的程序身份观察。"),
            record, "明确核对部署产物后刷新 runtime 身份；本诊断不会执行该程序。" if status != "ok" else None,
            record.get("observed_at"))
        usable = fingerprint is not None and os.access(path, os.X_OK)
        add(name + ".executable", "permissions", "ok" if usable else "error", name + " 可执行文件",
            "宿主可访问可执行文件。" if usable else "文件不存在、不是普通文件或没有执行权限。",
            {"path": path, "executable": usable}, "恢复适合本机的可执行产物及最小必要权限。" if not usable else None)
        if fingerprint is None:
            continue
        try:
            with open(path, "rb") as source:
                magic = source.read(4)
            system = platform.system()
            mismatch = system == "Linux" and magic in MACHO or system == "Darwin" and magic == b"\x7fELF"
            add(name + ".format", "runtime", "error" if mismatch else "ok", name + " 宿主格式",
                "产物格式与当前宿主系统冲突。" if mismatch else "未发现 ELF/Mach-O 与宿主系统冲突；指令集兼容仍未验证。",
                {"platform": system, "format": "Mach-O" if magic in MACHO else "ELF" if magic == b"\x7fELF" else "other"},
                "在目标系统重新构建，不能直接运行另一系统的编译产物。" if mismatch else None)
            if system == "Darwin" and magic in MACHO:
                try:
                    result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", path],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, timeout=2)
                    add(name + ".signature", "runtime", "ok" if result.returncode == 0 else "error", name + " 签名",
                        "macOS 签名验证通过。" if result.returncode == 0 else "macOS 签名验证失败。",
                        {"exit_code": result.returncode}, "重新构建并签名后再发布；不要忽略签名失败。" if result.returncode else None)
                except (OSError, subprocess.TimeoutExpired):
                    add(name + ".signature", "runtime", "unknown", name + " 签名", "签名工具不可用或在两秒内未完成。",
                        remedy="在宿主手动核查 codesign 及文件状态。")
            else:
                add(name + ".signature", "runtime", "not_applicable", name + " macOS 签名", "此文件不适用 macOS Mach-O 签名检查。")
        except OSError:
            add(name + ".format", "runtime", "unknown", name + " 宿主格式", "无法读取文件头；不执行该文件进行探测。")

    entry_path = runtime.get("entry_path") or ""
    root = None
    if entry_path:
        # Walk past the release subtree: the install root is the first ancestor
        # holding both the release pool and the deployment state.
        candidate = Path(entry_path).parent
        for _ in range(6):
            if (candidate / "releases").is_dir() and (candidate / "deploy-state").is_dir():
                root = candidate
                break
            candidate = candidate.parent
        else:
            root = Path(entry_path).parents[2] if len(Path(entry_path).parents) >= 3 else None
    if not entry_path or root is None:
        add("deploy.overlay", "deployment", "unknown", "overlay 部署布局",
            "无法从运行入口推断安装根；不猜测。", {"entry_path": entry_path or None},
            "核对服务启动入口后手动运行 deploy_overlay.py status。")
    else:
        link = root / "harness-ops"
        releases = root / "releases"
        if link.is_symlink():
            target = os.path.realpath(link)
            if os.path.isdir(target):
                count = len(os.listdir(releases)) if os.path.isdir(releases) else 0
                add("deploy.overlay", "deployment", "ok", "overlay 部署游标",
                    "安装目录由符号链接游标指向完整 release；升级与回滚都是单步原子切换。",
                    {"cursor": os.readlink(link), "target": target, "releases": count},
                    "升级/回滚继续走 deploy_overlay.py；不要直接改写游标目录或 rsync 覆盖安装根。")
            else:
                add("deploy.overlay", "deployment", "error", "overlay 部署游标",
                    "游标指向的目标不存在（悬空链接）；运行中的服务仍在用旧 inode，但重启会失败。",
                    {"cursor": os.readlink(link)},
                    "用 deploy_overlay.py rollback 回到最后一个完好 release，再核对 releases 目录。")
        elif link.is_dir():
            add("deploy.overlay", "deployment", "warning", "overlay 部署布局",
                "安装目录仍是普通目录（旧布局）；无法原子切换，也不能按 release 回滚。",
                {"root": str(root)},
                "用 deploy_overlay.py activate 完成首次迁移；迁移会把当前目录整体转为 baseline release。")
        else:
            add("deploy.overlay", "deployment", "not_applicable", "overlay 部署布局",
                "未发现可管理的安装目录；本诊断不创建目录。")
    for name, variable in (("adb", "ADB_BIN"), ("hdc", "HDC_BIN"), ("devlock", "DEVLOCK_BIN")):
        configured = jobs.bins.get(name)
        executable = env.get(variable) or (configured[0] if configured else name)
        cwd = configured[1] if configured else jobs.root
        path, fingerprint = program_file(executable, cwd, env)
        present = fingerprint is not None and os.access(path, os.X_OK)
        add("tool." + name, "tools", "ok" if present else "warning", name + " 工具",
            "宿主可找到该工具；没有执行或探测设备。" if present else "当前执行环境找不到可运行的工具。",
            {"configured": executable, "resolved_path": path, "present": present},
            "仅在需要对应设备功能时安装此工具，并修正服务/worker 的 PATH 或 " + variable + " 配置。" if not present else None)

    cache = jobs.export_cache.public()
    roots = {"data": node.data_root, "jobs": jobs.dir, "export": cache["configured_root"] or cache["effective_root"]}
    for name, path in roots.items():
        try:
            is_directory = stat.S_ISDIR(os.stat(path).st_mode)
            accessible = is_directory and os.access(path, os.R_OK | os.W_OK | os.X_OK)
            add("path." + name, "permissions", "ok" if accessible else "error", name + " 目录权限",
                "当前服务身份具备目录访问权限；未进行写入探针。" if accessible else "当前服务身份缺少目录或必要权限。",
                {"path": path, "accessible": accessible}, "核对配置目录、挂载状态与运行用户权限，不要放宽为全员可写。" if not accessible else None)
            disk = shutil.disk_usage(path)
            # Existing jobs input policy uses this reserve; this is not a quota.
            from job_input_archive import POLICY
            reserve = POLICY["min_free_bytes"]
            add("disk." + name, "storage", "ok" if disk.free >= reserve else "error", name + " 可用空间",
                "可用空间达到输入准备保留量；不代表未来任务容量充足。" if disk.free >= reserve else "可用空间低于输入准备所需保留量。",
                {"path": path, "free_bytes": disk.free, "total_bytes": disk.total, "min_free_bytes": reserve},
                "先确认原始证据及恢复记录，再清理可重建缓存或迁移数据根。" if disk.free < reserve else None)
        except OSError:
            add("path." + name, "storage", "error", name + " 存储", "目录不可访问，无法读取空间及权限。", {"path": path},
                "核对已配置路径及挂载状态；诊断不会创建或迁移目录。")
    if cache["availability"] != "available":
        add("export.cache", "storage", "error", "导出缓存", "配置的导出缓存不可用。", {"availability": cache["availability"]},
            "核对独立缓存根的权限与路径约束后重新启动服务。")
    add("devices.qualification", "devices", "unknown", "设备授权与实测资格",
        "未枚举或连接手机；授权、root、平台功能与恢复状态尚未在此诊断中验证。",
        {"status": "not_probed"},
        ["需要设备功能时，从设备工作流获取 owner/lease 后核查；缺 hdc 不代表 Android 不可用。",
         "Android：设备首次连接需在手机端确认 USB 调试授权；显示 unauthorized 时重新插拔并在设备端允许，不要重试强制命令。",
         "Harmony：需要 HDC 与对应驱动；Linux 安装 hdc 后配置 HDC_BIN，再用只读清单核连接。",
         "登记与身份核对走只读入口（devices / devices-resolve）；登记不构成实测资格。"])
    try:
        lease = jobs.leases.snapshot()
        if not isinstance(lease, dict):
            raise ValueError("lease snapshot has an unexpected shape")
        entries = lease.get("entries") if isinstance(lease.get("entries"), list) else []
        status = lease.get("status") if lease.get("status") in ("clear", "held", "blocked", "unknown") else "unknown"
        add("devices.lease", "devices",
            "ok" if status == "clear" else "warning" if status in ("held", "blocked") else "unknown",
            "设备所有权快照",
            {"clear": "当前无活动设备 owner 或恢复屏障。",
             "held": "存在活动 owner/lease；这是唯一所有权事实，不代表可以接管或设备已恢复。",
             "blocked": "存在未完成的恢复屏障；在核查前会阻止新的设备任务。",
             "unknown": "所有权快照缺失或不可读；不据此推断空闲。"}.get(status, "所有权快照状态未知。"),
            {"status": status, "entries": len(entries),
             "labels": [entry.get("label") for entry in entries if isinstance(entry, dict)][:8],
             "error": lease.get("error")},
            {"held": "接管前先按合同停止/归还当前 owner；不要凭快照过期判断空闲。",
             "blocked": "沿 session-recovery/release 显式核查并留恢复回执；不要删除记录来解锁。"}.get(status))
    except (OSError, ValueError) as error:
        add("devices.lease", "devices", "unknown", "设备所有权快照",
            "读取缓存的 lease 快照失败；不执行程序重试。",
            {"error": str(error)}, "核对 phonefarm 身份与锁根后重启服务再诊断。")
    return {"schema": SCHEMA, "node_id": node.node_id, "checked_at": checked_at, "scope": "host-only",
            "summary": {state: sum(check["status"] == state for check in checks) for state in STATUSES}, "checks": checks,
            "limitations": ["这是宿主时间点观察，不是设备资格或部署验收。", "路径和签名可能随后改变；不会自动刷新程序身份或执行修复。",
                            "未验证网络外部可达性、worker 身份下的实际写入、设备授权及执行期恢复状态。"]}
