"""Explicit client of the shared node directory; no remote job dispatch."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class ClientError(Exception):
    def __init__(self, code, message, http_status=None):
        super().__init__(message)
        self.code, self.http_status = code, http_status

    def public(self):
        return {"code": self.code, "message": str(self), "http_status": self.http_status}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes rejected values, which may be credentials.
        raise ClientError("invalid_arguments", "参数无效或缺失；更新/启停须显式 --revision，详见 --help")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def url(value):
    if not isinstance(value, str):
        raise ClientError("invalid_url", "URL 须为 HTTP(S) 字符串")
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (isinstance(value, str) and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                 and parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ClientError("invalid_url", "URL 须为无用户凭据、查询或 fragment 的 HTTP(S) 地址")
    return value.rstrip("/")


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", value) or value == "local":
        raise ClientError("invalid_name", "登记名须为 1–64 位小写字母、数字、下划线或短横线；local 保留")
    return value


def revision(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = 0
    if result < 1:
        raise argparse.ArgumentTypeError("revision must be positive")
    return result


def token_file(path):
    try:
        with open(path, encoding="utf-8") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("credential file exceeds limit")
        value = raw.strip()
    except (OSError, UnicodeError, TypeError, ValueError):
        raise ClientError("credential_unavailable", "无法读取指定凭据文件") from None
    return token(value)


def token(value):
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(c) < 32 or ord(c) >= 127 for c in value)):
        raise ClientError("invalid_credential", "凭据须为非空、无控制字符的 ASCII token")
    return value


def hub_token():
    if os.environ.get("PF_NODE_TOKEN"):
        return token(os.environ["PF_NODE_TOKEN"])
    if os.environ.get("PF_NODE_TOKEN_FILE"):
        return token_file(os.environ["PF_NODE_TOKEN_FILE"])
    raise ClientError("credential_unavailable", "hub 认证需显式提供 PF_NODE_TOKEN 或 PF_NODE_TOKEN_FILE")


class Client:
    def __init__(self, hub):
        self.hub, self.token = url(hub), hub_token()
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, body=None):
        writing = body is not None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json",
                   "User-Agent": "pf-fleet/2"}
        payload = None
        if writing:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.hub + path, data=payload,
                                         method="POST" if writing else "GET", headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read((8 << 20) + 1)
            if len(raw) > (8 << 20):
                raise ValueError("response size")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("response type")
            entries = value.get("items") if path.split("?", 1)[0] == "/nodes" and not writing else [value]
            if (not isinstance(entries, list) or (not writing and path.split("?", 1)[0] == "/nodes"
                    and (value.get("schema_version") != 1 or not isinstance(value.get("local"), dict)))):
                raise ValueError("directory protocol")
            for entry in entries:
                if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                        or not isinstance(entry.get("url"), str) or type(entry.get("revision")) is not int
                        or type(entry.get("credential_present")) is not bool
                        or not isinstance(entry.get("connection"), dict)
                        or entry.get("snapshot") is not None and not isinstance(entry.get("snapshot"), dict)):
                    raise ValueError("directory entry protocol")
            return value
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()  # Never echo response bodies or request credentials.
            code, message = {
                401: ("unauthorized", "hub 认证失败"),
                403: ("forbidden", "hub 拒绝此操作；写入需要管理凭据"),
                404: ("not_found_or_unsupported", "登记或目录 API 不存在；请用 nodes 核对并确认 hub 已升级"),
                409: ("conflict", "登记名或 revision 冲突，或已有刷新进行中；先查询状态，不自动覆盖或重试"),
            }.get(status, ("http_error", "hub 返回 HTTP " + str(status)))
            if 300 <= status < 400:
                code, message = "redirect_refused", "hub 重定向已拒绝；请显式使用正确地址"
            if writing and (300 <= status < 400 or status >= 500):
                message += "；写入结果请先用 show/nodes 核对，未自动重试"
            raise ClientError(code, message, status) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, UnicodeError):
            message = ("写入结果未知；请先用 show/nodes 查询，未自动重试"
                       if writing else "hub 请求失败或响应协议不可读")
            raise ClientError("write_result_unknown" if writing else "request_failed", message) from None


def parser():
    root = Parser(prog="pf-fleet --hub URL", description="共享节点目录；不代理跨节点作业。",
                  epilog="hub 认证：PF_NODE_TOKEN(_FILE)。远端凭据只用 --token-file；写操作不重试。")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("nodes", "status", "show"):
        sub = commands.add_parser(command)
        if command == "show":
            sub.add_argument("name")
            sub.add_argument("--devices", action="store_true", help="显式排队设备采样，立即返回当前缓存")
        if command != "nodes":
            sub.add_argument("--refresh", action="store_true", help="排队刷新到期目录缓存，不等待远端")
        sub.add_argument("--json", action="store_true", help="保留完整 API 对象")
    for command in ("register", "update"):
        sub = commands.add_parser(command)
        sub.add_argument("name")
        sub.add_argument("--url", required=command == "register")
        if command == "update":
            sub.add_argument("--revision", type=revision, required=True)
        tags = sub.add_mutually_exclusive_group()
        tags.add_argument("--tag", action="append", help="可重复；更新时替换完整标签列表")
        if command == "update":
            tags.add_argument("--clear-tags", action="store_true")
        for field in ("note", "ssh", "goals-dir"):
            sub.add_argument("--" + field, help="元数据；空字符串可清空")
        credential = sub.add_mutually_exclusive_group()
        credential.add_argument("--token-file", help="远端节点凭据文件，不是 hub 认证文件")
        if command == "update":
            credential.add_argument("--clear-token", action="store_true", help="明确清除已保存的远端凭据")
        sub.add_argument("--json", action="store_true")
    for command in ("disable", "enable"):
        sub = commands.add_parser(command)
        sub.add_argument("name")
        sub.add_argument("--revision", type=revision, required=True)
        sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("import", help="逐条创建指定旧 nodes.json 的登记；不覆盖、不删除源文件")
    sub.add_argument("file")
    sub.add_argument("--json", action="store_true")
    return root


def changes(args):
    result = {}
    for field in ("url", "note", "ssh", "goals_dir"):
        value = getattr(args, field, None)
        if value is not None:
            result[field] = url(value) if field == "url" else value
    if args.tag is not None:
        result["tags"] = args.tag
    elif getattr(args, "clear_tags", False):
        result["tags"] = []
    if args.token_file is not None:
        result["token"] = token_file(args.token_file)
    elif getattr(args, "clear_token", False):
        result["token"] = None
    return result


def import_nodes(client, path):
    try:
        source = Path(path).absolute()
        with source.open(encoding="utf-8") as stream:
            config = json.load(stream)
        if not isinstance(config, dict) or not isinstance(config.get("nodes"), list):
            raise ValueError("legacy registry shape")
    except (OSError, UnicodeError, ValueError):
        raise ClientError("invalid_import", "无法读取指定旧注册表；需要包含 nodes 数组的 JSON") from None
    results = []
    for index, entry in enumerate(config["nodes"]):
        label = None
        try:
            if not isinstance(entry, dict):
                raise ClientError("invalid_import_entry", "登记项须为对象")
            label = name(entry.get("name"))
            body = {"name": label, "url": url(entry.get("url"))}
            for key in ("tags", "note", "ssh", "goals_dir"):
                if key in entry:
                    body[key] = entry[key]
            if "token" in entry:
                raise ClientError("invalid_import_entry", "旧注册表凭据仅支持 token_file，不导入内联 token")
            if entry.get("token_file"):
                credential = Path(entry["token_file"])
                if not credential.is_absolute():
                    credential = source.parent / credential
                body["token"] = token_file(credential)
            results.append({"index": index, "name": label, "status": "created",
                            "entry": client.request("/nodes", body)})
        except ClientError as error:
            results.append({"index": index, "name": label, "status": "failed", "error": error.public()})
        except (TypeError, ValueError):
            results.append({"index": index, "name": label, "status": "failed",
                            "error": ClientError("invalid_import_entry", "登记项字段格式无效").public()})
    return {"schema_version": 1, "items": results,
            "created": sum(item["status"] == "created" for item in results),
            "failed": sum(item["status"] == "failed" for item in results)}


def clean(value):
    return "".join(c if ord(c) >= 32 and ord(c) != 127 else "?" for c in str(value))


def entry_text(entry, detail=False):
    connection = entry.get("connection") or {}
    snapshot = entry.get("snapshot") or {}
    fleet = snapshot.get("fleet") or {}
    devices = fleet.get("devices")
    count = str(len(devices)) if isinstance(devices, list) else "未知"
    queue = snapshot.get("queue") or {}
    lock = fleet.get("lock") or {}
    lease = queue.get("lease") or lock.get("lease") or {}
    lease_status = lease.get("status") or "unknown"
    print(f"{clean(entry.get('name', '?'))}  {clean(connection.get('status', 'unverified'))}"
          f"  revision={entry.get('revision', '?')}  node_id={clean(entry.get('node_id') or '未绑定')}"
          f"  devices={count}  lease={clean(lease_status)}"
          f"  stale={entry.get('stale', True)}  refreshing={entry.get('refreshing', False)}")
    print(f"  {clean(entry.get('url', ''))}  last_seen={connection.get('last_seen_at')}"
          f"  attempted={connection.get('attempted_at')}  snapshot_at={snapshot.get('at')}")
    if not detail:
        return
    identity = snapshot.get("identity") or {}
    host, service, storage = (identity.get(key) or {} for key in ("host", "service", "storage"))
    print(f"  host={clean(host.get('hostname', '未知'))}  platform={clean(host.get('platform', '未知'))}"
          f"  service_revision={clean(service.get('revision') or '未知')}  dirty={service.get('dirty')}")
    print(f"  data_root={clean(storage.get('data_root', '未知'))}"
          f"  jobs_root={clean(storage.get('jobs_root', '未知'))}")
    print(f"  queue active={queue.get('active_count', '未知')} queued={queue.get('queued_count', '未知')}"
          f" blocked={queue.get('blocked_count', '未知')}  max_running={queue.get('max_running', '未知')}")
    print(f"  lease={clean(lease_status)}  bridge_held={lease.get('bridge_held')}"
          f"  error={clean(lease.get('error') or '无')}  sampled_at={fleet.get('sampled_at')}")
    for record in lease.get("entries") or []:
        print(f"    {clean(record.get('label') or record.get('role') or 'lease')}"
              f"  state={clean(record.get('state', 'unknown'))}  job={clean(record.get('job_id') or '未知')}"
              f"  reason={clean(record.get('reason') or '未报告')}")


def main(hub, argv):
    as_json = "--json" in argv
    try:
        if argv and argv[0] in ("run", "migrate"):
            raise ClientError("unsupported_in_hub_mode", "共享目录模式尚不支持 run/migrate；不会回落本地注册表")
        args = parser().parse_args(argv)
        client = Client(hub)
        command = args.command
        if command in ("nodes", "status"):
            result = client.request("/nodes" + ("?refresh=1" if getattr(args, "refresh", False) else ""))
            if result.get("schema_version") != 1 or not isinstance(result.get("items"), list):
                raise ClientError("invalid_protocol", "hub 未返回目录协议 v1")
        elif command == "show":
            query = {}
            if args.refresh:
                query["refresh"] = 1
            if args.devices:
                query["devices"] = 1
            result = client.request("/nodes/" + name(args.name) +
                                    ("?" + urllib.parse.urlencode(query) if query else ""))
        elif command in ("register", "update"):
            label = name(args.name)
            body = changes(args)
            if command == "register":
                body["name"] = label
            else:
                if not body:
                    raise ClientError("invalid_arguments", "update 至少需要一个可变字段")
                body["revision"] = args.revision
            result = client.request("/nodes" + ("/" + label if command == "update" else ""), body)
        elif command in ("disable", "enable"):
            result = client.request("/nodes/" + name(args.name),
                                    {"revision": args.revision, "enabled": command == "enable"})
        else:
            result = import_nodes(client, args.file)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif command in ("nodes", "status"):
            identity = result.get("local") or {}
            print("local  node_id=" + clean(identity.get("node_id", "未知")))
            for entry in result["items"]:
                entry_text(entry)
            if not result["items"]:
                print("尚无远端登记")
        elif command == "import":
            for item in result["items"]:
                print(f"[{item['index']}] {item['name'] or '无效登记'}: " +
                      ("created" if item["status"] == "created" else item["error"]["message"]))
            print(f"created={result['created']} failed={result['failed']}；源文件保留")
        else:
            entry_text(result, detail=command == "show")
        return 2 if command == "import" and result["failed"] else 0
    except ClientError as error:
        if as_json:
            print(json.dumps({"error": error.public()}, ensure_ascii=False))
        else:
            print("pf-fleet: " + str(error), file=sys.stderr)
        return 2
