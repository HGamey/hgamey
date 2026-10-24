"""Shared research metadata client. A write has one target and one operation ID."""

import argparse
import http.client
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import uuid


HELP = """用法:
  pf-node research list KIND [--research-id ID] [--state STATE] [--request-id ID]
      [--limit 1..100] [--cursor CURSOR]
  pf-node research show KIND ID [--revision N]
  pf-node research history KIND ID [--limit 1..100] [--cursor CURSOR]
  pf-node research create KIND ID FILE [--operation-id ID] [--actor-label TEXT] [--reason TEXT]
  pf-node research update KIND ID FILE --revision N --reason TEXT [--operation-id ID] [--actor-label TEXT]
  pf-node research operation ID
  pf-node research events [--after N] [--research-id ID] [--limit 1..500]

KIND 为 research/direction/request/feedback。FILE 只含 data JSON 对象，至多 64 KiB，不含写入 envelope。
update 是带原 revision 的并发更新，必须说明理由；feedback 不可更新，修正用新 ID 与 supersedes。
写前核对显式目标并打印固定 operation ID，只 POST 一次。回执未知时使用 research operation 原ID
显式查询，不自动重发、换 ID、合并草稿或轮询。回复/关闭请求不自动批准、解阻或完成方向。
输出与网页同一 API JSON；错误仅保安全错误码、HTTP 状态与 current_revision，不回显源正文。
"""
KINDS = ("research", "direction", "request", "feedback")
MAX_DATA = 64 << 10
MAX_BODY = 128 << 10
MAX_RESPONSE = 8 << 20


def identifier(value):
    if (not isinstance(value, str) or value in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value)):
        raise argparse.ArgumentTypeError("ID 须为 1–64 位字母、数字、点、下划线或横线，且不能为 . 或 ..")
    return value


def integer(value, minimum=1, maximum=None):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError("需要非负十进制整数")
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("整数超出支持范围") from None
    if number < minimum or (maximum is not None and number > maximum):
        raise argparse.ArgumentTypeError(f"整数须不少于 {minimum}" + (f"，且不大于 {maximum}" if maximum else ""))
    return number


def parser():
    root = argparse.ArgumentParser(prog="pf-node research", description=HELP,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("kind", choices=KINDS)
    listing.add_argument("--research-id", type=identifier)
    listing.add_argument("--request-id", type=identifier)
    listing.add_argument("--state")
    showing = commands.add_parser("show")
    history = commands.add_parser("history")
    for command in (showing, history):
        command.add_argument("kind", choices=KINDS)
        command.add_argument("id", type=identifier)
    showing.add_argument("--revision", type=integer)
    for command in (listing, history):
        command.add_argument("--limit", type=lambda v: integer(v, maximum=100))
        command.add_argument("--cursor")
    for name in ("create", "update"):
        command = commands.add_parser(name)
        command.add_argument("kind", choices=KINDS)
        command.add_argument("id", type=identifier)
        command.add_argument("file")
        command.add_argument("--operation-id", type=identifier)
        command.add_argument("--actor-label")
        command.add_argument("--reason", required=name == "update")
        if name == "update":
            command.add_argument("--revision", type=integer, required=True)
    operation = commands.add_parser("operation")
    operation.add_argument("id", type=identifier)
    events = commands.add_parser("events")
    events.add_argument("--after", type=lambda v: integer(v, minimum=0))
    events.add_argument("--research-id", type=identifier)
    events.add_argument("--limit", type=lambda v: integer(v, maximum=500))
    return root


def _json(raw):
    def invalid_constant(_):
        raise ValueError("non-finite JSON constant")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number
    return json.loads(raw, parse_constant=invalid_constant, parse_float=finite_float)


def _data(path):
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_DATA + 1)
    except OSError:
        raise ValueError("无法读取指定 data 文件；未写入") from None
    if len(raw) > MAX_DATA:
        raise ValueError("data 文件超过 64 KiB；未写入")
    try:
        value = _json(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("object required")
        normalized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("data 文件须为有效 UTF-8 JSON 对象，不接非有限数字；未写入") from None
    if len(normalized) > MAX_DATA:
        raise ValueError("data JSON 超过 64 KiB；未写入")
    return value


class RequestError(Exception):
    def __init__(self, message, *, status=None, code=None, current_revision=None):
        super().__init__(message)
        self.status, self.code, self.current_revision = status, code, current_revision

    def public(self):
        value = {"code": self.code or "research_request_unconfirmed", "error": str(self),
                 "http_status": self.status}
        if self.current_revision is not None:
            value["current_revision"] = self.current_revision
        return value


class Client:
    def __init__(self, target):
        self.target = target

    def require_capability(self, *, write=False):
        node = self.target.require_write() if write else self.target.identity()
        capabilities = node.get("capabilities")
        if not isinstance(capabilities, list) or "research.state-v1" not in capabilities:
            raise ValueError("节点缺少 research.state-v1；未降级到旧 GOL goal/human.md")

    def request(self, method, path, body=None):
        request = self.target.request(method, path, body,
                                      {"Content-Type": "application/json"} if body is not None else None)
        try:
            with self.target.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise RequestError("节点回包超过上限；结果未确认")
            value = _json(raw)
        except urllib.error.HTTPError as error:
            status, code, revision = error.code, None, None
            try:
                raw = error.read(MAX_DATA + 1)
                value = _json(raw) if len(raw) <= MAX_DATA else None
                candidate = value.get("code") if isinstance(value, dict) else None
                if (isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate)
                        and candidate != self.target.token):
                    code = candidate
                candidate = value.get("current_revision") if isinstance(value, dict) else None
                if type(candidate) is int and candidate >= 1:
                    revision = candidate
            except (ValueError, UnicodeError, OSError, http.client.HTTPException, RecursionError):
                pass
            finally:
                error.close()
            if status == 409:
                message = "操作内容、对象版本或原目标冲突；保留草稿与原操作 ID，先读取当前状态"
            elif 300 <= status < 400:
                message = "节点返回重定向；未转发凭据或重发请求"
            elif status == 404:
                message = "对象、版本或原操作当前不可查；不据此自动重新提交"
            elif status in (401, 403):
                message = "节点拒绝访问；读取和写入需要对应权限"
            else:
                message = "研究状态请求被拒绝或未确认；未自动重试"
            raise RequestError(message, status=status, code=code, current_revision=revision) from None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
            raise RequestError("网络回执未确认；未自动重试") from None
        except (ValueError, UnicodeError, RecursionError):
            raise RequestError("节点回包不是有效 JSON；结果未确认") from None
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("node_id") != self.target.identity()["node_id"]):
            raise RequestError("回包未绑定原 node_id 或协议版本；结果未确认")
        return value


def _query(args, fields):
    values = {key: getattr(args, key) for key in fields if getattr(args, key, None) is not None}
    if values.get("cursor") == "":
        raise ValueError("cursor 不能为空；保留原页游标，不自动回到首屏")
    return "?" + urllib.parse.urlencode(values) if values else ""


def _error(error, *, operation_id=None, kind=None, identity=None):
    value = error.public()
    if operation_id is not None:
        value.update(operation_id=operation_id, object={"kind": kind, "id": identity})
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    detail = f"HTTP {error.status}" if error.status is not None else "未确认"
    if error.code:
        detail += f", code={error.code}"
    if error.current_revision is not None:
        detail += f", current_revision={error.current_revision}"
    print(f"pf-node: {error}（{detail}）", file=sys.stderr)


def _write(client, args):
    if args.command == "update" and args.kind == "feedback":
        raise ValueError("feedback 不可更新；修正需新 ID 与 supersedes")
    if args.reason is not None and (not args.reason.strip() or len(args.reason) > 16384):
        raise ValueError("reason 须为非空文字，至多 16384 字符")
    if args.actor_label is not None and (not args.actor_label.strip() or len(args.actor_label) > 128):
        raise ValueError("actor-label 须为非空文字，至多 128 字符")
    operation_id = args.operation_id or uuid.uuid4().hex
    client.require_capability(write=True)
    lookup = f"{client.target.command()} research operation {operation_id}"
    print(f"pf-node: {args.kind}/{args.id} operation_id={operation_id}；仅提交一次。"
          f"回执未知时用 {lookup} 查询原操作。", file=sys.stderr, flush=True)
    data = _data(args.file)
    body = json.dumps({"schema": "phonefarm/research-write@1", "operation_id": operation_id,
                       "base_revision": args.revision if args.command == "update" else None,
                       "data": data, "actor_label": args.actor_label, "reason": args.reason},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_BODY:
        raise ValueError("研究写入 JSON 超过 128 KiB；未写入")
    try:
        value = client.request("POST", f"/research-state/{args.kind}/{args.id}", body)
        obj = value.get("object")
        if (value.get("operation_id") != operation_id or not isinstance(obj, dict)
                or obj.get("kind") != args.kind or obj.get("id") != args.id
                or type(obj.get("revision")) is not int or obj["revision"] < 1):
            raise RequestError("回执未绑定原操作或对象；结果未确认")
    except RequestError as error:
        _error(error, operation_id=operation_id, kind=args.kind, identity=args.id)
        print(f"pf-node: 保留原 operation_id={operation_id}；仅用 {lookup} 查询。"
              "不换 ID、不重发 POST、不自动合并草稿。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"pf-node: 写入回执未确认；保留原 operation_id={operation_id}，用 {lookup} 查询，未自动重试。",
              file=sys.stderr)
        return 130
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0


def main(argv, *, target):
    argv = list(argv)
    if not argv or any(item in ("-h", "--help") for item in argv):
        print(HELP)
        return 0
    args = parser().parse_args(argv)
    client = Client(target)
    try:
        if args.command in ("create", "update"):
            return _write(client, args)
        if args.command == "list":
            if args.request_id is not None and args.kind != "feedback":
                raise ValueError("--request-id 筛选仅用于 feedback")
            if args.state is not None and args.kind == "feedback":
                raise ValueError("feedback 不提供 state 筛选")
            path = f"/research-state/{args.kind}" + _query(args, ("research_id", "state", "request_id", "limit", "cursor"))
        elif args.command == "show":
            path = f"/research-state/{args.kind}/{args.id}" + _query(args, ("revision",))
        elif args.command == "history":
            path = f"/research-state/{args.kind}/{args.id}/history" + _query(args, ("limit", "cursor"))
        elif args.command == "operation":
            path = "/research-operations/" + args.id
        else:
            path = "/research-events" + _query(args, ("after", "research_id", "limit"))
        client.require_capability()
        value = client.request("GET", path)
        if args.command == "show" and (value.get("kind") != args.kind or value.get("id") != args.id
                or (args.revision is not None and value.get("revision") != args.revision)):
            raise RequestError("回包未绑定原对象或指定版本；结果未确认")
        if args.command == "operation" and value.get("operation_id") != args.id:
            raise RequestError("回包未绑定原 operation_id；结果未确认")
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except RequestError as error:
        _error(error)
        return 2
    except ValueError as error:
        print(f"pf-node: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
