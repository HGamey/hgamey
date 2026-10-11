# Loaded temperature confirmation consumer

Refs system #14/#18, GOL independent confirmation PR29 and single-case PR37.
This increment consumes a new PF loaded-steady observation contract; it does
not change receiver temperature ceilings, raw acquisition or vendor protection.
SPEC precedes source implementation. The producer wire fields below were agreed with the PF qualification/observation
owner before source implementation.

## Authority and compatibility

Existing cold observations keep their version-one schema, cold gate, fixed
conditions and statistical rules. Their registered-plan serialization and SHA
fingerprints must remain byte compatible. Loaded observations use an explicit
new receiver contract, independently reconstructed typed raw sensor windows,
baseline-only reference bands and frozen policy/profile/reference hashes. No
legacy cold receipt, cap-only control probe or candidate window may be silently
upgraded to this contract.

GOL archives and checks the receiver's identities and qualification declaration.
It does not recompute thermal caps, choose units, accept caller numeric limits,
read hardware or infer a loaded valid window from a single point. Frozen
conditions, profile and baseline-only reference identities must match every
observation; unknown versions, missing/invalid hashes, mixed modes and altered
references block. Hash matching checks integrity, not acquisition authenticity.

The stage remains experiment/eligible=false; valid_observation means one valid
sample, never statistical PASS. Device/root/control/raw-window/workload/restore/
baseline gates remain required. A new typed thermal-window gate replaces only
the cold gate within the explicit loaded version. Metric sources, battery unit
validation/discharge window, independent run windows, balanced pairs and negative
results are unchanged. Real-game/proof/promotion flags remain false.

## Intended acceptance

Tests must prove version-one frozen SHA compatibility, actual CLI freeze/judge
consumption of complete declared loaded observations, refusal of malformed/
changed profile/reference identities, missing raw-window gates, synthetic origin,
cap-only reports and old/new mixing. Host fixtures are explicitly synthetic
mechanism checks even when they contain receiver-shaped declarations. Physical
whole-window loaded acceptance is separate and performed only by the device
owner after the PF producer/receiver combined gate passes.

## Frozen wire contract

Append optional `temperature_contract` at the end of GOL `Plan`, after metrics;
`None` is omitted from serialization. The exact typed fields and order are
`profile_id`, `receiver_policy_version`, `profile_sha256`, `reference_sha256`,
`thermal_contract_sha256`. Only `loaded_steady_v1`, receiver policy version 2
and lowercase 64-hex digests are supported; unknown fields refuse. Numeric
thresholds are not members of this GOL contract. The PF plan copies these from
its frozen conditions@2/thermal contract, before heldout sampling.

A loaded report must have `phonefarm/confirmation_observation@2` and
`phonefarm/confirmation_sample_qualification@2`. Its
`confirmation_binding.thermal_contract` must strictly parse and exactly match
the registered plan. All six retained root/control/trace/workload/restore/
baseline gates plus `thermal_profile_verified` and `thermal_window_verified`
must be boolean true. A cold boolean is not a replacement. A version-one
observation requires no registered temperature contract and its original cold
gate; mixed schemas/contracts refuse.

Loaded `confirmation_binding.raw_artifacts` additionally requires lowercase SHA
for `thermal_admission_sha256`, `thermal_window_sha256` and
`thermal_sampler_exit_sha256`. Admission/window hashes join the existing
trace/ENV/exploration global duplicate detector; the same pre-arm admission or whole window
cannot be reused as a distinct arm. The baseline-only frozen reference/warmup
is intentionally shared and its common hash is not an arm identity. The PF plan
includes all exploration baseline admission/window hashes in the exclusion set. Exit bytes may legitimately be the same bare
status across runs, so their valid hash is checked without claiming uniqueness.
Raw thermal parsing, reference reconstruction and chronology remain the PF
receiver's responsibility; this consumer does not authenticate acquisition.

Loaded qualification also requires report `version=2` and policy `version=2`,
matching `temperature_profile_id`, `temperature_profile_sha256` and
`thermal_reference_sha256`, with explicitly null legacy `temp_cap_c`/`cool_c`.
Missing values, an old qualification report or duplicated identity disagreement
block; GOL does not validate or rewrite the rest of PF's policy numerics.

## Offline acceptance 2026-10-12

360 full cargo tests passed, including four new actual loaded-consumer CLI tests
and the historical frozen-fingerprint unit regression. Strict Clippy
`--offline --all-targets -- -D warnings` passed. The CLI checks complete declared
v1/v2 observations, shared bare sampler exit status, changed/unknown profile
contract, numeric caller caps, old/new mixing, boolean gate errors, synthetic/
cap-only/refusal reports and reused/exploration thermal series. Inputs are
manually declared synthetic host fixtures, not actual PF raw thermal validation.

The historical fixture was registered by the pre-extension actual CLI from the
placeholder example plan; its prior fingerprint is retained unedited. No
physical device, model API or loaded whole-window PF receiver was executed here.
The PF producer/independent reconstruction and true device qualification are
separate acceptance. Independent source review found no new core blocker and
confirmed the baseline exploration exclusion obligation above. Logs and raw
source hashes remain local in `phonefarm/tasks/oneplus_live_20261012/
loaded_consumer_offline/`; proof/promotion stay false.

## Explicit first-case goal

The single-case profile may explicitly select `goal=render_cadence` or
`goal=render_efficiency`. Missing retains the historical receiver default
RenderEfficiency; no data-dependent fallback changes a requested goal. Unknown
goals refuse before execution. Cadence freezes exactly frame-p95 and fps rules;
efficiency additionally requires power. The receiver conditions' goal and
required_metrics must match any explicit request, and both routes retain the
same fixed improvement/regression/alpha/baseline/pair policy. Current/charging/
power may remain raw diagnostics in cadence mode; no energy benefit, displayed
FPS, present quality or gameplay proof follows from that mode.

Explicit-goal increment acceptance: 362 full tests passed, including eleven
single-case CLI tests; strict all-targets/all-features Clippy passed. Positive
cadence mechanism freezes two exact metrics and records a negative result with
false proof/promotion. Unknown and mismatched receiver goals refuse without
falling back to another goal. The earlier c7c90c6/360-test evidence and binary
remain unchanged. This source acceptance does not claim new native/GPU/device
validation or substitute for the separate integrated d4e5208/441-test gate.
