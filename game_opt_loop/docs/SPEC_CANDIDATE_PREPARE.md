# Offline candidate preparation driver

Refs [HGamey/.github#14](https://github.com/HGamey/.github/issues/14) and
[harness epic #11](https://github.com/HGamey/.github/issues/11). This builds on
PR31's [compiler evidence contract](SPEC_CANDIDATE_COMPILATION.md) and phonefarm
PR21's finite storage-to-sampled SPIR-V adapter.

Status: reviewed implementation contract; implementation has not been accepted. No model,
training, SDK installation, ADB/HDC, device write or GPU execution belongs here.

## Scope

Replace the four manual cross-repository commands with a finite executable
preparation flow. It produces compiler replay and adapter integrity evidence.
It is the build preparation part of a candidate driver, not an evaluation loop.
All GPU, semantic equivalence, quality, real-game proof, deployability and
promotion flags remain false, including after every offline stage succeeds.

CLI:

```
game_opt_loop candidate-prepare run --candidate C.json --phonefarm-bin PF --out NEWDIR
game_opt_loop candidate-prepare run --candidate C.json --phonefarm-bin PF --out NEWDIR --build --ndk MISSING_NDK
game_opt_loop candidate-prepare inspect --session DIR
```

Caller-selected explicit positive caps are bounded by receiver hard limits:
per-command wall clock, whole preparation wall clock, stdout and stderr bytes.
Defaults are 30 seconds per command, 180 seconds for the preparation budget and
1 MiB per output stream; hard ranges are 50–120,000 ms, 50–600,000 ms and
1,024–1,048,576 bytes respectively. The whole timer starts before snapshots;
each command receives at most the remaining budget. Bounded filesystem reads,
hashing and fsync use the trusted local OS and are not forcibly interrupted by
that child-process timer; this is not a hard real-time bound on host I/O.
The default limits are recorded before any child command. No shell string,
implicit SDK environment fallback, device serial or arbitrary stage argv is
accepted. The only phonefarm commands are gray-artifact adapt-storage,
verify-adaptation and the explicitly requested build capability check.

## Frozen inputs and executable scope

Read the candidate once through a bounded regular-file snapshot that rejects
symlinks along its full path. The exact raw candidate JSON is retained. Freeze
the actual running GOL executable and explicitly selected phonefarm executable
into fresh owner-only files; hash the bytes that those private copies execute.
Freeze limits, whole budget and requested build mode before spawning. The
candidate compilation receipt binds exact source, actual Naga SPIR-V, current
gate/track/limits, executable and embedded build/compiler inputs as in PR31.

The host is trusted. Executable hash equality is local integrity, not remote
attestation, a sandbox, dependency authenticity or proof that a caller-selected
program has no other network/device side effects. The driver supplies no device
commands and does not grant qualification based on a tool's self-declaration.

The GOL private copy executes the real candidate-artifact compile command,
followed by verify with the identity obtained from the compile receipt. Each
receipt must have the expected schema, a passing static gate and all proof flags
false. Failed/rejected compilation ends preparation and never invokes PF.

## Adaptation binding

Execute the actual frozen PF copy, with structured argv:

1. gray-artifact adapt-storage on GOL's frozen original.comp and original.spv.
2. gray-artifact verify-adaptation on the newly returned artifact directory.

Both commands require successful bounded process completion. Do not derive
success only from stdout text or a directory name. Resolve the returned artifact
under the new private adaptation output directory. Accept only the expected
flat five-file adaptation set, bounded regular files, descriptor-derived ID,
hash/size consistency and the verifier's expected offline result. The adapter's
original source and original SPIR-V must be byte-for-byte identical to GOL's
exported bytes. Bind original/adapted hashes, adapter descriptor/manifest hashes,
GOL frozen candidate identity and both executable identities in the final
receipt. Retain PF's caller_supplied_unverified association: the driver adds a
separate local compiler replay binding and cannot turn it into authenticated
attestation or semantic equivalence.

FrameGen and unsupported SR operations are not silently replaced by a supported
shader or assigned a looser gate. A PF adapter refusal is a retained negative
result. No fixed shader fallback exists.

## Journal, output and bounded execution

The session directory must be new, owner-only and reject symlink ancestors;
existing directories are never overwritten. Each child stage is sequential.
Durably publish Pending then InFlight before launching, with frozen argv/tool,
input identities and budgets. Persist stdout/stderr and real exit/signal/elapsed
result, then the stage outcome by atomic checkpoint replacement and directory
fsync. Completed is permitted only after validating the current artifact bytes
and process result. Missing receipts, invalid state or interrupted InFlight are
UNKNOWN, not success. They never automatically retry or resume a side effect.
Inspect is offline and cannot launch a command or upgrade an unknown result.
The fixed shell bootstrap waits for a permit: the journal records the actual
PID/process group durably before the permit allows the frozen binary to execute.
An interruption before that permit cannot be resumed as a pending command.

All controlled child commands get a fresh process group. Drain stdout/stderr
without an unbounded reader-thread join. Enforce per-stream output limits,
per-stage and remaining whole wall budget. On timeout, overflow or unknown child
status, terminate the controlled group, reap the direct child under a bounded
cleanup policy, and record uncertainty if absence cannot be established.
Closed pipes and a successful launcher exit are insufficient if the group is
still live or unknown. Deliberately detached processes are outside this trusted
process-group scope; the receipt does not claim a system sandbox.

## Native build gate

The real PF builder requires an explicitly installed Android NDK. An explicit
missing SDK request first records the caller path observation, then runs the
actual build command against the frozen adaptation and a fresh owner-only empty
SDK directory inside this session, with explicit --ndk and timeout. The actual
argv is frozen. This controlled missing-tools probe avoids reopening the mutable
caller path after a NotFound check; it does not claim to inspect an installed
caller SDK. It retains the real missing-tool refusal, and
records BLOCKED with build/device execution not accepted. It never installs an
SDK, substitutes OpenHarmony tools or uses a fake compiler as a native success.

The follow-up [native preparation contract](SPEC_CANDIDATE_NATIVE_PREPARE.md) consumes PF24's explicit inherited-group interface. Present SDK execution requires a bounded whole regular-file SDK snapshot and explicit four-layer-input expectation root, then outer group retirement and a second native verification stage. Unsupported SDK layouts remain BLOCKED. A successful controlled fake SDK is synthetic host-mechanism evidence; real NDK/Android/GPU validation remains not_run and every deploy/proof/promotion flag remains false.

## Acceptance and remaining work

`offline_compilation_adapter_complete` records completion of the four validated
compiler/adapter stages. `preparation_chain_complete` is true only when no
requested stage is blocked or not run: without a build request the result is
`PREPARED_OFFLINE`; a requested missing or unsupported SDK yields `BLOCKED` and false
for the complete preparation chain. These fields do not grant any proof flag.

Run actual local GOL compilation and replay against the actual PF adapter and
verifier. Archive exact argv, exit codes, tool hashes, stage journal and matched
source/SPIR-V/descriptor identities. Run the actual missing-SDK builder refusal.
Fake programs test timeout, output cap, lingering descendants, invalid receipts,
identity mismatches, gate failure ordering and interrupted journals. Those
mechanism tests are synthetic and never award real-source or candidate gates.

Run full Rust tests and strict clippy. A compatible bounded native preparation can produce `PREPARED_OFFLINE_NATIVE`; the complete driver still needs real NDK acceptance, approved layer identity, GPU capabilities and ownership,
device lease/recovery, controlled real measurements, independent paired quality,
qualification and heldout confirmation. Offline PREPARED does not satisfy them.
