#!/usr/bin/env python3
"""Relay phonefarm approvals to Telegram through an existing message gateway (brief) and back.

Runs on a host that can reach both the center (admin token) and the gateway. It never
decides on its own: it only forwards what a person types.

    approval_bridge.py --center https://gameharness.liyao.space --token-stdin \
        --gateway https://brief.example --gateway-key-file ~/.brief-key --chat tg:me

Each pending approval is sent once (screenshot + reason + a 4-character code).
A reply in the same chat of the form "允许 c75f" / "拒绝 c75f" (also: allow/deny/y/n)
is posted to POST /jobs/<job>/approvals/<id> with X-Approval-Via: telegram.
Messages the bridge sent itself are never parsed as replies.
"""

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request

ALLOW = {"允许", "同意", "批准", "allow", "approve", "yes", "y", "ok"}
DENY = {"拒绝", "不允许", "否", "deny", "reject", "no", "n"}
REPLY = re.compile(r"^\s*(\S+?)\s*[:：]?\s*([0-9a-f]{4})\s*$", re.I)
UA = "hgamey-approval-bridge/1"


def http(method, url, headers=None, body=None, timeout=30):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    h = {"User-Agent": UA, **(headers or {})}
    if body is not None and not isinstance(body, bytes):
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def code_of(approval_id):
    """Short reply code: the random suffix phonefarm puts at the end of every id."""
    return approval_id.rsplit("-", 1)[-1][-4:].lower()


def parse_reply(text):
    """'允许 c75f' -> ('allow', 'c75f'); anything else -> None."""
    m = REPLY.match(text or "")
    if not m or len(text) > 40:
        return None
    verb = m.group(1).lower()
    if verb in ALLOW:
        return "allow", m.group(2).lower()
    if verb in DENY:
        return "deny", m.group(2).lower()
    return None


def caption(item):
    code = code_of(item["id"])
    lines = [
        f"🛑 phonefarm 待审批 · {item.get('category_zh', item.get('category'))}" + ("（必须人工确认）" if item.get("critical") else ""),
        f"动作：{item.get('action', '')}",
        f"理由：{item.get('reason', '')}",
        f"目标：{item.get('goal', '')}"[:300],
        f"设备 {item.get('serial') or '?'} · {item.get('pkg') or '?'} · 作业 {item.get('job_id')}",
        f"剩余约 {max(0, int(float(item.get('expires_at') or 0) - time.time()))} 秒，超时按拒绝。",
        f"回复「允许 {code}」或「拒绝 {code}」",
    ]
    return "\n".join(lines)


class Bridge:
    def __init__(self, center, token, gateway, gateway_key, chat, log=print):
        self.center, self.token = center.rstrip("/"), token
        self.gateway, self.gkey, self.chat = gateway.rstrip("/"), gateway_key, chat
        self.log = log
        self.sent = {}          # approval id -> item
        self.own_messages = set()
        self.last_msg_id = None

    # center
    def _c(self, method, path, body=None, raw=False):
        status, data = http(method, self.center + path, {"Authorization": f"Bearer {self.token}",
                                                          "X-Approval-Via": "telegram"}, body)
        if raw:
            return status, data
        try:
            return status, json.loads(data or b"{}")
        except ValueError:
            return status, {}

    # gateway
    def _g(self, method, path, body=None):
        status, data = http(method, self.gateway + path, {"Authorization": f"Bearer {self.gkey}"}, body, timeout=60)
        try:
            return status, json.loads(data or b"null")
        except ValueError:
            return status, None

    def notify_pending(self):
        status, body = self._c("GET", "/approvals")
        if status != 200:
            self.log(f"center /approvals -> {status}")
            return
        for item in body.get("items", []):
            if item["id"] in self.sent:
                continue
            text = caption(item)
            shot = None
            if item.get("has_screenshot"):
                s, data = self._c("GET", f"/jobs/{item['job_id']}/approvals/{item['id']}/screenshot", raw=True)
                shot = data if s == 200 else None
            if shot:
                s, r = self._g("POST", "/api/send_file", {"chat": self.chat, "name": f"{item['id']}.jpg",
                                                          "data": base64.b64encode(shot).decode(), "text": text[:1024]})
            else:
                s, r = self._g("POST", "/api/send", {"chat": self.chat, "text": text})
            if s == 200 and isinstance(r, dict) and r.get("status") == "sent":
                self.sent[item["id"]] = item
                if r.get("message_id") is not None:
                    self.own_messages.add(r["message_id"])
                self.log(f"sent {item['id']} ({code_of(item['id'])})")
            else:
                self.log(f"gateway send failed {s}: {r}")

    def read_replies(self):
        s, msgs = self._g("GET", f"/api/messages?chat={self.chat}&limit=30")
        if s != 200 or not isinstance(msgs, list):
            return
        if self.last_msg_id is None:
            # first read: only messages newer than the bridge start count
            self.last_msg_id = max([m.get("id", 0) for m in msgs] or [0])
            return
        for m in msgs:
            mid = m.get("id", 0)
            if mid <= self.last_msg_id:
                continue
            self.last_msg_id = max(self.last_msg_id, mid)
            if m.get("message_id") in self.own_messages or mid in self.own_messages:
                continue
            parsed = parse_reply(m.get("text", ""))
            if not parsed:
                continue
            decision, code = parsed
            matches = [i for i in self.sent.values() if code_of(i["id"]) == code]
            if len(matches) != 1:
                self._g("POST", "/api/send", {"chat": self.chat, "text": f"没找到唯一对应 {code} 的待审批（{len(matches)} 条）"})
                continue
            item = matches[0]
            st, r = self._c("POST", f"/jobs/{item['job_id']}/approvals/{item['id']}", {"decision": decision, "note": "telegram"})
            done = "已允许" if decision == "allow" else "已拒绝"
            msg = f"{done}：{item.get('action')}" if st == 200 else f"提交失败（{st}）：{r.get('error') or r}"
            self._g("POST", "/api/send", {"chat": self.chat, "text": msg})
            self.log(f"{code} {decision} -> {st}")
            if st in (200, 409):
                self.sent.pop(item["id"], None)

    def forget_expired(self):
        now = time.time()
        for k in [k for k, v in self.sent.items() if float(v.get("expires_at") or 0) + 30 < now]:
            self.sent.pop(k)

    def tick(self):
        self.notify_pending()
        self.read_replies()
        self.forget_expired()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--center", required=True)
    ap.add_argument("--token-file")
    ap.add_argument("--token-stdin", action="store_true", help="read the admin token from the first stdin line")
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--gateway-key-file", required=True)
    ap.add_argument("--chat", default="tg:me")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    token = sys.stdin.readline().strip() if a.token_stdin else open(a.token_file).read().strip()
    gkey = open(a.gateway_key_file).read().strip()
    b = Bridge(a.center, token, a.gateway, gkey, a.chat,
               log=lambda s: print(time.strftime("%H:%M:%S"), s, flush=True))
    while True:
        try:
            b.tick()
        except (OSError, ValueError) as e:  # network blips: keep going
            b.log(f"tick error: {e!r}")
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
