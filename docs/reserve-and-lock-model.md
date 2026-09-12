# Reserve-and-lock authority model — design proposal

**Status:** MERGED into `design_new.md` on 2026-09-12 (commit on branch
`docs/reserve-and-lock-model`). All decisions below are settled; the model now lives in
`design_new.md` as the normative spec. This document is retained as the **decision record** —
the rationale, the trade-off against the prior braking-distance-lock model, and the resolved
decision list (P1–P9) — not as a live proposal. Where this document and `design_new.md` ever
disagree, `design_new.md` wins.

---

## 1. The idea, in one paragraph

Split a train's forward track into **two tiers**. The **reserved** tier is the path the train
has claimed toward its next target, acquired best-effort and held against other trains. The
**locked** tier is the subset of that reservation the train's **braking distance** currently
covers — the track it physically needs to stop — which grows and shrinks with speed. A locked
resource is uncancellable: nothing takes it from a train that needs it to stop. A
reserved-but-not-locked resource is softer: a **strictly higher-priority** train may **preempt**
it, and the holder then cancels its reservation from that resource onward, brakes to a stand
inside what it still holds, and re-plans the shed tail later. Reservations are kept short by
**chunking at holding points** — authority terminates at the next holding point and re-extends
past it only when the onward track is free — so no train claims a long corridor, and the natural
yield points sit exactly before the switches where conflicts happen.

The one sentence that captures the change from today: **today braking distance *is* the
authority end; in this model braking distance is the *lock* boundary, and the authority
(reservation) runs ahead of it to the next holding-point chunk, preemptible by priority above
the lock.**

---

## 2. What is true today (model B — the braking-distance lock)

The current design (the "two-stage braking-distance" model) collapses reservation and braking
distance into one horizon:

- **One horizon.** A powered run's authority is bounded each tick to its **braking-distance cap
  block** — the furthest block it can stop at or before — and acquisition stores authority only
  that far (**Braking-distance lock**, **Acquisition-cap rule**). There is no reservation beyond
  the braking distance; everything past it is unlocked planning intent, re-decided each tick.
- **Contention self-regulates without a cancel.** A train blocked ahead simply fails to extend
  its lock into the contested block and brakes to a stand inside what it holds; a stopped train
  sheds its surplus back to the floor through the **Stopped-run authority shrink**. No priority
  cancel, no timeout release.
- **Priority is planning-order only.** `RouteRun.effective_priority(route_registry)` sets the
  per-tick order trains try to acquire, and it chooses the admitted contender for an
  **uncommitted** contested zone in **Meet coordination**. But "**existing commitment decides
  before priority**" means that once a train has occupancy *or reservation* anywhere in the zone,
  priority cannot reverse it. So a higher-priority train that reaches a junction one tick after a
  lower-priority train simply waits behind it. **Priority cannot preempt.**

Model B's great win was deleting the idle-hold timeout, the contention-release cancel, the
`AcquisitionMode` enum, and the fixed waypoint lookahead — because with nothing reserved beyond
braking distance there was nothing to hoard and nothing to fight over (see
`docs/two-stage-braking-distance.md`). The cost it accepted is the one this model keeps: deadlock
is **prevented** (Meet + holding points + reversal-trap), not recovered from.

The limitation this proposal targets is the third bullet: **in B, priority is too weak to matter.**
If an express must get ahead of a local, B cannot deliver it — whoever physically reaches the
contested track first keeps it.

---

## 3. The proposed model — reserve-and-lock

### 3.1 Two tiers over one path

- **Reserved tier.** The resources the train has acquired toward its current target, best-effort,
  up to the next holding-point chunk (§3.4). Reservation is **soft-exclusive**: no other train may
  plan through a reserved resource, with one exception — a strictly higher-priority train may
  **preempt** a reserved-but-not-locked resource (§3.3). This is the `Block.reserved_by_path` /
  `Switch.locks` claim the current design already carries; what changes is that it now persists
  ahead of the braking-distance lock instead of being bounded by it.
- **Locked tier.** Within the reserved set, the resources the train's **braking distance** covers,
  floored at the next block ahead of the head, growing and shrinking with speed exactly as the
  current **Braking-distance lock** computes it. Locking is **hard-exclusive and uncancellable** —
  a locked resource is never preempted, never timed out, never released ahead of a rolling train.
  An emergency brake always fits inside the lock (the lock is sized by the gentler service rate),
  so a train can always stop within its locked span.

The lock is a moving sub-window inside the reservation. The reservation says where the train is
going; the lock says what it currently needs to stop.

### 3.2 Per-tick flow: Meet → reserve → lock

1. **Meet coordination runs first**, globally, before any train reserves (as it does today — once
   per tick after `order_runnable_trains(...)`). It decides which train may reserve a contested
   zone this tick: capacity check, and the commitment test of §3.3. Meet is the up-front gate that
   keeps two trains from reserving conflicting resources.
2. **The train reserves** best-effort the resources Meet cleared, up to its next holding-point
   chunk (§3.4). A resource Meet did not clear, or that another train holds, is simply not added —
   the reservation is shorter this tick and retried next tick.
3. **The train controller locks** the braking-distance subset within its reservation. Lock length
   follows speed; the lock never exceeds the reservation.

### 3.3 Priority preemption and its safety invariant

When a train would acquire a resource another train has **reserved**, the outcome is decided by two
facts:

- **Is the resource locked?** A **locked** resource is never preemptible. The newcomer waits behind
  it; the lock will recede on its own as the holder passes the resource or brakes to a stand and
  shrinks to the floor.
- **Who has higher priority?** Preemption requires **strictly higher** priority. **Equal priority
  is first-come-first-served** — the current holder keeps the reservation. Strict-greater cannot
  form a cycle, and equal-never-wins removes equal-priority churn.

When a strictly higher-priority train preempts a reserved-but-not-locked resource, the holder
**cancels its reservation from that resource onward** — keeping its locked span and the reserved
prefix up to the taken block — brakes to a stand inside what it still holds, and re-plans the shed
tail on a later tick.

**The safety invariant.** A resource that is reserved-but-not-locked is, by definition, *beyond the
holder's braking distance* — so the holder can always stop before it. Preemption therefore never
takes a resource a train needs to stop, which is why it is safe where model A's blind
contention-cancel was not.

**No "reserved but occupied" case.** Reservation is single-holder and exclusive, so a resource
reserved-but-not-locked by one train is physically clear — no other train occupies it. Preemption
therefore never lands on an occupied block, and the winner gets clear access. (A train's own tail
blocks are occupied but not forward-locked; they are not reservable by anyone else either, so the
"preempt only reserved-not-locked" rule already excludes every occupied block.)

### 3.4 Holding-point reservation chunking

Reservations are bounded, not run to a distant stop:

- **A route run treats a holding point as an implicit waypoint.** Authority (the reservation)
  terminates at the next holding point (`Block.is_holding_point`), and re-extends past it only when
  the onward track is reservable. No train ever reserves past a holding point it has not yet
  reached.
- **Authoring guidance.** Place a holding point before every critical switch, and a few waypoints
  along long legs. This keeps reservations short (less held at once, so less to contend over) and
  puts the natural yield points exactly where conflicts happen — before the switches.

This is model C's critical-point granularity (reserve clearance-to-clearance), brought in as a
planning rule over the reserve-and-lock tiers rather than as a separate mechanism. It is also what
makes the deadlock contract (§6) hold: Meet always has a safe holding point to hold a yielding
train at, before it locks into a contested zone.

### 3.5 Inherited unchanged from model B

- **The lock formula.** Lock speed = `max(current, min(current + BRAKING_LOCK_ACCEL_HEADROOM_KMH,
  atc_limit + BRAKING_LOCK_LIMIT_MARGIN_KMH))` (20 / 5 km/h); stopping distance at the service rate
  with the safety margin and a one-tick cushion; rounded up to whole blocks; floored at the next
  block ahead of the head.
- **EB fits inside the lock** (sized by the service rate; emergency braking is shorter).
- **One-block-ahead energization** (dead rail immediately past the lock floor).
- **The reversal-trap and junction-fouling exclusions** still govern where a *lock boundary* parks
  and where a reservation chunk may end.
- **Wedge detection** still diagnoses the residual no-holding-point standoff.

### 3.6 The stopped train: lock shrinks, reservation stays

When a train brakes to a stand, its **lock** shrinks to the floor (one block) — it only needs one
block to be ready to start. Its **reservation** is **kept**; it still intends to go where it
planned. The shrink frees the *lock* on the reserved tail, which makes that tail preemptible by a
strictly higher-priority train (the tail is now reserved-but-not-locked). This is the key
difference from model B, where a stopped train *released* its surplus reservation outright. Here,
redistribution happens only through preemption, so an equal-or-lower-priority train does not take a
stopped train's reserved tail — first-come-first-served.

**Why keeping the reservation is correct.** Under ATO a train never stands still in mid-corridor
with reserved track ahead for no reason — ATO stops *at* a target or when genuinely blocked, and in
those cases the lock is already at the floor and there is no long reserved tail to worry about. The
only way a train holds a reserved tail while stopped is a **manual** driver pausing the train. In
that case keeping the reservation is plainly the right behavior: cancelling a driver's reserved path
just because they stopped would be wrong. So the keep-the-reservation rule is not an efficiency
compromise — it is the correct manual-mode behavior, and the equal-priority case it was worried
about essentially does not arise under ATO.

---

## 4. Why this model over B — the priority win

The single reason to take on this model over B is **meaningful priority.** B deliberately made
priority planning-order-only, so it cannot make a lower-priority train yield track it already
holds. This model restores real priority — a strictly higher-priority train preempts the
reserved-but-not-locked tail of a lower-priority one — while keeping B's uncancellable safety lock.

It is a genuine synthesis, not a revert:

| | kept from B | revived from A | dropped from A |
| --- | --- | --- | --- |
| Reserve-and-lock | the uncancellable braking-distance lock; no idle-hold timeout; prevent-don't-recover deadlock | an explicit reservation ahead of the lock; priority-driven contention | `all_or_nothing`; the blind timeout release; the unbounded reserve-to-target corridor |

The honest cost: this re-introduces two things B deleted — **reservation exclusivity** (a reserved
chunk blocks equal/lower-priority trains again) and **contention-cancels** (now bounded to the
not-locked tier, triggered only by a strictly-higher-priority conflict, never by a timeout). Both
are mitigated by holding-point chunking, which keeps each reservation to one chunk. This is a good
trade **iff** priority matters to the system; if it does not, B is simpler and should stay.

---

## 5. `ato.engaged` — one principle, no per-cause flag

**`ato.engaged` clears only when ATO cannot continue to drive the train normally.** A preemption
shortens the reserved tail but leaves the train able to brake to a stand within its locked span, so
ATO handles it as an ordinary re-plan and stays engaged. Every internal re-plan — preemption,
authority truncation, broken-task skip, live-path re-resolution — keeps ATO engaged. Only the cases
where ATO genuinely cannot carry on clear it: **emergency brake, power-off, switch to manual, and
an off-authority deviation.** One invariant replaces the per-cause exemption flag the earlier
models needed.

---

## 6. Deadlock contract (unchanged from B, and strictly better on contention)

**Meet coordination prevents deadlock exactly when every contested zone has a holding point on each
approach** — which the §3.4 chunking guarantees by construction. The analysis:

- Opposing trains / single track with holding points: Meet admits one, holds the other at its
  holding point (it reserves only up to there and never locks into the zone). No deadlock.
- Preemption: strictly higher priority only, so no cycle and no livelock — the yielder sheds its
  tail and the winner makes progress.
- Priority cycle (A→B→C→A): impossible under a total priority order.
- **Residual — a contested zone with no holding point on the approach.** Meet has nowhere safe to
  hold the yielding train before it would lock in; if two trains both *lock* into the zone (locked
  = unpreemptable), they stop nose-to-nose. This is the **same residual model B accepts** → wedge
  detection diagnoses it, the operator clears it manually.

This model is **strictly better than B on contention** — priority resolves conflicts automatically
that B could only settle by first-come luck — while the hard residual is identical.

---

## 7. What changes, what's deleted, what stays

| Area | Model B (today) | Reserve-and-lock |
| --- | --- | --- |
| Forward horizon | One: braking distance = authority end | Two: reservation (to next holding-point chunk) + lock (braking distance within it) |
| Reservation beyond braking distance | None (unlocked planning intent) | **Reserved and held**, preemptible above the lock |
| Priority | Planning-order only; cannot preempt | **Strictly-higher preempts** a reserved-not-locked resource |
| Meet "commitment decides before priority" | Commitment = occupancy **or reservation** | Commitment = occupancy **or lock**; an unlocked reservation yields to strictly-higher priority |
| Stopped train | Releases surplus reservation to the floor (**Stopped-run authority shrink**) | **Lock** shrinks to the floor; **reservation kept**, its freed tail now preemptible |
| Reservation length bound | Braking distance | **Next holding-point chunk** (holding point treated as a waypoint) |
| `ato.engaged` on internal re-plan | Survives (narrow ruling) | Survives — "clears only when ATO can't drive normally" |
| `AcquisitionMode` | Already retired | Stays retired (best-effort is the only behavior; §8 D5 confirmed again) |
| Idle-hold / contention-release | Already deleted | Stay deleted (preemption replaces contention-release; no timeout) |
| Braking-lock formula, floor, EB-fit, energization | Authority boundary | Now the **lock** boundary; formula unchanged |
| Reversal-trap / junction-fouling exclusions | Lock/park boundary rules | Unchanged — apply to lock-boundary parking and chunk ends |
| Wedge detection | Diagnose-only | Unchanged |

---

## 8. Decisions

**Resolved in session:**

- **P1 — preemption grants the reservation right, on strictly-higher priority only; equal priority
  is first-come-first-served.** Locked resources are never preemptible.
- **P2 — cascade scope:** the preempted holder keeps its locked span and the reserved prefix up to
  the taken block; it sheds only the tail from the taken resource onward.
- **P3 — reservation chunking:** a route run treats a holding point as an implicit waypoint;
  reservations never run past the next holding point. Authoring guidance: holding points before
  critical switches, waypoints on long legs.
- **P4 — ordering:** Meet coordination runs before reservation; reservation is best-effort within
  what Meet cleared; the controller locks by braking distance within the reservation.
- **P5 — `ato.engaged`:** clears only when ATO cannot drive normally (EB, power-off, manual,
  deviation). Preemption and every internal re-plan keep it engaged.
- **P6 — deadlock:** prevented by Meet + holding-point chunking; the no-holding-point residual is
  wedge + operator, same as B. No known case Meet cannot handle given a holding point.
- **P7 — `all_or_nothing` stays retired:** a reserved resource can still be occupied or locked by
  another train, so no acquisition guarantees the whole path atomically; best-effort is the only
  behavior.
- **P8 — stopped train keeps its reservation; only the lock shrinks to the floor (O1 resolved).**
  An equal-priority crossing train waits (first-come-first-served); only a strictly-higher-priority
  train preempts. The justification is §3.6: under ATO a train never stands in mid-corridor holding
  a reserved tail for no reason, so this is a manual-mode situation, where cancelling a driver's
  reserved path just because they stopped would be wrong. Not an efficiency compromise — the correct
  manual-mode behavior.
- **P9 — rename "authority" to "reservation" (O2 resolved).** The reserved tier is the train's
  authority to occupy track, so the term becomes **reservation** throughout: `authority_end_block_id`
  → `reservation_end_block_id` and `Path.get_authority_end_block_id()` →
  `Path.get_reservation_end_block_id()`, and a new derived `lock_end_block_id` names the
  braking-lock boundary. The prose term "authority" becomes "reservation," and the section names
  that carry it are renamed to match (**Authority termination rule** → **Reservation termination
  rule**, **Authority truncation** → **Reservation truncation**, **Authority-side head derivation**
  → **Reservation-side head derivation**), so cross-references stay anchored. **Scope confirmed: a
  full sweep** — every "authority" in fields, getters, section names, and prose becomes
  "reservation," except where the standalone word clearly means something else.

**Remaining — scope / sequencing (O3):** doc-only `design_new.md` merge first (cross-reference
sweep + drift `doc-review` + one commit), then code, then a gap-analysis — as model B was done.

---

## 9. Site map (every affected `design_new.md` section)

Grouped by change cluster; section names, not line numbers.

- **A · The two-tier authority model (new core).** In **Path And PathDispatcher**: introduce the
  reservation tier as distinct from the lock tier. `create_path(...)` / `extend_path(...)` acquire
  and store the **reservation** to the next holding-point chunk; the **Braking-distance lock**
  subsection is re-homed as the *lock within the reservation*, no longer the acquisition boundary.
  Add `lock_end_block_id` (O2) to the **Path field schema** and the `received_path_plan` / progress
  projections. Reframe the **Acquisition-cap rule**: the braking-distance cap now bounds the lock,
  while the reservation is bounded by the holding-point chunk.
- **B · Preemption (new).** New canonical home near **Meet coordination** / **Resource
  acquisition**: the preemption rule, the strictly-higher-priority test, the locked-is-never-taken
  invariant, the "cancel from the taken resource onward" cascade (P1, P2). Wire it into the
  acquisition path (`reserve_segment` / `BlockManager.reserve_blocks_for_path` /
  `SwitchManager.acquire_locks_for_segment`): acquiring a reserved-not-locked resource held by a
  strictly-lower-priority train succeeds and triggers the holder's cancel-from-here.
- **C · Meet coordination commitment test.** Change "existing commitment decides before priority"
  so commitment = occupancy **or lock** (not reservation); an unlocked reservation is preemptible
  by strictly-higher priority (§7 row). Keep capacity, holding-point selection, and wedge as-is.
- **D · Holding-point chunking.** In **Route And RouteExecutor** (Planning Kernel step `66aa`,
  **Waypoint lookahead**, **Authority termination rule**): a holding point is an implicit waypoint
  that terminates the reservation and re-extends when clear (P3). Add the authoring guidance to the
  relevant **Block** / routing section.
- **E · Stopped-train lock shrink.** Re-home the **Stopped-run authority shrink** as a *lock*
  shrink: the lock recedes to the floor on a stand; the reservation is kept (O1). Update the
  **Observe, Plan, and Action Table** row accordingly.
- **F · `ato.engaged` principle.** State the single rule (P5) in the ATO sub-module canonical home;
  sweep the cancel-cause lists so internal re-plans (preemption, truncation, broken-skip,
  re-resolution) do not clear it, and only EB / power-off / manual / deviation do.
- **G · Priority becomes preemptive.** Update every "priority = planning order only" statement
  (**RouteRun overrides**, **Meet coordination**, the glossary) to the preemptive semantics.
- **H · Glossary + Canonical Entity Index.** New terms: reserved tier, locked tier, preemption,
  `lock_end_block_id`, holding-point chunk.

Then: a **drift `doc-review`** and one commit. Code and gap-analysis follow separately.

---

## 10. The stopped train and equal priority (resolved, P8)

In model B, a stopped train gives its surplus reservation back (the shrink releases it), so an
equal-priority crossing train can take it. In reserve-and-lock, a stopped train keeps its
reservation and only its lock shrinks, so an **equal**-priority crossing train waits (preemption
needs *strictly* higher priority).

This is the right behavior, not an efficiency compromise, because of *when* it can happen: **under
ATO a train never stands in mid-corridor holding a reserved tail for no reason** — ATO stops at a
target or when genuinely blocked, and in those cases the lock is at the floor with no long reserved
tail. The only way a train holds a reserved tail while stopped is a **manual driver pausing**, and
there, cancelling the driver's reserved path just because they stopped would be wrong. So the
equal-priority crossing conflict this section worried about essentially does not arise under ATO,
and in the manual case keeping the reservation is the correct behavior. Where genuine cross-train
contention exists, it is Meet coordination's job to prevent it up front (capacity + holding-point
chunking), consistent with "prevent, don't recover."

---

### Note on the in-flight gap-analysis

The braking-distance gap-analysis that started this session made four doc edits to `design_new.md`
describing **model B** (the slope start-block exemption, the cap parks-at/rolls-through rule, the
cushion ownership, and the braking-lock constant values). Those remain accurate for the **current
code** and are harmless to keep until this model lands, at which point the acquisition sections are
rewritten wholesale. The Phase 2 citation cleanup was paused and can resume or be dropped.
