# Actual offline preparation evidence

This directory records local execution of the new `candidate-prepare` command.
It is compiler/adapter integrity evidence, not real-game, GPU, quality, native
build, device recovery or deployment acceptance. No device, model, training,
SDK installation or paid API was used.

## Cases

| Case | Actual commands | Outcome |
| --- | --- | --- |
| `actual-offline` | GOL `candidate-artifact compile` + `verify`; PF `gray-artifact adapt-storage` + `verify-adaptation` | `PREPARED_OFFLINE`; compiler/adapter chain and non-build preparation complete |
| `actual-missing-sdk` | Same four actual commands, then actual PF `build` with this session's private empty SDK | `BLOCKED/MISSING_ANDROID_NDK`; compiler/adapter chain complete; requested full preparation incomplete |

The candidate is the archived local SR candidate from PR31. Compilation uses
the actual GOL/Naga implementation, and adaptation uses the actual phonefarm
finite SPIR-V adapter. The original source and original SPIR-V are equal byte
for byte across that boundary. The adapter still reports
`caller_supplied_unverified`; local compiler replay is not authenticated remote
attestation or semantic equivalence. Every proof flag remains false.

`actual-cli-summary.json` records tool hashes, candidate and adaptation IDs,
exit codes and original session paths. Each case contains exact argv, stdout,
stderr, exit codes and an archive of its journal, receipts and bounded artifacts.
Large private executable copies are omitted from this Git archive: hashes and
sizes remain in the original journal. `inspect.*` records inspection of the
original live session, not of this relocated archive. Absolute paths in the
archive are historical observations; an archive copy is not a portable session
with valid execution paths.

The missing caller SDK path was only observed as absent. The PF build argv uses
`session/controlled-empty-sdk`, and the receipt explicitly labels the probe as
controlled missing tools rather than inspection of the caller installation.
There was no controlled fake-NDK positive test here. A present/partial SDK is
`NOT_RUN/BLOCKED_PROCESS_GROUP_CONTRACT`; real NDK and GPU execution are not run.

## Verification

`full-tests.log` includes the explicitly enabled actual PF cross-repository
acceptance case: 355 tests passed, including 13 preparation CLI tests and two
targeted preparation filesystem tests. Synthetic PF fixtures cover output/timeout/lingering-group,
host interruption, invalid receipts, forged internally rehashed source,
prepared/journal contradictions and symlinks; they never grant real-source
qualification. Atomic permit publication and repeated directory inventory have
targeted native filesystem tests. A negative SDK probe is durably `BLOCKED`,
including before the final receipt, rather than `COMPLETED` with a failed exit.

`strict-clippy.log` records `cargo clippy --offline --locked --all-targets -- -D warnings`.

To regenerate actual CLI evidence, supply built binaries and a fresh archive
directory to `run_actual_acceptance.py`. It executes only the structured argv
shown in the archived files. It does not install an SDK or select a device.
