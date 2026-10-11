# One deterministic system case → receiver confirmation

Draft implementation scope: one declared sysparam candidate on a continuously
running refbench `bw_pingpong` carrier. No model calls, native shader injection,
APK installation, app launch, charge control or automatic device recovery.
References: [bridge #36](https://github.com/HGamey/game_opt_loop/issues/36), [system #14](https://github.com/HGamey/.github/issues/14),
[case #18](https://github.com/HGamey/.github/issues/18), GOL confirmation PR29 and
phonefarm confirmation-driver PR25 (lease PR19 and observation PR22).

## Inputs and authority

`case plan --profile P --out NEW` is entirely offline. A strict versioned profile
declares an explicit transport program/target, bounded probe and workload files,
one control ID/value, capture/lead/pair counts, named refbench scene/intensity/frame
configuration, APK SHA/source commit, and host stage/whole/output budgets. The
carrier declaration is frozen caller context, not proof of actual launch or
installed bytes. The device owner must install/start/verify the declared carrier
and its actual submit comm; no launcher or early self-exit may substitute.

The existing local sysparam whitelist/plan functions deterministically produce
the candidate. Caller probe metadata is a proposal filter, not qualified control
evidence. Unknown/unchanged/out-of-domain candidates are refused. Phonefarm
independently challenges only the selected reversible graph and validates the
candidate against actual receipts. No old `probe_sysparam.sh` fallback exists.
Frozen outputs are exact profile/probe/workload/candidate/request bytes and
hashes. Paths are explicit; bounded regular-file reads reject symlinks and
special files. Tool aliases must be resolved by the caller to regular paths.

`case run --case DIR --phonefarm-bin PF --execute` snapshots the actual GOL and
PF executable bytes privately and records their hashes. Its single-use marker
is durable before any receiver execution. It invokes exactly four stages:

1. PF `confirmation-driver prepare --request ... --out receiver --execute`.
2. GOL `confirm freeze --plan receiver/plan.json --out frozen.json`.
3. PF `confirmation-driver collect --session receiver --frozen ... --execute`.
4. GOL `confirm judge --frozen ... --samples receiver/samples.json --out report.json`.

Without `--execute`, no tool is invoked. No nested device lease is acquired:
the receiver owns each prepare/collect lease and all remote journals. Prepare
must report `PREPARED`, collect `COLLECTED_OBSERVATIONS`, with live-device
provenance for the real route. Fake/synthetic receipts cannot be relabelled or
advanced. Stage stdout/stderr are preserved as exact bounded bytes. Expected
output paths are fixed by the case, never taken from arbitrary receipt paths.

PF owns the policy: three admitted baseline runs, at least five counterbalanced
independent pairs, RenderEfficiency required frame cadence proxy/fps/battery
power, primary frame-p95 improvement >=1%, fps/power maximum regression 1%,
family alpha .05. GOL cannot change it or downgrade missing power to another
goal. Battery units, whole-window discharge, actual thermal/identity/control/
workload/restore evidence remain receiver gates. No assumptions about µA are
made by this bridge.

Optional `temperature_profile_id` is forwarded unchanged to the receiver's
registered preset resolver (`legacy_cold_v1` by default, or `loaded_steady_v1`
only when that producer contract is actually supported). No caller numeric
temperature limits or policy paths are accepted. Unknown IDs/unsupported old
receivers block. Actual sensor-domain/profile/policy hashes belong to PF's
sampling contract and conditions; a cap-only loaded control receipt without
`plan.json`/admission cannot advance into confirmation. Historical cold-policy
data is not relabelled as loaded steady-state data.

## Single-use execution and interruption

The stage intent is synced before spawn; PID/PGID, exact argv, executable hashes,
raw output, exit/signal, elapsed time and host group status are saved. Each stage
and the whole chain have frozen finite wall/output budgets. Success requires the
host process group absent; a closed-pipe live descendant is not success.
Spawn failure before any child is a known local failure. Timeout, signal, orphan,
output overflow, crash or incomplete stage is UNKNOWN. The bridge stops and does
not invoke later stages, replace raw files, resume an InFlight stage, reuse heldout
IDs or auto-retry. Run is create-only even after a known failure.

Killing/reaping a host group is **not** remote-worker/device-idle proof. UNKNOWN
and PF BLOCKED preserve recovery guidance: inspect the exact PF receiver session
and persistent lease barrier, explicitly run PF `confirmation-driver reconcile`
under the device owner where the receiver supports it, then start a new case.
An interrupted local freeze/judge may be replayed manually offline, but `case run`
never repeats any stage. `case inspect` is read-only and never clears markers.

## Output and limits

The final case report binds exact receiver replies, frozen plan, samples and
confirmation report; negative and UNKNOWN outcomes are retained. Completed
collection is not statistical PASS. A saved INCONCLUSIVE/BLOCKED judge report is
an auditable negative result, not a transport retry. Both `promotion_allowed`
and `real_game_optimization_proven` remain false even on statistical PASS.
Hash matching proves integrity conditional on the trusted local executables;
it does not authenticate hardware acquisition or tool origin.

Final archive hashing and publication also check the whole budget. An overrun
during synced publication leaves an additional immutable UNKNOWN marker;
inspection shows that error instead of treating the earlier result as completed.
Inspection applies the same finite receiver-policy/path/source and
stdout/file/exit checks to recorded stages. Rehashing a contradictory report and
ledger cannot make those recorded bytes self-consistent.

The case proves, at most, this whitebox system-parameter experiment and recorded
receiver metrics. KGSL is a GPU-submit cadence proxy, not displayed FPS. It does
not prove commercial-game optimization, SR/frame generation, GPU-layer semantic
equivalence, continuous image quality or train/deploy-loop completion.

Acceptance: actual CLI planning, strict unknown fields/control/workload tests,
actual PF fake-route rejection, positive/negative four-stage host mechanism,
timeout/spam/closed-pipe descendants, create-only interruption/inspect, input
tampering and full cargo test + strict Clippy. Mechanism fixtures are synthetic;
real device acceptance is separately performed only by the assigned owner.

## Offline acceptance 2026-10-12

355 full cargo tests passed, including nine actual single-case CLI tests; strict
Clippy `--offline --all-targets -- -D warnings` passed. The actual accepted
phonefarm binary (built source f88188c, SHA5abd653…) with a privately isolated
fake transport reported `SIMULATED_BLOCKED`; the bridge saved that refusal and
did not invoke freeze or collection. Four-stage mechanism fixtures use synthetic
host stubs and retain false proof/promotion flags. Independent review found and
closed final-publication deadline and rehashed stdout/report consistency gaps.
No physical device, model API, real sampling, statistical benefit or loaded
temperature whole-window acceptance was performed by these gates.

Raw logs and host fixture registry are local-only in
`phonefarm/tasks/oneplus_live_20261012/case_bridge_offline/`; SDK, device frames
and duplicated tool executables are not committed. Actual device acceptance
requires the owner's independently qualified control, validated power units,
continuous declared carrier and a compatible PF receiver.
