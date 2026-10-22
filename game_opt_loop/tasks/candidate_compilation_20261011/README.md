# Offline interface evidence

- `cargo-test.log`: 340 passing tests (including 10 actual candidate-artifact CLI cases).
- `clippy.log`: `cargo clippy --offline --locked --all-targets -- -D warnings`, exit 0.
- `cross-repository-summary.json`: actual argv, exit status and executable hashes for
  GOL compile/verify and phonefarm PR21 adapt-storage/verify-adaptation. All four
  commands exited 0. The original source/SPIR-V hashes match the exported compiler
  contract. No NDK, GPU, device, model or network API was invoked by these commands.

The captured raw SPIR-V files are actual Naga/adapter output, not fake compiler
receipts. The shader input is the repository SR template and does not represent
real-game capture or optimization. Adapter association remains
`caller_supplied_unverified`; GOL proof/promotion/deployable flags all remain false.

The binary hashes bind this recorded local run; binaries are not checked in and
the log is not a trusted remote attestation. Rebuilding or integrating changes
the executing binary and requires a fresh compile/verify receipt. Historical
artifact identity must not be silently relabeled as verified by a new binary.
