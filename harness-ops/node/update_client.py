"""Pinned read-only installation maintenance status; no update or retry."""
import http.client
import json
import sys
import urllib.error
import urllib.request

from target_client import NoRedirect, TargetError


def main(target, args):
    if args not in ([], ["--json"]):
        raise SystemExit("用法: pf-node --url URL --node-id ID update-status [--json]")
    as_json = args == ["--json"]
    try:
        if not target.expected_node_id:
            raise TargetError("target_required", "update-status requires an explicit expected node identity")
        identity = target.identity()
        if "node.updates-v1" not in identity.get("capabilities", []):
            raise TargetError("unsupported_capability", "node does not advertise node.updates-v1")
        with urllib.request.build_opener(NoRedirect).open(target.request("GET", "/node-updates"), timeout=10) as response:
            raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise TargetError("invalid_response", "maintenance response exceeds size limit")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema") != "hgamey/node-updates@1"
                or value.get("scope") != "host-only" or value.get("node_id") != identity["node_id"]
                or not isinstance(value.get("updater"), dict) or not isinstance(value.get("maintenance"), dict)
                or not isinstance(value.get("layout"), dict) or not isinstance(value.get("limitations"), list)):
            raise TargetError("invalid_response", "maintenance report does not match the pinned node or contract")
        updater, maintenance = value["updater"], value["maintenance"]
        if (type(updater.get("available")) is not bool
                or maintenance.get("state") not in ("clear", "blocked", "unknown")
                or maintenance.get("all_participants_verified") is not False):
            raise TargetError("invalid_response", "maintenance report has invalid status or scope")
        if as_json:
            print(json.dumps(value, ensure_ascii=False))
        else:
            print("节点 " + value["node_id"] + " · 宿主安装维护状态")
            print("更新入口: " + ("可用" if updater["available"] else "不可用") + " · " + str(updater.get("reason") or ""))
            print("维护状态: " + maintenance["state"] + " · " + str(maintenance.get("reason") or ""))
            if maintenance.get("run_id"):
                print("原更新: " + maintenance["run_id"])
            for name, path in value["layout"].items():
                print(name + ": " + str(path))
            for limitation in value["limitations"]:
                print("范围: " + str(limitation))
        return
    except (TargetError, urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, TargetError):
            code, message = error.code, str(error)
        elif isinstance(error, urllib.error.HTTPError):
            code, message = "http_error", f"maintenance request failed with HTTP {error.code}"
        else:
            code, message = "maintenance_unavailable", "cannot read a valid maintenance report; check connection and node configuration"
        if as_json:
            print(json.dumps({"error": {"code": code, "message": message},
                              "http_status": error.code if isinstance(error, urllib.error.HTTPError) else None}, ensure_ascii=False))
        else:
            print("pf-node update-status: " + code + ": " + message, file=sys.stderr)
        raise SystemExit(1)
