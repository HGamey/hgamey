# Existing panel: today’s phonefarm experiment

The exporter reads fixed terminal sources. It does not connect to a phone or
scan a directory. Keep registry and output beneath the explicit canonical task
root; never point it at credentials, an alias, a private snapshot or a session.

```sh
python3 tools/live_dashboard/export.py \
  --task-root /Users/kamico/work/GOSIM/HGamey/phonefarm/tasks/oneplus_live_20261012 \
  --eval-registry reporting/live-dashboard-publication/eval-input-registry.json \
  --out /Users/kamico/work/GOSIM/HGamey/phonefarm/tasks/oneplus_live_20261012/reporting/live-dashboard-publication/release-002
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
public. Root handles SSH/TLS. The finite deployment sequence is:

```sh
rsync -a --chmod=D755,F644 RELEASE_DIRECTORY/ osakasb:/srv/phonefarm/releases/RELEASE_NAME/
ssh osakasb 'ln -s /srv/phonefarm/releases/RELEASE_NAME /srv/phonefarm/current.next && mv -Tf /srv/phonefarm/current.next /srv/phonefarm/current'
```

The server runs `python3 -m http.server 8461 --bind 127.0.0.1 --directory
/srv/phonefarm/current`; nginx forwards the public HTTPS host there. Its browser
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
