# phonefarm synchronized task browser

This Python service browses actual phonefarm task records and confirmation/model
sessions. It has no execution endpoint and does not invoke a device or a model.
It also indexes schema-identified physical `livephase` reviews as
`experiment_phase`: diagnostic checkpoints, failed controls, prepare and
recovery are separate from qualified optimization rounds. A physical stage
with a successful old restore cannot resolve a later UNKNOWN barrier.
The Tasks routes are provided by this server, **not** by the native GOL Rust
`serve` command. Native GOL serves only the separate dashboard input.

```sh
python3 -B tools/phonefarm_web/catalog.py \
  --phonefarm-root /absolute/phonefarm \
  --gol-dashboard /absolute/captured-native-dashboard.json \
  --out /absolute/new-snapshot
python3 -B tools/phonefarm_web/server.py \
  --snapshot /absolute/new-snapshot \
  --assets /absolute/game_opt_loop/src/viz/assets \
  --host 127.0.0.1 --port 8711
```

`--tasks-root` is an alternate exporter input for an explicit data root. Output
must be new. It contains `catalog.json`, `artifacts/<opaqueid>.json`, then the
completion `release-manifest.json`. The server checks source-independent public
file integrity and never follows a client path. JSON artifacts are typed
redacted projections with their original source SHA recorded separately; raw
screen text, input text, credentials, transport data and screenshots are not
published. Files cannot be uploaded or executed through the server.

For additional repository task roots pass `--extra-tasks-root models=/absolute/tasks`
(repeatable, at most three extras/four sources in total). The Python function is
`export(tasks_root, out, dashboard=None, extra_tasks_roots={alias: whole_tasks_root})`.
Namespaces make colliding task/run directory names distinct. The source alias is
public, the absolute root is not. Entire roots are indexed, not selected game or
stage results. Build/privacy skips and global count/byte/depth caps still apply.

The snapshot time is Mac export time. A native GOL `generated_at` is the time its
dashboard was built; source record timestamps are separate again. `source_mtime`
is only filesystem metadata, not sample time. Missing record timestamps remain
null. A stale or failed native capture must be reported as such; never rewrite
an older capture's time to pretend it was queried again. Native evidence with no
safe artifact registration remains unavailable publicly. This is synchronized
history, not live phone state. Incomplete logs, unknown helper idleness and
missing metrics remain unknown; UI achieved does not qualify optimization.

For a native snapshot use only an accepted signed GOL binary:

```sh
accepted-game_opt_loop serve --root /absolute/game_opt_loop \
  --phonefarm-root /absolute/phonefarm --no-fleet --port 18712
```

Fetch only loopback `/api/dashboard`, with a deadline and 4MiB cap, then stop the
owned server. Do not proxy native `/api/file`. Root's periodic Mac service may
use a different private port (8712), fixed tool identity and the same no-fleet
mode; scheduling, synchronization and TLS deployment are separate from data
indexing. Catalog directory counts are bounded and diagnostics disclose caps;
counts describe indexed records, not an unbounded complete filesystem census.
The completion manifest includes `phase_imports`, matching every imported
review's original SHA and its bounded reference verification receipts. Only
sanitized review JSON is public; reference images/argv/scripts/markers are not.
Incomplete or undeclared reference integrity is explicit, and all diagnostic
phase qualification remains false. `kind=experiment_phase` filters this kind;
`/api/meta.counts.experiment_phase` counts these records, not measured gains.

## HTTP

See [SPEC.md](SPEC.md). Pages use `items/total/offset/limit` with limit1..200.
`/api/meta` provides source snapshot/counts. `/api/tasks`,
`/api/tasks/{task}/runs`, `/api/runs?task={task}&kind=all`, run detail, steps and
registered artifact IDs are supported. Native dashboard preserves its layer
structure with private paths/device identities removed. No `/api/file`, MCP
write tool, shell, ADB, HDC or device lock action is exposed.

## Host gates

```sh
python3 -B -m unittest discover -s tools/phonefarm_web -p 'test_*.py' -v
node --check src/viz/assets/app.js
git diff --check
```

Also run the real exporter and HTTP queries against a new snapshot. Tests use
owned synthetic records and must never be described as live device acceptance.
This tools/UI delta changes no Rust code or measurement contract, so the
documented Python/Node gate applies. Existing Cargo gates are not claimed as
verification of this new code.
