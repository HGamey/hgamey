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
  pf-node results search --node NAME [--node OTHER] [同 list 的过滤与 limit]
  pf-node results search --node NAME --source-node-id ID --revision N --cursor CURSOR [同原过滤]
  pf-node results compare FILE
  pf-node results show ID
  pf-node results register FILE
  pf-node results import FILE --adapter doom-lr4-026-v1|gol-optimize-v1 [--import-id ID]
      [--game-id ID] [--game-version VERSION] [--device TEXT] [--research-id ID] [--direction-id ID]
  pf-node results import-status ID

输出节点原始 JSON。登记只读至多 256 KiB 的清单；导入保留至多 1 MiB 的原 UTF-8 JSON。
register 使用文件内固定 result_id；import 在读文件前打印指定或生成的 import_id。
写入核对显式目标与能力，只提交一次。回执未知时，用 show 或 import-status 查询原 ID，
不换 ID、不自动重发。登记不表示测量、收益、设备恢复或同状态资格已经验证。
search 只读检索 1–4 个已绑定目录节点，必须显式指定 hub --node-id / PF_NODE_ID。
首屏从 /nodes 冻结目标；续页使用原 source.node_id/revision/cursor，不重新选目标或回第一页。
各节点独立分页，limit 按每节点计；部分失败仍输出完整 JSON，退出码 2。未自动重试。
compare 只读至多 16 KiB UTF-8 spec，需显式 node-id 与 results.compare-v1，单次 GET。
spec.schema=phonefarm/result-compare@1，node_id 为原目标，columns 为 2–4 个候选列（首列基线），
rows 为 1–16 个指标行，各行 metric_indexes 明确选原数组索引或 null；不按名称自动配对。
返回登记值差/正基准百分比及原描述差异，不代表科学可比、统计显著或已采纳。
不可查列保留完整响应并 exit2；缺值/未选/越界单元格不填零。不自动重试、注册或翻页。
"""
MAX_FILE = 1 << 20
MAX_REGISTER = 256 << 10
MAX_COMPARE = 16 << 10
MAX_RESPONSE = 6 << 20
OUTCOMES = {"gain", "no_gain", "regression", "failed", "not_run", "unknown"}
SEARCH_STATUSES = {"ok", "unavailable", "unauthorized", "unsupported", "identity_mismatch",
                   "configuration_changed", "disabled", "no_credentials", "unknown_node", "busy",
                   "invalid_cursor"}


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
        local = value.get("local") if method == "GET" and path == "/nodes" else value
        if (value.get("schema_version") != 1 or not isinstance(local, dict)
                or local.get("node_id") != self.target.identity()["node_id"]):
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


def _compare_spec(path, node_id):
    try:
        _, spec = _read(path, MAX_COMPARE)
    except RecursionError:
        raise ValueError("比较 spec 嵌套过深；未查询") from None
    if spec.get("schema") != "phonefarm/result-compare@1" or spec.get("node_id") != node_id:
        raise ValueError("比较需 phonefarm/result-compare@1 且 node_id 匹配原显式目标；未查询")
    columns, rows = spec.get("columns"), spec.get("rows")
    if not isinstance(columns, list) or not 2 <= len(columns) <= 4:
        raise ValueError("比较 columns 须有 2–4 个明确列，首列为基线")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("比较 rows 须有 1–16 个明确指标行")
    for group in (columns, rows):
        seen = set()
        for item in group:
            if not isinstance(item, dict):
                raise ValueError("比较行列须为 JSON 对象")
            identity = identifier(item.get("id"))
            if identity in seen:
                raise ValueError("比较行或列 ID 重复；未自动合并")
            seen.add(identity)
            if not isinstance(item.get("label"), str) or not item["label"].strip():
                raise ValueError("比较行列需明确 label；未自动选标签")
    for column in columns:
        identifier(column.get("result_id"))
    for row in rows:
        indexes = row.get("metric_indexes")
        if (not isinstance(indexes, list) or len(indexes) != len(columns)
                or any(index is not None and (type(index) is not int or not 0 <= index <= 255)
                       for index in indexes)):
            raise ValueError("每行 metric_indexes 须与列数相同，值为 0..255 整数或 null")
    return spec


def _compare(client, path):
    target = client.target
    if target.expected_node_id is None:
        raise ValueError("compare 需要显式 --node-id / PF_NODE_ID；未读取或选择替代节点")
    capabilities = client.require_capability()["capabilities"]
    if "node.target-v1" not in capabilities or "results.compare-v1" not in capabilities:
        raise ValueError("节点缺少 node.target-v1 或 results.compare-v1；未降级比较")
    spec = _compare_spec(path, target.expected_node_id)
    query = urllib.parse.urlencode({"spec": json.dumps(spec, ensure_ascii=False, separators=(",", ":"),
                                                      allow_nan=False)})
    value = client.request("GET", "/result-compare?" + query)
    columns, rows = value.get("columns"), value.get("rows")
    if (value.get("spec") != spec or value.get("basis") != "registered_values"
            or value.get("comparability") != "not_established"
            or value.get("baseline_column_id") != spec["columns"][0]["id"]
            or type(value.get("partial")) is not bool
            or not isinstance(columns, list) or len(columns) != len(spec["columns"])
            or not isinstance(rows, list) or len(rows) != len(spec["rows"])):
        raise RequestError("比较回包未绑定原 spec、基线或登记值范围；保留原选择，未自动重试")
    failed = False
    for column, requested in zip(columns, spec["columns"]):
        if (not isinstance(column, dict) or column.get("node_id") != target.expected_node_id
                or any(column.get(key) != requested[key] for key in ("id", "label", "result_id"))
                or column.get("status") not in ("available", "unavailable")):
            raise RequestError("比较列未保留原节点、结果或列顺序；未自动重试")
        if column["status"] == "available":
            record = column.get("result")
            if (not isinstance(record, dict) or record.get("node_id") != target.expected_node_id
                    or record.get("result_id") != requested["result_id"]):
                raise RequestError("比较列的原结果身份未确认；未自动重试")
        else:
            if column.get("result") is not None:
                raise RequestError("不可查比较列混有未确认原结果；未自动重试")
            failed = True
    for row, requested in zip(rows, spec["rows"]):
        cells = row.get("cells") if isinstance(row, dict) else None
        if (not isinstance(row, dict)
                or any(row.get(key) != requested[key] for key in ("id", "label", "metric_indexes"))
                or not isinstance(cells, list) or len(cells) != len(columns)):
            raise RequestError("比较行未保留原指标选择或行顺序；未自动重试")
        for cell, column, index in zip(cells, spec["columns"], requested["metric_indexes"]):
            if (not isinstance(cell, dict) or cell.get("column_id") != column["id"]
                    or cell.get("metric_index") != index or type(cell.get("metric_index")) is not type(index)
                    or cell.get("status") not in ("available", "not_selected", "result_unavailable", "invalid_index")):
                raise RequestError("比较单元格未绑定原列与 metric_index；未自动重试")
    if value["partial"] != failed:
        raise RequestError("比较回包的部分失败状态与列状态不符；未自动重试")
    return value


def _search(client, args):
    nodes, query = [], {}
    allowed = set(LABELS) | {"job_id", "outcome", "limit", "cursor", "source_node_id", "revision"}
    while args:
        option = args.pop(0)
        key = option[2:].replace("-", "_") if option.startswith("--") else ""
        if (key != "node" and (key not in allowed or key in query)) or not args:
            raise ValueError("无效、重复或缺值的 search 选项；用 results --help 查看用法")
        value = args.pop(0)
        if key == "node":
            if not re.fullmatch(r"[a-z0-9_-]{1,64}", value) or value == "local":
                raise ValueError("--node 须为已有目录登记名；本节点请用 results list")
            nodes.append(value)
        else:
            query[key] = value
    if not 1 <= len(nodes) <= 4 or len(set(nodes)) != len(nodes):
        raise ValueError("search 需选择 1–4 个不同的 --node")
    if "job_id" in query and not re.fullmatch(r"[0-9a-f]{12}", query["job_id"]):
        raise ValueError("job_id 须为 12 位任务 ID；搜索时分别作用于各源节点")
    if "outcome" in query and query["outcome"] not in OUTCOMES:
        raise ValueError("outcome 不在支持的结果状态中")
    if "limit" in query and (not query["limit"].isdigit() or not 1 <= int(query["limit"]) <= 100):
        raise ValueError("limit 须为每节点 1–100")
    source_id, revision = query.pop("source_node_id", None), query.pop("revision", None)
    if "cursor" in query:
        if len(nodes) != 1 or not query["cursor"] or source_id is None or revision is None:
            raise ValueError("续页需要一个 --node、原 --source-node-id、--revision 和非空 --cursor")
        if not re.fullmatch(r"[0-9a-f]{32}", source_id) or not re.fullmatch(r"[1-9][0-9]*", revision):
            raise ValueError("续页 source-node-id 须为 32 位小写十六进制，revision 须为正整数")
        targets = [{"name": nodes[0], "node_id": source_id, "revision": int(revision)}]
    elif source_id is not None or revision is not None:
        raise ValueError("--source-node-id 与 --revision 仅用于 --cursor 续页；首屏从只读目录选择")
    else:
        targets = None
    if client.target.expected_node_id is None:
        raise ValueError("search 需要显式 hub --node-id / PF_NODE_ID；先用 node 核对原节点身份")
    hub = client.target.identity()
    capabilities = hub.get("capabilities")
    if (not isinstance(capabilities, list) or "node.target-v1" not in capabilities
            or "results.federation-v1" not in capabilities):
        raise ValueError("hub 缺少 node.target-v1 或 results.federation-v1；未降级查询")
    if targets is None:
        # A directory read never refreshes or binds a previously unknown source.
        directory = client.request("GET", "/nodes")
        items = directory.get("items")
        if not isinstance(items, list):
            raise RequestError("节点目录回包无效；未查询远端")
        targets = []
        for name in nodes:
            matches = [item for item in items if isinstance(item, dict) and item.get("name") == name]
            if len(matches) != 1:
                raise ValueError(f"目录没有唯一的节点 {name}；先核对目录，未查询远端")
            entry = matches[0]
            if not isinstance(entry.get("node_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", entry["node_id"]):
                raise ValueError(f"目录节点 {name} 尚未绑定身份；请先在目录核对，本查询不自动绑定")
            if type(entry.get("revision")) is not int or entry["revision"] < 1:
                raise RequestError("目录节点 revision 无效；未查询远端")
            targets.append({"name": name, "node_id": entry["node_id"], "revision": entry["revision"]})
    if len({item["node_id"] for item in targets}) != len(targets):
        raise ValueError("所选目录项绑定同一 node_id；请只选择其中一项")
    if any(item["node_id"] == hub["node_id"] for item in targets):
        raise ValueError("本节点不列入远端 targets；请用 results list")
    query["targets"] = json.dumps(targets, separators=(",", ":"), ensure_ascii=True)
    value = client.request("GET", "/result-search?" + urllib.parse.urlencode(query))
    if type(value.get("partial")) is not bool or not isinstance(value.get("nodes"), list):
        raise RequestError("跨节点搜索回包无效；未自动重试或回到首屏")
    expected = {item["name"]: item for item in targets}
    seen, failed = set(), False
    for item in value["nodes"]:
        source = item.get("source") if isinstance(item, dict) else None
        name = source.get("name") if isinstance(source, dict) else None
        if not isinstance(name, str) or name not in expected or name in seen:
            raise RequestError("跨节点搜索回包未保留所选节点；未自动重试")
        seen.add(name)
        status = item.get("status")
        if (not isinstance(status, str) or status not in SEARCH_STATUSES
                or item.get("requested_cursor") != query.get("cursor")):
            raise RequestError("跨节点搜索分项状态或游标不符；未自动重试")
        if status == "ok":
            if (source.get("node_id") != expected[name]["node_id"]
                    or type(source.get("revision")) is not int
                    or source.get("revision") != expected[name]["revision"]
                    or not isinstance(item.get("items"), list)
                    or (item.get("next_cursor") is not None and not isinstance(item["next_cursor"], str))):
                raise RequestError("跨节点结果未绑定原源身份或版本；未自动重试")
        else:
            if item.get("items") is not None or item.get("next_cursor") is not None:
                raise RequestError("跨节点失败分项混有未确认结果；未自动重试")
            failed = True
    if seen != set(expected) or value["partial"] != failed:
        raise RequestError("跨节点搜索回包缺少分项或部分失败状态；未自动重试")
    return value


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
        if command == "search":
            value = _search(client, args)
            print(json.dumps(value, ensure_ascii=False, allow_nan=False))
            return 2 if value["partial"] else 0
        elif command == "compare" and len(args) == 1:
            value = _compare(client, args[0])
            print(json.dumps(value, ensure_ascii=False, allow_nan=False))
            return 2 if value["partial"] else 0
        elif command == "list":
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
