"""Shared research metadata client. A write has one target and one operation ID."""

import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import uuid
from cli_time import utc
from agent_duties import NOTE_STYLE, DELIVERY_STYLE, COMPARE_HELP, COLLABORATION, TASK_STATE_HELP


HELP = """用法:
  pf-node research recipes [ID]
  pf-node research plan-template exploration|confirmation --research-revision N [--recipe ID]
  pf-node research list KIND [--research-id ID] [--state STATE] [--request-id ID] [--follows ID]
      [--limit 1..100] [--cursor CURSOR]
  pf-node research show KIND ID [--revision N]
  pf-node research history KIND ID [--limit 1..100] [--cursor CURSOR]
  pf-node research assess ID --revision N
  pf-node research create KIND ID FILE [--operation-id ID] [--actor-label TEXT] [--reason TEXT]
  pf-node research direction add <方向ID> FILE [--based-on <方向ID>[:<任务ID>]] [--operation-id ID] [--actor-label TEXT]
  pf-node research update KIND ID FILE --revision N --reason TEXT [--operation-id ID] [--actor-label TEXT]
  pf-node research conclude <方向ID> verified|no_gain|rejected|deferred --reason "<依据>"
      [--evidence job:ID[:PATH] ...]                 # 读取当前 revision 给方向下结论
  pf-node research operation ID
  pf-node research events [--after N | --cursor CURSOR] [--research-id ID] [--limit 1..500]
      [--wait-seconds 0..15]
  pf-node research media REQUEST A|B|C... --revision N --output FILE
  pf-node research claim ID                       # 用自己的 agent token 认领无人负责的研究
  pf-node research assign ID AGENT|none           # 指派或取消负责人
  pf-node research link ID FOLLOWS|none           # 记录或取消这个任务接续哪个任务
  pf-node research close ID --reason TEXT         # 关闭任务（不做了或并入别的任务）
  pf-node research reopen ID --reason TEXT        # 重新打开，之后可再交付一个版本
  pf-node research note ID TEXT [--reply-to NOTE --decision adopt|defer|reject] [--ask] [--to AGENT]
      [--evidence job:ID[:PATH]|result:ID|url:URL ...] [--attach FILE ...]        # 在研究里留言/回应
  pf-node research compare ID --item JOB_OR_FILE [--item ...] [--pick K] --title TEXT --question TEXT [--direction ID]
  pf-node research thread ID                      # 按时间读研究里的留言
  pf-node research deliver ID --summary TEXT|@FILE --artifact job:ID[:PATH]|result:ID|url:URL ...
      [--game-version V] [--device D ...]          # 交付：总结、产物、适用范围；状态变为 delivered

recipes 离线查看优化路线的前提、改动、测量和恢复建议；优先级由外部 Agent 决定。
plan-template 离线输出 next_experiment JSON 草稿（title、plan、research_revision），可带路线，不提交任务。
把完整输出作为方向 data.next_experiment；不要嵌套进 next_experiment.plan，plan 只收字符串。
create 和 direction add 的方向 data 必填 research_id、title、hypothesis；next_experiment 只收 title、plan、research_revision。
direction add 把 --based-on 写成 data.based_on={direction_id,job_id}；方向须同任务或在 follows 前链且更早，指定运行须挂在该方向。
最小结构：{"research_id":"…","title":"…","hypothesis":"…","next_experiment":<plan-template 的输出>}。
KIND 为 research/direction/request/feedback/note。research list 必须带 KIND，可选值同前；FILE 只含 data JSON 对象，至多 64 KiB，不含写入 envelope。
research 列表可用 --follows <研究ID> 只取它的接续任务。
job:ID:PATH 引用该任务 out 目录内的相对文件；PATH 不能为绝对路径，不能含空段、. 或 ..。
--attach 直接附本机 PNG、JPEG、SVG、MP4 或 WebM，可重复；图片至多 16 MiB，视频至多 128 MiB。实验 Python 只保证有标准库。
--ask 把留言标为问人；首页列出未回复问题，人用 reply_to 回复后关闭，agent 继续做其他方向。
update FILE 只写要改的字段，没写的保持第 N 版值；显式 null 清空可空字段，必填字段不能清空。回执 changed_fields 列出实际变更。
update 是带原 revision 的并发更新，必须说明理由；feedback 不可更新，修正用新 ID 与 supersedes。
link 读取研究当前 revision 更新 follows；目标必须存在且不能是自己，none 取消接续，签到规则不变。
conclude 读取方向当前 revision 写入结论与 state_reason；可用 --evidence 重复引用任务产物，未回应人工留言时仍拒绝。
research/direction 的 update、deliver、create、conclude 仅负责人可写，其他 agent 用 note 留言；留言保持开放。
写前核对显式目标并打印固定 operation ID，只 POST 一次。回执未知时使用 research operation 原ID
显式查询，不自动重发、换 ID、合并草稿或轮询。回复/关闭请求不自动批准、解阻或完成方向。
输出与网页同一 API JSON；错误保安全错误码、HTTP 状态与 current_revision，不回显源正文。
400 invalid_research 还显示服务端生成的校验信息，按字段修正后用新 operation ID 显式提交。
events 使用 cursor 或 wait>0 时需显式 node-id 与 research.watch-v1；只发一次事件 GET，
空等成功退出，不自动重试、翻页或保存/确认消费位置。429 仅提示安全 retry_after_seconds。
next_cursor 是本批读取位置；处理并保存相应决定后才可作为自己的 checkpoint，head 不是进度。
失败保留原游标；事件只含研究元数据提交，不含 job/result/设备状态，不会自动唤醒运行器。
request/feedback data 实际含 evaluation 时需 research.evaluation-v1；旧字段缺席不会补默认值。
research data 含 criteria 键时需 research.criteria-v1，显式 null 可清空；缺席不会补键。
assess 需显式 node-id 与 research.criteria-v1，只 GET 指定 revision 的登记点值判断。
完整 JSON 输出；comparison.partial=true 退出 2，规则 unknown/null 不视为读取失败，退出 0。
判断不自动采纳、解阻或改写方向，不表示统计显著、因果收益或科学可比性。
media 只读下载固定版本的一个槽位，需显式 node-id 与评价能力。仅支持 PNG/JPEG/MP4/WebM，
图片至多 16 MiB、视频至多 128 MiB；同一遍流式校验服务器长度/SHA 后发布文件，不覆盖已有文件。
没有自动重试或续传；失败只清理本次 partial。下载完整不表示已解码、看过或已提交评价。
"""
HELP += "\n" + NOTE_STYLE + "\n" + DELIVERY_STYLE + "\n" + COMPARE_HELP + "\n" + COLLABORATION + "\n" + TASK_STATE_HELP

KINDS = ("research", "direction", "request", "feedback", "note")
MAX_DATA = 64 << 10
MAX_BODY = 128 << 10
MAX_RESPONSE = 8 << 20
MEDIA_LIMITS = {"image/png": 16 << 20, "image/jpeg": 16 << 20,
                "video/mp4": 128 << 20, "video/webm": 128 << 20}
ATTACHMENT_LIMITS = {**MEDIA_LIMITS, "image/svg+xml": 16 << 20}
ATTACHMENT_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm"}


def identifier(value):
    if (not isinstance(value, str) or value in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value)):
        raise argparse.ArgumentTypeError("ID 须为 1–64 位字母、数字、点、下划线或横线，且不能为 . 或 ..")
    return value


def based_on(value):
    direction, separator, job = value.partition(":")
    identifier(direction)
    if separator and not re.fullmatch(r"[0-9a-f]{12}", job):
        raise argparse.ArgumentTypeError("--based-on 的任务 ID 须为 12 位十六进制字符")
    return {"direction_id": direction, "job_id": job if separator else None}


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


def plan_templates():
    return json.loads((Path(__file__).parent / "dash_static/research-plans.json").read_text())


def parser():
    root = argparse.ArgumentParser(prog="pf-node research", description=HELP,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    template = commands.add_parser("plan-template")
    template.add_argument("mode", choices=("exploration", "confirmation"))
    template.add_argument("--research-revision", type=integer, required=True)
    recipe_ids = tuple(plan_templates()["recipes"])
    template.add_argument("--recipe", choices=recipe_ids)
    recipes = commands.add_parser("recipes")
    recipes.add_argument("id", nargs="?", choices=recipe_ids)
    listing = commands.add_parser("list")
    listing.add_argument("kind", choices=KINDS)
    listing.add_argument("--research-id", type=identifier)
    listing.add_argument("--request-id", type=identifier)
    listing.add_argument("--follows", type=identifier)
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
    assessment = commands.add_parser("assess")
    assessment.add_argument("id", type=identifier)
    assessment.add_argument("--revision", type=integer, required=True)
    direction = commands.add_parser("direction")
    direction_commands = direction.add_subparsers(dest="direction_command", required=True)
    add = direction_commands.add_parser("add")
    add.set_defaults(kind="direction", reason=None, revision=None)
    add.add_argument("id", type=identifier)
    add.add_argument("file")
    add.add_argument("--based-on", type=based_on)
    add.add_argument("--operation-id", type=identifier)
    add.add_argument("--actor-label")
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
    conclude = commands.add_parser("conclude")
    conclude.add_argument("id", type=identifier)
    conclude.add_argument("state", choices=("verified", "no_gain", "rejected", "deferred"))
    conclude.add_argument("--reason", required=True)
    conclude.add_argument("--evidence", action="append", default=[])
    operation = commands.add_parser("operation")
    operation.add_argument("id", type=identifier)
    events = commands.add_parser("events")
    position = events.add_mutually_exclusive_group()
    position.add_argument("--after", type=lambda v: integer(v, minimum=0))
    position.add_argument("--cursor")
    events.add_argument("--research-id", type=identifier)
    events.add_argument("--limit", type=lambda v: integer(v, maximum=500))
    events.add_argument("--wait-seconds", type=lambda v: integer(v, minimum=0, maximum=15))
    claim = commands.add_parser("claim")
    claim.add_argument("id", type=identifier)
    assign = commands.add_parser("assign")
    assign.add_argument("id", type=identifier)
    assign.add_argument("agent")
    link = commands.add_parser("link")
    link.add_argument("id", type=identifier)
    link.add_argument("follows")
    for name in ("close", "reopen"):
        command = commands.add_parser(name)
        command.add_argument("id", type=identifier)
        command.add_argument("--reason", required=True)
    note = commands.add_parser("note")
    note.add_argument("id", type=identifier)
    note.add_argument("text")
    note.add_argument("--reply-to", type=identifier)
    note.add_argument("--decision", choices=("adopt", "defer", "reject"))
    note.add_argument("--evidence", action="append", default=[])
    note.add_argument("--attach", action="append", type=Path, help="附加本机 PNG、JPEG、SVG、MP4 或 WebM，可重复")
    note.add_argument("--ask", action="store_true")
    note.add_argument("--to", type=identifier)
    comparison = commands.add_parser("compare")
    comparison.add_argument("id", type=identifier)
    comparison.add_argument("--item", action="append", required=True)
    comparison.add_argument("--pick", type=integer, default=1)
    comparison.add_argument("--title", required=True)
    comparison.add_argument("--question", required=True)
    comparison.add_argument("--direction", type=identifier)
    thread = commands.add_parser("thread")
    thread.add_argument("id", type=identifier)
    deliver = commands.add_parser("deliver")
    deliver.add_argument("id", type=identifier)
    deliver.add_argument("--summary", required=True)
    deliver.add_argument("--artifact", action="append", required=True)
    deliver.add_argument("--game-version")
    deliver.add_argument("--device", action="append", default=[])
    media = commands.add_parser("media")
    media.add_argument("id", type=identifier)
    media.add_argument("slot", choices=tuple("ABCDEFGH"))
    media.add_argument("--revision", type=integer, required=True)
    media.add_argument("--output", required=True)
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


# What to do next for refusals an agent can fix itself; keyed by the node's safe error code, never its text.
CODE_MESSAGES = {
    "research_guidance_unanswered": "这个研究里还有人工留言没回应，回应之前不能交付或给方向下结论："
                                    "pf-node checkin 列出要回应的留言，"
                                    "用 pf-node research note <研究> \"<回复>\" --reply-to <留言> --decision adopt|defer|reject 回应",
    "research_owned": "这个研究已有负责人；换负责人由人在网页上操作",
}


class RequestError(Exception):
    def __init__(self, message, *, status=None, code=None, current_revision=None, retry_after_seconds=None):
        super().__init__(message)
        self.status, self.code, self.current_revision = status, code, current_revision
        self.retry_after_seconds = retry_after_seconds

    def public(self):
        value = {"code": self.code or "research_request_unconfirmed", "error": str(self),
                 "http_status": self.status}
        if self.current_revision is not None:
            value["current_revision"] = self.current_revision
        if self.retry_after_seconds is not None:
            value["retry_after_seconds"] = self.retry_after_seconds
        return value


class Client:
    def __init__(self, target):
        self.target = target

    def require_capability(self, *, write=False):
        node = self.target.require_write() if write else self.target.identity()
        capabilities = node.get("capabilities")
        if not isinstance(capabilities, list) or "research.state-v1" not in capabilities:
            raise ValueError("节点缺少 research.state-v1；未降级到旧 GOL goal/human.md")

    def require_evaluation(self, *, pinned=False):
        if pinned and self.target.expected_node_id is None:
            raise ValueError("媒体读取需要显式 --node-id / PF_NODE_ID；未读取")
        self.require_capability()
        capabilities = self.target.identity()["capabilities"]
        if "research.evaluation-v1" not in capabilities or (pinned and "node.target-v1" not in capabilities):
            raise ValueError("节点缺少 research.evaluation-v1 或目标绑定能力；未降级读取/写入")

    def require_watch(self):
        if self.target.expected_node_id is None:
            raise ValueError("事件等待或 cursor 续读需要显式 --node-id / PF_NODE_ID；未读取")
        self.require_capability()
        capabilities = self.target.identity()["capabilities"]
        if "research.watch-v1" not in capabilities or "node.target-v1" not in capabilities:
            raise ValueError("节点缺少 research.watch-v1 或目标绑定能力；未降级等待/续读")

    def require_criteria(self, *, pinned=False):
        if pinned and self.target.expected_node_id is None:
            raise ValueError("判据读取需要显式 --node-id / PF_NODE_ID；未读取")
        self.require_capability()
        capabilities = self.target.identity()["capabilities"]
        if "research.criteria-v1" not in capabilities or (pinned and "node.target-v1" not in capabilities):
            raise ValueError("节点缺少 research.criteria-v1 或目标绑定能力；未降级读取/写入")

    def http_error(self, error):
        status, code, revision, retry_after = error.code, None, None, None
        validation = owner = reference_error = None
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
            candidate = value.get("retry_after_seconds") if isinstance(value, dict) else None
            if status == 429 and type(candidate) is int and 0 <= candidate <= 86400:
                retry_after = candidate
            candidate = value.get("owner") if isinstance(value, dict) else None
            if (status == 409 and code == "research_owned" and isinstance(candidate, str)
                    and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", candidate)):
                owner = candidate
            candidate = value.get("error") if isinstance(value, dict) else None
            if (isinstance(candidate, str) and 0 < len(candidate) <= 4096
                    and all(32 <= ord(char) != 127 for char in candidate)):
                if status == 400 and code == "invalid_research":
                    validation = candidate
                elif status == 404 and code == "research_reference_not_found":
                    reference_error = candidate
        except (ValueError, UnicodeError, OSError, http.client.HTTPException, RecursionError):
            pass
        finally:
            error.close()
        if code in CODE_MESSAGES:
            message = CODE_MESSAGES[code]
            if owner is not None:
                message += f"（负责人 {owner}；可用 research note 留言）"
        elif validation is not None:
            message = "研究数据校验失败：" + validation
        elif reference_error is not None:
            message = "研究引用无法解析：" + reference_error + "；不据此自动重新提交"
        elif status == 409:
            message = "操作内容、对象版本、原目标或原件状态冲突；保留原 ID，先读取当前状态"
        elif 300 <= status < 400:
            message = "节点返回重定向；未转发凭据或重发请求"
        elif status == 404:
            message = "对象、版本、原件或原操作当前不可查；不据此自动重新提交"
        elif status in (401, 403):
            message = "节点拒绝访问；读取和写入需要对应权限"
        elif status == 429:
            message = "研究请求暂不可用；保留原位置，按提示稍后显式重试，未自动重发"
        else:
            message = "研究状态请求被拒绝或未确认；未自动重试"
        return RequestError(message, status=status, code=code, current_revision=revision,
                            retry_after_seconds=retry_after)

    def request(self, method, path, body=None, headers=None):
        request = self.target.request(method, path, body,
                                      headers if headers is not None else {"Content-Type": "application/json"} if body is not None else None)
        try:
            with self.target.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise RequestError("节点回包超过上限；结果未确认")
            value = _json(raw)
        except urllib.error.HTTPError as error:
            raise self.http_error(error) from None
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
    if error.retry_after_seconds is not None:
        detail += f", retry_after_seconds={error.retry_after_seconds}"
    print(f"pf-node: {error}（{detail}）", file=sys.stderr)


def _event_response(value, args):
    # The server binds opaque cursors to their stream, scope and event anchor.
    # Check the new response envelope without interpreting or advancing a cursor.
    stream, scope = value.get("stream_id"), value.get("research_id")
    position, head = value.get("next_after"), value.get("head")
    cursor, items = value.get("next_cursor"), value.get("items")
    valid_scope = (scope is None or isinstance(scope, str) and scope not in (".", "..")
                   and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", scope))
    if (not isinstance(stream, str) or not re.fullmatch(r"[0-9a-f]{32}", stream)
            or "research_id" not in value or not valid_scope
            or (args.research_id is not None and scope != args.research_id)
            or (args.cursor is None and scope != args.research_id)
            or type(head) is not int or not 0 <= head <= (1 << 63) - 1
            or type(position) is not int or not 0 <= position <= head
            or not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048
            or not isinstance(items, list) or len(items) > (args.limit or 100)
            or type(value.get("has_more")) is not bool
            or value.get("wait_result") not in ("events", "timeout", "immediate")):
        raise RequestError("事件回包的流、范围或位置未确认；保留原游标", code="research_watch_invalid_response")


def _assessment_response(value, args):
    comparison = value.get("comparison")
    if (value.get("research_id") != args.id or type(value.get("revision")) is not int
            or value["revision"] != args.revision or not isinstance(comparison, dict)
            or comparison.get("node_id") != value["node_id"]
            or type(comparison.get("partial")) is not bool):
        raise RequestError("判断回包未绑定原研究、版本或比较目标；结果未确认",
                           code="research_assessment_invalid_response")


def _media_headers(response):
    if response.status != 200:
        raise RequestError("媒体读取未返回完整 200 响应；未发布文件", code="media_invalid_response")
    values = {}
    for name in ("Content-Type", "Content-Length", "X-Content-SHA256"):
        found = response.headers.get_all(name, [])
        if len(found) != 1:
            raise RequestError("媒体响应缺少唯一的类型、长度或 SHA；未发布文件", code="media_invalid_response")
        values[name] = found[0].strip()
    mime = values["Content-Type"].split(";", 1)[0].strip().lower()
    length, digest = values["Content-Length"], values["X-Content-SHA256"]
    if (mime not in MEDIA_LIMITS or not re.fullmatch(r"[0-9]{1,12}", length)
            or not 1 <= int(length) <= MEDIA_LIMITS[mime]
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or response.headers.get("Transfer-Encoding") is not None
            or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
        raise RequestError("媒体类型、大小、编码或 SHA 不符合同；未发布文件", code="media_invalid_response")
    return mime, int(length), digest


def _media(client, args):
    path = os.path.abspath(args.output)
    # lexists also refuses broken symlinks; the final link repeats this check atomically.
    if os.path.lexists(path):
        raise ValueError("输出文件已存在；未覆盖、未读取媒体")
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError("输出父目录不存在；未读取媒体")
    client.require_evaluation(pinned=True)
    endpoint = f"/research-state/request/{args.id}/media/{args.slot}?revision={args.revision}"
    request = client.target.request("GET", endpoint, headers={"Accept": ", ".join(MEDIA_LIMITS)})
    temporary, owned_inode, published = None, None, False
    try:
        try:
            response = client.target.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            raise client.http_error(error) from None
        with response:
            mime, length, expected_digest = _media_headers(response)
            fd, temporary = tempfile.mkstemp(prefix=".pf-research-media-", suffix=".partial",
                                              dir=os.path.dirname(path))
            info = os.fstat(fd)
            owned_inode = (info.st_dev, info.st_ino)
            received, digest = 0, hashlib.sha256()
            with os.fdopen(fd, "wb") as output:
                while received < length:
                    chunk = response.read(min(256 << 10, length - received))
                    if not chunk:
                        raise RequestError("媒体读取中断，长度未确认；未发布文件", code="media_read_unconfirmed")
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                actual_digest = digest.hexdigest()
                if received != length or actual_digest != expected_digest:
                    raise RequestError("媒体长度或 SHA 校验失败；未发布文件", code="media_integrity_mismatch")
                output.flush()
                os.fsync(output.fileno())
            info = os.lstat(temporary)
            if (info.st_dev, info.st_ino) != owned_inode:
                raise RequestError("本次 partial 身份已改变；未发布文件", code="media_local_error")
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise RequestError("输出路径在下载期间已存在；未覆盖，原件未发布", code="media_output_exists") from None
            published = True
        print(json.dumps({"schema_version": 1, "node_id": client.target.expected_node_id,
                          "request_id": args.id, "revision": args.revision, "slot": args.slot,
                          "path": path, "size": received, "sha256": actual_digest, "mime": mime},
                         ensure_ascii=False, allow_nan=False))
        print("pf-node: 原件长度与 SHA 已核对；下载不表示已解码、查看或提交反馈。", file=sys.stderr)
        return 0
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise RequestError("媒体读取或本地保存未确认；未自动重试，没有创建反馈", code="media_read_unconfirmed") from None
    except KeyboardInterrupt:
        print("pf-node: 媒体读取回执未确认；未自动重试，没有创建反馈。" +
              ("目标文件已在完整性校验后发布，请保留并核对。" if published else "未发布目标文件。"),
              file=sys.stderr)
        return 130
    finally:
        if temporary is not None and owned_inode is not None:
            try:
                info = os.lstat(temporary)
                if (info.st_dev, info.st_ino) == owned_inode:
                    os.unlink(temporary)
                else:
                    print("pf-node: partial 身份已改变，未删除他人文件。", file=sys.stderr)
            except FileNotFoundError:
                pass
            except OSError:
                print(f"pf-node: 本次 partial 未能清理，保留在 {temporary}", file=sys.stderr)


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
    if getattr(args, "based_on", None) is not None:
        data["based_on"] = args.based_on
    if args.kind in ("request", "feedback") and "evaluation" in data:
        client.require_evaluation()
    if args.kind == "research" and "criteria" in data:
        client.require_criteria()
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
        if error.status == 400 and error.code == "invalid_research":
            print("pf-node: 本次校验未写入；按字段修正后可换新 operation_id 显式提交；未自动重试。", file=sys.stderr)
        else:
            print(f"pf-node: 保留原 operation_id={operation_id}；仅用 {lookup} 查询。"
                  "不换 ID、不重发 POST、不自动合并草稿。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"pf-node: 写入回执未确认；保留原 operation_id={operation_id}，用 {lookup} 查询，未自动重试。",
              file=sys.stderr)
        return 130
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    return 0


def _post(client, kind, oid, data, base, reason):
    operation_id = uuid.uuid4().hex
    body = json.dumps({"schema": "phonefarm/research-write@1", "operation_id": operation_id, "base_revision": base,
                       "data": data, "actor_label": None, "reason": reason},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    print(f"pf-node: {kind}/{oid} operation_id={operation_id}", file=sys.stderr, flush=True)
    print(json.dumps(client.request("POST", f"/research-state/{kind}/{oid}", body), ensure_ascii=False))
    return 0


def _artifact(item, node_id, option="--artifact"):
    kind, _, value = item.partition(":")
    if kind == "job":
        job_id, separator, path = value.partition(":")
        reference = {"kind": "job", "job_id": job_id, "node_id": node_id}
        if separator:
            if not path:
                raise ValueError(option + " 的 job 产物路径不能为空")
            reference["path"] = path
        return reference
    if kind == "result":
        return {"kind": "result", "result_id": value, "node_id": node_id}
    if kind == "url" and value:
        return {"kind": "external", "locator": value, "node_id": None}
    raise ValueError(option + " 用 job:<任务ID>[:<产物相对路径>]、result:<结果ID> 或 url:<地址>")


def _attachment(client, path):
    if not path.is_file():
        raise ValueError("--attach 需要本机普通文件")
    mime = ATTACHMENT_EXTENSIONS.get(path.suffix.lower())
    if mime is None:
        raise ValueError("--attach 只接受 PNG、JPEG、SVG、MP4 或 WebM")
    maximum = ATTACHMENT_LIMITS[mime]
    if path.stat().st_size > maximum:
        raise ValueError(f"--attach 单文件不能超过 {maximum >> 20} MiB")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError(f"--attach 单文件不能超过 {maximum >> 20} MiB")
    attachment_id = uuid.uuid4().hex
    value = client.request("POST", "/research-attachments", raw, headers={
        "X-Attachment-ID": attachment_id, "X-Filename": path.name, "Content-Type": mime})
    return {"kind": "attachment", "attachment_id": value["attachment_id"], "node_id": value["node_id"],
            "filename": value["filename"], "content_type": value["content_type"], "sha256": value["sha256"]}


def _comparison_kind(path):
    kind = {".png": "image", ".jpg": "image", ".jpeg": "image",
            ".mp4": "video", ".webm": "video"}.get(Path(path).suffix.lower())
    if kind is None:
        raise ValueError("--item 只接受 PNG、JPEG、MP4 或 WebM")
    return kind


def _comparison_source(client, item, index):
    if item.startswith("job:"):
        reference = _artifact(item, None, "--item")
        if "path" not in reference:
            raise ValueError("--item 的 job 产物路径不能为空")
        kind = _comparison_kind(reference["path"])
        source = {"name": f"item-{index + 1}", "kind": "job_output",
                  "job_id": reference["job_id"], "path": reference["path"]}
    else:
        path = Path(item)
        kind = _comparison_kind(path)
        attachment = _attachment(client, path)
        base = path.name if re.fullmatch(r"[A-Za-z0-9._-]{1,48}", path.name) else "source"
        source = {"name": f"item-{index + 1}-{base}", "kind": "attachment", "attachment_id": attachment["attachment_id"],
                  "filename": attachment["filename"], "local_path": item}
    return source, kind


def _compare(client, args):
    if not 2 <= len(args.item) <= 8:
        raise ValueError("research compare 需要 2 到 8 个 --item")
    if not 1 <= args.pick <= len(args.item):
        raise ValueError("--pick 必须在 1 到素材数之间")
    client.require_evaluation()
    current = client.request("GET", f"/research-state/research/{args.id}")
    directions = []
    if args.direction:
        direction = client.request("GET", f"/research-state/direction/{args.direction}")
        if direction.get("research_id") != args.id:
            raise ValueError("--direction 不属于这个研究；未登记或创建评价")
        directions = [{"id": args.direction, "revision": direction["revision"]}]
    prepared = [_comparison_source(client, item, index) for index, item in enumerate(args.item)]
    sources, kinds = [item[0] for item in prepared], [item[1] for item in prepared]
    result_id, request_id = "r-" + uuid.uuid4().hex[:16], "ev-" + uuid.uuid4().hex[:12]
    job_ids = {source.get("job_id") for source in sources}
    manifest = {"schema": "phonefarm/result@1", "result_id": result_id, "title": args.title,
                "job_id": job_ids.pop() if len(job_ids) == 1 else None,
                "labels": {"research_id": args.id, "direction_id": args.direction},
                "origin": {"evidence_origin": "unknown"}, "outcome": "unknown", "sources": sources,
                "limitations": ["仅登记人工对比素材，不代表实验有效或画质结论"]}
    print(f"pf-node: 登记 result_id={result_id}；仅提交一次。", file=sys.stderr, flush=True)
    client.request("POST", "/results", json.dumps(manifest, ensure_ascii=False, separators=(",", ":"),
                                                  allow_nan=False).encode("utf-8"))
    node_id = client.target.identity()["node_id"]
    order = list(range(len(sources)))
    for index in range(len(order) - 1, 0, -1):
        other = secrets.randbelow(index + 1)
        order[index], order[other] = order[other], order[index]
    data = {"research_id": args.id, "type": "evaluation", "title": args.title, "need": args.question,
            "why": "需要人工比较多份素材的主观差异", "resume_hint": None,
            "context": {"research_revision": current["revision"], "directions": directions},
            "evidence_refs": [{"kind": "result_source", "node_id": node_id, "result_id": result_id,
                               "source_name": source["name"]} for source in sources],
            "evaluation": {"schema": "phonefarm/compare@1", "pick": args.pick,
                           "items": [{"ref": index, "media_kind": kinds[index]} for index in order]}}
    operation_id = uuid.uuid4().hex
    body = json.dumps({"schema": "phonefarm/research-write@1", "operation_id": operation_id,
                       "base_revision": None, "data": data, "actor_label": None, "reason": None},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    print(f"pf-node: request/{request_id} operation_id={operation_id}", file=sys.stderr, flush=True)
    client.request("POST", f"/research-state/request/{request_id}", body)
    query = urllib.parse.urlencode({"kind": "request", "id": request_id})
    print(json.dumps({"schema_version": 1, "node_id": node_id, "research_id": args.id,
                      "result_id": result_id, "request_id": request_id,
                      "url": client.target.url + "/dash/research?" + query,
                      "slots": {"ABCDEFGH"[position]: args.item[index] for position, index in enumerate(order)}},
                     ensure_ascii=False, allow_nan=False))
    return 0


def _conclude(client, args):
    if not args.reason.strip() or len(args.reason) > 16384:
        raise ValueError("reason 须为非空文字，至多 16384 字符")
    current = client.request("GET", f"/research-state/direction/{args.id}")
    if current.get("kind") != "direction" or current.get("id") != args.id or type(current.get("revision")) is not int:
        raise ValueError("方向回包未绑定原对象；未下结论")
    data = current["data"]
    data["state"], data["state_reason"] = args.state, args.reason
    if args.evidence:
        data["evidence_refs"] = [_artifact(item, client.target.identity()["node_id"], "--evidence") for item in args.evidence]
    return _post(client, "direction", args.id, data, current["revision"], args.reason)


def _task(client, args):
    """Owner, thread and delivery of one research: the operations an agent loop uses."""
    if args.command == "thread":
        client.require_capability()
        query = urllib.parse.urlencode({"research_id": args.id, "limit": 100})
        for item in reversed(client.request("GET", "/research-state/note?" + query)["items"]):
            speaker = "human" if item["auth_channel"] == "dashboard_steer" else item["actor_label"] or item["auth_channel"]
            answer = f" ↳{item['reply_to']}" + (f" [{item['decision']}]" if item["decision"] else "") if item["reply_to"] else ""
            ask = " [问人]" + ("[待回复]" if item.get("question_unanswered") else "") if item.get("ask") else ""
            stamp = utc(item["updated_at"])
            print(f"{stamp} {speaker} {item['id']}{ask}{answer}: {item['text']}")
        return 0
    client.require_capability(write=True)
    if args.command == "conclude":
        return _conclude(client, args)
    if args.command == "compare":
        return _compare(client, args)
    if args.command == "note":
        node_id = client.target.identity()["node_id"]
        data = {"research_id": args.id, "text": args.text, "reply_to": args.reply_to, "decision": args.decision,
                "ask": args.ask, "to": args.to, "evidence_refs": [_artifact(item, node_id, "--evidence") for item in args.evidence]
                + [_attachment(client, path) for path in args.attach or []]}
        return _post(client, "note", "n-" + uuid.uuid4().hex[:12], data, None, None)
    current = client.request("GET", f"/research-state/research/{args.id}")
    data, owner = current["data"], current["data"].get("owner")
    if args.command == "claim":
        me = client.request("GET", "/whoami").get("agent")
        if not me:
            raise ValueError("认领要用你自己的 agent token：先用管理 token 运行 pf-node agents token <名字>")
        if owner == me:
            print(f"pf-node: {args.id} 已经由你负责", file=sys.stderr)
            return 0
        if owner:
            raise ValueError(f"{args.id} 由 {owner} 负责；改派请人工在网页上操作")
        data["owner"] = me
        return _post(client, "research", args.id, data, current["revision"], f"{me} 认领")
    if args.command == "assign":
        data["owner"] = None if args.agent == "none" else args.agent
        return _post(client, "research", args.id, data, current["revision"], f"指派给 {args.agent}")
    if args.command == "link":
        data["follows"] = None if args.follows == "none" else args.follows
        reason = "取消接续" if args.follows == "none" else f"接续自 {args.follows}"
        return _post(client, "research", args.id, data, current["revision"], reason)
    if args.command in ("close", "reopen"):
        if not args.reason.strip() or len(args.reason) > 16384:
            raise ValueError("reason 须为非空文字，至多 16384 字符")
        data["state"] = "closed" if args.command == "close" else "active"
        data["state_reason"] = args.reason
        return _post(client, "research", args.id, data, current["revision"], args.reason)
    summary = Path(args.summary[1:]).read_text(encoding="utf-8") if args.summary.startswith("@") else args.summary
    node_id = client.target.identity()["node_id"]
    data["deliverable"] = {"summary": summary, "artifacts": [_artifact(item, node_id) for item in args.artifact],
                           "applies_to": {"game_version": args.game_version, "devices": args.device}}
    data["state"] = "delivered"
    return _post(client, "research", args.id, data, current["revision"], "交付")


def main(argv, *, target):
    argv = list(argv)
    if not argv or any(item in ("-h", "--help") for item in argv):
        print(HELP)
        return 0
    args = parser().parse_args(argv)
    if args.command in ("plan-template", "recipes"):
        templates = plan_templates()
        if args.command == "recipes":
            print(json.dumps(templates["recipes"][args.id] if args.id else templates["recipes"], ensure_ascii=False))
            return 0
        result = {**templates[args.mode], "research_revision": args.research_revision}
        if args.recipe:
            recipe = templates["recipes"][args.recipe]
            result["title"] = recipe["title"] + " · " + result["title"]
            result["plan"] += "\n\n路线建议（适配实际游戏/设备后执行）：\n" + recipe["plan"]
        print(json.dumps(result, ensure_ascii=False))
        return 0
    client = Client(target)
    try:
        if args.command == "direction":
            args.command = "create"
            return _write(client, args)
        if args.command in ("create", "update"):
            return _write(client, args)
        if args.command in ("claim", "assign", "link", "close", "reopen", "note", "compare", "thread", "deliver", "conclude"):
            return _task(client, args)
        if args.command == "media":
            return _media(client, args)
        if args.command == "list":
            if args.request_id is not None and args.kind != "feedback":
                raise ValueError("--request-id 筛选仅用于 feedback")
            if args.state is not None and args.kind == "feedback":
                raise ValueError("feedback 不提供 state 筛选")
            if args.follows is not None and args.kind != "research":
                raise ValueError("--follows 筛选仅用于 research")
            path = f"/research-state/{args.kind}" + _query(args, ("research_id", "state", "request_id", "follows", "limit", "cursor"))
        elif args.command == "show":
            path = f"/research-state/{args.kind}/{args.id}" + _query(args, ("revision",))
        elif args.command == "history":
            path = f"/research-state/{args.kind}/{args.id}/history" + _query(args, ("limit", "cursor"))
        elif args.command == "assess":
            path = f"/research-state/research/{args.id}/assessment" + _query(args, ("revision",))
        elif args.command == "operation":
            path = "/research-operations/" + args.id
        else:
            path = "/research-events" + _query(args, ("after", "cursor", "research_id", "limit", "wait_seconds"))
        watch = args.command == "events" and (args.cursor is not None or (args.wait_seconds or 0) > 0)
        if args.command == "assess":
            client.require_criteria(pinned=True)
        elif watch:
            client.require_watch()
        else:
            client.require_capability()
        value = client.request("GET", path)
        if watch:
            _event_response(value, args)
        if args.command == "assess":
            _assessment_response(value, args)
        if args.command == "show" and (value.get("kind") != args.kind or value.get("id") != args.id
                or (args.revision is not None and value.get("revision") != args.revision)):
            raise RequestError("回包未绑定原对象或指定版本；结果未确认")
        if args.command == "operation" and value.get("operation_id") != args.id:
            raise RequestError("回包未绑定原 operation_id；结果未确认")
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 2 if args.command == "assess" and value["comparison"]["partial"] else 0
    except RequestError as error:
        _error(error)
        if args.command == "events":
            print("pf-node: 原事件游标/after 保持不变；未自动续读或确认消费。", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"pf-node: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if args.command == "events":
            print("pf-node: 已停止事件读取；保留原游标，未确认消费。", file=sys.stderr)
        return 130
