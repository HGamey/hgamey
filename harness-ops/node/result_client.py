"""Explicit result registration/import client; writes are never replayed."""

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import uuid

from result_adapters import LABELS, SCHEMAS


HELP = """用法:
  pf-node results list [--job-id ID] [--game-id ID] [--game-version VERSION]
      [--device TEXT] [--research-id ID] [--direction-id ID] [--outcome VALUE]
      [--limit 1..100] [--cursor CURSOR]
  pf-node results show ID
  pf-node results register FILE
  pf-node results import FILE --adapter doom-lr4-026-v1|gol-optimize-v1 [--import-id ID]
      [--game-id ID] [--game-version VERSION] [--device TEXT] [--research-id ID] [--direction-id ID]
  pf-node results import-status ID

输出节点原始 JSON。登记只读至多 256 KiB 的清单；导入保留至多 1 MiB 的原 UTF-8 JSON。
register 使用文件内固定 result_id；import 在读文件前打印指定或生成的 import_id。
写入核对显式目标与能力，只提交一次。回执未知时，用 show 或 import-status 查询原 ID，
不换 ID、不自动重发。登记不表示测量、收益、设备恢复或同状态资格已经验证。
"""
MAX_FILE = 1 << 20
MAX_REGISTER = 256 << 10
MAX_RESPONSE = 6 << 20
OUTCOMES = {"gain", "no_gain", "regression", "failed", "not_run", "unknown"}


class RequestError(Exception):
    def __init__(self, message, *, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def identifier(value, *, importing=False):
    length = 48 if importing else 64
    if not isinstance(value, str) or value in (".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]{1," + str(length) + r"}", value):
        raise ValueError(f"ID 须为 1–{length} 位字母、数字、点、下划线或横线")
    return value


def _json(raw):
    def invalid_constant(_):
        raise ValueError("JSON contains a non-finite constant")
    return json.loads(raw, parse_constant=invalid_constant)


def _read(path, maximum=MAX_FILE):
    try:
        with open(path, "rb") as source:
            raw = source.read(maximum + 1)
    except OSError:
        raise ValueError("无法读取指定结果文件；未提交") from None
    if len(raw) > maximum:
        raise ValueError(f"结果文件超过 {maximum // 1024} KiB；未提交")
    try:
        content = raw.decode("utf-8")
        document = _json(content)
    except (ValueError, UnicodeError):
        raise ValueError("结果文件不是有效 UTF-8 JSON；未提交") from None
    if not isinstance(document, dict):
        raise ValueError("结果文件须为 JSON 对象；未提交")
    return content, document


class Client:
    def __init__(self, target):
        self.target = target

    def require_capability(self, *, write=False):
        node = self.target.require_write() if write else self.target.identity()
        capabilities = node.get("capabilities")
        if not isinstance(capabilities, list) or "results.index-v1" not in capabilities:
            raise ValueError("节点缺少 results.index-v1；不支持结果索引，未降级到任务或旧目录扫描")
        return node

    def request(self, method, path, body=None):
        headers = {"Content-Type": "application/json"} if body is not None else None
        request = self.target.request(method, path, body, headers)
        try:
            with self.target.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise RequestError("节点回包超过上限；结果未确认")
            value = _json(raw)
        except urllib.error.HTTPError as error:
            status, code = error.code, None
            try:
                raw = error.read((64 << 10) + 1)
                value = _json(raw) if len(raw) <= 64 << 10 else None
                candidate = value.get("code") if isinstance(value, dict) else None
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
                    code = candidate
            except (ValueError, UnicodeError, OSError, http.client.HTTPException):
                pass
            finally:
                error.close()
            message = "节点拒绝请求"
            if 300 <= status < 400:
                message = "节点返回重定向；已停止，未转发凭据或重发写入"
            elif status == 409:
                message = "原 ID 内容冲突或目标条件不符；保留原 ID 查询"
            elif status == 404:
                message = "原记录或入口不存在；不据此自动重新提交"
            raise RequestError(f"{message}（HTTP {status}" + (f", code={code}" if code else "") + "）",
                               status=status, code=code) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
            raise RequestError("网络回执未确认；未自动重试") from None
        except (ValueError, UnicodeError):
            raise RequestError("节点回包不是有效 JSON；结果未确认") from None
        if not isinstance(value, dict):
            raise RequestError("节点回包不是 JSON 对象；结果未确认")
        if value.get("schema_version") != 1 or value.get("node_id") != self.target.identity()["node_id"]:
            raise RequestError("回执未绑定原 node_id；结果未确认")
        return value


def _options(args, allowed):
    values = {}
    while args:
        option = args.pop(0)
        key = option[2:].replace("-", "_") if option.startswith("--") else ""
        if key not in allowed or key in values or not args:
            raise ValueError("无效、重复或缺值的 results 选项；用 results --help 查看用法")
        values[key] = args.pop(0)
    return values


def _submit(client, path, body, identity, *, importing):
    field = "import_id" if importing else "result_id"
    action = "import-status" if importing else "show"
    lookup = f"{client.target.command()} results {action} {identity}"
    try:
        value = client.request("POST", path, body)
        if value.get(field) != identity:
            raise RequestError(f"回执未绑定原 {field}；结果未确认")
    except (RequestError, KeyboardInterrupt) as error:
        print(f"pf-node: {error if isinstance(error, RequestError) else '提交被中断，回执未确认'}；"
              f"原 {field}={identity}。只用 {lookup} 查询；不自动重发，不换 ID。",
              file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0


def main(args, *, target):
    args = list(args)
    if not args or any(arg in ("--help", "-h") for arg in args):
        print(HELP)
        return 0
    try:
        command = args.pop(0)
        client = Client(target)
        if command == "list":
            query = _options(args, set(LABELS) | {"job_id", "outcome", "limit", "cursor"})
            if "job_id" in query and not re.fullmatch(r"[0-9a-f]{12}", query["job_id"]):
                raise ValueError("job_id 须为本节点 12 位任务 ID")
            if "outcome" in query and query["outcome"] not in OUTCOMES:
                raise ValueError("outcome 不在支持的结果状态中")
            if "limit" in query and (not query["limit"].isdigit() or not 1 <= int(query["limit"]) <= 100):
                raise ValueError("limit 须为 1–100")
            client.require_capability()
            value = client.request("GET", "/results" + ("?" + urllib.parse.urlencode(query) if query else ""))
        elif command in ("show", "import-status") and len(args) == 1:
            importing = command == "import-status"
            identity = identifier(args[0], importing=importing)
            client.require_capability()
            value = client.request("GET", ("/result-imports/" if importing else "/results/") + identity)
            if value.get("import_id" if importing else "result_id") != identity:
                raise RequestError("回包未绑定原 ID；结果未确认")
        elif command == "register" and len(args) == 1:
            client.require_capability(write=True)
            # The target is printed before reading; this ID is defined by the file itself.
            content, document = _read(args[0], MAX_REGISTER)
            if document.get("schema") != "phonefarm/result@1":
                raise ValueError("登记需要 phonefarm/result@1；未提交")
            identity = identifier(document.get("result_id"))
            print(f"pf-node: 登记 result_id={identity}；仅提交一次。", file=sys.stderr, flush=True)
            return _submit(client, "/results", content.encode("utf-8"), identity, importing=False)
        elif command == "import" and args:
            path = args.pop(0)
            options = _options(args, set(LABELS) | {"adapter", "import_id"})
            adapter = options.pop("adapter", None)
            if adapter not in SCHEMAS:
                raise ValueError("import 需显式支持的 --adapter；用 results --help 查看")
            identity = identifier(options.pop("import_id", None) or uuid.uuid4().hex, importing=True)
            client.require_capability(write=True)
            print(f"pf-node: 导入 import_id={identity}；仅提交一次。", file=sys.stderr, flush=True)
            content, document = _read(path)
            if document.get("schema") != SCHEMAS[adapter]:
                raise ValueError("报告 schema 与所选 adapter 不匹配；未提交")
            body = json.dumps({"schema": "phonefarm/result-import@1", "import_id": identity,
                               "adapter": adapter, "filename": os.path.basename(path),
                               "content": content, "labels": options}, ensure_ascii=False,
                              allow_nan=False).encode("utf-8")
            if len(body) > 6 << 20:
                raise ValueError("导入 HTTP JSON 超过 6 MiB；未提交")
            return _submit(client, "/result-imports", body, identity, importing=True)
        else:
            raise ValueError("无效的 results 命令；用 results --help 查看用法")
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except (RequestError, ValueError) as error:
        print(f"pf-node: {error}", file=sys.stderr)
        return 2
