# Two-stage braking-distance authority model — design proposal

**Status:** proposal for reaction. Nothing in `design_new.md` changes until you accept a scope.
This document is the write-up you parked for a dedicated session at the end of the
"Design review documentation" session. It is grounded in the current `design_new.md`
(cited by section name, since line numbers drift).

---

## 1. The idea, in one paragraph

Lock reserved track **by speed**, not by destination. At every tick a moving train hard-locks
exactly the track it would need to brake to a full stop if it started braking right now — its
**braking distance** — and nothing further. That locked span is the *safety lock*: it is
uncancellable, because the train physically needs it to stop. Everything beyond it — the rest
of the route to the next stop — is *planning intent*: where the train wants to go, contended,
advisory, and re-decided every tick, but not locked. A train extends its lock forward one step
at a time as the track ahead stays clear; when it can't extend, it simply brakes to a stop
inside what it already holds, and resumes when the track frees. This is textbook moving-block
ATP.

The single sentence that captures the whole change: **today braking distance is computed
*backward from* the authority end; in this model braking distance *is* the authority end.**

---

## 2. What is true today (the grounding you need to judge the change)

The current design keeps **three separate forward horizons**. Keeping them straight is the key
to seeing what this proposal actually moves:

1. **Reservation / authority horizon** — how far track is *locked* for the train. Today this
   extends to the **currently armed route target** (the next `stop`, `dwell`, or elapsed `run`;
   for `waypoint` chains it pre-extends up to `WAYPOINT_LOOKAHEAD_BLOCKS`, default 3). Authority
   grows one armed target at a time (**Authority termination rule**, **Planning Kernel** step
   `66aa`). When a resource ahead is taken, `best_effort` acquisition **truncates** the lock to
   the furthest acquirable prefix (**Resource acquisition**); a stalled truncated prefix is then
   released by the idle-hold / contention machinery (§4 below). So between two far-apart stops, a
   train locks the **entire span** between them — that is the hoarding this proposal removes.

2. **Energization horizon** — how far rail is actually powered. Today this is **exactly one block
   ahead** (**Power**, "Only the next reserved block is pre-energized"), for the stated safety
   reason that live rail beyond where the train is authorized to be *this instant* is rail a fast
   train keeps accelerating over. **This already matches the moving-block philosophy** and does
   not change.

3. **Braking horizon** — the ATC/ATO speed curve. Today the controller lookahead counts *back*
   from `authority_end_block_id` by a safety margin and builds the speed curve as the minimum of
   all restrictions (**Safety margin globals**, the lookahead payload's `target_stop_cm`). The
   curve is driven entirely by `SpeedProfile.deceleration_rate` (service), `emergency_brake_rate`,
   and `friction_rate`, all in `km/h/s`.

The proposal collapses horizon (1) onto horizon (3): the reservation is no longer "to the target,
truncated," it is "the braking distance, extended as the train rolls." Horizon (2) already agrees.

---

## 3. The proposed model

### Stage 1 — the safety lock (hard, uncancellable)

- **What it locks.** Every block the train's **braking distance** reaches into, in the travel
  direction, starting from the head. Braking distance is the distance to decelerate from the
  current speed to 0 at the service brake rate (`SpeedProfile.deceleration_rate`), plus the
  existing standoff margin (`TRACK_END_SAFETY_MARGIN_CM` / `PLATFORM_SAFETY_MARGIN_CM`), plus a
  cushion for one tick of travel and for detector-confirmation lag. Blocks are discrete, so the
  lock rounds **up** to whole blocks — you never lock a fraction of the block you might stop in.
- **It grows as the train accelerates, block by block.** Each tick the train tries to extend the
  lock forward far enough to stay ≥ braking distance ahead of the head. Extension goes through the
  ordinary interlocked acquisition (`ResourceCoordinator.reserve_segment`, switch-first,
  all-or-nothing per segment, flank protection) — nothing about the locking mechanics changes,
  only *how far* it reaches.
- **It is capped at the next stop.** A `stop` target (or an elapsed `run`) still terminates the
  lock: the reservation never grows past a place the train was sent to halt (the existing
  **Authority termination rule**). So the locked span is `min(braking-distance prefix,
  distance to the next stop target)`.
- **It is uncancellable.** Once locked, a block stays locked until the train's tail vacates it
  (ordinary tail-vacate) — with one new exception for a stopped train (Decision D3). No priority
  edit, no contention, no timeout takes a locked block away from a train that needs it to stop.
- **EB always fits inside the lock.** The lock is sized by the *service* brake rate
  (`SpeedProfile.deceleration_rate`), the gentler and therefore longer-distance stop. An emergency
  brake decelerates at the steeper `emergency_brake_rate` over a *shorter* distance, so a train
  that EBs always comes to rest within the span it already holds. The safety lock is never
  undersized for the harder stop.

### Stage 2 — planning intent (soft, advisory, never locked)

- The route beyond the safety lock is where the train *wants* to go. It is re-evaluated every
  tick, drives the braking curve's aim point, and seeds contention/meet decisions, but it locks
  nothing. If the intended path ahead is contested, the train's lock simply stops growing into
  the contested resource; the train brakes to a stand inside its lock and waits.

### The three details you flagged, pinned down

- **The floor.** At speed 0, braking distance is 0 → a train would lock nothing and never start.
  Floor the lock at **the next block**, so a stopped train always holds enough to begin moving.
  This floor coincides with today's one-block energization horizon, so a stopped train holds and
  energizes the same single block ahead.
- **The margin.** The lock length must cover where the train actually *is* plus where it will be
  before the next tick can react. It reuses the existing safety-margin globals and composes with
  the existing "the calculated span never runs ahead of the detector" cap — the lock must always
  extend at least as far as the confirmed span plus one tick of travel.
- **Discreteness.** Lock whole blocks; the lock boundary is a block seam, matching how energization
  and authority ends already work.

---

## 4. Why this deletes the idle-hold / contention cancels (and fixes parked item #1)

Today two release mechanisms live in **Planning Kernel** step `66aa`, both firing **only** when a
run holds a *truncated best-effort prefix* (`Path.reaches_target(...)` is false):

- **Idle-hold release** — a run making no progress for `IDLE_HOLD_RELEASE_TICKS` (default 30)
  **cancels** its held authority so higher-priority runs can use it.
- **Contention release** — a run holding a block a strictly-higher-priority waiter is blocked on
  **cancels** at once.

Both are **cancellations**, and committing either **clears `ato.engaged`** — which is precisely the
parked "exemption" problem: you said only operator / deviation / platform-disable should disengage
ATO, but these two *internal reroutes* disengage it too.

Under braking-distance locking there is **nothing to hoard**: a stalled train holds ≈ one block
(the floor), not a long reservation to a distant target. A lower-priority train blocked by a
higher-priority one just fails to *extend* into the contested block and brakes to a stand inside
what it holds; the contested block was never locked by it. So:

- The idle-hold release **disappears** (there is no long truncated prefix to time out).
- The contention release **disappears** (a lower-priority train never holds the block the
  higher-priority train wants — it never locked past braking distance).
- Every *remaining* cancel is operator / deviation / platform-disable — **exactly the three you
  said should disengage ATO.** "Every cancellation clears `ato.engaged`" (your Wave 2 ruling)
  becomes correct with **no exemption flag and no per-cause boolean.** The band-aid you were about
  to add is deleted along with the problem.

---

## 5. What already works this way (so this leans on, rather than invents)

- **Meet coordination** already **caps-and-holds, never cancels.** It writes `meet_ceiling_block_id`
  → `acquisition_cap_block_id`, bounding how far a train reserves this tick, leaving the route
  target unchanged, so the train "receives clean authority to that holding point and waits there."
  Its "**existing commitment decides before priority**" rule, its capacity check
  (single-track / crossover / throat via `routes_conflict`), and its holding-point selection are
  exactly the stage-2 contention machinery this model needs. **This becomes the primary contention
  mechanism** instead of a "reduces how often the cancels fire" layer.
- **Wedge detection** already only **diagnoses** deadlock (emits one event naming the trains and
  blocks) and leaves resolution to the operator. It changes no state and cancels nothing.
- **The reversal-trap exclusion** already stops a lock from terminating in a block a stopped train
  ahead needs in order to leave.
- **One-block-ahead energization** already puts dead rail immediately past the authority end.
- **The interlock** (`ResourceCoordinator`, switch-first, flank protection) already prevents two
  trains from locking conflicting resources — so moving-block extension can never double-lock a
  crossover; one train wins the extend, the other fails it and brakes.

The beyond-authority layer you worried was missing is, for the most part, **already built.**

---

## 6. The one cost you must accept: prevent deadlock, don't recover from it

"Locked = uncancellable" removes the escape hatch. Today the contention cancel doubles as a
deadlock *breaker*. Remove it and deadlock must be **prevented up front**. The prevention
machinery is what Meet coordination already is: commitment-before-priority + capacity check +
holding points + reversal-trap. The residual case is a **contested zone with no holding point on
the approach**: two trains brake to a stand nose-to-nose and neither can extend into the shared
resource. The honest contract:

> On a layout that offers a holding point before every contested zone, the system never deadlocks
> and never cancels for priority. Without one, trains stop safely short, **wedge detection**
> diagnoses the standoff, and the operator takes manual control to clear it.

This is a statable, defensible limitation — and it is the same one the doc already accepts for
wedges today. **Accepting this cost is the load-bearing decision** (Decision D1).

---

## 7. What changes, what's deleted, what stays

| Area | Today | Under this model |
| --- | --- | --- |
| Reservation horizon | To the armed route target, best-effort-truncated | Braking distance ahead of the head, extended per tick, capped at next stop |
| `AcquisitionMode` | `best_effort` vs `all_or_nothing`, route-level | **Retired** — incremental braking-distance locking is the only behavior (D5). The whole enum, `Route.acquisition_mode`, `RouteRun.effective_acquisition_mode`, and the `acquisition_mode` override/param/UI all go |
| Speed calibration leg | Reserves the whole leg atomically up front (`acquisition_mode = all_or_nothing`) | Folds onto incremental locking: extends as it rolls, runs the measured span at target speed on a clear leg; a leg interrupted before its measured span completes is **re-measured** (matches the existing "capped crossing is re-measured" ruling) |
| Idle-hold release (`idle_hold_ticks`, `IDLE_HOLD_RELEASE_TICKS`) | Cancels a stalled prefix | **Deleted** |
| Contention release (`contention_release`) | Cancels for a higher-priority waiter | **Deleted** |
| `ato.engaged` on internal reroute | Cleared (the parked exemption) | Never happens; only operator/deviation/platform-disable clears it |
| Waypoint lookahead (`WAYPOINT_LOOKAHEAD_BLOCKS = 3`) | Fixed 3-block pre-extend | Replaced by dynamic braking-distance extension (Decision D4) |
| Meet coordination | "Reduces how often the cancels fire" | Primary contention mechanism |
| Wedge detection | Diagnose-only | Unchanged (now the sole deadlock story with Meet) |
| Reversal-trap / junction-fouling exclusions | Safety floor | Unchanged, still the terminal rules for where a lock ends |
| One-block-ahead energization | Safety rule | Unchanged (already agrees) |
| Safety-margin globals, `SpeedProfile` brake rates | Feed the braking curve | Now also size the lock length |
| Stop-arrival, `authority_stop`, dwell, run-timer | Authority end = the stop target | Unchanged in intent; the braking curve still aims 0 at the stop even when the lock hasn't reached it yet |
| Authority truncation (detector-driven) | Pulls a lock back when a block is taken | Kept; generalized slightly for the stopped-train forward release (Decision D3) |

---

## 8. Decisions (resolved 2026-09-06)

**D1 — Accept the deadlock contract in §6? → YES.**
Deadlock is prevented (Meet holding points + capacity + reversal-trap), not recovered from. The
residual no-holding-point standoff is diagnosed by wedge detection and cleared by the operator.
This is already how wedges are handled; the model makes it the whole story.

**D2 — The floor. → ONE BLOCK** (recommended default, taken).
A stopped train always locks the next block, coinciding with the existing one-block energization
horizon.

**D3 — Does a *stopped* train shrink its forward lock to the floor? → YES.**
When a fast train brakes to a stand it releases everything it locked at speed beyond the floor,
freeing it for others. This is the mechanism that **replaces the contention release**. It is a
**new forward-directed shortening** (allowed only because the train is stopped — the existing rule
forbids releasing ahead of a *rolling* train), done as a generalization of
`truncate_path_before_block` that keeps the path `ACTIVE` and **does not** clear `ato.engaged`.

**D4 — Retire `WAYPOINT_LOOKAHEAD_BLOCKS`? → YES** (recommended default, taken).
The fixed 3-block pre-extend is replaced by dynamic braking-distance extension. The "arm one
target at a time / terminate at a stop" rules stay.

**D5 — `AcquisitionMode.all_or_nothing`? → RETIRE THE WHOLE ENUM.**
Incremental braking-distance locking is the only acquisition behavior, so the mode has no meaning.
Retiring it removes `PathDispatcher.AcquisitionMode`, `Route.acquisition_mode`,
`RouteRun.effective_acquisition_mode(...)`, the `acquisition_mode` entry in the RouteRun override
allowlist (leaving `priority` as the only overridable field), the `acquisition_mode` parameter on
`create_path` / `extend_path` / `get_waiting_conflict`, every `all_or_nothing` branch and its
"contradiction with the cap" rules, and the Route-editor `acquisition_mode` control.

- **Knock-on — speed calibration.** Calibration is the **only** current user of `all_or_nothing`
  (Plan step `c9e1`: it reserves the whole measured leg up front). It folds onto the incremental
  model with no new mechanism: on a clear leg the calibration train keeps extending ≥ braking
  distance ahead and runs the measured span at target speed exactly as before; a leg interrupted
  before its measured span completes is **re-measured**, per the existing "a capped crossing is
  re-measured, not failed" calibration ruling. Calibration no longer names an acquisition mode.

**D6 — Manual and unrouted trains. → SAME STAGE-1 LOCK** (recommended default, taken).
A routed manual (`manual_override`) train gets the same braking-distance lock, extended as it
drives, with ATC capping speed at the lock end. An unrouted manual train drives with the
braking-distance lock as its whole authority. Manual trains are not on a different model.

**D7 — Scope / sequencing. → OPEN (your call).**
The model is locked. What remains is *when* the `design_new.md` rewrite happens. It reshapes
acquisition, the authority-end model, truncation, idle-hold, best-effort, and the acquisition-mode
enum, and leans harder on Meet + wedge — "bigger than #2 and #3 combined." It is doc-only when it
lands; code lags and a gap-analysis follows later. See §9.

---

## 9. Suggested sequencing once you accept a scope

1. **Lock the model** (D1–D6 answered).
2. **Rewrite the reservation/authority contract** in `design_new.md`: the braking-distance lock,
   the floor, the margin composition, the stop cap, the stopped-train forward release.
3. **Delete** the idle-hold and contention-release rules and their fields/settings
   (`idle_hold_ticks`, `release_cooldown_ticks`, `IDLE_HOLD_RELEASE_TICKS`,
   `IDLE_HOLD_COOLDOWN_TICKS`), and the two `origin` labels (`idle_hold_release`,
   `contention_release`).
4. **Resolve parked item #1**: "every cancellation clears `ato.engaged`" now needs no exemption;
   sweep the ATO-disengage wording to match.
5. **Promote Meet coordination** from "reduces how often the cancels fire" to the primary
   contention mechanism; restate wedge as the sole deadlock diagnosis.
6. **Sweep every cross-reference** (the doc's cross-reference-drift rule), then a **drift
   doc-review**.
7. Later, separately: **gap-analysis** of the code against the new contract.

---

## 10. How it is built (the cap-based architecture)

The rewrite does **not** invent a new authority model. It reuses a mechanism the doc already
has: `acquisition_cap_block_id` (the **Acquisition-cap rule**) already means "plan toward the
target but store authority only up to this cap block, and hold there." Today that cap is fed only
by **Meet coordination**. The two-stage lock adds a **second feed, computed from speed**:

- **The braking-distance cap.** Each tick, `TrainController` lookahead already knows the current
  speed and the `SpeedProfile` brake rate. From those it computes the block seam at
  `braking distance + safety margin + one-tick/detector cushion`, rounded up, floored at the next
  block. That seam is a cap block.
- **The effective cap is the nearer of the two feeds** — the braking-distance cap and any Meet
  ceiling. Plan passes it as `acquisition_cap_block_id`; the dispatcher's existing cap-truncation
  stores authority only that far and keeps the route target farther.
- **Best-effort-to-the-cap self-regulates.** You lock as far toward the cap as the track is clear;
  if a conflict stops you short, ATC brakes you to a stop at that shorter authority end, which
  means your actual speed — and therefore your actual braking distance — can only be as large as
  what you locked. Speed and lock length chase each other to a safe fixed point. This is why
  there is exactly one acquisition behavior and the `all_or_nothing` / `best_effort` split has no
  job left (D5).

So the conceptual inversion of §1 ("braking distance *is* the authority end") is implemented as
"braking distance is one more source of the acquisition cap," and almost all the existing
acquisition, truncation, and cap machinery is kept as-is.

## 11. Site map (every affected section)

Grouped by change cluster; section names, not line numbers.

- **A · Braking-distance cap (new).** New canonical home near **Acquisition-cap rule** /
  **Resource acquisition**: the cap's length formula (brake rate + margin + cushion), the
  one-block floor, discreteness. Generalize the "used only by Meet coordination" wording on
  `acquisition_cap_block_id` everywhere it appears (**Path API migration**, **Acquisition-cap
  rule**, **Contention snapshot** `get_waiting_conflict` signature, **Meet coordination** Plan
  consumption) to "Meet coordination and the braking-distance lock, whichever is nearer."
- **B · Retire `AcquisitionMode` (D5).** ~35 sites: the enum definition and both branches
  (**create_path() flow** items 5/10/11 and the whole best-effort truncation block — the
  truncation-terminal rules *stay* as cap behavior, only the mode framing goes), the
  `acquisition_mode` parameter on `create_path` / `extend_path` / `get_waiting_conflict`, the
  return-contract `all_or_nothing` clauses, `Route.acquisition_mode` (**Route data**, field table,
  edit-classification, `last_edited_at` set), `RouteRun.effective_acquisition_mode` and the
  override-allowlist entry (**RouteRun overrides** — leaves `priority` only), the meet
  "contradictory acquisition mode" clauses, the speed-calibration `all_or_nothing` usage (**Speed
  calibration** step `c9e1`), the **End-to-end** and **Exact RouteExecutor tick calls** mentions,
  and the Route-editor control reference.
- **C · Delete idle-hold + contention-release (B).** The whole **Idle-hold maintenance and
  release** subsection (step `66aa`), `RouteRun.idle_hold_ticks` / `release_cooldown_ticks`
  fields, `IDLE_HOLD_RELEASE_TICKS` / `IDLE_HOLD_COOLDOWN_TICKS` settings + their validation, the
  `idle_hold_release` / `contention_release` origin labels (**ErrorRecord** origin list, Latch
  Table writer bullet, `cancel_path` message list, `ato.engaged` writers list), the
  **Contention snapshot** release consumer, the **Wedge detection** "only release path" sentence,
  the best-effort-truncated-must-not-complete paragraph, and the two self-consistency checklist
  questions.
- **D · Stopped-train forward shrink (D3, new).** New canonical home near **Authority
  truncation**: when a train reaches a stand holding more than the floor, pull its forward
  authority back to the floor via a forward-directed shortening (generalizing
  `truncate_path_before_block`), path stays `ACTIVE`, `ato.engaged` untouched. Place it in the
  **Observe, Plan, and Action Table** (stage + entity) and the stage-ownership bullets.
- **E · Retire `WAYPOINT_LOOKAHEAD_BLOCKS` (D4).** The **Waypoint lookahead** sub-step in `66aa`
  becomes "extend toward the next stop, capped at the braking-distance cap"; delete the setting,
  its validation, and the self-consistency question.
- **F · Meet promotion + `ato.engaged` (§12).** Update **Meet coordination**'s "reduces how often
  the cancels fire" composition note; resolve the `ato.engaged`-on-cancel wording per §12.
- **G · Manual trains (D6).** Confirm the manual "acquires the corridor" wording composes with the
  braking-distance cap (**Planning Kernel** gate, **Manual mode and movement-authority boundary**).

Then: a **drift `doc-review`** and one commit.

## 12. One decision the plan surfaced — which cancels clear `ato.engaged`?

Deleting idle-hold + contention removes two internal reroutes, but these internal (non-operator,
non-deviation, non-platform-disable) cancels **survive**: authority-truncation revoke, broken-task
skip, live-path re-resolution, structural extend-failure. So "every cancellation clears
`ato.engaged`" still disengages ATO on automatic reroutes. Three ways to resolve it:

- **Narrow (recommended).** `ato.engaged` is cleared **only** by EB activation, power-off, and
  `set_mode("manual")` — no path cancellation clears it. Every reroute (truncation, re-resolution,
  broken-skip, operator retarget) re-plans with ATO still engaged. This is the original
  layered-ATP ruling #6, needs no per-cause flag, and matches the moving-block ideal that
  contention is handled automatically. It walks back Wave 2's "clear on every cancel" breadth.
- **Per-cause.** Keep a distinction: operator cancel and platform-disable revoke clear it; internal
  reroutes do not. Closest to the literal "operator/deviation/platform-disable" wording, but
  reintroduces the per-cause branch you wanted to avoid.
- **Status quo.** Every surviving cancellation clears it. Simplest wording; over-clears on a ghost
  intrusion (a truncation-revoke would force the operator to re-engage ATO).

---

### One caution about the working tree

Your working tree currently holds an **uncommitted diff** — the #2 (drop cab) and #3
(settlement-then-plan) work from the previous session, done but not committed. This proposal
touches none of it, and I have not disturbed it. You may want to commit that separately before any
two-stage doc edits begin, so the two waves stay cleanly separated in history.
