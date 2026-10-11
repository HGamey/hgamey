# Phonefarm live experiment dashboard

Scope: reuse the existing `src/viz/assets` panel at source `6b7d45c`; add a
read-only Today tab. This is a static export for nginx, not another device
driver. No Rust measurement code changes, no fleet calls, no model calls.

## Data

`tools/live_dashboard/export.py` reads an explicit finite list of completed
receipts beneath the user-selected task root. It never enumerates a task tree.
Any `private-auth`, hidden component, symlink, traversal or unknown input path
is rejected before opening it. JSON/text is bounded to 2 MiB per file, 32 MiB
per export; no SDK, APK, binary, arbitrary `/api/file` or raw account screenshot
is published. Review-linked input hashes are verified on the same read bytes.
The absolute task root is anchored component by component with NOFOLLOW; root
or ancestor aliases are rejected. Reads duplicate that directory fd, avoiding
a later replacement of the caller's root path. Use an explicit canonical path.

Public data is a selected normalized summary with source relative path, bytes
and SHA256. Serial, boot ID, network address, local absolute paths and command
argv are not exported. Hashes establish local artifact integrity, not trusted
capture attestation. Unknown values remain null. Screenshots remain local;
their account identifiers are not silently exposed by a public static site.

Initial records cover actual SF30/SF60 diagnostics, thermal pre-write rejection
and actual FrameTimeline target absence. SF averageFPS is explicitly a retained
interval statistic; weighted histogram duration is not wall coverage. KGSL
frame/FPS fields in later eval receipts remain submission cadence proxies.
FrameTimeline global display rows never substitute for target SurfaceFrame.

An explicit completed eval input registry may be supplied for later results.
It binds the exact report and its command receipt hashes; receiver ABORT/REJECT,
missing metrics, qualification, restoration and baseline rows are preserved.
The panel does not grant quality, display, benefit, promotion or deployment
qualification from report labels. Every release defaults these proof flags to
false. A completed command is not proof that all experimental stages ran.
Registry records are sorted by the finite completed command time, not caller
list order. Completed prepare may bind an independent reconcile receipt and
the finite registered terminal review; restoring a session leaves prepare
BLOCKED and baseline 0/3. Recovery writes are not mislabeled as zero device
writes. A pre-write exit82 without TEMP values has an unknown detailed reason.

## Refresh and deployment

Each export creates a new directory, never overwrites an earlier release, and
publishes `release-manifest.json` last. It contains only existing UI assets,
`api/dashboard`, `api/live-experiments.json`, `latest.json` and manifest.
An exception leaves `INCOMPLETE.json` and no complete manifest.

The existing refresh button and a 30-second browser fetch read the static
JSON. No scheduled device work or server-side scan is created. The operator
exports a new release after a terminal checkpoint, rsyncs that finite directory
and atomically switches nginx's current symlink. `latest.json` identifies the
data hash and export timestamp; that timestamp is not a new phone measurement.
The old tabs retain the existing panel markup and helpers. A static release
includes an explicitly empty historical dashboard rather than importing old
redmagic results as today's OnePlus results.
The additive PR is based on current origin/main (`85c3524`), with only Today
styles/routes added. A normal Rust panel without the static live endpoint
returns to its existing Overview page; it does not replace the old dashboard
with a missing-live-data screen. The published release-005 is separately
bound to its original integrated assets and source `124b078`; it is not
silently regenerated from this adapted PR branch.

Validation: Python bounded/terminal/hash input checks, Node syntax and DOM-stub
render checks, local HTTP/browser QA. No Cargo whole gate is implied because
Rust source and measurement policies are unchanged. Root owns remote SSH,
TLS/nginx deployment and public readback.
