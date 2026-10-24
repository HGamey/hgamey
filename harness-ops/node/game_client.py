"""JSON client for node-owned game manifests and asynchronous preparation."""

import http.client
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


HELP = """用法:
  pf-node games list
  pf-node games show ID VERSION
  pf-node games register FILE
  pf-node games prepare ID VERSION [--request-id ID]
  pf-node games request REQUEST_ID
  pf-node games deploy ID VERSION --serial S [--check] [--request-id ID]
  pf-node games deployment JOB_ID
  pf-node games deployment-recover JOB_ID --request-id ID
  pf-node games upload ID VERSION ASSET FILE [--upload-id ID]
  pf-node games upload-status ID
  pf-node games upload-finalize ID
  pf-node games upload-cancel ID

读取和准备结果均输出节点原始 JSON。register 只上传不超过 64 KiB 的清单。
prepare 仅提交宿主资源准备任务，不安装应用或验证场景；结果未知时只查询原 ID，
不自动重发。使用 games request REQUEST_ID 找回原任务，再查看其准备报告。
upload 分块读取本地文件，不打包或计算本机全文件哈希；写入结果未知时只查询原上传 ID 后退出。
显式带原 --upload-id 重跑才能续传。传完仅提交校验任务；当前缓存 observation 才是资源校验证据。
deploy 仅部署配方选中的资源到已安装引擎的新目录；--check 只读取设备前提，仍经任务排队。
部署、应用读取、登录和场景状态分别报告。恢复接续原任务，不重放部署，不自动释放队列保留。
"""
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_MANIFEST = 64 << 10
UPLOAD_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
UPLOAD_CHUNK = 1 << 20
MAX_UPLOAD_CHUNK = 8 << 20
UPLOAD_PHASES = {"unclaimed", "receiving", "received", "sealed", "verifying", "verified", "failed",
                 "cancelled", "superseded", "busy", "unknown"}


class RequestError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("PF_NODE_URL 必须是无凭据、查询串或片段的 HTTP(S) 地址")
        self.url, self.token = url.rstrip("/"), token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, body=None, request_id=None, headers=None):
        request_headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "pf-node/1"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if request_id is not None:
            request_headers["X-Request-ID"] = request_id
        request_headers.update(headers or {})
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=60) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            code = None
            try:
                raw = error.read(MAX_MANIFEST + 1)
                value = json.loads(raw) if len(raw) <= MAX_MANIFEST else None
                candidate = value.get("code") if isinstance(value, dict) else None
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
                    code = candidate
            except (OSError, http.client.HTTPException, ValueError):
                pass
            finally:
                error.close()
            if 300 <= status < 400:
                message = "节点返回重定向；为避免转发凭据或重发写入，已停止"
            elif status == 409:
                message = "内容或请求 ID 冲突；请查询原版本／请求，不要换 ID 盲目提交"
            elif status in (401, 403):
                message = "节点拒绝访问；写操作需要管理 token"
            elif status == 404:
                message = "入口或记录不存在；不能据此推断设备或资源状态"
            else:
                message = "节点请求失败"
            # The bounded code is for protocol decisions only; never echo an error body.
            raise RequestError(f"{message}（HTTP {status}）", status, code) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
            raise RequestError("网络请求未确认；未自动重试") from None
        except (ValueError, UnicodeError):
            raise RequestError("节点回包不是有效 JSON；结果未确认") from None
        if not isinstance(value, dict):
            raise RequestError("节点回包不是 JSON 对象；结果未确认")
        return value


def identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError("游戏 ID／版本须为 1–64 位小写字母、数字、点、下划线或横线，首位为字母或数字")
    return value


def request_identifier(value):
    if not REQUEST_ID.fullmatch(value):
        raise ValueError("request_id 须为 1–128 位字母、数字、点、下划线、冒号或横线，首位为字母或数字")
    return value


def manifest_bytes(path):
    try:
        with open(path, "rb") as source:
            body = source.read(MAX_MANIFEST + 1)
    except OSError:
        raise ValueError("无法读取清单文件") from None
    if len(body) > MAX_MANIFEST:
        raise ValueError("清单不能超过 64 KiB；未上传")
    try:
        manifest = json.loads(body)
    except (ValueError, UnicodeError):
        raise ValueError("清单不是有效 JSON；未上传") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("清单必须是 schema_version=1 的 JSON 对象；未上传")
    return body


def confirmed_job(value, request_id):
    if (not re.fullmatch(r"[0-9a-f]{12}", str(value.get("id", "")))
            or value.get("request_id") != request_id):
        raise RequestError("任务回包未绑定原 request_id；结果未确认")
    return value


def submit_job(client, path, body, request_id, description):
    lookup = f"pf-node games request {request_id}"
    print(f"pf-node: {description} request_id={request_id}；仅提交一次。用 {lookup} 找回原任务。",
          file=sys.stderr, flush=True)
    try:
        job = confirmed_job(client.request("POST", path, body, request_id), request_id)
    except KeyboardInterrupt:
        print(f"pf-node: 提交结果未确认；请用 {lookup} 查询，不换 ID 重复提交。", file=sys.stderr)
        return 130
    except RequestError as error:
        if error.status is not None and error.status < 500:
            print(f"pf-node: {error}；原 request_id={request_id}。", file=sys.stderr)
            return 2
        print(f"pf-node: {error}；现在只查询原 request_id，不再次 POST。", file=sys.stderr)
        try:
            job = confirmed_job(client.request("GET", "/requests/" + urllib.parse.quote(request_id, safe="")), request_id)
        except (RequestError, KeyboardInterrupt) as lookup_error:
            print(f"pf-node: 尚未确认原任务；请用 {lookup} 继续查询，保留同一 ID，不自动新提交。",
                  file=sys.stderr)
            return 130 if isinstance(lookup_error, KeyboardInterrupt) else 2
    print(json.dumps(job, ensure_ascii=False))
    print(f"pf-node: 原任务 {job['id']}，phase={job.get('phase', '未报告')}；"
          "任务状态不等于资源校验通过、应用安装、场景就绪或设备恢复完成。", file=sys.stderr)
    return 0


def prepare(client, path, request_id):
    return submit_job(client, path + "/prepare", b"{}", request_id, "准备")


def deployment_node(client):
    node = client.request("GET", "/node")
    capabilities = node.get("capabilities")
    if not isinstance(capabilities, list) or "games.deploy-v1" not in capabilities:
        raise ValueError("节点缺少 games.deploy-v1；未提交，请升级节点服务和 phonefarm")
    if not isinstance(node.get("node_id"), str) or not node["node_id"]:
        raise RequestError("节点未提供持久 node_id；未提交")
    return node["node_id"]


def deployment_job_id(value):
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise ValueError("deployment 需要原任务的 12 位 ID")
    return value


def deployment_arguments(args, request_id, *, recovery=False):
    serial, mode, index = None, "apply", 0
    while index < len(args):
        option = args[index]
        if option in ("--serial", "--request-id") and index + 1 < len(args):
            value = args[index + 1]
            if option == "--serial" and not recovery:
                if (not value.strip() or len(value) > 256
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    raise ValueError("--serial 需要显式有效设备序列号")
                serial = value
            elif option == "--request-id":
                value = request_identifier(value)
                if request_id is not None and request_id != value:
                    raise ValueError("前后 --request-id 不一致；未提交")
                request_id = value
            else:
                raise ValueError("恢复的 serial/context 由原任务派生，不能由客户端指定")
            index += 2
        elif option == "--check" and not recovery:
            mode, index = "check", index + 1
        else:
            raise ValueError(HELP)
    if recovery and request_id is None:
        raise ValueError("deployment-recover 必须显式指定 --request-id ID")
    if not recovery and serial is None:
        raise ValueError("deploy 必须显式指定 --serial S")
    return serial, mode, request_id or str(uuid.uuid4())


def upload_identifier(value):
    if not UPLOAD_ID.fullmatch(value):
        raise ValueError("upload_id 须为 1–64 位小写字母、数字、横线或下划线，首位为字母或数字")
    return value


def upload_node(client):
    node = client.request("GET", "/node")
    if "games.upload-v1" not in node.get("capabilities", []):
        raise ValueError("节点缺少 games.upload-v1；未上传，请升级节点服务")
    if not isinstance(node.get("node_id"), str) or not node["node_id"]:
        raise ValueError("节点未提供持久 node_id；未上传")
    return node["node_id"]


def upload_binding(client, node_id, game_id, version, asset_name, upload_id):
    record = client.request("GET", f"/games/{identifier(game_id)}/versions/{identifier(version)}")
    manifest = record.get("manifest")
    if (record.get("node_id") != node_id or record.get("game_id") != game_id
            or record.get("version") != version or not isinstance(manifest, dict)
            or manifest.get("game_id") != game_id or manifest.get("version") != version):
        raise RequestError("游戏清单与所选节点／版本不匹配；未上传")
    assets = manifest.get("assets")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == asset_name] if isinstance(assets, list) else []
    if len(matches) != 1:
        raise ValueError("清单中没有唯一匹配的资产名；未上传")
    asset = matches[0]
    if (type(asset.get("size")) is not int or asset["size"] < 0
            or not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])):
        raise RequestError("清单未提供有效大小和 SHA-256；未上传")
    return {"upload_id": upload_id, "node_id": node_id, "game_id": game_id, "version": version,
            "asset_name": asset_name, "size": asset["size"], "sha256": asset["sha256"]}


def confirmed_upload(record, binding):
    if (record.get("schema_version") != 1 or any(record.get(k) != v for k, v in binding.items())
            or record.get("phase") not in UPLOAD_PHASES):
        raise RequestError("上传回包与节点／清单／上传身份不匹配；已停止")
    offset, maximum = record.get("offset"), record.get("chunk_max_bytes")
    if (offset is not None and (type(offset) is not int or not 0 <= offset <= binding["size"])
            or type(maximum) is not int or not 1 <= maximum <= MAX_UPLOAD_CHUNK):
        raise RequestError("上传回包缺少有效 offset 或块大小上限；已停止")
    return record


def write_upload(client, method, path, binding, body, headers=None, next_offset=None):
    """An unconfirmed write gets one lookup, then returns without another write."""
    upload_id = binding["upload_id"]
    lookup = f"pf-node games upload-status {upload_id}"
    try:
        record = confirmed_upload(client.request(method, path, body, headers=headers), binding)
        if next_offset is not None and record["offset"] != next_offset:
            raise RequestError("PUT 回包未确认本块的精确持久 offset；已停止")
        return record, 0
    except KeyboardInterrupt:
        print(f"pf-node: 已暂停；写入结果可能未知，用 {lookup} 查询后显式续传。", file=sys.stderr)
        return None, 130
    except RequestError as error:
        print(f"pf-node: {error}；只查询原 upload_id={upload_id}，不继续写入。", file=sys.stderr)
        try:
            record = confirmed_upload(client.request("GET", "/uploads/" + upload_id), binding)
        except (RequestError, KeyboardInterrupt) as lookup_error:
            print(f"pf-node: 仍未确认上传状态；保留原 ID，用 {lookup} 查询，不换新 ID 重投。", file=sys.stderr)
            return None, 130 if isinstance(lookup_error, KeyboardInterrupt) else 2
        print(f"pf-node: 已找回当前状态；显式带 --upload-id {upload_id} 重跑才续传。", file=sys.stderr)
        return record, 2


def print_upload(record, exit_code=0):
    if record is not None:
        print(json.dumps(record, ensure_ascii=False))
        print(f"pf-node: upload_id={record['upload_id']}，phase={record['phase']}，"
              f"已确认 offset={record['offset']} / {record['size']}；"
              "字节收齐或校验任务已记录不等于缓存 verified，设备状态仍未验证。", file=sys.stderr)
    return exit_code


def file_stamp(source):
    info = os.fstat(source.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("上传源必须是普通文件")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def check_file(source, expected):
    if file_stamp(source) != expected:
        raise ValueError("本地文件在上传期间改变；已停止，请查询原 upload_id")


def upload_file(client, game_id, version, asset_name, path, upload_id=None):
    resume = upload_id is not None
    upload_id = upload_identifier(upload_id) if resume else str(uuid.uuid4())
    print(f"pf-node: upload_id={upload_id}；恢复此上传须显式带 --upload-id {upload_id}。",
          file=sys.stderr, flush=True)
    binding = upload_binding(client, upload_node(client), game_id, version, asset_name, upload_id)
    record = None
    try:
        # Nonblocking open lets us reject FIFOs without waiting for a writer.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            original = file_stamp(source)
            if original[2] != binding["size"]:
                raise ValueError("本地文件大小与清单不符；未上传")
            if resume:
                try:
                    record = confirmed_upload(client.request("GET", "/uploads/" + upload_id), binding)
                except RequestError as error:
                    if error.status != 404 or error.code != "upload_not_found":
                        raise
            if record is None or resume and record["phase"] == "unclaimed":
                # An interrupted registration can persist its immutable binding
                # before acquiring content. Only this explicit resume repairs it.
                body = json.dumps({k: binding[k] for k in ("upload_id", "game_id", "version", "asset_name")},
                                  ensure_ascii=False).encode()
                record, code = write_upload(client, "POST", "/uploads", binding, body)
                if code:
                    return print_upload(record, code)
            if record["phase"] == "verified":
                return print_upload(record)
            if record["phase"] in {"sealed", "verifying"}:
                check_file(source, original)
                record, code = write_upload(client, "POST", f"/uploads/{upload_id}/finalize", binding, b"{}")
                return print_upload(record, code)
            if record["offset"] is None or record["phase"] not in {"unclaimed", "receiving", "received"}:
                print("pf-node: 当前会话不能续写；请查询／核查原状态，不自动新建上传。", file=sys.stderr)
                return print_upload(record, 2)
            offset = record["offset"]
            while offset < binding["size"]:
                check_file(source, original)
                source.seek(offset)
                length = min(UPLOAD_CHUNK, record["chunk_max_bytes"], binding["size"] - offset)
                chunk = source.read(length)
                check_file(source, original)
                if len(chunk) != length:
                    raise ValueError("本地文件提前结束；已停止上传")
                record, code = write_upload(client, "PUT", "/uploads/" + upload_id, binding, chunk,
                                            {"Content-Type": "application/octet-stream",
                                             "Content-Length": str(length), "Upload-Offset": str(offset)},
                                            next_offset=offset + length)
                if code:
                    return print_upload(record, code)
                if record["phase"] not in {"receiving", "received"}:
                    print("pf-node: 分块回包不再允许续写；已停止，请查询原上传。", file=sys.stderr)
                    return print_upload(record, 2)
                offset = record["offset"]
                print(f"pf-node: upload_id={upload_id} 已确认 {offset} / {binding['size']} 字节。",
                      file=sys.stderr, flush=True)
            check_file(source, original)
            record, code = write_upload(client, "POST", f"/uploads/{upload_id}/finalize", binding, b"{}")
            return print_upload(record, code)
    except (OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ValueError) else "无法读取本地上传文件"
        print(f"pf-node: {reason}；保留 upload_id={upload_id}，未自动继续写入。", file=sys.stderr)
        return print_upload(record, 2)


def upload_existing(client, action, upload_id):
    upload_id = upload_identifier(upload_id)
    node_id = upload_node(client)
    record = client.request("GET", "/uploads/" + upload_id)
    if any(not isinstance(record.get(k), str) for k in ("game_id", "version", "asset_name")):
        raise RequestError("上传记录缺少清单绑定；已停止")
    binding = upload_binding(client, node_id, record["game_id"], record["version"], record["asset_name"], upload_id)
    record = confirmed_upload(record, binding)
    if action == "status":
        return print_upload(record)
    if action == "finalize" and record["offset"] != binding["size"]:
        print("pf-node: 尚未确认字节收齐；未提交校验。", file=sys.stderr)
        return print_upload(record, 2)
    print(f"pf-node: {action} upload_id={upload_id}；仅写入一次，结果未知只查原 ID。",
          file=sys.stderr, flush=True)
    record, code = write_upload(client, "POST", f"/uploads/{upload_id}/{action}", binding, b"{}")
    return print_upload(record, code)


def main(args, *, url, token, request_id=None):
    if args in (["--help"], ["-h"]):
        print(HELP)
        return 0
    try:
        if not args:
            raise ValueError(HELP)
        command = args[0]
        if request_id is not None:
            request_identifier(request_id)
            if command not in ("prepare", "deploy", "deployment-recover"):
                raise ValueError("--request-id 仅用于 games prepare/deploy/deployment-recover；查找原任务请用 games request ID")
        body, method = None, "GET"
        if command == "list" and len(args) == 1:
            path = "/games"
        elif command == "deploy" and len(args) >= 3:
            path = f"/games/{identifier(args[1])}/versions/{identifier(args[2])}/deploy"
            serial, mode, key = deployment_arguments(args[3:], request_id)
            client = Client(url, token)
            body = json.dumps({"node_id": deployment_node(client), "serial": serial, "mode": mode}).encode()
            return submit_job(client, path, body, key, "检查设备部署前提" if mode == "check" else "部署资源")
        elif command == "deployment" and len(args) == 2:
            path = f"/jobs/{deployment_job_id(args[1])}/deployment"
        elif command == "deployment-recover" and len(args) >= 2:
            path = f"/jobs/{deployment_job_id(args[1])}/deployment/recover"
            _, _, key = deployment_arguments(args[2:], request_id, recovery=True)
            client = Client(url, token)
            deployment_node(client)
            return submit_job(client, path, b"{}", key, "接续原部署恢复")
        elif command == "upload" and (len(args) == 5 or len(args) == 7 and args[5] == "--upload-id"):
            return upload_file(Client(url, token), identifier(args[1]), identifier(args[2]), args[3], args[4],
                               args[6] if len(args) == 7 else None)
        elif command in ("upload-status", "upload-finalize", "upload-cancel") and len(args) == 2:
            return upload_existing(Client(url, token), command.removeprefix("upload-"), args[1])
        elif command in ("show", "prepare") and len(args) >= 3:
            path = f"/games/{identifier(args[1])}/versions/{identifier(args[2])}"
            if command == "show" and len(args) != 3:
                raise ValueError(HELP)
            if command == "prepare":
                if len(args) == 5 and args[3] == "--request-id":
                    supplied = request_identifier(args[4])
                    if request_id is not None and request_id != supplied:
                        raise ValueError("前后 --request-id 不一致；未提交")
                    request_id = supplied
                elif len(args) != 3:
                    raise ValueError(HELP)
                return prepare(Client(url, token), path, request_id or str(uuid.uuid4()))
        elif command == "register" and len(args) == 2:
            path, method, body = "/games", "POST", manifest_bytes(args[1])
        elif command == "request" and len(args) == 2:
            path = "/requests/" + urllib.parse.quote(request_identifier(args[1]), safe="")
        else:
            raise ValueError(HELP)
        result = Client(url, token).request(method, path, body)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (RequestError, ValueError) as error:
        print(f"pf-node: {error}", file=sys.stderr)
        if args and args[0] == "register" and isinstance(error, RequestError) and (error.status is None or error.status >= 500):
            print("pf-node: 登记结果未确认；先用 games show ID VERSION 查询原版本，不自动重发。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("pf-node: 已停止等待；写入结果可能未知，请查询原版本或 request_id。", file=sys.stderr)
        return 130
