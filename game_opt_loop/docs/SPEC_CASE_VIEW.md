# Unified read-only case view

Refs [system #18](https://github.com/HGamey/.github/issues/18). Stacked on
[PR32](https://github.com/HGamey/game_opt_loop/pull/32); consumes the saved schemas
of [PR29](https://github.com/HGamey/game_opt_loop/pull/29) and
[PR30](https://github.com/HGamey/game_opt_loop/pull/30), without importing or running
either producer or any driver. This is an evidence reader, not a confirmation judge.

## Discovery and bounded snapshots

Search only `runs/` and `tasks/` beneath the configured GOL and phonefarm roots.
Bound discovery to depth 5, 256 directories, 4096 directory entries, 64 cases,
2 MiB per JSON document, 16 MiB total document bytes and 32 associations per case.
Reaching a limit is visible; a partial list is never described as complete.
Hidden components, parent traversal and symbolic links are refused. Unix traversal
and reads use directory descriptors and `openat(O_NOFOLLOW|O_NONBLOCK)`, including
every configured root component; regular-file
metadata, bounded read, SHA and UTF-8/JSON parsing use the same opened file.
These are input-byte/traversal limits, not an RSS guarantee. Unsupported non-Unix
snapshot acquisition reports NOT_RUN instead of using a weaker reader.

Keep primary UTF-8 text and its byte SHA alongside the complete parsed JSON.
Pretty JSON is a derived display, not the original byte stream. Parse failures,
duplicate object keys, malformed/unknown schema, inaccessible references, hash
mismatches and limit omissions remain visible. Never silently fill qualification
or missing source fields. Multiple primary paths with identical bytes are labelled
as duplicate snapshots, not counted as independent experiments.

## Cases and associations

Recognize `game_opt_loop/confirmation_report@1` and
`game_opt_loop/model_pipeline_journal@1` exactly. Preserve unknown fields.
Confirmation displays saved PASS/INCONCLUSIVE/BLOCKED, issues, frozen hashes,
metric upper bounds/margins, assumptions, claim scope and source attestation.
It does not recalculate or authenticate them. Adjacent `samples.json` may provide
run-report associations only when its snapshot SHA matches `samples_sha256`.
Arm report SHA must agree with both sample and saved report mappings.

Model cases show each saved stage state, decisions, errors, requests, receipts,
declared usage/source/model format, reconciliation and limitations. Completed
means the saved driver protocol claims completion, not successful training,
deployment, evaluation qualification or actual model semantics. UNKNOWN/in_flight
and failed/rejected/deferred remain distinct and visible; the view never resumes.
JSON receipt/evaluation/deployment artifacts may be shown with their declared SHA.

Associations are converted to finite relative components within the fixed roots,
use the same descriptor reader, and are limited to visible `.json` files. Declared
absolute paths outside those roots and every `..` are refused; in-root absolute
paths are converted to relative paths before opening. Hash match proves only the
read bytes match a declaration, not authenticity. The existing `/api/file` file
whitelist is not broadened. No binary/model/driver/trace loading or execution.

Adjacent `summary.json` and optional `case_context.json` are unverified context,
preserved in full. `case_context` may explicitly label a copied offline fixture as
synthetic using schema `game_opt_loop/case_context@1` and `evidence_origin`; this
label cannot authenticate a case. The cross-CLI fixture uses hypothetical driver
declarations and transformed archived excerpts, and is always presented as a
synthetic demonstration even if its saved report declares recorded receiver data.

## Presentation and acceptance

Add an independent Cases page with status/source/qualification declarations,
negative results, per-stage recovery notes and complete associated receiver JSON.
Primary original UTF-8 text and SHA are expandable; display escapes all strings.
Refresh preserves selection and expanded evidence. Every case and the collection
set `view_grants_game_proof=false` and `view_allows_promotion=false`, regardless of
any contradictory primary or referenced claim. A report's own claims remain raw
and are labelled unverified, never translated to UI permission.

The rendered summaries show at most 32 metric rows and 128 issues, with explicit
notices when truncated; full saved fields remain in the raw document. Evidence
expansion is keyed by case identity and document path, not discovery indices.

Use actual saved confirmation cross-CLI and fake model journal snapshots as
synthetic fixtures, with contract mutations for UNKNOWN/failed/reconciliation,
unknown fields, missing fields, contradictory proof flags, duplicate keys, unsafe
paths, symlinks/FIFO, hash mismatch and limit truncation. Check full dashboard JSON,
real localhost HTTP with `--no-fleet`, and actual browser rendering/refresh.
No model, driver, device or GPU calls. Actual game case attestation remains open.
