"""One pinned status or frame request; no retry loop or alternate capture path."""
import argparse
import http.client
import json
from pathlib import Path
import urllib.error
from urllib.parse import urlencode

from device_scope import binding
from target_client import TargetError


def main(target, argv):
    parser = argparse.ArgumentParser(prog="pf-node live")
    parser.add_argument("command", choices=("status", "frame"))
    parser.add_argument("serial")
    parser.add_argument("--device-binding", metavar="FILE", help="original devices-resolve {node_id,binding} JSON; omit for global scope")
    parser.add_argument("--output", metavar="FILE", help="new JPEG file for one frame; never overwrite")
    parser.add_argument("--json", action="store_true", help="status and frame metadata are always JSON")
    args = parser.parse_args(argv)
    if (args.command == "frame") != bool(args.output):
        parser.error("frame requires --output FILE; status does not accept --output")
    try:
        if not target.expected_node_id:
            raise TargetError("target_required", "live requires --node-id or PF_NODE_ID")
        if args.command == "frame":
            target.require_write()  # Observation needs target enforcement, including with a read-only token.
            if Path(args.output).exists():
                raise ValueError("output already exists; choose a new file")
        declared = None
        if args.device_binding:
            with Path(args.device_binding).open("rb") as source:
                raw = source.read(8193)
            if len(raw) > 8192:
                raise ValueError("device binding file exceeds 8 KiB")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("node_id") != target.expected_node_id:
                raise ValueError("device binding file node_id does not match the pinned node")
            declared = binding(envelope.get("binding"))
            if declared["serial"] != args.serial:
                raise ValueError("serial must match the original device binding")
        query = {"serial": args.serial}
        if declared is not None:
            query["device_binding"] = json.dumps(declared, separators=(",", ":"))
        suffix = "?" + urlencode(query)

        def read_status():
            with target.opener.open(target.request("GET", "/live/status" + suffix), timeout=15) as response:
                raw = response.read((1 << 20) + 1)
            if len(raw) > 1 << 20:
                raise ValueError("live status exceeds size limit")
            return json.loads(raw)

        if args.command == "status":
            print(json.dumps(read_status(), ensure_ascii=False))
            return
        if declared is not None and read_status().get("policy", {}).get("device_binding_v1") is not True:
            raise ValueError("node does not support device-bound screen capture; no global fallback")
        result = {"node_id": target.expected_node_id, "serial": args.serial,
                  "requested_scope": "device" if declared else "global",
                  **({"device_binding": declared} if declared else {})}
        with target.opener.open(target.request("GET", "/live/frame" + suffix), timeout=20) as response:
            metadata = {key: response.headers.get(key) for key in ("X-Frame-Seq", "X-Frame-At", "X-Frame-Mode")}
            if response.status == 204:
                print(json.dumps({**result, "state": "no_frame", "saved_to": None, "bytes": 0,
                                  "frame_headers": metadata}, ensure_ascii=False))
                return
            if response.headers.get_content_type() != "image/jpeg":
                raise ValueError("frame response is not JPEG")
            raw = response.read((4 << 20) + 1)
            if not raw.startswith(b"\xff\xd8") or len(raw) > 4 << 20:
                raise ValueError("frame response is not JPEG or exceeds size limit")
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None and int(declared_size) != len(raw):
                raise ValueError("frame response length is incomplete")
        output = Path(args.output)
        with output.open("xb") as destination:
            destination.write(raw)
        print(json.dumps({**result, "state": "frame", "saved_to": str(output.absolute()),
                          "bytes": len(raw), "frame_headers": metadata}, ensure_ascii=False))
    except (TargetError, urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        if isinstance(error, TargetError):
            code, message = error.code, str(error)
        elif isinstance(error, urllib.error.HTTPError):
            code, message = "http_error", f"live request returned HTTP {error.code}"
            error.close()
        else:
            code, message = "live_unavailable", str(error)
        print(json.dumps({"error": {"code": code, "message": message}, "node_id": target.expected_node_id,
                          "serial": args.serial, "retried": False}, ensure_ascii=False))
        raise SystemExit(1)

