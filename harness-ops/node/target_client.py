"""Explicit connection facts and one immutable target binding for CLI requests."""

import http.client
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request


class TargetError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (isinstance(value, str) and bool(value)
                 and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
                 and parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        parsed.port
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise TargetError("invalid_url", "需显式提供无凭据、查询或片段的 HTTP(S) URL（--url / PF_NODE_URL）")
    return value.rstrip("/")


def node_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise TargetError("invalid_target", "node_id 须为 32 位小写十六进制（--node-id / PF_NODE_ID）")
    return value


def token(value):
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(c) < 32 or ord(c) >= 127 for c in value)):
        raise TargetError("invalid_credential", "凭据须为非空、无控制字符的 ASCII token")
    return value


def token_file(path):
    try:
        with open(path, encoding="utf-8") as source:
            value = source.read(16385)
        if len(value) > 16384:
            raise ValueError("size")
    except (OSError, UnicodeError, TypeError, ValueError):
        raise TargetError("credential_unavailable", "无法读取指定凭据文件；未查找其他凭据") from None
    return token(value.strip())


def credentials(explicit_file=None, *, environ=None):
    env = os.environ if environ is None else environ
    if explicit_file is not None:
        return token_file(explicit_file), "explicit_file"
    direct, filename = env.get("PF_NODE_TOKEN"), env.get("PF_NODE_TOKEN_FILE")
    if direct is not None and filename is not None:
        raise TargetError("credential_conflict", "PF_NODE_TOKEN 与 PF_NODE_TOKEN_FILE 同时存在；请只设置一项或显式 --token-file")
    if direct is not None:
        return token(direct), "environment_token"
    if filename is not None:
        return token_file(filename), "environment_file"
    raise TargetError("credential_unavailable", "认证需显式 --token-file、PF_NODE_TOKEN 或 PF_NODE_TOKEN_FILE")


class Target:
    def __init__(self, connection_url, credential, expected_node_id=None, *,
                 credential_source="explicit", identity_path="/node"):
        self.url = url(connection_url)
        self.token = token(credential)
        self.expected_node_id = node_id(expected_node_id) if expected_node_id is not None else None
        self.credential_source = credential_source
        self.identity_path = identity_path
        self.opener = urllib.request.build_opener(NoRedirect())
        self._identity = None
        self._announced = False

    @classmethod
    def resolve(cls, connection_url=None, expected_node_id=None, explicit_file=None, *,
                environ=None, identity_path="/node"):
        env = os.environ if environ is None else environ
        connection_url = url(connection_url if connection_url is not None else env.get("PF_NODE_URL"))
        expected_node_id = expected_node_id if expected_node_id is not None else env.get("PF_NODE_ID")
        if expected_node_id is not None:
            node_id(expected_node_id)
        credential, source = credentials(explicit_file, environ=env)
        return cls(connection_url, credential, expected_node_id, credential_source=source,
                   identity_path=identity_path)

    def headers(self, extra=None):
        headers = {"Accept": "application/json", "User-Agent": "phonefarm-target/1", **(extra or {})}
        headers["Authorization"] = "Bearer " + self.token
        if self.expected_node_id is not None:
            headers["X-Expected-Node-ID"] = self.expected_node_id
        return headers

    def identity(self, timeout=10):
        if self._identity is not None:
            return self._identity
        request = urllib.request.Request(self.url + self.identity_path, headers=self.headers(), method="GET")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read((1 << 20) + 1)
            if len(raw) > 1 << 20:
                raise ValueError("size")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("identity")
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if 300 <= status < 400:
                raise TargetError("redirect_refused", "节点身份请求返回重定向，已拒绝转发凭据") from None
            if status == 409:
                raise TargetError("target_mismatch", "节点拒绝原预期目标；保留原 URL/node_id，未写入") from None
            raise TargetError("identity_unavailable", f"无法确认节点身份（HTTP {status}）；未写入") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, UnicodeError):
            raise TargetError("identity_unavailable", "节点身份请求未确认；未写入") from None
        actual = node_id(value.get("node_id"))
        if self.expected_node_id is not None and actual != self.expected_node_id:
            raise TargetError("target_mismatch", "节点身份与原预期 node_id 不同；未写入或查询原记录")
        self._identity = value
        return value

    def require_write(self):
        if self.expected_node_id is None:
            raise TargetError("target_required", "写入需要 --node-id / PF_NODE_ID；先用 node 只读取得原节点 ID")
        identity = self.identity()
        capabilities = identity.get("capabilities")
        if not isinstance(capabilities, list) or "node.target-v1" not in capabilities:
            raise TargetError("target_unsupported", "节点缺少 node.target-v1；新客户端拒绝降级写入")
        if not self._announced:
            host, storage = identity.get("host"), identity.get("storage")
            facts = {"url": self.url, "node_id": self.expected_node_id,
                     "host": host.get("hostname") if isinstance(host, dict) else None,
                     "data_root": storage.get("data_root") if isinstance(storage, dict) else None,
                     "credential_source": self.credential_source}
            print("执行目标 " + json.dumps(facts, ensure_ascii=True), file=sys.stderr, flush=True)
            self._announced = True
        return identity

    def before_request(self, method, path):
        if method not in ("GET", "HEAD", "OPTIONS"):
            self.require_write()
        elif self.expected_node_id is not None and path != self.identity_path:
            self.identity()

    def request(self, method, path, data=None, headers=None):
        self.before_request(method, path)
        return urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers(headers))

    def command(self, program="pf-node"):
        flag = "--hub" if program == "pf-fleet" else "--url"
        args = [program, flag, self.url]
        if self.expected_node_id is not None:
            args += ["--node-id", self.expected_node_id]
        return shlex.join(args)
