# Offline supervised native preparation evidence

Source implementation commit: `70141d57dfe6238afa219d01604e191162353967`, stacked on GOL #34. SPEC: `docs/SPEC_CANDIDATE_NATIVE_PREPARE.md`.

## What was actually executed

`actual/actual-cli-summary.json` records three actual local GOL/PF command chains: current resource-ownership PF with exact `ownership5`, original PF24 with exact `legacy4`, and actual missing-SDK refusal against a private empty SDK. Both positive cases complete six stages, with eight actual controlled helper observations in the native build PGID. The missing-SDK case is BLOCKED, has no completed native build and cannot claim preparation complete. Actual argv, exit, streams, journal, private SDK/source inventories and returned descriptor/manifest are retained.

The SDK is an explicitly synthetic mechanism fixture. Its Python helpers copy an existing SPIR-V fixture and emit a 64-byte ELF-looking placeholder plus fake export/dependency text. The placeholder is not a working Vulkan layer or verified Android binary. Actual NDK, GPU, device, model, training/API and SDK downloads: zero. All qualification, semantic, quality, deployment, real-game and promotion proof flags remain false.

The SDK snapshot supports bounded regular files/directories, not ordinary real-NDK symlink layouts. Toolchain authenticity, interpreter/system-library freezing, network isolation, deliberately detached tools and actual Android code generation are unaccepted. These are trusted local execution records, not remote attestation.

## Integrity and process gates

The tests cover current/legacy exact layer sets and actual descriptor source bytes, timeout/full stderr, live descendants with closed pipes, original tool/SDK mutation after private copying, wrong declared layer source, private snapshot mutation and rehashed receipt contradictions. Unit checks cover input presence/extras, cumulative actual SDK byte limits, expired rehash/stream deadlines and finite descriptor policy. Existing thirteen preparation CLI tests retain four-stage gate/refusal/overflow/unknown/no-retry coverage.

`independent-review/` copies A's independent before/after review. The initial inspector accepted a rehashed nested `reused` contradiction despite unchanged actual stdout. The corrected inspector refuses it, and both corrected four/five input controls pass. A also records the actual owned host group returning ESRCH after the closed-pipe child counterexample. The UNKNOWN journal remains UNKNOWN; it is not promoted by the later absence observation.

## Honest test history

`preliminary-full-failure.txt` and `preliminary-eight-failure.txt` retain failed test attempts. The original two-second descendant could exit before the outer observer, and a one-second helper budget sometimes killed Python before the marker was written under parallel Mac load. These were fixture timing failures; safe UNKNOWN behavior was retained. The descendant now sleeps 60 seconds. The helper counterexamples explicitly freeze a ten-second helper/sixty-second stage test budget, assert actual entry, and measure the recorded native stage bound. Production defaults and safety policy were not relaxed. `helper-final.txt` shows the targeted correction passing.

Final full/strict-clippy results are in `verification.json` and the accompanying final log files. Archives intentionally omit the large private GOL/PF executable copies and duplicate known repository C++/header source bytes. `archival-omissions.json` retains each omitted source identity; the actual native descriptor and frozen source table retain the same hashes. Their exact hashes are recorded, and the live owned session path is retained; these archives are evidence, not portable sessions on which to bypass normal inspect validation. Original command logs and SPIR-V/native placeholder bytes are preserved where useful and remain synthetic-scoped.
