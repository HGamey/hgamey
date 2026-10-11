#!/usr/bin/env python3
"""Write an explicit five-stage fake plan. Does not run it or train any model."""
import hashlib
import json
from pathlib import Path
import sys

directory = Path(sys.argv[1]).resolve()
directory.mkdir(parents=True, exist_ok=True)
data = directory / "synthetic_data.json"
data.write_text('{"source":"synthetic","not_game_frames":true}\n')
driver = Path(__file__).with_name("fake_driver.py").resolve()
plan = {"schema": "game_opt_loop/model_pipeline_plan@1", "pipeline_id": "synthetic_demo",
        "mode": sys.argv[2] if len(sys.argv) > 2 else "expert",
        "candidate_description": "Synthetic protocol demo, not a trained model or real game experiment.",
        "model": {"framework": "synthetic", "runtime": "synthetic_no_device", "abi": "synthetic/json",
                  "quantization": {"method": "synthetic_no_conversion", "precision": "synthetic", "calibration_data_sha256": None}},
        "data": [{"name": "data", "role": "data", "path": str(data), "sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                  "format": "synthetic/json", "source": "synthetic"}], "stages": []}
for stage, role in zip(["design", "train", "quantize", "deploy", "evaluate"],
                       ["architecture", "weights", "quantized_model", "deployment_receipt", "evaluation_report"]):
    plan["stages"].append({"stage": stage, "argv": [str(Path(sys.executable).resolve()), str(driver), "ok"],
                           "cwd": str(directory), "driver_artifacts": [str(driver)],
                           "outputs": [{"name": stage + ".json", "role": role, "format": "synthetic/json", "source": "synthetic"}],
                           "budget": {"wall_ms": 10000, "output_bytes": 1048576, "steps": 1, "tokens": 0, "device_ms": 0}})
target = directory / "plan.json"
target.write_text(json.dumps(plan, indent=2) + "\n")
print(target)
