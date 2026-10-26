"""One bounded read, no retries, repair execution or device qualification."""
import json
import http.client
import sys
import urllib.error
import urllib.request

from target_client import TargetError, NoRedirect


def main(target, args):
    if args not in ([], ["--json"]):
        raise SystemExit("用法: pf-node --url URL --node-id ID doctor [--json]")
    as_json = args == ["--json"]
    try:
        if not target.expected_node_id:
            raise TargetError("target_required", "doctor requires --node-id or a configured expected node identity")
        identity = target.identity()
        if "node.doctor-v1" not in identity.get("capabilities", []):
            raise TargetError("unsupported_capability", "node does not advertise node.doctor-v1")
        with urllib.request.build_opener(NoRedirect).open(target.request("GET", "/doctor"), timeout=10) as response:
            raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise TargetError("invalid_response", "doctor response exceeds size limit")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != "hgamey/node-doctor@1" or value.get("scope") != "host-only"
                or value.get("node_id") != identity["node_id"]
                or not isinstance(value.get("checks"), list) or not isinstance(value.get("summary"), dict)):
            raise TargetError("invalid_response", "doctor response does not match the pinned node or contract")
        summary = value["summary"]
        states = ("ok", "warning", "error", "unknown", "not_applicable")
        for key in states:
            if type(summary.get(key)) is not int or summary[key] < 0:
                raise TargetError("invalid_response", "doctor summary has invalid counts")
        if not isinstance(value.get("limitations"), list) or any(
                not isinstance(item, dict) or item.get("status") not in states
                or not isinstance(item.get("remediation"), list) for item in value["checks"]):
            raise TargetError("invalid_response", "doctor checks are incomplete")
        if set(summary) != set(states) or any(summary[key] != sum(item["status"] == key for item in value["checks"]) for key in states):
            raise TargetError("invalid_response", "doctor summary disagrees with its checks")
        if as_json:
            print(json.dumps(value, ensure_ascii=False))
        else:
            print("节点 " + value["node_id"] + " · 仅宿主诊断")
            for item in value["checks"]:
                print(f"[{item['status']}] {item['title']}: {item['message']}")
                for action in item["remediation"]:
                    print("  建议: " + action["text"])
            for limitation in value["limitations"]:
                print("范围: " + limitation)
        raise SystemExit(2 if summary["warning"] or summary["error"] else 0)
    except (TargetError, urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, TargetError):
            code, message = error.code, str(error)
        elif isinstance(error, urllib.error.HTTPError):
            code, message = "http_error", f"doctor request failed with HTTP {error.code}"
        else:
            code, message = "doctor_unavailable", "cannot read a valid doctor report; check connection and node configuration"
        if as_json:
            print(json.dumps({"error": {"code": code, "message": message},
                              "http_status": error.code if isinstance(error, urllib.error.HTTPError) else None}, ensure_ascii=False))
        else:
            print("pf-node doctor: " + code + ": " + message, file=sys.stderr)
        raise SystemExit(1)
