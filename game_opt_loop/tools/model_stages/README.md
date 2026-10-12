# Actual local CPU model stages

These external drivers implement Design, Train and Quantize through the existing
`game_opt_loop model-pipeline` request/receipt contract. They execute real
TensorFlow 2.20.0 GradientTape updates, H5 save/reload and fp32/float16-weight
TFLite conversion and CPU parsing/invocation. Deploy and Evaluate deliberately
return failed receipts: Android deployment, GPU inference, quality improvement
and game optimization remain untested.

## Explicit synthetic mechanism run

Use an already installed Python 3.12 venv containing TensorFlow 2.20.0, NumPy and
h5py. The planner does not install packages or execute stages. It resolves the
venv's interpreter to a regular executable, freezes its hash and a runtime
configuration, and records the venv site-packages and pyvenv.cfg hash. This is
trusted local dependency execution, not a fully authenticated dependency closure.

Run from this repository with fresh absolute output paths whose ancestors have
no symlinks. On macOS, use a canonical `/private/...` path instead of a `/var`
alias where applicable.

```sh
python3 tools/model_stages/make_plan.py \
  --out /absolute/owned-task/synthetic-smoke-001 \
  --python /absolute/registered-venv/bin/python --synthetic

/absolute/frozen/game_opt_loop model-pipeline start \
  --plan /absolute/owned-task/synthetic-smoke-001/plan.json \
  --out /absolute/owned-task/synthetic-smoke-001/pipeline
```

The intended complete command result is **exit 1**, with stage states
`completed, completed, completed, failed, pending`: deployment is BLOCKED.
Do not change this to a fake successful deployment. The three completed stages
are local math/format evidence on explicitly synthetic data; their loss curve
does not establish native-data quality or game gain. A failed deploy resume must
not retrain. An in-flight/UNKNOWN session requires the generic explicit recovery
contract and is never automatically rerun by these drivers.

The initial mechanism plan freezes four synthetic frame IDs, train IDs `[0,1,2]`
and heldout ID `[3]`, seed 23, batch 1, eight steps and a 443-parameter residual
network. LR 16×16 and HR 24×24 patches use the exact 3/2 geometry.

## Native-data plan, pending actual capture

Supply `--native-dataset /absolute/frozen/dataset.json` instead of `--synthetic`.
The descriptor must have this complete field set (replace paths and hashes with
the actual completed capture before planning):

```json
{
  "schema": "game_opt_loop/paired_training_dataset@1",
  "source": "unknown",
  "kind": "native_refbench",
  "seed": 23,
  "steps": 8,
  "batch": 1,
  "learning_rate": 0.001,
  "train_frame_ids": [0, 1, 2],
  "heldout_frame_ids": [3],
  "capture": {
    "manifest": {"path": "/absolute/run/manifest.json", "sha256": "ACTUAL_SHA256"},
    "rules": {"path": "/absolute/frozen/rules.json", "sha256": "ACTUAL_SHA256"},
    "candidate_spirv": {"path": "/absolute/frozen/candidate.spv", "sha256": "ACTUAL_SHA256"}
  },
  "synthetic_frames": [],
  "patches_per_frame": 1
}
```

The pinned bfffeb2 refbench consumer requires completion and current raw hashes.
The driver separately requires declared procedural native capture, at least four
frames, opaque native LR 1280×720 and independent native HR 1920×1080. Candidate
output is never HR truth. Color is linear shader UNORM8 normalized to float32;
even LR patch origins map to integer HR origins. No missing-data synthetic
fallback, HR-downsampled LR, frame overlap or silent color conversion is allowed.
Capture provenance remains unknown; ledger identity does not authenticate GPU
execution or real-game source. Native training is NOT_RUN until those actual
files exist and a new frozen plan is executed.

## Budgets and outputs

The registered finite contract is Design 30s, Train 180s/≤128 steps/batch≤2,
Quantize 120s, raw data ≤384MiB and stage output ≤4MiB. Startup/import time counts;
cold TF import may exhaust a stage and must not be hidden or silently retried.
The planner uses eight steps. The generic host enforces its process-group wall
and flat output limits; token/device usage is declared zero, not an authenticated
external meter or network sandbox.

Train writes `trained.weights.h5` and `train-evidence.json`; Quantize writes
`model-fp32.tflite`, `model-fp16.tflite` and `quantize-evidence.json`. Evidence
records actual gradients/loss/weight hashes, disjoint frame IDs, CPU runtime,
TFLite tensor shapes/types/operators and false game/promotion flags. Float16
stored weights can dequantize to float32 on CPU and do not prove Android GPU
float16 execution or speed.

Keras config is in a create-only mode0700 `pipeline/runtime-cache-<stage>` with
an exact request-bound `owner.json`. Only owner.json and keras.json are allowed.
Their actual hashes/count/bytes are recorded, and their bytes plus current flat
stage files and the final receipt count against the same frozen 4MiB budget.
Unknown cache files, links, owner changes and reuse reject. This does not modify
user HOME/global Keras settings and is an observed bounded inventory, not an OS
disk quota or complete accounting of transient dependency files.

## Verification

Standard-library contracts (no TF import):

```sh
python3 -m unittest discover -s tests/model_stages -p test_contracts.py -v
```

Actual runtime tests are opt-in and create a new owned artifact directory:

```sh
GOL_MODEL_TEST_PYTHON=/absolute/registered-venv/bin/python \
GOL_MODEL_TEST_GOL=/absolute/frozen/game_opt_loop \
GOL_MODEL_TEST_ARTIFACT_DIR=/absolute/owned-task/runtime-tests-001 \
python3 -m unittest discover -s tests/model_stages -p test_runtime.py -v
```

Absent explicit runtime/tool environment, the runtime suite reports NOT_RUN.
Its successful synthetic case still performs real backprop/H5/TFLite work; its
negative cases mutate actual H5 bytes/NaN weights and frozen data contracts.
No test invokes a device, paid model API or Android deployment.
