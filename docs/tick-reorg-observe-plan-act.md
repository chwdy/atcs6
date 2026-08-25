# Proposal: Observe → Plan → Act tick reorganization

**Status:** proposal / staging. Not part of `design_new.md`. This document specifies a
target tick structure so the reshaping can be reviewed before the ~800-reference
declarative rewrite of `design_new.md` is attempted. Delete after adoption.

**Scope:** restructure *which stage* each computation lives in and *when* it runs
relative to planning. The control laws are untouched — same attribution logic, same
ATC/ATO/manual command generation, same voltage curves, same debounce and seam-gate.
What moves is the tick spine.

---

## Why

On the real layout the physical train rolls continuously; the software only updates in
discrete ticks. Today the software's picture of the train and the detector reading are
out of step within one tick:

- Each tick is handed `delta`, the time that has already passed since the last tick. Its
  length is known exactly.
- But the tick **reads the detector** (the train's real position now) *before* it
  **calculates the train's movement** up to now. So it compares "where the detector says
  the train is now" against "where the software thought it was one tick ago" — two
  pictures a tick apart.
- And one step does two jobs at once: it calculates the movement *and* sets the track
  voltage. The voltage set now drives the *next* stretch of time, but the movement it
  calculated covered the stretch that just passed. The command and the movement it is
  paired with are a tick out of step.

The fix is to split those two jobs that `TrainPhysics.tick(...)` does today:

- **Calculating the movement** (from speed and the elapsed time) belongs at the front of
  the tick, right next to the detector read, so both describe the same instant and use
  the same known `delta`. There is no guessing about the future here — this reconstructs
  the stretch of time that just passed, whose length is already known.
- **Setting the track voltage** (which drives the *next* stretch of time) belongs at the
  back of the tick.

Doing it this way also writes down occupancy before any train plans, which removes the
brief window today where the calculated position is fresh but `Block.occupied_by` still
shows the previous tick's value.

What it does **not** fix: you set a voltage now and cannot measure its effect until you
read the detector next tick. That gap is real and unavoidable — calculating the movement
from speed is what covers it between reads. This reorg only stops the software's own
picture from being out of step with its own detector.

---

## Target tick structure

The tick has two parts. First, **Observe** works out where every train actually is —
for all trains, as one batch — before anyone plans. Then each train **Plans** and
**Acts** one at a time in priority order, because a train's reservations must be visible
to the next train that plans.

`delta` is the time that has elapsed since the last tick — a known, already-past
interval. "Calculate the train's movement" means: from its speed and `delta`, work out
how far it moved and slide its span forward by that much (this is what
`TrainPhysics.tick` does today, minus the voltage it also emits).

```
tick(delta):                           # delta = time since last tick, known exactly
  advance simulation_time

  OBSERVE  (all trains, as one batch)  # was: Step 0 + Phase-2 integration + Phase-3 (a)/(b)
    read every block's detector once  → on/off per block, no train name attached
    calculate every train's movement over `delta`     (all trains, before any planning)
    decide whose block each detector reading is, using those fresh positions
       (a clear is held back unless that train's tail has reached the block's far edge;
        one warning per held block — see Power, "A clear read from under the train")
    mint / hold / retire unidentified trains for readings that match no train
    make each train's calculated position agree with its detector reading
       (never move a span backward; a clear never moves a span)
    write down occupancy: Block.occupied_by via the ordinary occupy / vacate path
    off-route detection (calculated block disagrees with the detected block)
    "reached its stop, free its track" + authority release
    presence_unknown / ghost handling

  order runnable trains

  for each train, in priority order:
    PLAN                               # was: Phase 1 (unchanged)
       RouteExecutor planning kernel 9c4f–66aa; create_path / extend_path
    ACT                                # was: Phase-2 command+control+voltage + Phase-3 (c)–(g)
       drain the train's queued operator commands
       work out the ATC / ATO / manual command
       set the track voltage that drives the NEXT interval
       finish up: consume pending_path_cancel_request, pending_route_abort,
                  pending_power_off, pending_route_deactivate; lighting-cal re-check

  drive the world de-energize queue toward 0; refresh signals   (unchanged, tick tail)
  add up Block.occupied_seconds                                 (unchanged, tick tail)
```

### Old → new mapping

| Today | Target stage | Note |
| --- | --- | --- |
| Step 0: detector read / debounce / decide-whose-block / detected-block notes / unidentified / authority truncation / ghost | **Observe** | all of it moves into the batch position step |
| Step 0: `reconcile_to_detected_occupancy` (make calculated position agree with detector) | **Observe** | runs after the movement calculation now, not before |
| Phase 2: movement calculation + `on_physics_updated` progress append | **Observe** | the movement-calculation half of `TrainPhysics.tick` |
| Phase 3 (a): progress drain → write occupancy + off-route detection | **Observe** | produced and consumed in the same stage now |
| Phase 3 (b): reached-its-stop + authority release | **Observe** (end) | Decision 2 |
| Phase 1: planning kernel | **Plan** | unchanged |
| Phase 2: operator-command drain + ATC/ATO/manual + voltage | **Act** | the command-and-drive half of `TrainController.tick` / `TrainPhysics.tick` |
| Phase 3 (c)–(g): latch teardown, lighting-cal re-check | **Act** (end) | command/intent cleanup stays at the bottom |
| Tick tail: de-energize drain, signal refresh, occupied-seconds | unchanged | |

`TrainPhysics.tick(...)` splits in two: the part that calculates movement runs in
Observe; the part that sets the track voltage runs in Act. `TrainController.tick(...)`'s
command drain and control decision stay together in Act.

---

## Target stage table

Same contract shape as the current **Phase Table**: each stage is a strict boundary;
code must not cross into another stage's entity chain; retained cross-stage
communication uses only the latches in the reclassification below.

| Stage | Runs | Entry point | What it does | What it writes | What it must not do |
| --- | --- | --- | --- | --- | --- |
| **Observe** | all trains, one batch | `SimulationController.update(...)` before ordering | read every block's detector once → calculate every train's movement over `delta` → decide whose block each reading is (fresh positions) → mint/hold/retire unidentified trains → make each calculated position agree with its detector reading → write `Block.occupied_by` via the ordinary occupy/vacate path → off-route detection → reached-its-stop + authority release → presence_unknown/ghost | `physics_sync.current_span`; `Block.occupied_by` / caution / switch occupancy via `ResourceCoordinator`; `Path.status` / `current_resource_index`; stop-arrival `RouteRun` finishes via `advance_walk_position(...)`; detector debounce state; unidentified lifecycle; `resource_sequence` truncation + dropped reservations/locks; `presence_unknown`; `pending_path_cancel_request` (off-route, via writer helper); ghost `phase_latches.power_off` / `path_cancel_request` | plan routes; set voltage; create/extend authority; run dwell entry/exit or route-deviation cleanup (those are Plan) |
| **Plan** | one train at a time, priority order | `RouteExecutor.tick_train_phase1(train_id, delta)` | planning kernel `9c4f`–`66aa` (unchanged) | all current Phase-1 `RouteRun` writes (unchanged); `pending_path_cancel_request` when reconciliation/run-completion demands it | call `cancel_path(...)` directly; clear authority directly; read Act outputs from this same tick |
| **Act** | one train at a time, priority order | `SimulationController` per-train act pass | drain queued operator commands → work out ATC/ATO/manual command → set `track_voltage` for the next interval → finish up (consume the four latches) → lighting-cal re-check | `TrainController` state, `eb.active`, `ato.running`, `track_voltage` to `BlockPowerController`; `pending_power_off`; the four `phase_latches` clears; `complete_power_off()` cleanup | calculate movement or move a span; write `Block.occupied_by`; change `RouteRun` task-walk state; call `RouteExecutor` planning methods |

**Why the stages can't form a loop.** Plan reads only what Observe already wrote (every
train's committed position). Act reads what Plan decided (the ATC speed limit comes from
the planned route ahead). Observe reads last tick's routes (set by Plan/Act of the
previous tick) for off-route and reached-its-stop checks — never this tick's Plan. A
powered-off train skips Plan and gets its power-on command in the Act command drain, then
plans next tick — the same one-tick delay the current tick has for a powered-off train.

---

## Latch reclassification

The reorg splits today's Phase 3 into two homes: the **occupancy commit** moves to
Observe (front), the **command/intent teardown** stays in Act (back). That reclassifies
the handoffs into three kinds. Naming stays the same; only stage assignments change.

### 1. Go away — become internal to Observe, no longer passed between stages

These are notes one stage leaves for another. Once the movement calculation and the
occupancy write both live in Observe, the note is written and read inside the same
stage, so it stops being a hand-off at all.

| Today | Why it goes away |
| --- | --- |
| `pending_detected_entered_blocks` / `pending_detected_vacated_blocks` (Step-0 → Phase-2 note) | the step that fills it and the step that reads it are both in Observe now |
| `pending_block_exit_edges` (which way an unidentified train left a block) | same — written and read inside Observe |
| `physics_sync.queued_progress_reports` (Phase-2 → Phase-3 list of what moved) | this list is filled during the Observe movement step and read later in the same Observe stage |

These are already not latches (they never survive a tick). The reorg just tightens them
into a single stage; none needs to be passed between stages any more.

### 2. Within-tick — stay latches, still resolve in the same tick

Consumed by the Act teardown at the bottom of the same tick they are written.

| Latch | Writer | Consumer | Gate |
| --- | --- | --- | --- |
| `pending_power_off` | Act command drain | Act teardown | `pending_path_cancel_request is None` (inclusive cancel committed earlier this tick) |
| `pending_route_abort` | API handler / Act | Act teardown | same |
| operator-command acceptance (`power_on`, `set_cab`, …) | Act command drain | Act command drain | none |

No regression: these resolve same-tick exactly as they do today, because the teardown
stays at the bottom rather than moving to the front.

### 3. Cross-tick — intrinsic multi-tick latches, unchanged

These survive across ticks because of a gating predicate on physical/recovery state,
not because of stage ordering. The reorg must preserve them exactly, or it would
release track ahead of a moving train.

| Latch | Gate | Consumer stage |
| --- | --- | --- |
| `pending_path_cancel_request` while rolling | `TrainController.is_stopped() == True` | Act teardown |
| `RouteRun.path_cancelled` | `controller.is_recovery_clean` | Plan (route-deviation cleanup) |
| `pending_route_deactivate` for an OFF train | waits for power-on | Act teardown |
| `ops.service_stop_active` | service-stop duration | Plan (step `6a0d`) |

**This is the load-bearing warning.** "Reorder removes latches" is only true for
category 1. Category 3 is safety-critical and must not collapse to same-tick: a
cancel must never release authority or blocks under a rolling train.

---

## Decisions (settled 2026-08-24)

1. **Work out every train's position first, then plan — SETTLED.** At the top of the
   tick, calculate every train's new position from its speed and the elapsed time, for
   all trains, before any train plans. Then decide whose block each detector reading
   belongs to, using those fresh positions (not last tick's). Reason: today a train that
   plans early in the tick sees the up-to-date position of trains ordered before it but
   the stale position of trains ordered after it; working out all positions first means
   every train plans against where every other train actually is, so it cannot reserve or
   drive toward a block another train has just rolled into. Same total work, safer
   ordering. This makes the position step a batch (all trains do it, then the tick moves
   on); planning and command-issuing stay one-train-at-a-time in priority order, because a
   train's reservations must be visible to the next train that plans.

2. **"The train reached its stop, free its track" runs at the end of Observe — SETTLED.**
   Each tick first finishes accounting for what the previous tick's commands did (where
   everyone ended up, who arrived, who wandered off), then plans the next move. Freeing a
   completed train's track is part of that accounting, so it happens at the end of
   Observe, before planning. Handling it here means it is done in the same tick it
   happened; deferring it past planning or into the next tick would force a carried-over
   note ("this train arrived, deal with it later") across the tick boundary — one more
   flag to track. End-of-Observe avoids that flag.

3. **Off-route detection runs at the end of Observe, inside the position-agreement step —
   SETTLED.** When the calculated position and the detector reading disagree about which
   block a train is on, that train is off its planned route. The sharp case is a train
   crossing a switch: the switch has no detector, so the block just past it is the first
   place the detector can confirm which branch the train took. Detecting the mismatch
   right there — while the two positions are already being reconciled — is the natural
   place. It writes the cancel-this-train's-authority note; the track is still not
   released until the train has stopped (unchanged safety rule).

4. **This is a reordering, not "no change" — ACCEPTED.** Position is worked out before
   planning instead of after. The train drives identically (same braking, same speed
   logic, same everything it physically does); only the internal order flips.

---

## Adoption plan for `design_new.md`

Once the shape and the four decisions are locked, the `design_new.md` rewrite is a
single declarative sweep (its no-drift rule forbids a partial one). Affected surfaces:

- **Core Runtime Loop** — Per-train tick transaction, Procedure / phase ownership table,
  Phase Table, Latch Table, Running-and-stopped (the stopped-pass step list).
- **Every "Phase 1 / Phase 2 / Phase 3 / Step 0" reference** (~800), rewritten to the
  new stage names with their step IDs preserved where they carry meaning.
- **Power → Block occupancy detection** — the reconcile/commit/seam-gate rules already
  written, re-anchored to the Observe stage.
- **TrainController / TrainPhysics** sections — the `tick(...)` bisection (integration →
  Observe, voltage → Act), and the progress list becoming internal to Observe.
- **Speed calibration** and **Lighting-voltage calibration** — their Phase-2 / Phase-3
  hooks re-homed to Observe/Act.

The rewrite changes no control law and adds no field; it is a pure re-homing of existing
computations plus the latch reclassification above.
