# Candidate compilation evidence v1

Refs [system interface issue 14](https://github.com/HGamey/.github/issues/14) and
[system epic 11](https://github.com/HGamey/.github/issues/11).

## Delivered scope

`candidate-artifact compile --candidate C.json --out NEW_DIRECTORY` freezes the
exact candidate JSON bytes, the contained UTF-8 source, the repository's actual
Naga-generated SPIR-V, the complete normalized static gate result, track contract,
and limits. `candidate-artifact verify --artifact DIRECTORY --expected-identity SHA256` reads one bounded
snapshot of every artifact, re-runs the same repository gate and compiler on that
source, and compares gate output and exact little-endian SPIR-V bytes. It never
invokes a model, external compiler, phonefarm, or a device.

The strict candidate has the existing Candidate fields: id, generation, track,
parent_id, budget_ms, shader_source, fingerprint, origin, and optional notes.
Unknown/duplicate JSON fields, unknown tracks/origins, empty identities, incorrect
dedup fingerprints, nonpositive budgets, and oversized inputs are rejected.
The fingerprint remains a normalized dedup hint; full SHA-256 of exact candidate
JSON and source bytes defines frozen identity. Limits are the fixed current
Validator defaults, not caller-selected relaxed limits. All TrackSpec fields and
binding slots are frozen, including historical reference values; these are
configuration, not measured quality or power evidence.

The immutable flat directory contains candidate.json, original.comp,
original.spv (only if the gate passed), rules.json, gate.json, descriptor.json,
manifest.json. The descriptor binds candidate identity, track/limits hash,
normalized gate hash, source/SPV hashes, crate version, embedded compiler-policy
implementation hashes, Cargo.toml/Cargo.lock hashes and the bounded, streamed
on-disk current executable hash. Gate elapsed time is diagnostic and deliberately
excluded from replay equality and frozen identity. A rejected candidate is still
persisted as `gate_rejected`, without SPIR-V; compile exits nonzero. Verification
of a rejection replays its negative evidence, but does not allow adaptation.
Missing fields, wrong schemas, noncanonical rules/descriptor, additional files,
hash mismatch, altered gate output, a changed compiler binary or dependency lock,
and gate failures disguised as successes fail closed.
Verification requires the identity frozen outside the directory (compile receipt
or campaign). Rehashing an edited ledger cannot preserve that identity. A caller
who supplies an attacker's replacement identity trusts a different candidate;
local checks do not authenticate that caller's approval.

All data used in gate execution comes from the bounded snapshot whose bytes were
hashed, not a subsequent file read. Unix directory traversal uses directory file
descriptors and O_NOFOLLOW for every path component, regular files only, checked
size limits, create_new files and a newly created output directory. There is no
overwrite or symlink traversal. A manifest is written last; interrupted writes
leave an incomplete directory that cannot verify. Existing directories cannot be
resumed or overwritten. File and directory syncs preserve the completed ledger.
The implementation does not claim to defend against a privileged process changing
the executable or forging all local records. A hash is integrity, not an authenticated
remote compiler attestation; Cargo.lock does not authenticate the complete toolchain.

## PR21 consumer contract

Successful compile and verify JSON reports expose `adapter_input` with fixed
relative paths `original.comp`/`original.spv`, both SHA-256 values,
descriptor_sha256, manifest_sha256, candidate_json_sha256 and frozen_identity.
A scheduling driver must first successfully verify this directory, bind that
exact descriptor/manifest identity in its campaign, then pass these source/SPV
files to actual `phonefarm gray-artifact adapt-storage`. The driver must compare
the adapter descriptor's original source/SPV hashes to this export and verify the
adaptation. This adds a separately replayed local compilation association; it
does not relabel PR21's `caller_supplied_unverified` as trusted attestation.
The actual cross-repository test uses Naga output and the real phonefarm adapter,
not a fake compiler. FrameGen can compile, but the finite SR-only PR21 adapter is
not promised to accept it.

`host_compiled_replay_match` means only this repository compiler and static gate
reproduce these exact bytes. Every report sets gpu_validated,
semantic_equivalence_proven, quality_verified, real_game_optimization_proven,
deployable and promotion_allowed to false. Neither a gate PASS nor deterministic
SPIR-V proves GPU execution, Vulkan validation, image equivalence, or real games.

## Remaining complete scheduling driver

This feature is one real offline interface, not a completed candidate driver.
A complete driver must preserve these gates rather than remove them to reach PASS:

1. Bind compile -> finite adapter -> real native build identities, source/SPV and
   layer bytes; freeze exact receiver executable, target/workload, candidate,
   baseline, conditions, quality rules, and approval. Missing NDK/native build
   remains blocked. A fake build receipt proves only the protocol mechanism.
2. Acquire the receiver's device lease, baseline admission and cold-state gates;
   enforce per-command/whole-run budgets and durable in-flight status before
   side effects. Unknown remote execution never retries automatically. Crash or
   incomplete rollback must keep an abandoned/blocked lease until explicit
   reconciliation proves idle and complete restoration.
3. Before deployability, validate actual VkDevice capabilities, image format,
   usage, layouts/ownership, descriptor and resource lifetimes. Recording and
   accepted queue counters do not prove fence completion or output pixels. The
   existing global descriptor reuse requires the native lifetime workstream.
4. Bind installed layer/SPV identity and actual runtime completion to the report.
   Keep performance and quality capture runs separate. Paired native captures
   need provenance, clock/alignment and candidate binding; procedural whitebox or
   synthetic output cannot qualify a real game. Metrics-only PASS is insufficient.
5. The current heldout receiver is a sysparam path. Native shader candidates must
   not be disguised as sysparams or admitted through legacy nullable quality /
   missing qualification compatibility. Add an explicit native measurement and
   quality contract, with independent confirmatory pairs and frozen thresholds.

Offline work can validate identities, replay compilation/adaptation, immutable
state transitions, argv transport, receipts and negative cases. Descriptor/image
correctness, actual GPU completion/equivalence, authentic paired game quality,
performance/whole power windows, and physical restoration require GPU/device
evidence and remain unverified in this change.

## Validation

Actual CLI tests cover SR/FrameGen, unsafe source rejection, omitted/unknown and
duplicate fields, frozen gate/limits/binary/source/SPV/candidate tampering,
re-hashed forged bytecode, symlink files/ancestors, overwrite, unexpected files,
bounded reads and gate replay. Full Rust tests and strict clippy are required.
Cross-repository adapter acceptance is logged separately because phonefarm is
not a build dependency and native SDK/GPU use is outside this offline scope.
