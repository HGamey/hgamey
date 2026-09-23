---
name: game_opt_loop
description: Optimize one game on one real Android/OpenHarmony phone, end to end. Use when asked to make a game run faster, smoother, or cooler on a device — `game_opt_loop optimize --game <name>` probes what the device actually allows (root, charge suspend, Vulkan layer injection, white-box carrier), opens whichever of the three change surfaces are available (system parameters / render-flow rewrites / image operators), has an LLM propose candidates for each, gets every candidate measured and judged on the real device by phonefarm, and writes one unified archive plus report.json + report.md. Also covers `--plan` (read-only survey, no device writes), the device lock used to share one phone between agents, and the known traps on Genshin Impact 7.1.0.
license: MIT
metadata:
  version: 1.0
  source-repo: github.com/HGamey/game_opt_loop
  requires: phonefarm (github.com/BH3GEI/phonefarm), optionally refbench
---

# game_opt_loop

`game_opt_loop` answers one question about one phone and one game:

> **What can actually be changed here to make this game run better — and does it?**

It is an orchestrator. It decides *what to try*; it never touches the device
itself. Every write, every measurement, every statistical verdict and every
rollback happens in [`phonefarm`](https://github.com/BH3GEI/phonefarm).

## The three change surfaces

A mobile game's cost can be attacked from three places. `optimize` opens
whichever of them the device allows, and says plainly why it skipped the rest.

| | Surface | What it changes | Needs |
|---|---|---|---|
| ③ | **System parameters** | CPU/GPU/bus frequency floors and ceilings, governors, refresh rate | root |
| ② | **Render-flow rewrites** | LoadOp/StoreOp, attachment precision, offscreen render scale — via a Vulkan layer | root + the target app installed (layer `.so` goes in its own lib dir) |
| ① | **Image operators** | A compute shader spliced into the pipeline (super-resolution, frame generation, post-FX simplification) | a white-box carrier, **or** a black-box game you can inject a layer into |

They run in the order ③ → ② → ①. That is not arbitrary: system parameters do
not touch the image (so image quality need not be measured — cheapest and
safest), render-flow rewrites do, and image operators are the most expensive
(compile, inject, quality pass). Budget is handed out in that order, split by
how many layers are still unplanned, with a floor of one candidate each — so a
tight budget runs the cheap layers and drops the expensive ones rather than
starving all of them.

**`optimize` drives ③ and ② itself.** ① has its own entry point in this repo
(`game_opt_loop --track sr --carrier refbench …` — template → mutation → safety
gate → Pareto archive); `optimize` surveys it, puts it in the plan and tells you
how to run it, but does not drive it and does not take budget for it.

**If none of the three can be opened, `optimize` measures and writes down why.**
That is a result, not a failure.

## Repository layout

Three sibling repos. This is not a preference — the default paths
(`../phonefarm/phonefarm`, `../refbench`, `../phonefarm/loop_v1/carriers/`) are
baked into the defaults, and carrier discovery walks `..`.

```
~/projects/
  game_opt_loop/     ← this repo. Private, under the HGamey org.
  phonefarm/         ← the device automation + measurement base. Public (BH3GEI).
  refbench/          ← white-box Vulkan render target. Optional but valuable.
```

- **`game_opt_loop` is private.** Cloning it needs membership in the **HGamey**
  GitHub org. If `git clone git@github.com:HGamey/game_opt_loop.git` returns
  "Repository not found", that is an access problem, not a typo — ask the owner
  for org access rather than hunting for a public mirror.
- `phonefarm` is public: `git clone git@github.com:BH3GEI/phonefarm.git`.
- `refbench` gives you a target whose source you control, with a known-answer
  quality ground truth. Without it, a black-box game can still be optimized —
  just through layer injection only.

Build:

```bash
cd ~/projects/phonefarm/src && cargo build --release \
  && cp target/release/phonefarm .. && cd .. && codesign --force --sign - ./phonefarm
cd ~/projects/game_opt_loop && cargo build --release
```

> On Apple Silicon the `codesign` step is not optional. `[profile.release]
> strip = true` invalidates the ad-hoc signature, and an invalidated binary does
> not error — it **hangs silently in `_dyld_start`** (alive, 0% CPU, no output),
> which looks exactly like a hung device.

## Using it

```bash
cd ~/projects/game_opt_loop

# Read-only survey: what could be done here? Writes nothing to the device.
cargo run --release -- optimize --game 原神 --plan

# The real thing, on a named device, with a time budget.
cargo run --release -- optimize --game 原神 --device 91253241019A --budget 2h

# A white-box carrier instead of a third-party game.
cargo run --release -- optimize --game refbench --budget 45m

# Something not in the built-in table.
cargo run --release -- optimize --game com.example.game --package com.example.game --plan
```

| Flag | Meaning |
| :--- | :--- |
| `--game <name>` | `原神` / `genshin` / `refbench` / a carrier directory name / a package name |
| `--device <serial>` | Target device. Omit **only** when exactly one is online — with several online it refuses rather than guesses |
| `--package <pkg>` | Package name, when the game name is not recognized. Overrides the built-in table |
| `--budget <45m\|2h\|900s>` | Real-device time budget. Bare numbers are minutes. Default **30m**, deliberately small — enough for one candidate on the cheapest layer |
| `--plan` | Survey + plan only. **Not a single byte is written to the device** |
| `--out <dir>` | Artifacts: `caps.json`, `report.json`, `report.md`, `requests/`. Default `runs/optimize` |
| `--no-llm` | Skip the model, use only the local deterministic mutator |
| `--sysparam-probe <probe.txt>` | Feed an already-collected system-parameter probe (see "Current limits") |

Artifacts:

- **`caps.json`** — what the device allows, and *how each conclusion was reached*
  (the exact command and what it printed). A `root: true` with no provenance is
  worthless when something goes wrong later.
- **`report.json`** / **`report.md`** — per layer: used or not and why; every
  candidate with its numbers; what was kept and what was dropped, with reasons;
  and the frozen test conditions.

## What `--plan` is for

`--plan` is safe to run on a shared phone, at any time, mid-experiment. It runs
one read-only shell command (`getprop` / `cat` / `pm path` / `id`) and stops.
A unit test asserts the generated probe script contains no `setprop`, no
`settings put`, no redirect-to-file, no `am start` — that test is the guarantee.

Read the output top down:

1. **Device line** — root / charge suspend / layer mounting / target installed.
2. **Layers to run** — route, generations × candidates, rounds per arm, estimate.
3. **Layers not to run** — the reason, and exactly what is missing.
4. **Frozen test conditions** — cool-down target, temperature cap, fan, in-game
   frame-rate setting.
5. **Known traps** for that target.

## Sharing one phone: the device lock

Several agents work against one phone. Everything that touches the device —
including `--plan` — goes through the lock at `/private/tmp/claude-501/devlock`.

```bash
/private/tmp/claude-501/devlock status               # who holds it, who is queued
/private/tmp/claude-501/devlock run <label> -- <cmd> # take it, run, release
```

`run` acquires, heartbeats every 60s in the background, runs the command, and
releases. **If the lock is held it returns 1 and puts your label at the end of
the queue** — that is the normal case, not an error. Wait for your turn and
re-issue; do not steal the lock and do not busy-poll it in a tight loop. A lock
whose heartbeat is older than 600s is considered abandoned and can be taken over
(the takeover is logged).

```bash
/private/tmp/claude-501/devlock run gol -- \
  cargo run --release -- optimize --game 原神 --plan
```

## Known traps

These cost real device hours to find. Every one of them also prints at runtime,
but read them before you start.

### Genshin Impact

- **Gamepad input is dead as of 7.1.0.** The right stick no longer turns the
  camera. A gamepad workload therefore *looks* like it ran — full duration, no
  errors — while the camera never moved, so both A and B arms measure a static
  scene. **Use the touch workload**
  (`phonefarm/knobs/gray/workload_spin_touch_v1.json`).
- **Cold start lands on the login page.** The workload script runs to completion
  there too, and you measure the login screen. Confirm the game is already in
  open-world exploration state before starting. If you automate getting in,
  click the login button and nothing else.
- **Never press BACK or A in a workload.** In Genshin, A is interact/confirm —
  one chest or one dialogue near the character destroys repeatability. The
  workload turns the camera and does nothing else; the character's position is
  guaranteed unchanged by construction (the left stick is never touched).
- **Day/night cycle is the biggest source of non-repeatability.** Measured:
  across dusk, five rounds scatter 5.35% and every round is hotter than the last
  (monotone ratio 1.00); inside a stable night window, 1.37%. Run inside a
  lighting-stable window. The in-game clock does not respond to `input swipe`,
  `input motionevent` or gamepad input — all three were tried.

### Measurement conditions

- **Power measured while charging is not power.** On the battery rail you read
  the net of charge and discharge; on the USB rail you read wall power including
  what is being pushed into the battery. `optimize` asks phonefarm to suspend
  charging first. On this hardware the node that actually works is
  `/sys/class/qcom-battery/charging_enabled` — the `power_supply` candidates all
  accept the write and change nothing. If charging cannot be suspended,
  `power_watt` comes back **`null`**, not a plausible-looking number.
- **Record the in-game frame-rate / quality setting.** Change it and every
  earlier baseline is void — and nothing in the data shows you that. It goes in
  `conditions.ingame_fps_setting`; the tool cannot read it yet, so a human fills
  it in.
- **Record the fan state.** With and without an external fan the same phone
  settles ten-plus degrees apart, so frame times and watts are not comparable
  across fan states. When unknown the report says `unknown` — it never guesses.
- **A cool-down target is not an absolute temperature.** After dozens of rounds
  the device simply does not return to a fixed low temperature; a constant
  threshold starves every candidate on cool-down timeout. Targets are derived
  from the temperature measured during the survey. Cool-down timeout does *not*
  void a candidate (ABBA alternation makes residual thermal drift hit both arms
  equally); the temperature **cap** is the separate safety gate that does.
- **"SoC temperature" means specific thermal zones, not the hottest one.** A
  phone exposes dozens of zones — battery, skin, charge port, modem. Taking the
  global maximum measured **105.0 °C** on NX809J where the SoC was actually
  **41.3 °C (`cpu-0-0`)**, and since the cool-down target is derived from that
  number it became 106 °C — a gate the device is always below, i.e. no gate at
  all, printed as a perfectly normal-looking line. Only `cpu-` / `cpullc` /
  `gpuss` zones count; `0` and `>=100 °C` are sentinels, not readings. The
  survey records **which zone** the number came from, because a wrong zone looks
  exactly like a right one.
- **Root-only paths need a root existence check.** `/sys/class/qcom-battery/`
  is not traversable by the shell uid, so a plain `[ -e ]` returns false for a
  node that is really there. That reads downstream as "charging cannot be
  suspended", and then every watt measured that session is worthless while the
  report's explanation looks entirely reasonable.
- **Thermal protection is never a knob.** Anything matching
  `thermal / trip_point / cooling / fan / tsens / bcl / throttl` is refused — not
  probed, not proposed, not applied. Turning off thermal protection buys pretty
  numbers with hardware safety. The host refuses it and the device side refuses
  it again, deliberately redundantly.

### Mounting a Vulkan layer hurts bystanders

`debug.vulkan.layers` is a **global** property. Apps started during the mount
window that do not have the layer `.so` in their own lib directory **fail to
start** — a vendor app (`cn.nubia.gameassist`) was observed crash-looping every
~2 seconds until the property was cleared. phonefarm clears the property as soon
as the layer loads, shrinking the window to seconds, but on a shared device
always tear down afterwards (`knobs/gray/enable_layer.sh off`).

## How verdicts are reached

`game_opt_loop` proposes; it never judges. Each candidate goes to
`phonefarm eval --request <json> --json`, which applies it, runs the workload
A/B alternating, tests significance, keeps or drops, restores, and reports.

- **A/B/A/B alternating, not A-then-B.** Run in blocks, the B arm spends its
  whole life on a hotter phone and the temperature difference is charged to the
  candidate.
- **The decision rule is frozen before any candidate data exists.** Kept = at
  least one metric significantly improved and no metric significantly regressed.
- **The two sides are deliberately asymmetric.** With K metrics, the "improved"
  side is Bonferroni-tightened to α/K while the "regressed" side stays at 0.05.
  Better to miss a real improvement than to let a real regression through.
- **Rounds are never traded for budget.** With 5 rounds per arm the smallest
  reachable two-sided p from an exact permutation test is 0.0079, which just
  clears the tightened 0.0167. At 4 rounds it is 0.0286 — unreachable. Quietly
  dropping to 4 rounds buys a full run of "nothing is significant" that reads
  exactly like "nothing worked." `optimize` drops a whole layer instead, and says so.
- **`ABORT` is not a negative sample.** A knob that did not fully apply, a run
  over the temperature cap, an end-of-run snapshot that does not match, a rewrite
  that never got bound — the measurement did not happen. It is neither evidence
  for nor against; it is never fed back to the proposer.
- **`null` is never 0.** An unmeasurable metric comes back `null` with a
  human-readable reason attached. A `0.000 W` looks like a number and ruins the
  comparison.

## Current limits

Stated plainly so you do not mistake a gap for a bug:

- **`phonefarm eval --request` is new.** Until the receiving end lands,
  `optimize` probes for it and reports honestly that layers ③ and ② cannot run
  — it does not fabricate a report. The image-operator path still works today
  through the existing `phonefarm gpu-op`.
- **The system-parameter whitelist needs a real-device probe** (write a value,
  read it back, confirm the kernel did not bounce it). That write belongs to
  phonefarm. Until that path lands, feed an already-collected probe with
  `--sysparam-probe <probe.txt>`.
- **Layer ① is planned but not driven by `optimize`.** The image-operator
  evolution loop has its own entry point in this repo:
  `game_opt_loop --track sr --carrier refbench --generations 3 --children 4`.
  It appears in the plan under "layers not to run", with that command spelled
  out, and it consumes no budget.
- **The render-flow knob catalog lags the layer on purpose.** Only
  `loadop_dont_care` has landed and been verified to bind on real hardware; the
  rest are declared in the contract and refused locally. Proposing a knob the
  layer does not implement buys a wasted run whose "no significant difference"
  is indistinguishable from "this rewrite does not help."

## The contract between the two repos

`contracts/eval_request.schema.json` and `contracts/eval_report.schema.json` are
the only agreement between `game_opt_loop` and `phonefarm`. Both sides pin their
tests to the same files. See `contracts/README.md` for the v1 → v2 story and the
three disciplines that run through every field.

## Using phonefarm on its own

`phonefarm` is a standalone device automation and measurement base and is useful
without any of this: deterministic script replay, conformance test batches,
on-device model latency benchmarking, frame capture, farm keep-alive, read-only
result inspection, an MCP tool service. Its own skill is `skills/phonefarm/SKILL.md`
in that repo. Use it directly when the job is "drive this device" rather than
"optimize this game."
