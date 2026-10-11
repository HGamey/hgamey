#!/usr/bin/env python3
"""External protocol test driver. Every output is synthetic; no training/device/API."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


request = json.loads(Path(os.environ["GOL_PIPELINE_REQUEST"]).read_text())
stage_dir = Path(os.environ["GOL_PIPELINE_STAGE_DIR"])
receipt_path = Path(os.environ["GOL_PIPELINE_RECEIPT"])
behavior = sys.argv[1] if len(sys.argv) > 1 else "ok"
print("synthetic fake driver", request["stage"], sys.argv[2:], flush=True)
# Count real process invocations independently from the framework's journal.
with (stage_dir.parent / "fake_calls.jsonl").open("a") as stream:
    stream.write(json.dumps({"stage": request["stage"], "argv": sys.argv[2:]}) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
if behavior == "nonzero":
    print("synthetic driver failure", file=sys.stderr, flush=True)
    sys.exit(9)
if behavior == "sleep":
    time.sleep(2)
if behavior == "oversize":
    Path(request["outputs"][0]["path"]).write_bytes(b"x" * (request["budget"]["output_bytes"] + 1))
    time.sleep(2)
if behavior == "background":
    child = os.fork()
    if child == 0:
        time.sleep(2)
        os._exit(0)

outputs = []
if behavior != "negative":
    for output in request["outputs"]:
        assert output["source"] == "synthetic", "fake output must be synthetic"
        value = {"source": "synthetic", "stage": request["stage"],
                 "not_a_trained_model": True,
                 "inputs": [{"path": a["path"], "sha256": a["sha256"]} for a in request["inputs"]]}
        data = json.dumps(value, sort_keys=True).encode()
        Path(output["path"]).write_bytes(data)
        outputs.append({"name": output["name"], "sha256": hashlib.sha256(data).hexdigest()})
if behavior == "missing":
    sys.exit(0)
receipt = {"schema": "game_opt_loop/model_stage_receipt@1", "pipeline_id": request["pipeline_id"],
           "plan_sha256": request["plan_sha256"], "stage": request["stage"],
           "request_sha256": digest(request),
           "status": "failed" if behavior == "negative" else "succeeded",
           "usage": {"steps": 1 if request["stage"] == "train" else 0, "tokens": 0, "device_ms": 0},
           "outputs": outputs, "note": "Synthetic protocol exercise; no model training, API, deployment or game measurement."}
if behavior == "usage":
    receipt["usage"]["tokens"] = request["budget"]["tokens"] + 1
if behavior == "wrong_binding":
    receipt["request_sha256"] = "0" * 64
if behavior == "wrong_hash":
    receipt["outputs"][0]["sha256"] = "0" * 64
if behavior == "mutate_input":
    Path(request["inputs"][0]["path"]).write_text("synthetic driver mutated frozen input")
tmp = receipt_path.with_suffix(".tmp")
with tmp.open("w") as stream:
    json.dump(receipt, stream)
    stream.flush()
    os.fsync(stream.fileno())
tmp.replace(receipt_path)
if behavior == "receipt_then_wait":
    (stage_dir / "ready.flag").write_text("synthetic driver alive; host can be killed for at-most-once test")
    time.sleep(0.5)
    (stage_dir / "driver_finished.flag").write_text("synthetic driver stopped")
