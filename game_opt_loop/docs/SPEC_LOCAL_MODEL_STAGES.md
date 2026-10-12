# Actual local model stages (design/train/quantize)

Refs HGamey/.github issue16 and epic11. Base is GOL `6b7d45cc4a2c2e68b3b7bd351eb0a97c9655f176`; this is a separate worktree/branch. The existing model-pipeline request/receipt, hashes, expert decisions, process-group budget, UNKNOWN and no-retry rules remain authoritative. No PF/device/API call or Android-deployment implementation is added.

## Delivered finite scope

An explicitly configured local Python driver implements Design → Train → Quantize with TensorFlow 2.20.0. Design validates/fixes a small 3/2 SR architecture and emits its concrete configuration. Train executes real GradientTape/optimizer steps, preserves actual loss/gradient/initial/final weight evidence, saves valid H5 weights and reloads them. Quantize loads the actual completed training weights and converts actual fp32 and float16-weight TFLite artifacts, then parses their schema/shape/types/operators and checks host CPU inference on a frozen bounded sample. Host inference is explicitly not Android/GPU evaluation. Float16 weights do not establish a float16 Android kernel/delegate or speed/power improvement.

Deploy and Evaluate return explicit failed/BLOCKED receipts because Android integration is not implemented. The generic five-stage pipeline may retain three completed stages then failed deployment; it must never appear completed or promote. Its `model_semantics_verified`, game-proof and promotion flags remain false. The driver can separately report exactly the locally checked format/backprop/conversion facts.

## Data contract

Two explicitly different inputs are supported:

1. Future native data: complete source-pinned refbench capture from bfffeb2, exact completion/manifest/run-output/rules/current-source hashes and original per-frame native_lr/native_hr raw hashes. Candidate output is not HR truth. Require 1280×720 and independent1920×1080 tight opaque RGBA8, exact 3/2 spatial ratio, same registered frame/phase/command-recording identity, no missing/reindexed frames or missing marker. Source remains unknown/procedural-whitebox acquisition declaration, never authenticated real game. This route waits for B; missing native input must BLOCK, not manufacture training data.
2. Explicit synthetic mechanisms: a separately named, bounded deterministic corpus used only to test actual math/backprop/export. Synthetic fields stay synthetic through every output. No CPU-generated pair is labelled native GPU or commercial game data.

The training specification freezes data-file SHA, split-by-frame IDs, color convention, sampling seed/grid, architecture, learning rate, batch, optimizer steps, wall/output budgets before the first sample. Train and heldout frames are disjoint; patch-level random splitting is forbidden. Four consecutive real frames provide only a tiny correlated procedural example, not generalization evidence. Data changes, duplicates/split overlap, undeclared files, truncated raw, alpha/color/schema mismatch or unknown format reject with a negative receipt. No quality-based deletion, HR-derived fake LR, undocumented resize or nonexistent historical weights.

Native pixel colors are linear shader UNORM8; training consumes RGB float32/255 under that explicit linear convention, without pretending to be sRGB. Fixed patches use even integer LR origins and sizes; HR origin/size are exactly 3/2 of them, with matching normalized footprint. The old sr_loop integer scale2/pixel-shuffle and screenshot-downsampling dataset routines are reference ideas only, not reused as this native pairing contract.

## Model and budget contract

Proposed initial mechanism model: bilinear resize to exact 3/2 output plus a tiny bounded residual Conv2D network. No arbitrary Python architecture/code execution from a JSON genome, no external downloads/weights. Fixed CPU-only seed, one interop/two intraop threads and bounded patch input/output sizes are recorded; CPU numerical reproducibility is scoped to the pinned runtime, not universal bit equality.

Initial tests use a small static LR patch (even dimensions) and matched HR patch, fixed≤8 training steps/batch1, positive bounded LR,≤8 channels/two Conv2D layers and≤10000 parameters. Each stage uses the existing host wall/output process-group limits and the registered #16 budget below. The driver enforces its actual loop count and reports steps/tokens0/device_ms0; generic pipeline still labels usage driver-declared rather than a trusted device/API meter. Actual initial/final weight SHA and nonzero weight-delta prove a local update occurred, not useful training or lower loss/real-game quality.

## Wire / lifecycle

- Read `GOL_PIPELINE_REQUEST`, `GOL_PIPELINE_RECEIPT`, `GOL_PIPELINE_STAGE_DIR` only. Request schema/stage/model/budgets, exact input snapshot bytes/hashes, output names/roles/formats/source and request digest match existing Rust canonical sorted-JSON semantics.
- Local stage input names/roles/formats and each stage's exact output set/roles/formats are finite registered contracts; a rehashed user declaration cannot relabel generated H5/TFLite or architecture as a different format or Android artifact.
- Register Python executable and every script/config as driver artifacts. Runtime package/dependency versions/import identity are recorded, but this is trusted local execution, not a network sandbox or authenticated build closure.
- No shell concatenation, network/device command or package installer in drivers. Successful outputs are create-only in the current stage, synced, parsed/rehashed, then receipt atomically published last. Missing/invalid upstream weights or data never silently initialize a new training run.
- Failed receipt has an explicit error code and no valid model outputs; partial files remain negative evidence. No automatic resume of in-flight side effects; only generic explicit reconcile can handle unknown execution.

## Required acceptance

Real existing GOL CLI → these actual external stage drivers: source-tag propagation, real GradientTape/weight change/H5 reload, actual fp32/fp16 conversions and TFLite interpreter shape/finite-output checks, separate heldout IDs and no leakage. Negative cases bind tampered input/weights, missing completion, incorrect3/2 dimensions, mixed color/source, split overlap, missing/nonfinite data, absent outputs/receipt, budget/UNKNOWN and unsupported Deploy/Evaluate. Synthetic positives are actual training/quantization mechanism checks only. Native-data training/Android run and gain remain NOT_RUN until separately performed.

Each actual TFLite invocation records the identity of the selected patch from the actual heldout array, including frame/host-frame ID, LR/HR original raw SHA, crop origin/shape and little-endian float32 input SHA. The declared heldout list is a split membership contract, not the physical sample order; a reversed multi-frame heldout declaration must not mislabel the first invoked source frame. Acceptance includes a real CPU training/conversion/invocation with heldout IDs `[3,2]` and actual first patch from frame2.

No Rust production change is expected for the external driver; full existing Rust tests and strict Clippy will still be required before a source PR, alongside actual local runtime tests. New Python source/tests live under `tools/model_stages/`; examples/plan builder should produce explicitly frozen user-configured argv. Large weights/TFLite/data stay in tasks, never Git.

## Registered budget

Root budget `hgamey/local_sr_stage_budget@1` SHA `afa104d480b49aa5e35488021996ecd07b92fbf95c7276d722a4a6a133ef6440` fixes Design30s/Train180s≤128steps/batch≤2/Quantize120s/output≤4MiB/raw≤384MiB, CPU-only/API0/device0. Geometry patches are LR16×16→HR24×24. Initial actual synthetic smoke remains ≤8steps/twoConv≤8filters/≤10000parameters. Root subsequently verified TF2.20.0 CPU import/matmul in101.21s after preserving the original60s timeout. Installation/import is setup evidence, not training or Android acceptance; these drivers must pass their own actual stage execution under the unchanged30/180/120s limits.

Official API scope references: [Keras weights saving/loading](https://keras.io/api/models/model_saving_apis/weights_saving_and_loading/) and [TensorFlow post-training quantization](https://github.com/tensorflow/tensorflow/blob/v2.20.0/tensorflow/lite/g3doc/performance/post_training_quantization.md). Float16 weights can be dequantized to float32 on CPU; they do not prove Android GPU float16 execution.

## Actual runtime wiring

A venv `bin/python` is a symlink and the existing GOL driver-file gate rejects it before execution. The implementation does not relax that gate: planning resolves the known interpreter to a regular executable and explicitly freezes a runtime-config containing the requested venv site-packages, actual interpreter SHA and pyvenv.cfg SHA. Driver scripts/data and the configured site path reject symlink ancestors; current dependency modules/dynamic libraries are a trusted local installation rather than a complete authenticated code snapshot. Only runtime stages add that frozen site-packages path before importing the fixed TensorFlow version. Design does not import TF. Wall times include startup/import as observed by GOL.

The copied `refbench_native_pairs_v1.py` is the unchanged bfffeb2 strict validator (SHA71dc9856...), invoked from its bounded byte snapshot after checking this identity. The stage adapter binds its regular_bytes/parse entrypoints to the stricter same-fd/ancestor-no-follow/finite-number helpers in protocol.py; the pinned completion/pairing algorithms are unchanged. Its completion/hash/claim scope is reused; driver checks native declaration,1280/1920 geometry, opaque LR/HR and original SHA, never substitutes candidate for HR. RGB alpha removal and integer crop/float normalization are explicit derivations. Synthetic fixtures have their own RGB format and source, never pass the native branch.

Keras runtime configuration cache is placed under the owned pipeline parent (`runtime-cache-<stage>`), not inside the stage's flat regular-only output directory. The initial trial putting this cache inside a stage correctly produced UNKNOWN from the existing stage inventory gate; that negative session is preserved, never repaired/resumed as success. Cache configuration is local dependency state, not a declared model output or a sandbox/disk-quota claim. Large data/weights/models remain explicitly bounded and in tasks.

## Counted exclusive runtime cache

Cache creation is create-only under the owned pipeline parent, mode0700, with immutable owner binding pipeline/plan/stage/request digest and exact path. Only owner.json and keras.json may exist, each bounded64KiB and regular/no-symlink; unknown files, links, owner changes or cache collision reject. At driver wall/cost checkpoints, every current regular stage file plus these side-cache bytes is counted against the original4MiB budget. Actual cache path/ownerSHA/file count/bytes/SHA and combined stage cost enter training/quantization evidence. Moving the cache outside the flat output plane is compatibility, not permission to hide its bytes or to modify user HOME/global Keras configuration. This is bounded observed file inventory, not an OS filesystem quota or complete transient-library-temp accounting.
