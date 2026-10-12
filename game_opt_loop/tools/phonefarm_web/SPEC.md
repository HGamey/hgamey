# Generic phonefarm synchronized task API

This is a read-only task browser over phonefarm-produced files, not a task
executor or a claim of live hardware state. No game-specific directory or
curated report list is part of discovery. The existing UI run format and
confirmation sessions remain separate run kinds.

## Files and origin

The Mac exporter accepts one explicit tasks root. It discovers ordinary
`<task>/runs/<run>/log.jsonl` records and schema-identified confirmation
`state.json` + `request.json` + `journal.jsonl` sessions. Recognized result
schemas can add diagnostic child phases and model pipeline records. A malformed
or unsupported record is diagnosed, never silently made successful. It never
creates a fake log.jsonl to make confirmation sessions look like UI traversals.

Completed producer `review.json`/`telemetry.json` records under `livephase` are
indexed as `experiment_phase`, separately from UI runs, confirmation sessions,
model pipelines and qualified optimization rounds. The adapter accepts finite
physical-checkpoint/terminal-review schemas, not game directory names. Explicit
legacy terminal shapes without schema remain legacy declarations. Unsupported
JSON is diagnosed. Result files beside an accepted review do not count a second
physical phase. Source tests/builds outside this producer branch are excluded.

Only typed known scalars and finite nested observation objects are public.
Scripts, argv, screenshots, marker contents, PID/UID/boot/token and arbitrary
error/input strings remain private. Each phase retains its original review SHA.
Declared artifact hashes are resolved lexically inside the phase, then opened
with component NOFOLLOW/regular checks and bounded streaming hash verification:
128 references/phase, 12MiB/reference, 512MiB/3000 references/export. Out-of-phase
or private references are not read. Changed, missing or oversized refs give
partial-integrity diagnostics; the declared producer status remains separate
from UNKNOWN integrity and never becomes verified evidence. Public artifacts contain only typed review projections and opaque
reference hashes. A manifest import receipt reconciles each original review
and reference hash. Old successful recovery never updates a later phase.

All diagnostic-phase performance qualification remains false. Completion, host
success, restore and remote idle are separate fields. Values retain declared
metric scope, null power and submit-proxy/display distinctions. SF fractions
refer to retained labelled buckets, not continuous FPS or a precise jank limit.

Traversal is bounded by depth, entry and file counts. Dot paths, private-auth,
build/runtime caches, symlinks, nonregular files and oversize inputs are refused
or skipped with diagnostics. Open files with O_NOFOLLOW and O_NONBLOCK, validate
regular type and read at most limit+1 bytes. No credential or arbitrary artifact
is read through JSON path fields. No device command or paid model request occurs.

Public records use typed field projections. Raw transport argv, PID/boot/serial,
tokens, account UI text, model prompts and arbitrary error text are not exported.
An opaque ID is derived from a root-relative source identity. Public artifact
bytes are sanitized projections; both their own SHA and the original source SHA
are recorded. No original screenshot is published by default. Unknown status,
missing metrics and incomplete sampling stay UNKNOWN/null. A completed diagnostic
is not a recovered device or an optimized game.

Multiple registered roots have namespace IDs and bounded equal shares of the
unchanged global byte/file/directory/entry budgets. Discovery rotates between
roots breadth-first and visits normal runs/livephase/sessions/pipeline branches
before unrelated branches. Filesystem mtime may schedule discovery; it is never
presented as a measured event timestamp. Cap diagnostics and indexed counts
remain explicit. This prevents build caches in the first root from suppressing
all real sessions in later roots; it does not add a case-specific include list.

## API v1

Every response has `snapshot {id,synchronized_at,origin:'mac_filesystem_sync',
live_device:false}`. This time is the export time; source timestamps and hashes
remain separate. The immutable catalog contains tasks, runs, steps and artifact
metadata, plus a native GOL dashboard snapshot if explicitly provided.

- `GET /api/meta` => `{schema:'phonefarm_web/meta@1',snapshot,
  counts:{tasks,ui_runs,confirmation,model_pipeline,experiment_phase},diagnostics,device_live:false}`
- `GET /api/tasks?offset=0&limit=20` and
  `/api/tasks/{task_id}/runs?offset=0&limit=20&kind=all` =>
  `{schema:'phonefarm_web/page@1',snapshot,items,total,offset,limit}`
- `GET /api/runs?task={id}&kind=all&offset=0&limit=20` is the same run page.
- `GET /api/runs/{run_id}` => `{schema:'phonefarm_web/run_detail@1',snapshot,
  run,steps:[first 100],step_total,artifacts,diagnostics}`
- `GET /api/runs/{run_id}/steps?offset=0&limit=20` => page;
  `/api/runs/{run_id}/steps/{index}` => `{snapshot,step}`.
- `GET /api/artifacts/{artifact_id}` returns only a registered exported file,
  with its actual hash. No raw `path` parameter or `/api/file` is supported.
- `GET /api/dashboard` retains the native GOL dashboard shape plus snapshot.
  Export comes from one accepted `serve --no-fleet` localhost invocation, not
  reimplemented report cards. Only safe registered evidence URLs remain usable.

Task item: `{id,name,run_count,latest_run_id,updated_at,kinds}`.
Run item: `{id,task_id,kind,label,status,started_at,finished_at,updated_at,
qualification:{eligible,scope},metrics,step_count,artifact_count,
source:{schema,sha256,origin}}`. Kind is ui_run/confirmation/model_pipeline/experiment_phase.
Step: `{index,type,name,status,at,summary:{typed fields},artifact_ids}`.
Artifact: `{id,label,kind,bytes,sha256,source_sha256,source_relative_path}`.
Limits are strict nonnegative offsets and 1..200 pages; unknown IDs =>404,
bad query =>400. JSON is strict and finite. POST/PUT/DELETE =>405. No shell,
MCP execution tool, device enumeration or file upload endpoint exists.

## Synchronization and HTTP service

Exporter writes a fresh output directory and publishes its manifest last. It
records source-file hashes and the export source identity. The server validates
the complete catalog/manifest and serves from one explicit release root. Root
owns scheduling, atomic synchronization and TLS deployment. Requests reread a
validated catalog so an atomic new release is visible, not a cached fabricated
live state. Source files, old exports and device recovery markers are untouched.

Native dashboard capture uses an exact accepted signed GOL binary, explicit
GOL/PF roots, `--no-fleet`, private loopback port, finite runtime and output cap.
The server never proxies native /api/file or invokes GOL/PF per public request.
Absolute machine paths and device identities are removed from its public copy.

## Gates

Use actual file-backed UI/session fixtures, malformed/partial files, fake origin,
missing metrics, symlink/FIFO/private-auth/oversize, private sentinels, pagination,
HTTP unknown IDs/invalid queries/write rejection and artifact integrity tests.
Run the actual exporter against the real tasks root and query the real HTTP
service. This tools-only Python change does not alter Rust or measurement
contracts, and follows the existing tools-only Python/Node gate; no device or
Cargo execution is implied. No test fixture becomes a hardware result.
