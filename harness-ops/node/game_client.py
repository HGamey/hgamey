"""JSON client for node-owned game manifests and asynchronous preparation."""

import http.client
import json
import re
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

读取和准备结果均输出节点原始 JSON。register 只上传不超过 64 KiB 的清单。
prepare 仅提交宿主资源准备任务，不安装应用或验证场景；结果未知时只查询原 ID，
不自动重发。使用 games request REQUEST_ID 找回原任务，再查看其准备报告。
"""
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_MANIFEST = 64 << 10


class RequestError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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

    def request(self, method, path, body=None, request_id=None):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "pf-node/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if request_id is not None:
            headers["X-Request-ID"] = request_id
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=60) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
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
            raise RequestError(f"{message}（HTTP {status}）", status) from None
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


def prepare(client, path, request_id):
    lookup = f"pf-node games request {request_id}"
    print(f"pf-node: 准备 request_id={request_id}；仅提交一次。用 {lookup} 找回原任务。",
          file=sys.stderr, flush=True)
    try:
        job = confirmed_job(client.request("POST", path + "/prepare", b"{}", request_id), request_id)
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
          "任务状态不等于资源校验通过、应用安装或场景就绪。", file=sys.stderr)
    return 0


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
            if command != "prepare":
                raise ValueError("--request-id 仅用于 games prepare；查找原任务请用 games request ID")
        body, method = None, "GET"
        if command == "list" and len(args) == 1:
            path = "/games"
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
