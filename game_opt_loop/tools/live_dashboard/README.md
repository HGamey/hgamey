# Existing panel: today’s phonefarm experiment

The exporter reads fixed terminal sources. It does not connect to a phone or
scan a directory. Keep registry and output beneath the explicit canonical task
root; never point it at credentials, an alias, a private snapshot or a session.

```sh
task_release_name="release-$(date -u +%Y%m%dT%H%M%SZ)"
python3 tools/live_dashboard/export.py \
  --task-root /Users/kamico/work/GOSIM/HGamey/phonefarm/tasks/oneplus_live_20261012 \
  --eval-registry reporting/live-dashboard-publication/eval-input-registry.json \
  --out "/Users/kamico/work/GOSIM/HGamey/phonefarm/tasks/oneplus_live_20261012/reporting/live-dashboard-publication/$task_release_name"
```

Use a fresh release name for each **completed** checkpoint. Registry schema
`hgamey/live_eval_input_registry@1` accepts at most 32 explicit entries, each
with `terminal:true`, `id`, `kind` (`existing_prepare` or `existing_eval`),
`report`, `report_sha256`, `command`, and `command_sha256`. Paths are finite
relative paths, hashes bind the bytes actually read. Optional restoration is
an exact command/report pair with their hashes. A supported completed prepare
review can additionally be bound by `checkpoint_review` and its SHA256.
This local operator registry is integrity metadata, not trusted attestation.

Inspect `release-manifest.json` and rehash its six files before deploying only
that release. No original command, account screenshot or arbitrary file API is
public. Root handles SSH/TLS through its bounded, receipt-checked deployment
script (replace the two final arguments with the exact release and independent
review paths):

```sh
python3 /Users/kamico/work/GOSIM/HGamey/phonefarm/tasks/oneplus_live_20261012/deployment_phonefarm_20261012/deploy_release.py \
  --release FINAL_RELEASE_DIRECTORY --review-receipt A_EXACT_REVIEW_RECEIPT
```

The script uploads only the release to the user's remote cache, then uses sudo
install and an atomic current symlink. The actual remote directories are
`/var/www/phonefarm/releases` and `/var/www/phonefarm/current`. Nginx's SNI443
routing forwards to a local nginx TLS static server at `127.0.0.1:8461`; there
is no Python HTTP server in the deployment. Its browser
fetches `/api/live-experiments.json` with no-store every 30 seconds, preserving
the prior data and a visible warning on failure. The export timestamp is not a
new phone sample. Refresh creates no device work.

Checks (no Cargo gate; Rust is unchanged):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/live_dashboard -p 'test_*.py' -v
node --check src/viz/assets/app.js
node tools/live_dashboard/test_frontend.cjs
git diff --check
```
