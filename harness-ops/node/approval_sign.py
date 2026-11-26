"""Center-side signing of approval decisions (ed25519).

phonefarm (src/approval.rs `Signed`) only accepts a decision whose signature verifies against the
public key it was given at launch (PF_APPROVAL_PUBKEY). The private key is generated when the node
service starts and lives only in this process's memory: job processes run as the same user, so a
key file would be readable by them, but they cannot read the parent's memory. A restart makes a new
key; approvals still pending in jobs started before it can no longer be signed for and time out
(deny) inside phonefarm, which is the safe direction.
"""

import json
import os

MESSAGE = "phonefarm-approval@1\n{job}\n{id}\n{decision}"


def message(job, approval_id, decision):
    """Exactly what phonefarm verifies (`Signed::message`)."""
    return MESSAGE.format(job=job, id=approval_id, decision=decision).encode()


class Signer:
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self._key = Ed25519PrivateKey.generate()
        self._pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def public_hex(self):
        return self._pub.hex()

    def sign(self, job, approval_id, decision):
        return self._key.sign(message(job, approval_id, decision)).hex()

    def valid(self, job, approval_id, decision, sig_hex):
        from cryptography.exceptions import InvalidSignature
        try:
            self._key.public_key().verify(bytes.fromhex(sig_hex or ""), message(job, approval_id, decision))
            return True
        except (InvalidSignature, ValueError):
            return False


def make(log=print):
    try:
        return Signer()
    except ImportError:
        log("⚠ python cryptography 不可用: 审批决定不签名(作业能伪造自己的审批), 请安装 python3-cryptography")
        return None


def policy_file():
    return os.environ.get("HG_APPROVAL_POLICY_FILE") or os.path.expanduser("~/.config/hgamey/approval-policy.json")


def center_policy_json():
    """The node's approval policy, handed to each job; the job's own [approval] can only tighten it.
    Missing file = phonefarm defaults (ask for everything sensitive). A broken file = the strictest
    reading phonefarm offers for unknown values, i.e. the defaults again."""
    try:
        with open(policy_file(), encoding="utf-8") as f:
            value = json.load(f)
        return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)
    except (OSError, ValueError):
        return "{}"
