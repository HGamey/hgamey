"""Center-side signing of approval decisions (ed25519).

phonefarm (src/approval.rs `Signed`) only accepts a decision whose signature verifies against the
public key it was given at launch (PF_APPROVAL_PUBKEY). The private key is generated when the node
service starts and lives only in this process's memory: job processes run as the same user, so a
key file would be readable by them, but they cannot read the parent's memory. A restart makes a new
key; approvals still pending in jobs started before it can no longer be signed for and time out
(deny) inside phonefarm, which is the safe direction.

What signing does not cover. While jobs run as the same OS user as this service, a job can read the
dashboard steer password or an admin token from disk and ask for a genuine signature through the normal
routes, or edit this service's files and wait for a restart; a job that brings its own executable never
goes through phonefarm at all. The fix for those is running jobs under their own OS user (the contained
execution policy, which also receives the approval environment).
"""

import ctypes
import hashlib
import json
import os
import sys

MESSAGE = "phonefarm-approval@2\n{job}\n{id}\n{decision}\n{content}"
# 签不了名时下发给作业的「公钥」: phonefarm 认不出就什么决定都不认(失败关闭), 不会退回不签名的旧模式
UNAVAILABLE = "unavailable"


def message(job, approval_id, decision, content):
    """Exactly what phonefarm verifies (`Signed::message`). `content` = content_hash of what the person was shown."""
    return MESSAGE.format(job=job, id=approval_id, decision=decision, content=content).encode()


def content_hash(request_bytes, screenshot_bytes):
    """sha256(request file bytes + NUL + screenshot bytes), hex — phonefarm `content_hash`. phonefarm keeps
    its own copy in memory, so a request or screenshot rewritten after it was written no longer verifies."""
    return hashlib.sha256(request_bytes + b"\0" + screenshot_bytes).hexdigest()


class Signer:
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self._key = Ed25519PrivateKey.generate()
        self._pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def public_hex(self):
        return self._pub.hex()

    def sign(self, job, approval_id, decision, content):
        return self._key.sign(message(job, approval_id, decision, content)).hex()

    def valid(self, job, approval_id, decision, content, sig_hex):
        from cryptography.exceptions import InvalidSignature
        try:
            self._key.public_key().verify(bytes.fromhex(sig_hex or ""), message(job, approval_id, decision, content))
            return True
        except (InvalidSignature, ValueError):
            return False


def _not_dumpable():
    """Linux: same-user processes may not ptrace us or read /proc/<pid>/mem (the key lives in memory)."""
    if not sys.platform.startswith("linux"):
        return True
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        return False


_POLICY = None


def make(log=print):
    """Signer, or None when this node cannot sign. Callers must then fail closed (see UNAVAILABLE)."""
    global _POLICY
    _POLICY = _read_policy()  # 启动时读一次: 之后作业改文件不影响本次运行
    try:
        signer = Signer()
    except ImportError:
        log("⚠ python cryptography 不可用: 审批一律拒绝(不会退回不签名), 请安装 python3-cryptography")
        return None
    if not _not_dumpable():
        log("⚠ 设不了 PR_SET_DUMPABLE=0: 同用户进程可能读到审批签名私钥")
    return signer


def policy_file():
    return os.environ.get("HG_APPROVAL_POLICY_FILE") or os.path.expanduser("~/.config/hgamey/approval-policy.json")


def center_policy_json():
    """The node's approval policy as read at service start, handed to each job; the job's own [approval]
    can only tighten it. Missing or broken file = phonefarm defaults (ask for everything sensitive)."""
    return _POLICY if _POLICY is not None else _read_policy()


def _read_policy():
    try:
        with open(policy_file(), encoding="utf-8") as f:
            value = json.load(f)
        return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)
    except (OSError, ValueError):
        return "{}"
