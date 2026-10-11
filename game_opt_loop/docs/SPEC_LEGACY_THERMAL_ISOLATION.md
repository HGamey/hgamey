# Legacy confirmation receiver thermal isolation

Base: verified draft GOL38 head `9c6b393bc5793b7190f28aa0c6a8688bc15f5ac9`.
This specification precedes the repair. Scope is the `temperature_contract=None`
confirmation receiver route; it does not change frozen-plan serialization, thermal
thresholds, statistical rules, PF acquisition, or the registered loaded contract.

## Historical compatibility

A legacy observation retains `phonefarm/confirmation_observation@1`, sample
qualification `@1`, and all seven original boolean gates including `cold_verified`.
Actual historical PF PR22 source `516b7dd` omitted `qualification.version` and
`qualification.policy` in the single observation envelope, while its optional
`baseline_admission_qualification` included an explicit version-one policy.
That omission remains compatible. If envelope qualification version is present,
it must be JSON integer 1. If a policy is present, it must be an object with
integer version 1. An admission qualification, when present, must retain its
explicit integer version-one report and policy. Null/string/float versions and
explicit unknown or loaded versions refuse; missing is allowed only for the
historically omitted envelope fields.

## No mixed thermal or capability contract

Inspect the finite wire locations: observation root, qualification, optional
qualification policy, sample qualification, sample gates, confirmation binding,
raw artifact bindings, and optional baseline admission qualification/policy.
At those locations a `thermal_` or `temperature_` field, or explicit thermal,
temperature, profile identity or purpose field, refuses even when null or false.
This includes loaded gates, thermal raw hashes, thermal contracts and
`capability_absolute_v1` purpose/profile declarations. The existing cold numeric
policy fields `temp_cap_c` and `cool_c` are unchanged. Do not recursively inspect
free text, checks, frame evidence or arbitrary archived raw extensions: those
may contain legitimate historical diagnostic names. This finite contract checks
known wire contradictions, not authentication of a caller's acquisition claim.

Missing and contradictory new fields cannot be excused by `cold_verified=true`,
by old schema labels, or by recomputing an outer report hash. A cap-only control
receipt does not become a confirmation observation by relabeling it.

## Acceptance and limits

Actual CLI freeze/judge tests retain the pre-extension frozen fingerprint and
positive historical omitted/envelope-version-one forms, including historical
admission policy. Countercases mutate declared synthetic receiver fixtures,
recompute report SHA and sample references, and require BLOCKED with false
promotion/real-game flags. Cover mixed integer/string/null versions, cap/loaded
profile declarations, purposes, thermal contract/gates/raw bindings and admission
policy mixing. These are offline consumer checks, not real raw PF validation,
thermal safety, physical control, game attribution or an optimization result.

Run the complete source test suite and strict all-target/all-feature Clippy in an
independent target. Preserve accepted binaries/targets and all prior evidence.

## Offline acceptance 2026-10-12

Complete `cargo test --offline` passed 364 tests in this independent worktree and
its own target (293 unit + 2 + 19 + 11 + 6 + 5 + 11 + 17 integration/CLI).
Strict `cargo clippy --offline --all-targets --all-features -- -D warnings`
passed. The two additional CLI tests contain historical omitted/explicit-v1
positive controls and 31 independently rehashed mixed-field countercases.
All countercases BLOCKED; valid declared historical samples keep a metrics-only
PASS with false real-game/promotion flags. Actual CLI stdout/stderr/exit receipts,
input reports and rehashed samples were retained locally through the explicit
`LOADED_CONFIRMATION_TEST_ARTIFACT_DIR` test option. Old accepted binaries,
PF worktrees, raw device evidence and the historical frozen fingerprint were
unchanged. Device/model API/GPU/physical thermal or control validation calls: 0.
