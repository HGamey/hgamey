# Native capture completion publication

Refs PR2 / system data contract #15. This change is capture-only; BW performance,
source UNKNOWN, independent native HR reference, and sampling policy do not change.

## Contract

The existing exclusive `capture_<run_id>` directory is the only completion root.
Quality capture writes `refbench_out.json` there, not the shared app-wide path.
The final `completion.json` (`refbench/native_capture_completion@1`) binds the
run ID, exact manifest SHA, exact run-output SHA, frame count and submitted/total
frames. It is create-only, file/directory-synced and published last. A manifest
`complete=true` alone is not publication: a crash before the marker remains
incomplete to the strict host consumer. Old captures without this marker are
explicitly rejected; the manifest @1 format remains unchanged.

Successful completion requires: clean full run; all selected frames in order with
matching phase/roles/dimensions; the actual callback's successful submission fence
wait before map/write; all raw bytes written, closed and synced; re-read regular
raw files with exact sizes/hashes; successful run-scoped output; final manifest;
then final marker. Marker hashes bind immutable checked byte snapshots on import.
Host file checks prove integrity, not GPU/acquisition attestation.

Any raw/checkpoint/output/marker write, close, sync or final raw verification error
is sticky. No later finalize may clear it. The native path must set clean=false
and exit2. Best-effort failure checkpoint can itself fail; absence/mismatch of the
final marker still blocks the receiver. The directory is never reused, and there
is no fallback to global stale output or an earlier marker. Failed publication
removes its own marker if directory sync failed. No guarantee against a hostile
same-UID writer; consumer independently checks current raw/output/manifest bytes.

## Tests / limits

Real host filesystem countercases cover existing directory/raw/output/marker
collisions, symlinks, manifest-temp collision, missing/tampered marker, wrong
output ID/config/clean/submit count, failed/incomplete capture and stale output.
Existing host syntax and actual Android NDK build are required. Android build is
codegen only: GPU execution, lifecycle/synchronization, source authenticity,
quality/performance/power and real-game acceptance remain NOT_RUN/false. Existing
unbounded Vulkan general waits still require the outer device owner's barrier.
