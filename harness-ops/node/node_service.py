#!/usr/bin/env python3
"""HGamey 手机节点服务：跑在插着手机的那台机器上，只监听 127.0.0.1，由 cloudflared 隧道出网。

两张脸，管理 token（Authorization: Bearer <PHONEFARM_NODE_TOKEN>）全部可用；
另有**只读 token** = HMAC-SHA256(管理 token, "hgamey-node-readonly-v1") 的十六进制，可读 /node、/jobs 与产物，以及 POST /mcp 的只读工具
（给云端 Actions 用：泄露了也碰不了手机）。两边各自从管理 token 推导，不用传文件。

  /mcp    MCP streamable HTTP（JSON 响应形态）⇄ stdio `phonefarm serve`，工具白名单。给云端 pi 用。
  /jobs   真机任务。给开发机上的 agent 用（客户端 pf-node）：
            POST /jobs           body = tar.gz（可空）, 头 X-Job: {"bin": "phonefarm", "argv": [...], "label": "judge"}
                                 argv 里 @in/<路径> → 上传文件解开后的绝对路径；@out → 本任务输出目录
            GET  /jobs/<id>      状态 JSON（running / exit code）
            GET  /jobs/<id>/log?offset=N   stdout+stderr 增量
            GET  /jobs/<id>/out  输出目录 tar.gz（老接口，仍可用；现在从磁盘缓存发，支持 Range）
                 /out?meta=1                → {"kind": "tar.gz"|"file", "size", "sha256", "etag"}
                 /out?offset=N&length=M&etag=E  分块下载（断了从 offset 续）；etag 对不上回 409（输出变了，重来）
                 /out?path=<相对路径>[&meta=1|&offset=..]  只下输出目录里的一个文件（目录则打成 tar.gz）
                 也认标准 Range: bytes=a-b（回 206），If-Range 对不上就回整份
            POST /jobs/<id>/cancel  {"force": false} 持久化停止请求；force=true 显式升级
            POST /jobs/<id>/kill    兼容旧升级语义；killed 表示请求已记录
          X-Job 可带 "exe": "@in/<路径>"，用上传的可执行代替缺省的（测未合并分支）。
  /admin/restart  退出进程，由 run_node.sh 的循环用新代码重新拉起。
  /health 无需 token，只回 ok（给隧道探活）。
  /dash   实时看板只读看板，用独立查看密码（~/.config/hgamey/dash.pass），逻辑在 dash.py。
  /remote 远程操作设备（个人操作员令牌，MCP 在 /remote/mcp），网页控制台 /dash/control，逻辑在 node_remote.py。

phonefarm/adb/devlock 在 phonefarm 根目录下执行，game_opt_loop 在它自己的仓库目录下执行。
执行任务使用独立的锁 owner，label 只供展示；phonefarm/GOL/helper 保留内部锁路线，raw adb/fetch_apk 由 devlock run 包裹。
显式 devlock 管理命令的 acquire/renew/release 等身份仍可跨请求，不改写其位置参数。
"""
import sqlite3
import argparse, errno, hashlib, hmac, io, itertools, json, os, re, signal, subprocess, sys, tarfile, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from node_read import NodeRead
from node_history import JobHistory, HistoryError, load_history_config
from job_export_cache import ExportCache, validate_root as validate_export_cache_root
from device_registry import DeviceRegistry, RegistryError, LIMIT as MAX_DEVICE_REGISTRY
from device_scope import DeviceScope, validate_job as validate_device_job, resource as device_resource, scope as job_scope, lease_problem
from node_results import NodeResults, ResultError, MAX_RECORD, MAX_IMPORT_BODY
from node_result_search import NodeResultSearch
from node_result_compare import compare_query
from node_result_cross_compare import cross_compare_query
from node_research import NodeResearch, ResearchError, MAX_WRITE as MAX_RESEARCH_WRITE
from node_media import MediaError
from node_fleet import FleetState
from node_directory import NodeDirectory, DirectoryError
from node_games import NodeGames, GameError, MAX_MANIFEST
from node_uploads import NodeUploads
from node_deployments import NodeDeployments, DeploymentError
from game_assets import AssetUploadError, UPLOAD_MAX
import job_runtime
import job_inputs
import job_preparation
import job_budget
from job_capacity import normalize_slots, check_fit, CapacityExceeded
from runtime_identity import ConfiguredPrograms, ProgramCompatibilityError
from job_events import NodeJobEvents, JobEventError
from job_dispatch import JobDispatch, file_lock
from job_queue import RequestConflict
from job_policy import PolicyUnavailable, require_policy
from node_lease import LeaseState, device_access, job_lease_receipts
from node_session import session_call, recover_session, recovery_plan, SessionError, MAX_REQUEST
from artifact_io import (SafeRoot, ArtifactChanged, relative_path, describe_file, ensure_unchanged,
                         directory_signature, archive_directory, EXPORT_LIMITS, ExportError, export_lock)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import dash  # 实时看板 /dash（只读看板，独立查看密码）；它坏了不能拖垮 /jobs 和 /mcp
except Exception as e:  # noqa: BLE001
    dash = None
    print(f"dash 模块加载失败，/dash 不可用: {e!r}", file=sys.stderr)
try:
    import node_remote  # 远程操作 /remote 与 /dash/api/remote（操作员令牌）；坏了不拖垮其它接口
except Exception as e:  # noqa: BLE001
    node_remote = None
    print(f"node_remote 加载失败，/remote 不可用: {e!r}", file=sys.stderr)

MCP_ALLOW = {
    "phonefarm_devices", "phonefarm_fleet", "phonefarm_tasks", "phonefarm_runs", "phonefarm_last",
    "phonefarm_status", "phonefarm_show", "phonefarm_stats", "phonefarm_lessons", "phonefarm_tree",
    "phonefarm_campaign", "phonefarm_hyp", "phonefarm_pred", "phonefarm_caps", "phonefarm_tools",
    "phonefarm_schema", "phonefarm_config", "phonefarm_cat",
    "phonefarm_run", "phonefarm_benchmark", "phonefarm_script",
}
CALL_TIMEOUT = 300
OUT_CHUNK_MAX = 16 << 20   # 分块下载单块上限
MAX_UPLOAD = 512 << 20


def log(s):
    print(time.strftime("%F %T"), s, file=sys.stderr, flush=True)


# 只读 token 能调的 MCP 工具：白名单去掉会起任务、碰手机的三个
MCP_RO = MCP_ALLOW - {"phonefarm_run", "phonefarm_benchmark", "phonefarm_script"}


def readonly_token(admin_token):
    return hmac.new(admin_token.encode(), b"hgamey-node-readonly-v1", "sha256").hexdigest()


def rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


# ═════════════ /mcp ═════════════

class McpChild:
    """一个 `phonefarm serve` 子进程；JSON-RPC id 在桥内改写，多客户端互不串号；挂了自动重启。"""

    def __init__(self, root):
        self.root, self.ids, self.lock = root, itertools.count(1), threading.RLock()
        self.waiters, self.proc, self.init_req = {}, None, None
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            [os.path.join(self.root, "phonefarm"), "serve", "--root", self.root], cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        if self.init_req:
            self.request(dict(self.init_req))
            self.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _reader(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            w = self.waiters.pop(msg.get("id"), None)
            if w:
                w[1] = msg
                w[0].set()

    def _write(self, msg):
        with self.lock:
            if self.proc.poll() is not None:
                log(f"phonefarm serve 退出({self.proc.returncode})，重启")
                self._start()
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def request(self, msg):
        if msg.get("method") == "initialize":
            self.init_req = {k: v for k, v in msg.items() if k != "id"}
        orig, inner = msg.get("id"), next(self.ids)
        w = [threading.Event(), None]
        self.waiters[inner] = w
        self._write({**msg, "id": inner})
        if not w[0].wait(CALL_TIMEOUT):
            self.waiters.pop(inner, None)
            return rpc_error(orig, -32001, f"phonefarm 超过 {CALL_TIMEOUT}s 没回")
        return {**w[1], "id": orig}

    def notify(self, msg):
        self._write(msg)

    def handle(self, msg, allow=MCP_ALLOW):
        method = msg.get("method")
        if "id" not in msg:
            self.notify(msg)
            return None
        if method == "tools/call":
            name = (msg.get("params") or {}).get("name")
            if name not in allow:
                log(f"mcp 拒绝 {name}")
                return rpc_error(msg["id"], -32601, f"工具 {name} 不在白名单里")
            log(f"mcp tools/call {name}")
        resp = self.request(msg)
        if method == "tools/list" and "result" in resp:
            resp["result"]["tools"] = [t for t in resp["result"].get("tools", []) if t.get("name") in allow]
        return resp


# ═════════════ /jobs ═════════════

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _job_lock_args(argv, owner):
    """An explicit CLI label must not turn separate jobs into nested owners."""
    result, i = [], 0
    values = ({"--serial", "--so", "--mode", "--package", "--out", "--max-seconds", "--actor-id", "--power-policy",
               "--evidence-origin", "--request-id", "--request", "--session"} if argv[:1] == ["gray-layer"] else
              {"--serial", "--out", "--pairs", "--mode", "--evidence-origin"} if argv[:1] == ["gray-ab"] else set())
    while i < len(argv):
        arg = argv[i]
        if arg in values:
            if i + 1 == len(argv):
                raise ValueError(f"{arg} requires a value")
            result.extend(argv[i:i + 2])
            i += 2
        elif arg == "--lock-label":
            if i + 1 == len(argv):
                raise ValueError("--lock-label 缺少值；显示名请用任务 label")
            result.extend([arg, owner])
            i += 2
        elif arg.startswith("--lock-label="):
            result.extend(["--lock-label", owner])
            i += 1
        else:
            result.append(arg)
            i += 1
    return result


def _job_time_budget(spec):
    if "time_budget" not in spec:
        return None
    value = job_budget.normalize(spec["time_budget"])
    if device_access(spec) == "recovery":
        raise ValueError("time_budget is not supported for recovery jobs")
    return value


def _job_disk_guard(spec):
    if "disk_guard" not in spec:
        return None
    value = job_budget.normalize_disk_guard(spec["disk_guard"])
    if device_access(spec) == "recovery":
        raise ValueError("disk_guard is not supported for recovery jobs")
    return value


def _job_host_slots(spec):
    if "host_slots" not in spec:
        return None
    value = normalize_slots(spec["host_slots"])
    if device_access(spec) == "recovery":
        raise ValueError("host_slots is not supported for recovery jobs")
    return value


def read_host_capacity(path):
    """Read an explicit administrator policy before starting the service."""
    if path is None:
        return None
    with open(path, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("host capacity configuration exceeds 4 KiB")
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"schema", "slots"}
            or value["schema"] != "phonefarm/host-capacity@1"):
        raise ValueError("host capacity requires schema phonefarm/host-capacity@1 and slots")
    return normalize_slots(value["slots"])


class Jobs:
    def __init__(self, root, bins, data_dir=None, max_running=2, worker_prefix=None, host_capacity=None,
                 history_sources=None, export_cache_root=None):
        self.root, self.bins = root, bins
        self.history_sources = history_sources if history_sources is not None else []
        self.data_root = os.path.abspath(data_dir or root)
        self.update_config_dir = os.path.expanduser(os.environ.get("HG_CONFIG_DIR") or "~/.config/hgamey")
        if not os.path.isabs(self.update_config_dir):
            self.update_config_dir = os.path.join(os.path.expanduser("~/node"), self.update_config_dir)
        self.dir = os.path.join(self.data_root, "jobs" if data_dir else "_jobs")
        validate_export_cache_root(export_cache_root, self.dir, self.history_sources)
        os.makedirs(self.dir, exist_ok=True)
        self._read_root = SafeRoot(self.dir)
        self.export_cache = ExportCache(self._read_root, self.dir, export_cache_root, self.history_sources)
        if not isinstance(max_running, int) or not 1 <= max_running <= 64:
            raise ValueError("max_running must be between 1 and 64")
        self.pf_env, self.worker_entry = None, None
        if worker_prefix is not None:
            from worker_config import node_configuration, phonefarm_environment
            config = node_configuration(worker_prefix, self.dir)
            self.pf_env = phonefarm_environment(config)
            if "jobs.execution-reconcile-v1" in config.get("capabilities", []):
                self.worker_entry = os.path.join(worker_prefix, "phonefarm-worker")
        self.programs = ConfiguredPrograms(bins, self.pf_env)
        self.leases = LeaseState(bins, env=self.pf_env)
        self.device_scopes = DeviceScope(self)
        self.dispatch = JobDispatch(self, max_running, host_capacity=host_capacity)
        self._out_guard, self._sha_cache = threading.Lock(), {}

    def path(self, jid, *p):
        if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
            raise KeyError(jid)
        path = os.path.join(self.dir, jid)
        for part in ("", *p):
            path = os.path.join(path, part) if part else path
            if os.path.islink(path):
                raise ValueError("job paths cannot be symbolic links")
        return path

    def _write_meta(self, jid, meta):
        self.path(jid)  # Validate the public ID before constructing a relative path.
        self._read_root.write_json(jid + "/meta.json", meta)

    def _read_meta(self, jid):
        self.path(jid)
        return self._read_root.read_json(jid + "/meta.json")

    def start(self, spec, body, request_id=None, *, admit=None):
        if not isinstance(spec, dict):
            raise ValueError("job spec must be an object")
        policy = require_policy(spec)
        binname, argv = spec.get("bin"), spec.get("argv") or []
        if binname not in self.bins:
            raise ValueError(f"bin 只能是 {sorted(self.bins)}")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError("argv 必须是字符串数组")
        if spec.get("label") is not None and not isinstance(spec["label"], str):
            raise ValueError("label 必须是字符串")
        external_lock = binname in {"adb", "fetch_apk"}
        if external_lock and spec.get("exe"):
            raise ValueError("adb/fetch_apk 不支持 exe 替换；lease helper 请走 phonefarm 路线并自行持有 lease")
        if binname == "fetch_apk" and (len(argv) != 4 or any(a.startswith("--cache-root") for a in argv)):
            raise ValueError("fetch_apk requires source, byte size, explicit serial and sha256; cache root is node-owned")
        access = device_access(spec)
        if access not in {"none", "write", "recovery"}:
            raise ValueError("device_access must be none, write or recovery")
        time_budget = _job_time_budget(spec)
        disk_guard = _job_disk_guard(spec)
        host_slots = _job_host_slots(spec)
        recovery_for = spec.get("recovery_for")
        device_binding = validate_device_job(spec, typed_recovery=access == "recovery" and "device_binding" in spec)
        resources = spec.get("resources", [] if device_binding else
                             ["legacy-device"] if binname in {"phonefarm", "game_opt_loop", "adb", "fetch_apk"} else [])
        if access == "recovery":
            self.path(recovery_for)
            parent = self.dispatch.queue.get(recovery_for)
            if not parent:
                raise KeyError(recovery_for)
            if parent["spec"].get("device_binding") != device_binding:
                raise ValueError("recovery must inherit the original device binding")
            resources = parent["resources"]
        elif recovery_for is not None:
            raise ValueError("recovery_for requires device_access=recovery")
        if not isinstance(resources, list) or len(resources) > 32 or any(
                not isinstance(r, str) or not r or len(r) > 160 or any(ord(c) < 32 for c in r) for r in resources):
            raise ValueError("resources must be at most 32 nonempty resource names")
        if device_binding:
            resources = sorted(set(resources) | {device_resource(device_binding)})
            if len(resources) > 32:
                raise ValueError("resources including the required device reservation must fit within 32 names")
        inputs = (job_inputs.validate(spec["inputs"], getattr(self, "node_id", None))
                  if "inputs" in spec else None)
        spec = {"bin": binname, "argv": argv, "label": spec.get("label"), "resources": sorted(set(resources)),
                **({"inputs": inputs} if inputs is not None else {}),
                **({"execution_policy": policy} if "execution_policy" in spec else {}),
                **({"time_budget": time_budget} if time_budget is not None else {}),
                **({"disk_guard": disk_guard} if disk_guard is not None else {}),
                **({"host_slots": host_slots} if host_slots is not None else {}),
                **({"device_access": access} if "device_access" in spec else {}),
                **({"recovery_for": recovery_for} if recovery_for else {}),
                **({"device_binding": device_binding} if device_binding else {}),
                **({"exe": spec["exe"]} if spec.get("exe") else {})}
        if device_binding and access == "recovery":
            from node_session import validate_recovery_spec
            validate_recovery_spec(self, spec)
        original_admit = admit
        def admit():
            # submit() calls this only for a new request while holding intake.
            check_fit(spec, self.dispatch.queue.capacity())
            self.programs.require_session(spec)
            if device_binding and access == "write":
                self.device_scopes.admit(device_binding)
            if original_admit is not None:
                original_admit()
        return self.dispatch.submit(spec, body, request_id, admit=admit)

    def _prepare(self, row, body):
        spec, jid = row["spec"], row["job_id"]
        binname, argv = spec["bin"], spec["argv"]
        external_lock = binname in {"adb", "fetch_apk"}
        owner = f"job-{jid}"
        # devlock management commands retain their explicit cross-request labels.
        effective_argv = _job_lock_args(argv, owner) if binname in {"phonefarm", "game_opt_loop"} else argv
        jin, jout = self.path(jid, "in"), self.path(jid, "out")
        self._read_root.mkdir(jid)
        self._read_root.mkdir(jid + "/in")
        shared_gid = (int(self.pf_env["PHONEFARM_SHARED_GID"]) if self.pf_env is not None
                      and spec.get("execution_policy", "process_group") == "process_group" else None)
        self._read_root.mkdir(jid + "/out", shared_gid=shared_gid)
        self._write_meta(jid, {**self.dispatch.base_meta(row), "input_preparation_expected": True})
        with self._read_root.create_file(jid + "/log"):
            pass
        job_preparation.prepare(self, jid, body, spec.get("inputs"))

        def sub(a):
            a = a.replace("@out", jout)
            return re.sub(r"@in/", jin + "/", a)

        exe, cwd = self.bins[binname]
        if spec.get("inputs", {}).get("cwd") == "input":
            # Preserve a configured relative executable when changing its cwd.
            if not os.path.isabs(exe) and os.sep in exe:
                exe = os.path.abspath(os.path.join(cwd, exe))
            cwd = jin
        if spec.get("exe"):  # 用上传的可执行替换（测未合并分支）；只能指向本任务的 in 目录
            exe = os.path.realpath(sub(spec["exe"]))
            if not exe.startswith(os.path.realpath(jin) + "/") or not os.path.isfile(exe):
                raise ValueError("exe 只能是本任务上传的文件 @in/...")
            os.chmod(exe, 0o755)
        command = [exe] + [sub(a) for a in effective_argv]
        if "device_binding" in spec and spec["argv"][:2] == ["gray-layer", "session"]:
            # Binding is opaque JSON, not an @in/@out template. Check serial
            # after ordinary argument expansion before adding the owned flag.
            validate_device_job({**spec, "argv": command[1:]})
            command += ["--device-binding", json.dumps(spec["device_binding"], separators=(",", ":"))]
        if binname == "fetch_apk":
            command = [exe, "--cache-root", os.path.join(self.data_root, "_games", "cache")] + command[1:]
        if external_lock:
            command = [self.bins["devlock"][0], "run", owner, "--"] + command
        self._read_root.write_json(jid + "/prepared.json", {"command": command, "cwd": cwd})
        meta = self._read_meta(jid)
        meta["phase"] = "queued"
        self._write_meta(jid, meta)
        self._read_root.sync_tree(jid + "/in")
        self._read_root.sync(jid + "/log", directory=False)
        self._read_root.sync(jid + "/out", directory=True)
        self._read_root.sync(jid, directory=True)
        self._read_root.sync("", directory=True)

    def validate_prepared_scope(self, row, prepared):
        spec, jid = row["spec"], row["job_id"]
        if "device_binding" not in spec:
            return
        exe, cwd = self.bins["phonefarm"]
        if spec.get("inputs", {}).get("cwd") == "input":
            if not os.path.isabs(exe) and os.sep in exe:
                exe = os.path.abspath(os.path.join(cwd, exe))
            cwd = self.path(jid, "in")
        argv = [re.sub(r"@in/", self.path(jid, "in") + "/", arg.replace("@out", self.path(jid, "out")))
                for arg in _job_lock_args(spec["argv"], f"job-{jid}")]
        if spec["argv"][:2] == ["gray-layer", "session"]:
            argv += ["--device-binding", json.dumps(spec["device_binding"], separators=(",", ":"))]
        else:
            from node_session import validate_recovery_spec
            validate_recovery_spec(self, spec)
        if prepared["command"] != [exe, *argv] or prepared["cwd"] != cwd:
            raise ValueError("prepared scoped command differs from its frozen job binding")

    def status(self, jid):
        row = self.dispatch.queue.get(jid)
        reason = (row or {}).get("state_reason") or {}
        if reason.get("kind") == "cancelled_before_launch":
            meta = self.dispatch.unstarted_meta(row, {"outcome": "cancelled", "at": reason["at"],
                                                     "note": "cancelled before execution"})
        elif reason.get("kind") == "durability":
            if reason.get("unstarted"):
                meta = self.dispatch.unstarted_meta(row, reason["unstarted"])
            else:
                try:
                    meta = self._execution_status(jid)
                except (OSError, ValueError):
                    meta = self.dispatch.base_meta(row)
                    meta["recovery"] = {"status": "required", "reason": "execution record is unavailable"}
            meta.update(state="running", phase="storage_blocked")
            meta["controls"] = {"cancel_supported": False, "force_supported": False,
                                "reason": "retry-finalize to commit the job record; command is not replayed"}
        elif row and row["state"] in {"preparing", "queued"}:
            meta = self.dispatch.base_meta(row)
            meta["phase"] = row["state"]
            meta["controls"] = {"cancel_supported": True, "force_supported": False, "reason": None}
        else:
            raw = self._read_meta(jid) if row and row["state"] == "done" else None
            if raw and raw.get("phase") in {"preparing", "queued"}:
                # Cancellation won the DB transaction before launch. An intake
                # crash may leave its last metadata write behind the done row.
                meta = raw
                meta.update(state="finished", phase="finished", outcome="cancelled", exit=None,
                            recovery={"status": "not_needed", "reason": "cancelled before execution"})
                meta["execution"]["worker_alive"] = False
                request = job_runtime.read_cancel(self._read_root, jid)
                if request:
                    meta["cancel"] = {**request, "status": "not_applied", "signals": []}
                meta["controls"] = {"cancel_supported": False, "force_supported": False, "reason": "job is terminal"}
            else:
                meta = self._execution_status(jid)
        if row and row["state"] == "claimed" and meta.get("state") == "finished":
            meta.update(state="running", phase="finalizing")
            meta["controls"] = {"cancel_supported": False, "force_supported": False,
                                "reason": "waiting for worker release and result storage commit"}
        meta["input_preparation"] = job_preparation.record(self._read_root, jid, expected=meta.get("input_preparation_expected", False))
        return self.dispatch.attach(meta, row)

    def _execution_status(self, jid, worker_alive=None):
        meta = self._read_meta(jid)
        if not isinstance(meta, dict) or meta.get("id") != jid:
            raise ValueError("invalid job metadata")
        if (meta.get("execution") or {}).get("kind") == "supervised-v1":
            return self._supervised_status(jid, meta, worker_alive=worker_alive)
        if "exit" not in meta:
            try:   # 重启前起的任务：从 exitcode 文件拿真实退出码
                with self._read_root.open_file(jid + "/exitcode") as f:
                    rc = int(f.read(128).strip())
                    ended = os.fstat(f.fileno()).st_mtime
                meta.update(exit=rc, ended=ended, adopted=True)
            except (FileNotFoundError, ValueError):
                pass
        if "exit" not in meta and not _pid_alive(meta.get("pid")):
            # 节点服务重启前起的任务：进程已经结束，但新进程不是它的父进程，拿不到真实退出码。
            # 记 -1 并标注，免得客户端（pf-node wait/follow）永远等 "running"。
            meta.update(exit=-1, ended=time.time(), lost=True,
                        note="节点服务重启过，这个任务的真实退出码丢了；看日志和输出目录判断结果")
        meta["state"] = "finished" if "exit" in meta else "running"
        meta["controls"] = {"cancel_supported": False, "force_supported": False,
                            "reason": "legacy job has no verifiable worker ownership"}
        return meta

    def _supervised_status(self, jid, meta, worker_alive=None):
        alive = job_runtime.runner_alive(self._read_root, jid) if worker_alive is None else worker_alive
        if not alive:
            # A worker may have finished after our first metadata read.
            meta = self._read_meta(jid)
        terminal = meta.get("phase") in {"finished", "interrupted"}
        starting = (meta.get("phase") in {"preparing", "queued"} or
                    meta.get("phase") == "starting" and time.time() < meta["execution"].get("launch_deadline", 0))
        if not terminal and not alive and not starting:
            meta.update(phase="interrupted", outcome="interrupted", exit=-1, lost=True,
                        note="worker ownership lost; child termination and device recovery are unknown",
                        recovery={"status": "required", "reason": "worker disappeared without a terminal receipt"})
            terminal = True
        meta["state"] = "finished" if terminal else "running"
        meta["execution"]["worker_alive"] = alive
        if terminal and (meta["execution"].get("program_identity") or {}).get("state") == "pending":
            # A lost worker cannot finish its diagnostic record. Preserve raw
            # evidence and never fill it by probing today's configured program.
            meta["execution"]["program_identity"] = {
                "state": "unknown", "source": "program_self_report", "record": None,
                "reason": "worker ended before recording a program runtime identity"}
        reconciliation = meta["execution"].get("reconciliation")
        if not alive and reconciliation and reconciliation.get("state") in {"requested", "running"}:
            meta["execution"]["reconciliation"] = {
                **reconciliation, "state": "unknown", "error": "reconciliation helper exited without a terminal receipt"}
        request = job_runtime.read_cancel(self._read_root, jid)
        if request:
            receipt = meta.get("cancel") or {}
            meta["cancel"] = {**request, **receipt, "mode": request["mode"],
                              "requested_at": request["requested_at"]}
            if not receipt.get("status"):
                meta["cancel"]["status"] = "not_applied" if terminal else "requested"
            meta["cancel"].setdefault("signals", [])
            if not terminal:
                meta["phase"] = "cancelling"
        supported = not terminal and (alive or starting)
        meta["controls"] = {"cancel_supported": supported, "force_supported": supported,
                            "reason": None if supported else ("worker ownership lost" if meta.get("lost") else "job is terminal")}
        return meta

    def log_from(self, jid, offset, limit=4 << 20):
        if not 0 <= offset <= (1 << 63) - 1 or not 1 <= limit <= (4 << 20):
            raise ValueError("offset must be nonnegative; limit must be between 1 and 4194304")
        self.path(jid)
        with self._read_root.open_file(jid + "/log") as f:
            f.seek(offset)
            return f.read(limit)

    def out_blob(self, jid, rel=None):
        """Return an open regular file plus its metadata. Caller must close it.

        Every component is opened relative to the pinned jobs root. Directory
        exports retain disk caching; tar reads opened fds rather than path names.
        """
        import stat
        self.path(jid)
        rel = relative_path(rel or "")
        target = jid + "/out" + ("/" + rel if rel else "")
        fd = self._read_root.open_fd(target)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            f = os.fdopen(fd, "rb")
            try:
                info = describe_file(f, self._sha_cache, self._out_guard)
                return f, {"kind": "file", "path": os.path.normpath(rel), **info}
            except BaseException:
                f.close()
                raise
        phase, replaced = "inspect", False
        destination_fd = None

        def storage_error(error, failed_phase, original=None):
            space = failed_phase != "cleanup" and error.errno in {errno.ENOSPC, errno.EDQUOT}
            details = {"phase": failed_phase, "cache_replaced": replaced, "errno": error.errno}
            if original is not None and isinstance(original, ExportError):
                details["original_code"] = original.code
            return ExportError(507 if space else 503,
                               "export_write_space_failed" if space else "export_storage_unavailable",
                               "directory export temporary cleanup failed" if failed_phase == "cleanup"
                               else "directory export storage operation failed", details)

        def cached(signature):
            try:
                old = cache_store.read_json(cache + ".json")
                if old.get("sig") != signature or (binding is not None and old.get("binding") != binding):
                    return None
                f = cache_store.open_file(cache)
                try:
                    info = describe_file(f, self._sha_cache, self._out_guard)
                    if info["size"] == old.get("size") and info["sha256"] == old.get("info", {}).get("sha256"):
                        return f, {"kind": "tar.gz", "path": os.path.normpath(rel or "."), **info}
                except BaseException:
                    f.close()
                    raise
                f.close()
            except ExportError:
                raise
            except (FileNotFoundError, ValueError, KeyError, TypeError, AttributeError):
                # A missing or invalid cache may be regenerated; an I/O failure
                # is reported instead of silently starting another write.
                pass
            return None

        try:
            signature = directory_signature(fd, limits=EXPORT_LIMITS)
            phase = "cache_location"
            cache_store, prefix, binding = self.export_cache.target(jid)
            if binding is not None:
                binding = {**binding, "path": rel}
            cache = prefix + "/.outcache-" + hashlib.sha1(rel.encode()).hexdigest()[:10] + ".tar.gz"
            phase = "cache_lookup"
            hit = cached(signature)
            if hit is not None:
                return hit
            phase = "lock"
            with export_lock(self._read_root, jid):
                destination_fd = cache_store.open_fd(prefix, directory=True)
                phase = "cache_lookup"
                hit = cached(signature)
                if hit is not None:
                    return hit
                temporary = cache + "." + uuid.uuid4().hex + ".tmp"
                phase = "create"
                f = cache_store.create_file(temporary)
                failure = None
                try:
                    phase = "archive"
                    archived_signature = archive_directory(fd, f, limits=EXPORT_LIMITS, space_fd=destination_fd)
                    if archived_signature != signature:
                        raise ArtifactChanged("artifact directory changed before archiving; retry")
                    phase = "publish_archive"
                    replaced = None
                    cache_store.replace(temporary, cache)
                    replaced = True
                    # Rename changes the file stamp: keep the original single
                    # content hash after publication, then publish its metadata.
                    phase = "describe"
                    info = {"kind": "tar.gz", "path": os.path.normpath(rel or "."),
                            **describe_file(f, self._sha_cache, self._out_guard)}
                    phase = "publish_metadata"
                    cache_store.write_json(cache + ".json", {"sig": signature, "size": info["size"], "info": info,
                                                           **({"binding": binding} if binding is not None else {})})
                except BaseException as error:
                    failure = error
                finally:
                    try:
                        cache_store.unlink(temporary)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        failure = storage_error(error, "cleanup", failure)
                    if failure is not None:
                        try:
                            f.close()
                        except OSError as error:
                            if isinstance(failure, ExportError):
                                failure = ExportError(failure.status, failure.code, str(failure),
                                                      {**failure.details, "close_errno": error.errno})
                            else:
                                failure = storage_error(error, "close", failure)
                if failure is not None:
                    raise failure
                return f, info
        except OSError as error:
            raise storage_error(error, phase) from error
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(fd)

    def cancel(self, jid, force=False):
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        row = self.dispatch.queue.get(jid)
        if row and self.dispatch.queue.cancel_pending(jid):
            self.dispatch.finish_unstarted(row, "cancelled", "cancelled before execution")
            return {"accepted": True, "job": self.status(jid)}
        meta = self.status(jid)
        if not meta["controls"]["cancel_supported"]:
            return {"accepted": False, "job": meta, "reason": meta["controls"]["reason"],
                    "error": meta["controls"]["reason"]}
        job_runtime.request_cancel(self._read_root, jid, force=force)
        self.dispatch.start()
        return {"accepted": True, "job": self.status(jid)}

    def session(self, jid, *, request_id=None, action=None):
        return session_call(self, jid, request_id=request_id, action=action)

    def recover_session(self, jid, request_id):
        return recover_session(self, jid, request_id)

    def retry_finalize(self, jid):
        self.path(jid)
        with file_lock(self.dispatch.intake_path):
            row = self.dispatch.queue.get(jid)
            if not row:
                raise KeyError(jid)
            if row["state"] != "blocked" or (row.get("state_reason") or {}).get("kind") != "durability":
                raise RequestConflict("only a job blocked on result storage can retry-finalize")
            self._check_recovery_children(jid)
            committed = self.dispatch.finalize(row)
        self.dispatch.start()
        return {"storage_committed": committed, "job": self.status(jid)}

    def _check_recovery_children(self, jid):
        children = [r["job_id"] for r in self.dispatch.queue.rows(("preparing", "queued", "claimed", "blocked"))
                    if r["spec"].get("recovery_for") == jid]
        if children:
            raise RequestConflict("recovery jobs must finish and release any blocked reservations first: " + ", ".join(children))

    def host_execution_problem(self, jid, meta):
        """Read existing owned-scope completion facts, never infer from a PID."""
        execution = meta.get("execution") or {}
        if (meta.get("state") != "finished" or execution.get("kind") != "supervised-v1"
                or execution.get("worker_alive") is not False):
            return "source worker completion is unconfirmed; reconcile its execution domain first"
        row = self.dispatch.queue.get(jid)
        contained = ((row or {}).get("spec", {}).get("execution_policy") == "contained"
                     or meta.get("execution_policy") == "contained"
                     or (execution.get("process_management") or {}).get("policy") == "contained")
        if contained:
            return self.host_completion_problem(jid, meta)
        group_status = (execution.get("owned_process_group") or {}).get("status")
        not_started = (execution.get("child_pid") is None and group_status == "not_started"
                       and (meta.get("recovery") or {}).get("status") == "not_needed")
        if (meta.get("phase") != "finished" or meta.get("lost")
                or group_status != "absent" and not not_started):
            return "source original process group completion is unconfirmed"
        return None

    def host_completion_problem(self, jid, meta):
        """Qualify contained handback for queue release and session recovery."""
        import stat
        from job_cgroup import completion_problem
        execution = meta.get("execution") or {}
        if execution.get("worker_alive") is not False:
            return "worker completion is unconfirmed"
        problem = completion_problem(meta)
        if problem:
            return problem
        try:
            fd = self._read_root.open_fd(jid + "/out", directory=True)
            try:
                st = os.fstat(fd)
                if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
                    return "output handback to the node's private identity is unconfirmed"
            finally:
                os.close(fd)
        except (OSError, ValueError) as error:
            return "output handback cannot be verified: " + str(error)
        return None

    def reconcile_execution(self, jid, force=False):
        if type(force) is not bool:
            raise ValueError("force must be a boolean")
        if self.worker_entry is None:
            raise RequestConflict("fixed worker reconciliation is not configured on this node")
        self.path(jid)
        with file_lock(self.dispatch.intake_path):
            row = self.dispatch.queue.get(jid)
            status = self.status(jid)
            if not row or row["state"] != "blocked" or status["state"] != "finished":
                raise RequestConflict("execution reconciliation requires a finished job with a blocked reservation")
            if status.get("phase") in {"finalizing", "storage_blocked"}:
                raise RequestConflict("commit the source result before execution reconciliation")
            execution = status.get("execution") or {}
            if (execution.get("kind") != "supervised-v1" or execution.get("worker_alive") is not False
                    or row["spec"].get("execution_policy") != "contained"
                    and (execution.get("process_management") or {}).get("policy") != "contained"):
                raise RequestConflict("only a contained job without a live worker can be reconciled")
            self._check_recovery_children(jid)
            try:
                proc = job_runtime.launch_reconcile(self._read_root, jid, self.worker_entry, force)
            except ValueError as error:
                raise RequestConflict(str(error)) from None
        def finish():
            proc.wait()
            try:
                with file_lock(self.dispatch.intake_path):
                    self.dispatch.finalize(self.dispatch.queue.get(jid))
                self.dispatch.start()
            except (OSError, ValueError, sqlite3.Error) as error:
                self.dispatch.last_error = f"{type(error).__name__}: {error}"
        threading.Thread(target=finish, daemon=True).start()
        return {"accepted": True, "job": self.status(jid)}

    def release_queue(self, jid, note):
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError("release requires a nonempty review note (at most 2000 characters)")
        with file_lock(self.dispatch.intake_path):
            row = self.dispatch.queue.get(jid)
            if not row:
                raise KeyError(jid)
            if row["state"] != "blocked":
                raise RequestConflict("only a blocked reservation can be released")
            status = self.status(jid)
            lease = self.leases.snapshot(refresh=True, force=True) if device_access(row["spec"]) != "none" else None
            eligibility = self.queue_release_eligibility(row, status, lease=lease)
            if not eligibility["eligible"]:
                raise RequestConflict(eligibility["reason"])
            if not self.dispatch.release_host_capacity(row):
                raise RequestConflict("host capacity release could not be confirmed; inspect execution and storage before releasing the reservation")
            try:
                self._read_root.write_json(jid + "/queue-release.json", {"note": note.strip(), "at": time.time(), "lease_observation": lease})
                self._read_root.sync(jid, directory=True)
                self._read_root.sync("", directory=True)
            except (OSError, ValueError) as error:
                self.dispatch.block_storage(row, "queue-release", error)
                return {"released": False, "job": self.status(jid)}
            self.dispatch.queue.set_state(jid, "done")
        self.dispatch.start()
        return {"released": True, "job": self.status(jid)}

    def queue_release_eligibility(self, row, meta, *, lease=None):
        """Same advisory decision shown by GET and rechecked fresh by release."""
        reason = None
        if row["state"] != "blocked":
            reason = "only a blocked reservation can be released"
        elif (row.get("state_reason") or {}).get("kind") == "durability":
            reason = "retry-finalize must commit the result before releasing resources"
        else:
            try:
                self._check_recovery_children(row["job_id"])
            except RequestConflict as error:
                reason = str(error)
        execution = meta.get("execution") or {}
        if reason is None and row.get("host_capacity_release") is None:
            reason = self.host_execution_problem(row["job_id"], meta)
        elif reason is None and (row["spec"].get("execution_policy") == "contained"
                               or (execution.get("process_management") or {}).get("policy") == "contained"):
            reason = self.host_completion_problem(row["job_id"], meta)
        if reason is None and device_access(row["spec"]) != "none":
            observation = self.leases.snapshot() if lease is None else lease
            reason = lease_problem(row["spec"], observation, "release")
        return {"eligible": reason is None, "reason": reason, "scope": job_scope(row["spec"])}

    def kill(self, jid):
        """Compatibility: true means escalation was requested, not completed."""
        return self.cancel(jid, force=True)["accepted"]


# ═════════════ HTTP ═════════════

def make_handler(mcp, jobs, token, board=None, node=None, fleet=None, remote=None):
    jobs.dispatch.start()
    ro_token = readonly_token(token)
    node = node or NodeRead(jobs, jobs.data_root, os.path.dirname(os.path.abspath(__file__)))
    if not hasattr(jobs, "history"):
        jobs.history = JobHistory(jobs.history_sources, jobs.dir, node.node_id)
    history = jobs.history
    jobs.events = NodeJobEvents(jobs.dispatch.queue, node.node_id)
    device_registry = DeviceRegistry(jobs, node.node_id)
    if not hasattr(jobs, "results"):
        jobs.results = NodeResults(jobs, node.node_id)
    results = jobs.results
    if not hasattr(jobs, "research"):
        jobs.research = NodeResearch(results, node.node_id)
    research = jobs.research
    if not hasattr(jobs, "directory"):
        jobs.directory = NodeDirectory(jobs.data_root, node.node_id)
    directory = jobs.directory
    if not hasattr(jobs, "result_search"):
        jobs.result_search = NodeResultSearch(directory, node.node_id)
    result_search = jobs.result_search
    if not hasattr(jobs, "games"):
        jobs.games = NodeGames(jobs, node.node_id)
    games = jobs.games
    if not hasattr(jobs, "uploads"):
        jobs.uploads = NodeUploads(jobs, games)
    uploads = jobs.uploads
    if not hasattr(jobs, "deployments"):
        jobs.deployments = NodeDeployments(jobs, games)
    fleet = fleet or getattr(board, "monitor", None) or FleetState(
        jobs.root, None, os.path.join(jobs.data_root, "_dash", "screens"), lease_state=jobs.leases)
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def parse_request(self):
            self._target_continue = False
            if not super().parse_request():
                return False
            if not self._target_guard():
                return False
            if self._target_continue:
                return super().handle_expect_100()
            return True

        def handle_expect_100(self):
            # Do not invite a large request body until its target is checked.
            self._target_continue = True
            return True

        def _target_guard(self):
            # Bind the connection before routing, consuming an upload body or
            # activating observation. Legacy requests without a pin remain
            # readable/executable under their existing authorization contract.
            parsed = urlparse(self.path)
            headers = self.headers.get_all("X-Expected-Node-ID", [])
            query = parse_qs(parsed.query, keep_blank_values=True).get("expected_node_id", [])
            values = headers + query
            if not values:
                return True
            if (len(headers) > 1 or len(query) > 1
                    or any(not re.fullmatch(r"[0-9a-f]{32}", v) for v in values)
                    or len(set(values)) != 1):
                code, error, message = 400, "invalid_target", "expected node must be one consistent 32-character identity"
            else:
                # A page shell must load to explain a changed backend. Its
                # subsequent API/media requests carry the pin and are checked.
                page = parsed.path.rstrip("/") in {
                    "/dash", "/dash/jobs", "/dash/games", "/dash/fleet",
                    "/dash/control", "/dash/live", "/dash/maint", "/dash/results", "/dash/research", "/dash/login",
                }
                if (page and self.command == "GET" and not headers
                        or parsed.path == "/dash/login" and not headers
                        or values[0] == node.node_id):
                    return True
                code, error, message = 409, "target_mismatch", "connected node differs from the expected target; original request was not routed"
            self.close_connection = True
            self._send(code, {"code": error, "error": message}, extra={"Connection": "close"})
            return False

        def log_message(self, *a):
            pass

        def _send(self, code, data=b"", ctype="application/json", extra=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                for one in (v if isinstance(v, list) else [v]):
                    self.send_header(k, one)
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, allow_ro=False):
            got = self.headers.get("Authorization", "")
            self.ro = False
            if hmac.compare_digest(got, f"Bearer {token}"):
                return True
            if allow_ro and hmac.compare_digest(got, f"Bearer {ro_token}"):
                self.ro = True
                return True
            log(f"401 {self.command} {self.path} 来自 {self.headers.get('CF-Connecting-IP', self.client_address[0])}")
            self._send(401, {"error": "unauthorized"})
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            if not 0 <= n <= MAX_UPLOAD:
                raise ValueError("上传太大")
            return self.rfile.read(n) if n else b""

        def _remote(self, method):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if remote and (parts[:1] == ["remote"] or parts[:3] == ["dash", "api", "remote"] or parts == ["dash", "control"]):
                node_remote.handle(remote, self, method, parts, u.query, board, node=node)
                return True
            return False

        def do_GET(self):
            if self._remote("GET"):
                return
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if parts == ["health"]:
                return self._send(200, b"ok", "text/plain")
            if board and parts == ["dash", "api", "access"]:
                if not board.authed(self):
                    return self._send(401, {"error": "需要查看密码"})
                return self._send(200, {"steer": board.steered(self),
                                        "steer_configured": bool(board._steer_pass())})
            # Dashboard and token clients share contracts. Only explicit /fleet reads
            # activate device metadata sampling; node/jobs reads stay host-only.
            if board and parts[:2] == ["dash", "api"] and parts[2:3] in (["node"], ["nodes"], ["games"], ["uploads"], ["jobs"], ["job-sources"], ["job-events"], ["fleet"], ["queue"], ["requests"], ["leases"], ["results"], ["result-imports"], ["result-search"], ["remote-result"], ["result-cross-compare"], ["result-compare"], ["research-state"], ["research-operations"], ["research-events"]):
                if not board.authed(self):
                    return self._send(401, {"error": "需要查看密码"})
                return self._read_get(parts[2:], parse_qs(u.query))
            if board and parts[:1] == ["dash"]:  # 实时看板自己管认证（查看密码，不是管理 token）
                try:
                    return board.handle_get(self, parts, u.query)
                except Exception as e:  # noqa: BLE001
                    log(f"dash GET {u.path} 出错: {e!r}")
                    return self._send(500, {"error": "dash 内部错误"})
            if not self._authed(allow_ro=True):
                return
            return self._read_get(parts, parse_qs(u.query))

        def _read_get(self, parts, q):
            try:
                if parts == ["node"]:
                    return self._send(200, node.identity())
                if parts[:1] == ["job-sources"]:
                    q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if len(parts) == 1:
                        if set(q) - {"expected_node_id"}:
                            return self._send(400, {"code": "history_invalid_request", "error": "source discovery has no query filters"})
                        return self._send(200, history.sources())
                    if len(parts) >= 3 and parts[2] == "jobs":
                        source_id = unquote(parts[1])
                        if len(parts) == 3:
                            return self._send(200, history.list_jobs(source_id, q))
                        jid = unquote(parts[3])
                        if len(parts) == 4:
                            return self._send(200, history.detail(source_id, jid, q))
                        if len(parts) == 5 and parts[4] == "artifacts":
                            return self._send(200, history.artifacts(source_id, jid, q))
                        if len(parts) == 5 and parts[4] == "log":
                            return self._send(200, history.log_from(source_id, jid, q), "application/octet-stream")
                        if len(parts) == 5 and parts[4] == "out":
                            return self._result_source(history.out_blob(source_id, jid, q), q)
                    return self._send(404, {"code": "history_route_not_found", "error": "no such history read operation"})
                if parts == ["job-events"]:
                    q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "job events require an explicit node identity"})
                    return self._send(200, jobs.events.events(q))
                if parts == ["node", "devices"]:
                    return self._send(200, device_registry.read())
                if parts == ["node", "devices", "resolve"]:
                    if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "device resolution requires an explicit node identity"})
                    if set(q) - {"serial", "expected_node_id"} or len(q.get("serial", [])) != 1:
                        raise ValueError("device resolution requires exactly one serial")
                    return self._send(200, {"node_id": node.node_id,
                                            "binding": jobs.device_scopes.resolve(q["serial"][0])})
                if parts[:1] == ["research-state"]:
                    if len(parts) == 4 and parts[1] == "research" and parts[3] == "assessment":
                        if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                            return self._send(400, {"code": "target_required", "error": "criteria assessment requires an explicit node identity"})
                        if set(q) - {"revision", "expected_node_id"} or "revision" not in q or any(len(v) != 1 for v in q.values()):
                            raise ValueError("criteria assessment requires one explicit revision")
                        return self._send(200, research.assessment(unquote(parts[2]), int(q["revision"][0])))
                    if len(parts) == 5 and parts[1] == "request" and parts[3] == "media":
                        if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                            return self._send(400, {"code": "target_required", "error": "evaluation media requires an explicit node identity"})
                        if set(q) - {"revision", "expected_node_id"} or "revision" not in q or any(len(v) != 1 for v in q.values()):
                            raise ValueError("evaluation media requires an explicit revision and no source override")
                        blob, info = research.media(unquote(parts[2]), parts[4], int(q["revision"][0]))
                        with blob:
                            return self._stream_out(blob, info, {}, media_type=info["media"]["mime"])
                    if len(parts) == 2:
                        return self._send(200, research.list(parts[1], q))
                    if len(parts) == 4 and parts[3] == "history":
                        return self._send(200, research.history(parts[1], unquote(parts[2]), q))
                    if len(parts) == 3:
                        if set(q) - {"revision", "expected_node_id"} or any(len(v) != 1 for v in q.values()):
                            raise ValueError("unsupported research object query")
                        revision = int(q["revision"][0]) if "revision" in q else None
                        return self._send(200, research.get(parts[1], unquote(parts[2]), revision))
                if len(parts) == 2 and parts[0] == "research-operations":
                    return self._send(200, research.operation(unquote(parts[1])))
                if parts == ["research-events"]:
                    if ("cursor" in q or q.get("wait_seconds", ["0"])[0] != "0") and not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "event wait and cursor require an explicit node identity"})
                    return self._send(200, research.events(q))
                if parts == ["result-search"]:
                    if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "result search requires an explicit hub identity"})
                    return self._send(200, result_search.search(q))
                if parts in (["remote-result"], ["result-cross-compare"]):
                    q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "cross-node result reads require an explicit hub identity"})
                    value = (result_search.remote_result(q) if parts == ["remote-result"] else
                             cross_compare_query(results, result_search, node.node_id, q))
                    return self._send(200, value)
                if parts == ["result-compare"]:
                    if not (self.headers.get("X-Expected-Node-ID") or q.get("expected_node_id")):
                        return self._send(400, {"code": "target_required", "error": "result comparison requires an explicit node identity"})
                    return self._send(200, compare_query(results, node.node_id, q))
                if parts == ["results"]:
                    return self._send(200, results.list(q))
                if len(parts) == 2 and parts[0] == "results":
                    return self._send(200, results.get(unquote(parts[1])))
                if len(parts) == 4 and parts[0] == "results" and parts[2] == "sources":
                    return self._result_source(results.source(unquote(parts[1]), unquote(parts[3])), q)
                if len(parts) == 2 and parts[0] == "result-imports":
                    return self._send(200, results.imported(unquote(parts[1])))
                if len(parts) == 3 and parts[0] == "result-imports" and parts[2] == "source":
                    return self._result_source(results.imported(unquote(parts[1]), source=True), q)
                if parts == ["games"]:
                    return self._send(200, games.list())
                if len(parts) == 2 and parts[0] == "uploads":
                    return self._send(200, uploads.get(unquote(parts[1])))
                if len(parts) == 4 and parts[0] == "games" and parts[2] == "versions":
                    return self._send(200, games.get(unquote(parts[1]), unquote(parts[3])))
                if parts[:1] == ["nodes"] and len(parts) <= 2:
                    refresh = q.get("refresh", ["0"])[0]
                    devices = q.get("devices", ["0"])[0]
                    if refresh not in {"0", "1"} or devices not in {"0", "1"}:
                        raise ValueError("refresh and devices must be 0 or 1")
                    if len(parts) == 1:
                        if devices == "1":
                            raise ValueError("select a registered node before refreshing devices")
                        items = directory.refresh() if refresh == "1" else directory.list()
                        return self._send(200, {"schema_version": 1, "now": time.time(),
                                                "local": node.identity(), "items": items})
                    name = unquote(parts[1])
                    entry = (directory.refresh(name, devices=devices == "1")
                             if refresh == "1" or devices == "1" else directory.get(name))
                    return self._send(200, entry)
                if parts == ["queue"]:
                    return self._send(200, {**jobs.dispatch.snapshot(), "node_id": node.node_id})
                if parts == ["leases"]:
                    return self._send(200, {**jobs.leases.snapshot(refresh=True), "node_id": node.node_id})
                if len(parts) == 2 and parts[0] == "requests":
                    row = jobs.dispatch.queue.by_request(JobDispatch.request_key(unquote(parts[1])))
                    if not row:
                        raise KeyError(parts[1])
                    return self._send(200, jobs.status(row["job_id"]))
                if parts == ["fleet"]:
                    cached = q.get("cached", ["0"])[0]
                    if cached not in {"0", "1"}:
                        raise ValueError("cached must be 0 or 1")
                    if cached == "1":
                        return self._send(200, {**fleet.snapshot(), "node_id": node.node_id})
                    wait = float(q.get("wait", ["0"])[0])
                    if not 0 <= wait <= 5:
                        raise ValueError("wait must be between 0 and 5 seconds")
                    return self._send(200, {**fleet.request(wait), "node_id": node.node_id})
                if parts == ["jobs"]:
                    return self._send(200, node.list_jobs(q))
                if parts == ["mcp"]:
                    return self._send(405, b"", extra={"Allow": "POST"})
                if len(parts) == 2 and parts[0] == "jobs":
                    return self._send(200, jobs.status(parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "log":
                    off = int(q.get("offset", ["0"])[0])
                    limit = int(q.get("limit", [str(4 << 20)])[0])
                    return self._send(200, jobs.log_from(parts[1], off, limit), "application/octet-stream")
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "artifacts":
                    return self._send(200, node.artifacts(parts[1], q))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "inputs":
                    return self._send(200, job_inputs.record(jobs, parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "input-reference":
                    if set(q) != {"path"} or len(q["path"]) != 1:
                        raise ValueError("input reference requires exactly one artifact path")
                    return self._send(200, job_inputs.reference(jobs, parts[1], q["path"][0]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "leases":
                    jobs._read_meta(parts[1])
                    return self._send(200, job_lease_receipts(jobs._read_root, parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "deployment":
                    return self._send(200, jobs.deployments.get(parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "session":
                    return self._send(200, jobs.session(parts[1]))
                if len(parts) == 4 and parts[0] == "jobs" and parts[2:] == ["session", "recovery"]:
                    if set(q) - {"expected_node_id"}:
                        raise ValueError("session recovery plan has no source override")
                    return self._send(200, recovery_plan(jobs, parts[1]))
                if len(parts) == 5 and parts[0] == "jobs" and parts[2:4] == ["session", "actions"]:
                    return self._send(200, jobs.session(parts[1], request_id=unquote(parts[4])))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "out":
                    return self._out(parts[1], q)
            except (ResearchError, MediaError, RegistryError, JobEventError, HistoryError) as e:
                return self._send(e.status, e.payload)
            except ResultError as e:
                return self._send(e.status, {"code": e.code, "error": str(e)})
            except sqlite3.Error:
                return self._send(503, {"code": "node_storage_unavailable", "error": "node records could not be read; retain the original ID"})
            except job_inputs.InputError as e:
                return self._send(e.status, {"code": e.code, "error": str(e)})
            except AssetUploadError as e:
                return self._send(e.status, e.payload)
            except (DirectoryError, GameError) as e:
                return self._send(e.status, {"error": str(e)})
            except (SessionError, DeploymentError) as e:
                return self._send(e.status, e.payload)
            except (KeyError, FileNotFoundError):
                return self._send(404, {"error": "no such job"})
            except ExportError as e:
                return self._send(e.status, e.payload)
            except ArtifactChanged as e:
                return self._send(409, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _out(self, jid, q):
            rel = (q.get("path") or [""])[0] or None
            blob, info = jobs.out_blob(jid, rel)
            with blob:
                return self._stream_out(blob, info, q)

        def _result_source(self, source, q):
            blob, info = source
            if isinstance(blob, bytes):
                # Imported JSON is already bounded, immutable and hashed once
                # in the transaction. It needs no temporary file or rehash.
                if "meta" in q:
                    return self._send(200, info)
                return self._send(200, blob, "application/json; charset=utf-8", {
                    "Content-Disposition": "attachment", "X-Content-SHA256": info["sha256"],
                    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
            with blob:
                return self._stream_out(blob, info, q)

        def _device_registry_post(self, dashboard):
            self.close_connection = True
            if not self._upload_write_auth(dashboard):
                return
            try:
                if not (self.headers.get("X-Expected-Node-ID") or
                        parse_qs(urlparse(self.path).query).get("expected_node_id")):
                    return self._send(400, {"code": "target_required", "error": "device registration requires an explicit node identity"})
                length = self._upload_length(MAX_DEVICE_REGISTRY, 1)
                if self.headers.get_content_type() != "application/json":
                    raise RegistryError("invalid_request", "device registration requires application/json")
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise RegistryError("invalid_request", "device registration body was interrupted")
                return self._send(200, device_registry.write(body))
            except RegistryError as error:
                return self._send(error.status, error.payload)
            except AssetUploadError:
                return self._send(400, {"code": "invalid_request", "error": "device registration requires a Content-Length of 1 to 65536 bytes"})
            except (ValueError, OSError):
                return self._send(503, {"code": "unavailable", "error": "registration could not be confirmed; read the registry before any explicit retry"})

        def _runtime_refresh(self, dashboard):
            self.close_connection = True
            if not self._upload_write_auth(dashboard):
                return
            try:
                if not (self.headers.get("X-Expected-Node-ID") or
                        parse_qs(urlparse(self.path).query).get("expected_node_id")):
                    return self._send(400, {"code": "target_required", "error": "runtime refresh requires an explicit node identity"})
                length = self._upload_length(1024, 2)
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("runtime refresh requires application/json")
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body) != length or json.loads(body) != {}:
                    raise ValueError("runtime refresh requires an empty JSON object")
                return self._send(200, node.refresh_runtime())
            except AssetUploadError as error:
                return self._send(error.status, error.payload)
            except (ValueError, OSError) as error:
                return self._send(400, {"code": "invalid_runtime_refresh", "error": str(error)})

        def _research_post(self, parts, dashboard):
            self.close_connection = True
            if not self._upload_write_auth(dashboard):
                return
            try:
                if len(parts) != 3 or parts[0] != "research-state":
                    raise ResearchError(404, "research_operation_not_found", "no such research write operation")
                if not (self.headers.get("X-Expected-Node-ID") or
                        parse_qs(urlparse(self.path).query).get("expected_node_id")):
                    raise ResearchError(400, "target_required", "research writes require an explicit node identity")
                length = self._upload_length(MAX_RESEARCH_WRITE, 1)
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("research writes require application/json")
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("research body was interrupted; query the original operation ID")
                value = research.write(parts[1], unquote(parts[2]), json.loads(raw),
                                       "dashboard_steer" if dashboard else "admin_token")
                return self._send(201, value)
            except (ResearchError, MediaError) as e:
                return self._send(e.status, e.payload)
            except AssetUploadError as e:
                return self._send(e.status, {"code": "invalid_research_request", "error": e.message})
            except (OSError, sqlite3.Error):
                return self._send(503, {"code": "research_storage_error", "error": "save could not be confirmed; query the original operation ID"})
            except (ValueError, TypeError, OverflowError) as e:
                return self._send(400, {"code": "invalid_research", "error": str(e)})

        def _result_post(self, parts, dashboard):
            self.close_connection = True
            if not self._upload_write_auth(dashboard):
                return
            try:
                if parts not in (["results"], ["result-imports"]):
                    raise ResultError(404, "result_operation_not_found", "no such result operation")
                if not (self.headers.get("X-Expected-Node-ID") or
                        parse_qs(urlparse(self.path).query).get("expected_node_id")):
                    raise ResultError(400, "target_required", "result registration requires an expected node identity")
                maximum = MAX_IMPORT_BODY if parts == ["result-imports"] else MAX_RECORD
                length = self._upload_length(maximum, 1)
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("result writes require application/json")
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("result request body was interrupted; query the original ID")
                payload = json.loads(raw)
                value = results.import_report(payload) if parts == ["result-imports"] else results.register(payload)
                return self._send(201, value)
            except ResultError as e:
                return self._send(e.status, {"code": e.code, "error": str(e)})
            except AssetUploadError as e:
                return self._send(e.status, {"code": "invalid_result_request", "error": e.message})
            except (OSError, sqlite3.Error):
                return self._send(503, {"code": "result_storage_error", "error": "registration could not be confirmed; query the original ID"})
            except (ValueError, TypeError, OverflowError) as e:
                return self._send(400, {"code": "invalid_result", "error": str(e)})

        def _stream_out(self, blob, info, q, *, media_type=None):
            ensure_unchanged(blob)
            if "meta" in q:
                return self._send(200, info)
            size, etag = info["size"], info["etag"]
            if "etag" in q and q["etag"][0] != etag:
                return self._send(409, {"error": "输出内容变了，从头下载", **info})
            start, end, status = 0, size, 200
            if "offset" in q:  # 分块接口（走 Cloudflare 稳妥，不依赖 Range 头被透传）
                start = int(q["offset"][0])
                length = int((q.get("length") or [OUT_CHUNK_MAX])[0])
                if length < 1:
                    return self._send(416, {"error": "length must be positive"})
                end = min(size, start + min(length, OUT_CHUNK_MAX))
                if not 0 <= start <= size:
                    return self._send(416, {"error": "offset 越界", **info})
            else:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                ifr = self.headers.get("If-Range", "").strip('" ')
                if m and m.group(0) != "bytes=-" and (not ifr or ifr == etag):
                    a, b = m.groups()
                    if a:
                        start, end = int(a), min(size, int(b) + 1) if b else size
                    else:  # bytes=-N：最后 N 字节
                        start, end = max(0, size - int(b)), size
                    if start >= size or end <= start:
                        return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    status = 206
            ensure_unchanged(blob)
            self.send_response(status)
            self.send_header("Content-Type", media_type or ("application/gzip" if info["kind"] == "tar.gz" else "application/octet-stream"))
            self.send_header("Content-Length", str(end - start))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", "inline" if media_type else "attachment")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", f'"{etag}"')
            self.send_header("X-Content-SHA256", info["sha256"])
            self.send_header("X-Total-Size", str(size))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
            self.end_headers()
            blob.seek(start)
            left = end - start
            while left > 0:
                b = blob.read(min(left, 256 << 10))
                if not b:
                    # Content-Length was already sent: terminate this response
                    # instead of leaving keep-alive clients waiting indefinitely.
                    self.close_connection = True
                    break
                self.wfile.write(b)
                left -= len(b)

        def do_DELETE(self):
            if self._remote("DELETE"):
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if self._history_read_only(parts):
                return
            if self._authed():
                self._send(200, b"")

        def _history_read_only(self, parts):
            parts = [unquote(p) for p in parts]
            dashboard = parts[:3] == ["dash", "api", "job-sources"]
            if parts[:1] != ["job-sources"] and not dashboard:
                return False
            # The target guard already ran. Never consume a mutation body or
            # allow its leftover bytes to become the next keep-alive request.
            self.close_connection = True
            if dashboard:
                if not board or not board.authed(self):
                    self._send(401, {"error": "需要查看密码"})
                    return True
            elif not self._authed(allow_ro=True):
                return True
            self._send(405, {"code": "history_read_only", "error": "historical job sources are read-only"},
                       extra={"Allow": "GET", "Connection": "close"})
            return True

        def _upload_write_auth(self, dashboard):
            if not dashboard:
                return self._authed()
            if not board.authed(self):
                self._send(401, {"error": "需要查看密码"})
            elif self.headers.get("X-HG") != "1":
                self._send(400, {"error": "请求格式不对"})
            elif not board.steered(self):
                self._send(403, {"error": "需要掌舵密码", "need_steer": True})
            else:
                return True
            return False

        def _upload_length(self, maximum, minimum=0):
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,20}", lengths[0]):
                raise AssetUploadError(400, "invalid_upload_length", "a single Content-Length is required; chunked transfer is unsupported")
            length = int(lengths[0])
            if not minimum <= length <= maximum:
                raise AssetUploadError(413, "invalid_upload_length", "upload request exceeds its permitted size")
            return length

        def _upload_post(self, parts, dashboard):
            self.close_connection = True
            if not self._upload_write_auth(dashboard):
                return
            try:
                length = self._upload_length(MAX_MANIFEST)
                if self.headers.get_content_type() != "application/json":
                    raise AssetUploadError(400, "invalid_upload_type", "upload action requires application/json")
                self.connection.settimeout(30)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise AssetUploadError(400, "upload_interrupted", "upload action body was interrupted; query the same ID")
                code, result = uploads.write([unquote(p) for p in parts], json.loads(body or b"{}"))
                return self._send(code, result)
            except AssetUploadError as e:
                return self._send(e.status, e.payload)
            except GameError as e:
                return self._send(e.status, {"error": str(e)})
            except RequestConflict as e:
                return self._send(409, {"error": str(e)})
            except (OSError, sqlite3.Error):
                return self._send(503, {"code": "upload_storage_error", "error": "upload could not be confirmed; query the same ID"})
            except ValueError:
                return self._send(400, {"code": "invalid_upload", "error": "invalid upload action"})

        def do_PUT(self):
            # Always close chunk connections, including rejected/unconsumed
            # bodies, so residual file bytes cannot become another HTTP request.
            self.close_connection = True
            if self._remote("PUT"):
                return
            parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
            if self._history_read_only(parts):
                return
            dashboard = bool(board and parts[:3] == ["dash", "api", "uploads"])
            if dashboard:
                parts = parts[2:]
            if not self._upload_write_auth(dashboard):
                return
            try:
                if len(parts) != 2 or parts[0] != "uploads":
                    raise AssetUploadError(404, "upload_operation_not_found", "no such upload operation")
                length = self._upload_length(UPLOAD_MAX, 1)
                if self.headers.get_content_type() != "application/octet-stream":
                    raise AssetUploadError(400, "invalid_upload_type", "upload chunk requires application/octet-stream")
                offsets = self.headers.get_all("Upload-Offset", [])
                if len(offsets) != 1 or not re.fullmatch(r"[0-9]{1,20}", offsets[0]):
                    raise AssetUploadError(400, "invalid_upload_offset", "a nonnegative Upload-Offset is required")
                handler, deadline = self, time.monotonic() + 30

                class Body:
                    def read(self, size):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("upload body deadline")
                        handler.connection.settimeout(remaining)
                        return handler.rfile.read1(size)

                return self._send(200, uploads.chunk(parts[1], int(offsets[0]), length, Body()))
            except AssetUploadError as e:
                return self._send(e.status, e.payload)
            except OSError:
                return self._send(503, {"code": "upload_storage_error", "error": "upload could not be confirmed; query the same ID"})

        def do_POST(self):
            if self._remote("POST"):
                return
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if self._history_read_only(parts):
                return
            if parts == ["node", "devices"]:
                return self._device_registry_post(False)
            if board and parts == ["dash", "api", "node", "devices"]:
                return self._device_registry_post(True)
            if parts == ["node", "runtime", "refresh"]:
                return self._runtime_refresh(False)
            if board and parts == ["dash", "api", "node", "runtime", "refresh"]:
                return self._runtime_refresh(True)
            if parts[:1] == ["research-state"]:
                return self._research_post(parts, False)
            if board and parts[:3] == ["dash", "api", "research-state"]:
                return self._research_post(parts[2:], True)
            if parts[:1] in (["results"], ["result-imports"]):
                return self._result_post(parts, False)
            if board and parts[:2] == ["dash", "api"] and parts[2:3] in (["results"], ["result-imports"]):
                return self._result_post(parts[2:], True)
            if parts[:1] == ["uploads"]:
                return self._upload_post(parts, False)
            if board and parts[:3] == ["dash", "api", "uploads"]:
                return self._upload_post(parts[2:], True)
            if board and parts[:1] == ["dash"]:
                n = int(self.headers.get("Content-Length", 0))
                if n > (256 << 10):
                    self.close_connection = True
                    return self._send(413, {"error": "太大"})
                try:
                    return board.handle_post(self, parts, self.rfile.read(n) if n else b"")
                except Exception as e:  # noqa: BLE001
                    log(f"dash POST {self.path} 出错: {e!r}")
                    return self._send(500, {"error": "dash 内部错误"})
            if not self._authed(allow_ro=parts == ["mcp"]):
                return
            try:
                if parts[:1] == ["games"]:
                    if not 0 <= int(self.headers.get("Content-Length", 0)) <= MAX_MANIFEST:
                        self.close_connection = True
                        return self._send(413, {"error": "game request exceeds 64 KiB"})
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        self.close_connection = True
                        return self._send(400, {"error": "game request requires application/json"})
                    code, result = games.write([unquote(p) for p in parts], json.loads(self._body() or b"{}"),
                                               self.headers.get("X-Request-ID"))
                    return self._send(code, result)
                if parts[:1] == ["nodes"] and len(parts) <= 2:
                    if not 0 <= int(self.headers.get("Content-Length", 0)) <= MAX_REQUEST:
                        self.close_connection = True
                        return self._send(413, {"error": "node registration exceeds 64 KiB"})
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        return self._send(400, {"error": "node registration requires application/json"})
                    data = json.loads(self._body() or b"{}")
                    if len(parts) == 1:
                        return self._send(201, directory.create(data))
                    return self._send(200, directory.update(unquote(parts[1]), data))
                if len(parts) == 4 and parts[0] == "jobs" and parts[2:] == ["execution", "reconcile"]:
                    if int(self.headers.get("Content-Length", 0)) > MAX_REQUEST:
                        self.close_connection = True
                        return self._send(413, {"error": "execution reconciliation exceeds 64 KiB"})
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        return self._send(400, {"error": "execution reconciliation requires application/json"})
                    data = json.loads(self._body() or b"{}")
                    if not isinstance(data, dict) or set(data) - {"force"}:
                        raise ValueError("execution reconciliation accepts only a force boolean")
                    return self._send(200, jobs.reconcile_execution(parts[1], force=data.get("force", False)))
                if len(parts) == 4 and parts[0] == "jobs" and parts[2:] == ["deployment", "recover"]:
                    if not 0 <= int(self.headers.get("Content-Length", 0)) <= MAX_REQUEST:
                        self.close_connection = True
                        return self._send(413, {"error": "deployment recovery request exceeds 64 KiB"})
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        self.close_connection = True
                        return self._send(400, {"error": "deployment recovery requires application/json"})
                    return self._send(200, jobs.deployments.recover(parts[1], json.loads(self._body() or b"{}"),
                                                                   self.headers.get("X-Request-ID")))
                if len(parts) == 4 and parts[0] == "jobs" and parts[2] == "session" and parts[3] in {"actions", "recover"}:
                    if int(self.headers.get("Content-Length", 0)) > MAX_REQUEST:
                        self.close_connection = True
                        return self._send(413, {"error": "session action exceeds 64 KiB"})
                    if "application/json" not in self.headers.get("Content-Type", ""):
                        return self._send(400, {"error": "session action requires application/json"})
                    action = json.loads(self._body())
                    if not isinstance(action, dict):
                        raise ValueError("session action must be a JSON object")
                    if parts[3] == "recover":
                        if action:
                            raise ValueError("session recovery body must be an empty object; source paths and serial are derived by the node")
                        return self._send(200, jobs.recover_session(parts[1], self.headers.get("X-Request-ID")))
                    return self._send(200, jobs.session(parts[1], action=action))
                if parts == ["jobs"]:
                    # Refuse an unavailable guarantee before reading a potentially large upload.
                    try:
                        spec = json.loads(self.headers.get("X-Job", "{}"))
                        require_policy(spec)
                        _job_time_budget(spec)
                        _job_disk_guard(spec)
                        _job_host_slots(spec)
                        validate_device_job(spec, typed_recovery=device_access(spec) == "recovery" and "device_binding" in spec)
                        if "device_binding" in spec and not (self.headers.get("X-Expected-Node-ID") or
                                parse_qs(urlparse(self.path).query).get("expected_node_id")):
                            self.close_connection = True
                            return self._send(400, {"code": "target_required", "error": "bound jobs require an explicit node identity"})
                        if "inputs" in spec:
                            job_inputs.validate(spec["inputs"], node.node_id)
                        key = self.headers.get("X-Request-ID")
                        if key is not None:
                            key = jobs.dispatch.request_key(key)
                        if key is None or jobs.dispatch.queue.by_request(key) is None:
                            check_fit(spec, jobs.dispatch.queue.capacity())
                            jobs.programs.require_session(spec)
                    except (ValueError, CapacityExceeded):
                        self.close_connection = True
                        raise
                body = self._body()
                if parts == ["mcp"]:
                    return self._mcp(body)
                if parts == ["jobs"]:
                    return self._send(200, jobs.start(spec, body, request_id=self.headers.get("X-Request-ID")))
                if parts == ["admin", "restart"]:  # 交给外层守护循环重新拉起（加载新代码）
                    self._send(200, {"restarting": True})
                    threading.Timer(0.5, lambda: os._exit(0)).start()
                    return
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] in {"cancel", "kill"}:
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("cancel body must be an object")
                    receipt = jobs.cancel(parts[1], force=True if parts[2] == "kill" else data.get("force", False))
                    if parts[2] == "kill":
                        receipt["killed"] = receipt["accepted"]
                    code = 409 if not receipt["accepted"] and receipt["job"]["state"] == "running" else 200
                    return self._send(code, receipt)
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "retry-finalize":
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("retry-finalize body must be an object")
                    return self._send(200, jobs.retry_finalize(parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "queue-release":
                    data = json.loads(body or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("release body must be an object")
                    return self._send(200, jobs.release_queue(parts[1], data.get("note")))
            except RegistryError as e:
                return self._send(e.status, e.payload)
            except (DirectoryError, GameError) as e:
                return self._send(e.status, {"error": str(e)})
            except sqlite3.Error as e:
                jobs.dispatch.last_error = f"{type(e).__name__}: {e}"
                return self._send(503, {"error": "job storage commit could not be confirmed; query job and queue before retrying"})
            except (SessionError, DeploymentError) as e:
                return self._send(e.status, e.payload)
            except (KeyError, FileNotFoundError):
                return self._send(404, {"error": "no such job"})
            except RequestConflict as e:
                return self._send(409, {"error": str(e)})
            except PolicyUnavailable as e:
                return self._send(422, e.payload)
            except (CapacityExceeded, ProgramCompatibilityError) as e:
                return self._send(e.status, e.payload)
            except (ValueError, tarfile.TarError) as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def _mcp(self, body):
            try:
                msg = json.loads(body)
            except ValueError:
                return self._send(400, rpc_error(None, -32700, "parse error"))
            allow = MCP_RO if self.ro else MCP_ALLOW
            out = ([r for r in (mcp.handle(m, allow) for m in msg) if r] if isinstance(msg, list)
                   else mcp.handle(msg, allow))
            if not out:
                return self._send(202, b"")
            extra = {}
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                extra["Mcp-Session-Id"] = uuid.uuid4().hex
            self._send(200, out, extra=extra)

    return H


def argument_parser():
    def bounded_int(low, high):
        def parse(value):
            number = int(value)
            if not low <= number <= high:
                raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
            return number
        return parse

    def history_path(value):
        if not os.path.isabs(value):
            raise argparse.ArgumentTypeError("history roots configuration must be an absolute path")
        return value

    ap = argparse.ArgumentParser()
    ap.add_argument("--phonefarm", default=os.path.expanduser("~/node/phonefarm"), help="phonefarm 根目录（含 phonefarm 可执行）")
    ap.add_argument("--game-opt-loop", default=os.path.expanduser("~/node/game_opt_loop"), help="game_opt_loop 仓库目录（含 game_opt_loop 可执行），与 phonefarm 同级")
    ap.add_argument("--port", type=bounded_int(0, 65535), default=8765)
    ap.add_argument("--data-dir", help="独立运行数据根；新任务写到其中jobs/，不自动搬迁旧_jobs")
    ap.add_argument("--max-running", type=bounded_int(1, 64), default=2, help="宿主队列并发上限（含待核查的资源保留），默认2")
    ap.add_argument("--host-capacity", help="管理员宿主调度容量 JSON 文件（最多4KiB）；省略沿用SQL配置，首次按max-running初始化CPU slots")
    ap.add_argument("--worker-prefix", help="管理员安装的固定worker目录；共用PF环境，不启用contained准入")
    ap.add_argument("--history-roots", type=history_path, help="旧作业只读来源的绝对配置路径（hgamey/job-history@1，最多64KiB）")
    ap.add_argument("--export-cache-root", help="当前作业目录导出的独立绝对缓存根；省略保留旧布局，不迁移原件或游戏资源")
    return ap


def main():
    ap = argument_parser()
    a = ap.parse_args()
    try:
        history_sources = load_history_config(a.history_roots)
        host_capacity = read_host_capacity(a.host_capacity)
        jobs_root = os.path.join(os.path.abspath(a.data_dir or a.phonefarm), "jobs" if a.data_dir else "_jobs")
        validate_export_cache_root(a.export_cache_root, jobs_root, history_sources)
    except (OSError, ValueError) as error:
        ap.error(str(error))
    # 兼容直接 shell 后台启动：继承的 SIGINT 忽略态会穿过 exec，让作业收不到停止信号。
    # 装回 Python 处理器后，子进程 exec 会将它重置为默认。
    signal.signal(signal.SIGINT, signal.default_int_handler)
    token = os.environ.get("PHONEFARM_NODE_TOKEN", "")
    if len(token) < 32:
        sys.exit("PHONEFARM_NODE_TOKEN 未设置或太短（至少 32 字符）")
    root = os.path.abspath(a.phonefarm)
    gol = os.path.abspath(a.game_opt_loop)
    bins = {"phonefarm": (os.path.join(root, "phonefarm"), root),  # 名字 → (可执行, 工作目录)
            "game_opt_loop": (os.path.join(gol, "game_opt_loop"), gol),
            "adb": ("/opt/homebrew/bin/adb", root), "devlock": (os.path.join(root, "tools", "devlock"), root),
            # 更新节点上的 phonefarm / game_opt_loop：上传的 tar 覆盖到对应目录（保留 runs 等数据）
            "update_node": (os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_node.sh"), os.path.dirname(root)),
            # 节点自己下载 APK 并装到手机（大安装包不经开发机）
            "fetch_apk": (os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_apk.sh"), os.path.dirname(root))}
    jobs = Jobs(root, bins, data_dir=a.data_dir, max_running=a.max_running, worker_prefix=a.worker_prefix,
                host_capacity=host_capacity, history_sources=history_sources, export_cache_root=a.export_cache_root)
    fleet = FleetState(root, None, os.path.join(jobs.data_root, "_dash", "screens"), lease_state=jobs.leases)
    board = None
    if dash:
        try:
            board = dash.Dash(root, gol, jobs.dir, jobs=jobs, fleet=fleet)
        except Exception as e:  # noqa: BLE001
            log(f"dash 起不来，/dash 不可用: {e!r}")
    remote = None
    if node_remote:
        try:
            import shutil
            from remote_owner import PFRemote
            pf_exe, pf_cwd = jobs.leases.bins["phonefarm"]
            owner_backend = PFRemote(pf_exe, pf_cwd, jobs.leases.env,
                                     os.path.join(jobs.data_root, "_remote"))
            owner_backend.env.setdefault("ADB_BIN", bins["adb"][0] if os.path.isfile(bins["adb"][0])
                                         else shutil.which("adb", path=owner_backend.env.get("PATH")) or "adb")
            remote = node_remote.Remote(node_remote.storage_from_leases(jobs.leases),
                adb=owner_backend.env.get("ADB_BIN", bins["adb"][0]),
                hdc=owner_backend.env.get("HDC_BIN") or shutil.which("hdc", path=owner_backend.env.get("PATH")),
                backend=owner_backend)
        except Exception as e:  # noqa: BLE001
            log(f"node_remote 起不来，/remote 不可用: {e!r}")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(McpChild(root), jobs, token, board, fleet=fleet,
                                                                  remote=remote))
    log(f"节点服务 127.0.0.1:{srv.server_port}  /mcp({len(MCP_ALLOW)} 工具)  /jobs({', '.join(sorted(bins))})"
        f"  entry={os.path.abspath(__file__)}  phonefarm={root}"
        f"  data_root={jobs.data_root}  jobs_root={jobs.dir}  node_id={jobs.node_id}")
    try:
        srv.serve_forever()
    finally:
        if remote:
            remote.close()
        jobs.directory.close()
        jobs.games.close()
        jobs.history.close()
        jobs.export_cache.close()
        fleet.close()
        srv.server_close()


if __name__ == "__main__":
    main()
