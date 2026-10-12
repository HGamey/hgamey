# Read-only web deployment

The server serves the existing GOL panel plus a generic phonefarm task browser.
It indexes actual UI logs, confirmation sessions, model journals and typed
physical phase reviews. It does not fabricate legacy optimization rounds from
diagnostics. Missing legacy inputs display unavailable, rather than zero.
The same assets fall back to the native dashboard when native GOL lacks
`/api/meta`; native GOL does not acquire the Python task routes through this UI.

Deploy a complete exported snapshot and these assets with:

```sh
python3 -B tools/phonefarm_web/deploy.py \
  --snapshot /absolute/exported-snapshot \
  --evidence /absolute/owned-deployment-receipts --install
```

The fixed deployment host is `osakasb`; HTTPS nginx proxies to a restricted
loopback Python service. Application and data releases have verified manifests,
with previous immutable releases retained. Routine data updates omit `--install`.
The public API exposes registered redacted artifacts only, not arbitrary files,
MCP execution, ADB, HDC or phone controls. Snapshot time is export time, not a
live phone observation. Original source SHA and public artifact SHA are distinct.

Current deployment is explicit synchronization after experiment checkpoints.
Automatic Mac scheduling is not enabled. Before enabling it, implement bounded
deployment process retirement, protection against stale late publication, input
deduplication and an owned snapshot retention limit. Do not remove task originals
or accepted tools to bound synchronization storage.

Verification: Python catalog tests, Node syntax, an actual source export,
HTTP checks and local/public browser acceptance. Confirm real phase details,
artifact hashes, native dashboard fallback and mobile overflow. These gates do
not qualify captured GPU buffers or optimization benefits.
