# Circular route could not close its lap

- **Status:** Fixed; `design_new.md` swept.
- **Test reference:** `tests/test_demo7_circular_lap_seam.py` (dt sweep 0.1 / 0.2 / 0.3).

## Symptom

demo7 traversed its four blocks in loop order

`BLK_AA62D2 -> BLK_AA866E -> BLK_AA7B7E -> BLK_AA6FB5`

and then, on the first wrap, could not take authority back into `BLK_AA62D2`.
The run kept the old path, rolled to its authority end, stopped there, and the
resulting path completion found no matching stop task — `route_completion_mismatch`,
which halts the whole runtime. Reproduced at simulation time 148.5 s.

## Root cause

`Path.resource_sequence` is a *history*: it keeps every block the path has ever
authorized. The repeated-resource rule was enforced against that history, but
what the rule protects is *concurrent* holdings — one path must not hold one
block twice at the same time. Those two readings agree for an acyclic route and
diverge for a loop, where the lap ends where it began.

By the time the walk wraps, the seam block has been fully released: head entry
consumed its reservation and tail vacate cleared `occupied_by`, so it is FREE.
Authorizing it again is an ordinary acquisition. The history-based test rejected
it anyway, and that rejection cascaded:

1. `extend_path()` rejected the wrapped target.
2. Phase 1 only latched a cancel-and-replan for `INVALID_REVERSE`, so the
   rejection was recorded as an error and the stale path retained (110 retries).
3. The retained authority ended short of the target, so ATC brought the train to
   a stand there — and *stopping* is what arms `try_complete_active_path()`.
4. Completion is stop-task-only by contract, and a waypoint-only route has no
   stop task to match, so the mismatch became fatal.

Three separate divergences from `design_new.md`, all of which the doc got right:

| Site | Doc | Old code |
| --- | --- | --- |
| repeated-resource rejection | belongs to `no_viable_path` (**return contract**) | returned `invalid_request` |
| Phase-1 failure branch | structural failures latch cancellation (**Planning Kernel**) | only `invalid_reverse` did |
| extend toward an already-reached target | `reached_requested_target` (**return contract**) | fell into the same rejection |

**Route-definition validation** explicitly sanctions the configuration that crashed: "a waypoint-only
`CIRCULAR` route runs the loop continuously."

## Fix

`PathDispatcher._blocks_held_by(path)` is the new single definition of what a
path holds right now — its not-yet-consumed reservations plus whatever its train
still physically occupies. Three sites test against it instead of against
`get_all_blocks()`: the `extend_path` guard, the candidate revisit filter, and
`get_reachable_endpoints_for_extension`. Switches keep the history test, because
a path holds its switch locks for its whole lifetime.

Alongside that:

- `extend_path()` returns `reached_requested_target` when the requested block is
  already the authority end. The test is on the block alone: a task authored
  `task_direction = "either"` re-resolves every tick and legitimately flips
  direction once the head is inside the target block, and keying on direction
  read that flip as a request extend could not serve. The result reports the
  live authority's own direction rather than echoing the caller's.
- Phase 1 latches cancel-and-replan for `NO_VIABLE_PATH` as well as
  `INVALID_REVERSE`. `BLOCKED_BEFORE_TARGET` deliberately stays out — it is a
  temporary live conflict and the path stays valid.
- Phase 1 writes `task_runs[...].resolved_target` from the dispatcher's result
  rather than from the tick's re-resolve, so stop completion's match key
  describes the authority the path actually holds.
- demo7's default mode is now `lap_stop`: three waypoints plus a stop and a
  dwell on the starting block — one lap, one stand, repeat. Because the lap ends
  on a stop, the path completes there and the next lap starts from a fresh
  `create_path`, which is what keeps `resource_sequence` from accumulating.

## Two claims in the original report that were wrong

- **`demo7.py` said the loco "finished at BLK_AA6FB5 while its active path still
  targeted BLK_AA866E."** It did not. The path's `requested_destination_block_id`
  was `BLK_AA6FB5` — which is exactly why the completion gate accepted it.
  `PATH_T_LOOP_TO_BLK_AA866E_001` is only the creation-time path *id*.
- **The suggested fix — treat every repeated-resource rejection as
  cancel-and-replan — would have broken the demo.** Of 491 rejections in a
  single run, 381 were `target == authority_end`: the path already reached the
  target and the call was a harmless no-op. Latching a cancel on those tears
  down working authority on three of every four legs.

## Known limitation

A loop whose seam runs through a *switch* still cannot close: switch locks are
held for the path's lifetime, so the switch never leaves the held set. Closing
that case needs per-occurrence lock accounting. A switchless oval — the common
display layout — is unaffected.

Truly stopless looping (`DEMO7_MODE = "continuous"`) now laps indefinitely
without failing, but nothing completes its path, so `resource_sequence` grows by
one lap per lap. Fine for a short demo; use `lap_stop` for anything long-running.
