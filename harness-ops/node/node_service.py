#!/usr/bin/env python3
"""HGamey 手机节点服务：跑在插着手机的那台机器上，只监听 127.0.0.1，由 cloudflared 隧道出网。

两张脸，管理 token（Authorization: Bearer <PHONEFARM_NODE_TOKEN>）全部可用；
另有**只读 token** = HMAC-SHA256(管理 token, "hgamey-node-readonly-v1") 的十六进制，只能 POST /mcp、只能调只读工具
（给云端 Actions 用：泄露了也碰不了手机）。两边各自从管理 token 推导，不用传文件。

  /mcp    MCP streamable HTTP（JSON 响应形态）⇄ stdio `phonefarm serve`，工具白名单。给云端 pi 用。
  /jobs   真机任务。给开发机上的 agent 用（客户端 pf-node）：
            POST /jobs           body = tar.gz（可空）, 头 X-Job: {"bin": "phonefarm", "argv": [...], "label": "judge"}
                                 argv 里 @in/<路径> → 上传文件解开后的绝对路径；@out → 本任务输出目录
            GET  /jobs/<id>      状态 JSON（running / exit code）
            GET  /jobs/<id>/log?offset=N   stdout+stderr 增量
            GET  /jobs/<id>/out  输出目录 tar.gz
            POST /jobs/<id>/kill
          X-Job 可带 "exe": "@in/<路径>"，用上传的可执行代替缺省的（测未合并分支）。
  /admin/restart  退出进程，由 run_node.sh 的循环用新代码重新拉起。
  /health 无需 token，只回 ok（给隧道探活）。
  /dash   作战室只读看板，用独立查看密码（~/.config/hgamey/dash.pass），逻辑在 dash.py。

phonefarm/adb/devlock 在 phonefarm 根目录下执行，game_opt_loop 在它自己的仓库目录下执行（相对路径的 knobs/、loop_v1/ 照常可用）；碰设备的子命令自己拿设备锁、排队。
"""
import argparse, hmac, io, itertools, json, os, re, signal, subprocess, sys, tarfile, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import dash  # 作战室 /dash（只读看板，独立查看密码）；它坏了不能拖垮 /jobs 和 /mcp
except Exception as e:  # noqa: BLE001
    dash = None
    print(f"dash 模块加载失败，/dash 不可用: {e!r}", file=sys.stderr)

MCP_ALLOW = {
    "phonefarm_devices", "phonefarm_fleet", "phonefarm_tasks", "phonefarm_runs", "phonefarm_last",
    "phonefarm_status", "phonefarm_show", "phonefarm_stats", "phonefarm_lessons", "phonefarm_tree",
    "phonefarm_campaign", "phonefarm_hyp", "phonefarm_pred", "phonefarm_caps", "phonefarm_tools",
    "phonefarm_schema", "phonefarm_config", "phonefarm_cat",
    "phonefarm_run", "phonefarm_benchmark", "phonefarm_script",
}
CALL_TIMEOUT = 300
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


class Jobs:
    def __init__(self, root, bins):
        self.root, self.bins = root, bins
        self.dir = os.path.join(root, "_jobs")
        os.makedirs(self.dir, exist_ok=True)
        self.procs = {}

    def path(self, jid, *p):
        if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
            raise KeyError(jid)
        return os.path.join(self.dir, jid, *p)

    def start(self, spec, body):
        binname, argv = spec.get("bin"), spec.get("argv") or []
        if binname not in self.bins:
            raise ValueError(f"bin 只能是 {sorted(self.bins)}")
        if not all(isinstance(a, str) for a in argv):
            raise ValueError("argv 必须是字符串数组")
        jid = uuid.uuid4().hex[:12]
        jin, jout = self.path(jid, "in"), self.path(jid, "out")
        os.makedirs(jin)
        os.makedirs(jout)
        if body:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
                t.extractall(jin, filter="data")

        def sub(a):
            a = a.replace("@out", jout)
            return re.sub(r"@in/", jin + "/", a)

        exe, cwd = self.bins[binname]
        if spec.get("exe"):  # 用上传的可执行替换（测未合并分支）；只能指向本任务的 in 目录
            exe = os.path.realpath(sub(spec["exe"]))
            if not exe.startswith(os.path.realpath(jin) + "/") or not os.path.isfile(exe):
                raise ValueError("exe 只能是本任务上传的文件 @in/...")
            os.chmod(exe, 0o755)
        cmd = [exe] + [sub(a) for a in argv]
        env = dict(os.environ, DEVLOCK_LABEL=spec.get("label") or "node", PF_JOB_OUT=jout, PF_JOB_IN=jin)
        logf = open(self.path(jid, "log"), "wb")
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
        meta = {"id": jid, "bin": binname, "argv": argv, "label": spec.get("label"), "started": time.time(), "pid": p.pid}
        json.dump(meta, open(self.path(jid, "meta.json"), "w"), ensure_ascii=False)
        self.procs[jid] = p
        threading.Thread(target=self._wait, args=(jid, p, logf), daemon=True).start()
        log(f"job {jid} [{spec.get('label')}] {binname} {' '.join(argv)[:200]}")
        return meta

    def _wait(self, jid, p, logf):
        rc = p.wait()
        logf.close()
        meta = json.load(open(self.path(jid, "meta.json")))
        meta.update(exit=rc, ended=time.time())
        json.dump(meta, open(self.path(jid, "meta.json"), "w"), ensure_ascii=False)
        log(f"job {jid} exit {rc}")

    def status(self, jid):
        meta = json.load(open(self.path(jid, "meta.json")))
        if "exit" not in meta and jid not in self.procs and not _pid_alive(meta.get("pid")):
            # 节点服务重启前起的任务：进程已经结束，但新进程不是它的父进程，拿不到真实退出码。
            # 记 -1 并标注，免得客户端（pf-node wait/follow）永远等 "running"。
            meta.update(exit=-1, ended=time.time(), lost=True,
                        note="节点服务重启过，这个任务的真实退出码丢了；看日志和输出目录判断结果")
            json.dump(meta, open(self.path(jid, "meta.json"), "w"), ensure_ascii=False)
        meta["state"] = "finished" if "exit" in meta else "running"
        return meta

    def log_from(self, jid, offset):
        with open(self.path(jid, "log"), "rb") as f:
            f.seek(offset)
            return f.read(4 << 20)

    def out_tar(self, jid):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(self.path(jid, "out"), arcname=".")
        return buf.getvalue()

    def kill(self, jid):
        """先 SIGINT（让 phonefarm 走还原流程）；15 秒后还活着升 SIGTERM，再 15 秒升 SIGKILL（整个进程组）。
        排队等锁的进程有的不理 SIGINT，只发一次会出现「kill 回 true 但永远 running」。"""
        p = self.procs.get(jid)
        if not p or p.poll() is not None:
            return False

        def escalate():
            for sig, wait in ((signal.SIGINT, 15), (signal.SIGTERM, 15), (signal.SIGKILL, 0)):
                if p.poll() is not None:
                    return
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    return
                log(f"job {jid} 发 {sig.name}")
                try:
                    p.wait(wait)
                except subprocess.TimeoutExpired:
                    pass

        threading.Thread(target=escalate, daemon=True).start()
        return True


# ═════════════ HTTP ═════════════

def make_handler(mcp, jobs, token, board=None):
    ro_token = readonly_token(token)
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, data=b"", ctype="application/json", extra=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
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
            if n > MAX_UPLOAD:
                raise ValueError("上传太大")
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if parts == ["health"]:
                return self._send(200, b"ok", "text/plain")
            if board and parts[:1] == ["dash"]:  # 作战室自己管认证（查看密码，不是管理 token）
                try:
                    return board.handle_get(self, parts, u.query)
                except Exception as e:  # noqa: BLE001
                    log(f"dash GET {u.path} 出错: {e!r}")
                    return self._send(500, {"error": "dash 内部错误"})
            if not self._authed():
                return
            try:
                if parts == ["mcp"]:
                    return self._send(405, b"", extra={"Allow": "POST"})
                if len(parts) == 2 and parts[0] == "jobs":
                    return self._send(200, jobs.status(parts[1]))
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "log":
                    off = int(parse_qs(u.query).get("offset", ["0"])[0])
                    return self._send(200, jobs.log_from(parts[1], off), "application/octet-stream")
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "out":
                    return self._send(200, jobs.out_tar(parts[1]), "application/gzip")
            except (KeyError, FileNotFoundError):
                return self._send(404, {"error": "no such job"})
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if self._authed():
                self._send(200, b"")

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
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
                body = self._body()
                if parts == ["mcp"]:
                    return self._mcp(body)
                if parts == ["jobs"]:
                    spec = json.loads(self.headers.get("X-Job", "{}"))
                    return self._send(200, jobs.start(spec, body))
                if parts == ["admin", "restart"]:  # 交给外层守护循环重新拉起（加载新代码）
                    self._send(200, {"restarting": True})
                    threading.Timer(0.5, lambda: os._exit(0)).start()
                    return
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "kill":
                    return self._send(200, {"killed": jobs.kill(parts[1])})
            except (KeyError, FileNotFoundError):
                return self._send(404, {"error": "no such job"})
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phonefarm", default=os.path.expanduser("~/node/phonefarm"), help="phonefarm 根目录（含 phonefarm 可执行）")
    ap.add_argument("--game-opt-loop", default=os.path.expanduser("~/node/game_opt_loop"), help="game_opt_loop 仓库目录（含 game_opt_loop 可执行），与 phonefarm 同级")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    # run_node.sh 用 & 在后台起本进程：非交互 shell 会把后台命令的 SIGINT 设成「忽略」，并一路遗传给
    # 我们起的任务（忽略态跨 exec 保留）—— 结果 kill/停止发的 SIGINT 对 bash 脚本、排队等锁的 devlock 无效。
    # 这里装回 Python 自己的处理器：exec 时处理器会被重置为默认，子进程就能正常收 SIGINT。
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
    jobs = Jobs(root, bins)
    board = None
    if dash:
        try:
            board = dash.Dash(root, gol, jobs.dir, jobs=jobs)
        except Exception as e:  # noqa: BLE001
            log(f"dash 起不来，/dash 不可用: {e!r}")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(McpChild(root), jobs, token, board))
    log(f"节点服务 127.0.0.1:{a.port}  /mcp({len(MCP_ALLOW)} 工具)  /jobs({', '.join(sorted(bins))})  根目录 {root}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
