# Independent candidate native preparation review

Status: no remaining publication blocker observed within the stated offline scope. Final reviewed commit is `70141d57dfe6238afa219d01604e191162353967`; production prepare/native/SPEC bytes exactly match the independently exercised frozen GOL binary and reviewed snapshots. `committed-source-verification.json` records this check. Later test fixture timing changes were inspected only, without repeating C's suite. Review is read-only with respect to C's source/target. Evidence and controlled SDK copies are owned by this review.

## Confirmed before counterexample

Frozen GOL `bf1a2290296b5dc04e7ad1759e1fc3aadc19b6801c383556e7325f2d9ffee482`, actual PF24 `db1721a7779741367f974b27d57e398b1392cd08fb4c16972e95015a5fe65005`, explicitly synthetic regular SDK. Actual GOL/PF children completed and the unmodified session inspected successfully. All proof/deployability/Android-codegen flags remained false.

Only the nested `build_receipt.reused` values in journal native stages and prepared native binding were changed from `false` to `true`; the outer prepared receipt was rehashed. Actual `native_build.stdout.log` bytes and its journal identity remained unchanged. Before inspector returned exit 0 and accepted the contradictory receipt. Original journal/prepared bytes were restored and valid inspect again returned 0.

Raw command/exit/stream identities and output are under `before-four-input/`; `tamper-result.json` records the exact accepted contradiction. `run_review.py` is the independent actual-CLI runner. Initial source/SPEC snapshots are preserved separately. This is an integrity contradiction, not a claim that local hashes authenticate the host.

## Static findings sent to implementation owner

1. Whole deadline was checked before commands, but post-native SDK/artifact rehash and successful publication did not check it. Final large regular-file reads could cross the whole budget and still publish success.
2. SDK aggregate precheck used pathname metadata before the opened fd snapshot; actual successful snapshot byte count was added without a second checked aggregate cap. A growth between those observations could exceed the total cap.
3. Native descriptor lacked finite target/flags/full origin policy checks, despite receiver claims of binding current artifact bytes.

These are static findings; no large-file race or whole-deadline runtime counterexample is claimed. Corrected source now checks opened-fd metadata plus actual checked aggregate bytes, streams SDK/native hashes with the whole timer, checks final publication, and freezes the finite descriptor policy. C owns the corresponding regression/full-suite execution; this review does not repeat that suite.

## Corrected independent actual-CLI gates

Frozen corrected GOL `0b6a45e5453d6d73f9184bf67d29aefb5de14c8041d8cad8c5720f51370943e6`, root integrated PF `2065e6dd948109f00be3e860b792267e6275f812523f24a4275f45e2a78219de`. `after-five-input/` completed all six stages with positive group-absence receipts, exact `ownership5` inputs including `dispatch_ownership.h`, and eight actual helper observations all matching the owned native build PGID and private SDK. Valid and restored inspectors returned 0. The same rehashed `reused` contradiction returned exit 1 with `native build nested receipt contradicts actual stdout`.

`after-five-closed-pipes/` uses an actual helper which forks a child, closes both output descriptors and sleeps 60 seconds. Native execution remained UNKNOWN, verification remained PENDING and inspection did not accept it. No late writer appeared; a subsequent read-only signal-0 observation of the exact owned native group returned ESRCH (`owned-group-final-observation.json`). The transient recorded `group_absent:false` remains intact in its UNKNOWN journal; this review does not upgrade that session.

`independent-verification.json` contains compact observations and executable identities; raw argv, exit codes, stdout/stderr and receipts remain beside the controlled session artifacts. Initial/corrected source and SPEC snapshots retain exact SHA-256 identities. Root integration source/layer files and all shared targets were unchanged.

`after-four-input/` also completed the corrected GOL against original PF24 with exact `legacy4` input identity, six completed/absent groups and eight helper observations. The same forged nested receipt was rejected, with valid/restored inspect controls accepted. Both finite source contracts are covered independently.

No real NDK, GPU, device, model, training or external API calls occur. Regular-only bounded SDK support is a host mechanism; common real-NDK layouts and actual Android compilation remain unaccepted.
