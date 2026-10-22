# Explicit blackbox game replay case v2

Base: GOL `6b7d45cc4a2c2e68b3b7bd351eb0a97c9655f176`. Dependencies:
GOL PR37/38 and phonefarm queued-origin/native-reader plus fixed-tool bundle
integration (the reviewed `ba0fba4` contract). Refs HGamey/.github#17 and #19.

## Scope and compatibility

`case plan/run/inspect` selects a protocol by its explicit profile/manifest
schema. Existing `system_case_profile@1` and `system_case@1` continue through
the original `system_case.rs`, whose source bytes and implementation fingerprint
are unchanged. Unknown versions do not fall back. The new `@2` implementation
is isolated because the old manifest hashes that entire source file.

V2 is one deterministic sysparam proposal for a declared `game_replay` carrier,
with a valid Android package and exact Linux render comm. No particular game is
hard-coded. It requires explicit `goal=render_cadence`,
`temperature_profile_id=loaded_steady_v1`,
`render_attribution_profile_id=kgsl_queued_origin_spf_v1`, a concrete native
reader artifact, and `source_budget_calls=0`. These are receiver preset IDs,
not permission to edit numeric policy, infer owner identity or bypass raw gates.

The carrier has a declared application version, scene ID and
`reset_semantics=caller_replay_no_exact_scene_reset`. It is caller context, not
an APK/foreground/exact-scene attestation. An absent mandatory context, artifact
or receiver witness blocks. Package PID/epochs, queued origins, grouping and
baseline validity are independently reconstructed by phonefarm. Context-process
ownership, installed-kernel-source identity, continuous foreground, displayed
frames, quality and energy benefit remain unverified; no truth switch upgrades
them. This is the formal sampling entry, not algorithm hook/NN deployment.

## Frozen native artifact and tools

Before any execution, copy exactly the receiver artifact's five regular files:
`source.c`, `reader.elf`, `build.json`, `compiler.stdout`, `compiler.stderr`.
Reject extra/missing files, links/ancestors, special files and traversal. Source,
receipt/logs each <=128KiB, ELF <=1MiB. The source must equal the pinned PF reader
source; build schema, official NDK revision/target/fixed flags, exact source/ELF/
log hashes, successful compiler exit and absent host group are checked. The ELF
must be ELF64LE AArch64 PIE. Compiler identity is a local build declaration, not
independent authentication; no SDK or compiler is invoked. Freeze these actual
bytes beneath `native-reader/`, and put that directory into PF's actual
`render_reader_artifact` request. PF revalidates the same source/build contract.

Transport bytes are frozen at planning. Actual GOL/PF executable bytes are
privately copied by the existing run mechanism and hashed before the first
stage. Native receipt/source/tool bytes cannot be replaced through the caller
path after planning. Symlink or missing native artifact is a local refusal,
not an old comm-only proxy fallback.

## Actual four-stage chain and receiver consumption

Keep prepare -> confirm freeze -> collect -> confirm judge, with the existing
create-only intents, finite budgets/process groups, raw stdout/stderr, UNKNOWN,
no resume/retry and explicit PF reconcile/new-session guidance. No nested lease,
device launch/installation, charge changes or model call is added.

After PREPARED, enforce current receiver `confirmation_conditions@3`, matching
loaded policy/thermal contract and package/comm registration; actual candidate,
baseline, conditions and admission byte hashes must equal the receiver plan.
Require `confirmation_baseline_admission@2`, its exact render-contract reference,
the `frozen_render_attribution@1` contract, exactly three independently hashed
baseline records and their exact fixed admission_N-run.json references. Read
the current trace/ENV/thermal/render and other fixed raw receipts, bind
phase/role/pair/serial/conditions/artifact/source to that run, and compare all12
baseline trace/render hashes with the frozen records. Require the actual
heldout bundle/run/registration FileRefs, with the same fixed raw inventory and
report bindings. Archive these current raw bytes as hashes, not just declared
references. Large trace/ENV/thermal files are <=128MiB; other evidence <=2MiB;
the combined baseline/heldout unique raw-byte inventory has a512MiB limit
(not an RSS claim). Require the complete initial witness and matching pinned
reader source/ELF/build copies. The existing Plan fields bind complete conditions/admission
bytes before heldout; no new PF Plan field or old confirmation fingerprint is
introduced. SPF/caller/context/epoch selection is receiver frozen before arms.

After COLLECTED_OBSERVATIONS, require the exact fixed pair count/order/IDs and
fixed report paths. Each report must bind the frozen render-contract SHA,
both target-command-origin/grouping gates, the same scope/grouping and raw
before/after/final hashes. Read the actual fixed witness files with bounded
regular-file reads; missing/tampered/reused/exploration witness bytes block.
No comm-only, different SPF/context/caller/epoch, mixed profile or asserted
display/quality/owner verification is accepted. Preserve the receiver reports
unchanged and run the existing statistical judge; negative judgments remain
negative. V2 run and inspect repeat these checks, not just outer ledger hashes.

## Evidence and limits

Every proof/promotion flag stays false, including statistical PASS. Hashes check
integrity conditional on trusted local tools; they do not authenticate acquisition.
The formal metrics are the registered KGSL target-command submit cadence proxy
and inferred grouping, not present/display FPS or complete GPU frame timing.
Missing raw linkage remains BLOCKED/UNKNOWN. Old records are not retrospectively
attributed. Source/gate changes do not re-label prior accepted binaries.

Acceptance: v1 bytes/fingerprint unchanged; actual v2 CLI plan/freeze and four
subprocess stages with clearly synthetic mechanism fixtures; native file/hash/
inventory and profile rejection; complete/tampered receiver binding/grouping/
witness countercases; actual PF fake transport refuses before confirmation;
full GOL Cargo and strict all-targets/all-features Clippy. No device, real model
API, shader build or Android/GPU/benefit acceptance is part of these host gates.
