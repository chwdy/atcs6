# ATCS6 Refactor Implementation Index

This file is a compact implementation map for active coding.
It does not define new behavior.
If this file conflicts with `design_new.md`, `design_new.md` wins.

## How To Use This File

- Keep this file open while implementing.
- Use it to find the canonical home of a rule quickly.
- Use `design_new.md` when a change touches ownership, payload shape, state transitions, callback order, completion/cancellation behavior, or tie-break rules.
- If you make a new cross-component decision during implementation, patch `design_new.md` first, then update the short log at the bottom of this file.

## Fast Map

| Question | Canonical Home In `design_new.md` | Working Rule |
| --- | --- | --- |
| Who owns live runtime orchestration? | `Core Runtime Loop`, `SimulationController runtime surface` | `SimulationController` owns the shared runtime surface, registries, tick loop, per-train phase ordering, and `add_train(...)`. |
| Who owns live authority? | `Layered Architecture`, `Path And PathDispatcher` | `PathDispatcher` owns `Path` records and active-authority registries; `TrainController.received_path_plan` is only a pushed mirror. |
| Who owns live span and speed? | `Train Entity`, `TrainController`, `TrainPhysics` | `TrainController.current_span` and `TrainController.current_speed_kmh` are canonical; `TrainPhysics` only computes `PhysicsSpanUpdate`. |
| Who writes physical block occupancy? | `Dispatcher progress/completion flow`, `Block`, `BlockManager` | Normal runtime block entry goes only through `PathDispatcher.on_controller_progress(...)` -> `BlockManager.occupy_blocks(...)`. |
| Who writes physical switch occupancy/protection? | `Switch Entity`, `Dispatcher progress/completion flow` | Dispatcher-mediated resource-entry and tail-vacate logic owns `Switch.occupied_by` and `train_protection_claims`. |
| Who commits path completion? | `Core Runtime Loop`, `Path completion notification flow`, `Dispatcher progress/completion flow` | Phase 3 drains post-move progress first and commits any movement-derived cancellation inline, then runs the post-physics controller stop check, then samples current system time and calls `PathDispatcher.try_complete_active_path(...)`; only after that normal completion chance does it consume any remaining explicit cancel/OFF teardown. |
| Who consumes pass-through waypoints? | `Canonical Route-Progress Events` | `RouteExecutor.handle_train_advanced(...)` is the Phase-3 head-advance callback. Each call consumes at most ONE waypoint for its single reported block advance against the current next waypoint pointer. If the same physics tick crossed multiple boundaries, the next queued callback is what discovers and tests the next waypoint. The callback must not arm or persist the next planning target. |
| Is route progress depart gated? | `Dispatcher progress/completion flow`, `TrainController` | Stop completion is depart gated; head-entry route progress is not. |
| Who owns dwell entry/exit? | `Arrival detection`, `Dispatcher completion handling`, `TrainController` | `RouteExecutor` records stop arrival and enters dwell on the next eligible Phase 1. Dwell is a normal route step with no path creation while pending/active; the same train's later Phase 3 cleanup pass owns the dwell-complete timer check, marks the stop waypoint finished, calls `TrainController.finish_dwell()`, and performs any terminal route removal. |
| Where is the one-active-path rule? | `Layered Architecture`, `Path status state machine`, `create_path() flow` | One `ACTIVE` path per train; grow that same path with `extend_path(...)`. |
| How do cross-phase handoffs work? | `Core Runtime Loop` | Use the controller-owned per-train Phase 3 mailbox/flag table: `queued_progress_reports`, `pending_cancel_request`, `pending_power_off`. Movement-derived deviation commits inline while Phase 3 drains `queued_progress_reports`. `pending_cancel_request` is only the explicit non-movement cancel handoff, including operator/admin cancel, route-edit invalidation, and EB; if multiple same-tick writers set it, the latest payload wins and Phase 3 still commits at most one explicit latched cancel after the normal completion chance. API/UI/admin operations set commands or latches and never call dispatcher teardown inline. If Phase 1 decides a live path must be replaced, it only refreshes that cancel handoff and returns; after the Phase 3 cancel/callback flow and stopped recovery, a later Phase 1 creates a brand-new path. Stop completion and dwell-complete cleanup are Phase 3 checks, not mailbox latches. |
| Can same-tick retries happen? | `Core Runtime Loop`, `Planning Kernel` | Yes, but only while the work stays inside one phase. Phase 1 starts by correcting current physical position through covered-waypoint normalization, may skip already-covered waypoints on the same pass, may then arm the first uncovered target on that same pass, must not retry the same resolved `(block, direction)` pair twice in one pass, and sets `NO_PATH` only when all same-tick candidates fail without authority creation or growth while no live path exists. |
| Where is the hard-conflict behavior? | `Dispatcher progress/completion flow`, `Block`, `BlockManager` | Conflicting physical entry is unrecoverable in v1: emit fatal diagnostic, stop simulation advancement, clear involved trains' authority, cancel/remove involved route runs, require manual cleanup. |
| How are reservation-release mismatches handled? | `BlockManager` | Ignore the mismatched block and emit a warning/diagnostic. |
| How does power-off teardown run? | `Core Runtime Loop`, `Power-off procedure`, `Power/cab/mode command contracts` | `TrainController.power_off_cmd()` immediately clears `ato_running` and sets `pending_power_off`; the same train's later Phase 3 consume pass handles normal report/completion work first, then cancels any still-live path, removes the `RouteRun`, and transitions the controller to `OFF`. While already `OFF`, route Phase 1 and Phase 3 are skipped, but the Phase-2 command slot still runs so `TrainController.power_on_cmd()` can be accepted. |
| When is lookahead refreshed? | `TrainController`, `Core Runtime Loop` | Phase 2 starts from the cached lookahead produced by the previous post-physics refresh when present, or builds it on demand if absent; any `receive_path_plan(...)` replacement or refresh clears that cache immediately, and `TrainController.on_physics_updated(...)` then refreshes canonical lookahead after physics every tick and stores the next-tick cache. |
| How are scheduling priorities interpreted? | `Core Runtime Loop`, `Route And RouteExecutor` | `Route.priority` is an integer from `0` to `9`, with `9` highest and `0` lowest. Omitted route priority defaults to `5`, and non-route/one-off/manual trains also run at effective priority `5`. |
| How does manual power-percent mode move? | `TrainController`, `TrainPhysics`, `SpeedProfile` | In manual `power_percent` mode, `manual_power_percent` maps directly through calibration to voltage and absolute `speed_kmh` for physics. Service acceleration/deceleration rates do not shape that submode; ATC/service/EB braking forces the percent to `0` for that tick. |
| What does a disabled switch mean? | `Switch Entity`, `Pathfinder`, `TrainPhysics` | A disabled switch is missing track/open circuit. Pathfinding and live traversal treat it as absent, and runtime enable/disable is a topology-availability change that triggers caution/topology reevaluation. |
| What is the outer tie-break for equal candidates? | `create_path() flow`, `Route And RouteExecutor` | Lower `tile_count`, then lower `walk_length_cm`, then lower `candidate_rank`, then final dead-heat `end_a`. |
| Where are the canonical shared schemas? | `Canonical Entity Index` | `Path`, `path_tile`, `resource_sequence`, `received_path_plan`, `RouteRun`, `TrainSpan`, `PhysicsSpanUpdate`. |

## Recommended Implementation Order

1. Runtime surface and registries
   What to land:
   - `SimulationController` as the shared runtime surface
   - shared controller registry keyed by `train_id`
   - shared physics registry keyed by `train_id`
   - `add_train(...)` writing initial `TrainController.current_span`
   Exit condition:
   - no layer constructs controllers directly except through `SimulationController.get_or_create_controller(train_id)`

2. Shared schemas and data ownership split
   What to land:
   - `Path.resource_sequence`
   - `path_tile` entry shape
   - `received_path_plan`
   - `TrainSpan`
   - `PhysicsSpanUpdate`
   Exit condition:
   - producers and consumers all use the same shape, with no train-side runtime mirrors left as hidden dependencies

3. Block and switch resource model
   What to land:
   - computed `Block.state`
   - `reserved_for_train`
   - `BlockManager.reserve_blocks_for_path(...)`
   - `BlockManager.release_path_block_reservations(...)`
   - `BlockManager.on_block_tail_vacated(...)`
   - `Switch.locks`, `occupied_by`, `flank_protection`, `train_protection_claims`
   Exit condition:
   - no normal runtime write path bypasses `BlockManager` for block state, and no runtime switch lock state lives on `TrackSegment`

4. Dispatcher authority lifecycle
   What to land:
   - `paths_by_id[path_id]`
   - `active_path_id_by_train[train_id]`
   - `create_path(...)`, `extend_path(...)`, `cancel_path(...)`
   - dispatcher push of `received_path_plan`
   - synchronous completion/cancellation callbacks
   Exit condition:
   - old completion-event queue is gone, dispatcher is the sole owner of live authority, and all cancel commits happen from Phase 3

5. Controller runtime model
   What to land:
   - controller-owned span/speed
   - controller state machine
   - lookahead as canonical target-speed/limit source
   - `departure_ready_path_id`
   - `receive_path_plan(...)`
   - `on_physics_updated(...)`
   Exit condition:
   - `Train` no longer acts as a runtime mirror for position, speed, state, or path authority

6. Route execution model
   What to land:
   - `RouteRun` field set
   - `_arm_next_target(...)` (merged `_find_next_waypoint_target` as internal step)
   - covered-waypoint normalization
   - `handle_train_advanced(...)`
   - completion/cancellation recovery
   Exit condition:
   - route progress comes only from the route-owned events defined in `design_new.md`, and Phase 1 always corrects current physical position before arming authority

7. Physics integration
   What to land:
   - `TrainPhysics.tick(...)` producing `PhysicsSpanUpdate`
   - boundary-by-boundary callback flow
   - no direct dispatcher/block writes from physics
   Exit condition:
   - all normal progress, release, and completion effects go through controller -> dispatcher

8. UI and export cleanup
   What to land:
   - `Train.to_dict()` persistent/config only
   - `TrainController.to_dict()` live/runtime only
   - UI reads from lookahead / controller payloads / dispatcher read models
   Exit condition:
   - no UI dependency still reads removed train-side live mirrors as the source of truth

## High-Risk Invariants To Recheck During Coding

- One `ACTIVE` path per train.
- `PathDispatcher` is the sole owner of live authority.
- `TrainController.received_path_plan` is a pushed mirror only.
- `TrainController.current_span` and `current_speed_kmh` are canonical live physical state.
- `TrainPhysics` computes movement; it does not mutate dispatcher state directly.
- `TrainPhysics.tick(...)` must callback at least once every tick, even on no-change ticks.
- Phase 3 is the only phase that calls `PathDispatcher.on_controller_progress(report)`; movement-derived cancellation commits inline during that report drain, and explicit latched cancellation commits later in the same Phase 3 pass.
- EB requests cancellation through `pending_cancel_request`; Phase 3 commits the dispatcher cancel.
- `RouteExecutor` never calls `PathDispatcher.cancel_path(...)` from Phase 1 or Phase 2; incompatible live authority means latch cancel and stop planning until Phase 3 cancellation has finished.
- Route edits, operator retargeting, and path-id mismatches use cancel-and-replan; do not keep a hidden desired target attached to a still-live path.
- `PathDispatcher.cancel_path(...)` releases future reservations/locks and clears controller authority before it marks the path `CANCELLED` and clears `active_path_id_by_train[train_id]`.
- `current_waypoint_index` is physical progress; `target_waypoint_index` is planning only.
- Pass-through progress is head-entry based and not depart gated.
- Stop completion is depart gated and dispatcher-committed only.
- Phase 3 owns the dwell-complete timer check, `finish_dwell()`, and any terminal route removal; next-leg planning still waits for a later Phase 1 tick.
- Completion `event_time` is sampled from the current system clock at the Phase 3 completion call site.
- `NO_PATH` must not coexist with a still-live path or `received_path_plan`; truncated live authority remains `RUNNING`.
- Route-managed already-covered stop targets are skipped, not same-block routed.
- Phase 1 corrects already-covered/missed waypoints before planning a new path, and may continue the same pass after such correction when no Phase-3-owned mutation is needed.
- Any `receive_path_plan(...)` replacement or refresh clears cached lookahead; the next Phase 2 rebuilds on demand.
- Hard conflicting physical entry is unrecoverable in v1 and has no automatic resume workflow.
- State-machine transitions for STANDBY ↔ RUNNING_MOVING and RUNNING_WAITING → RUNNING_MOVING use reactive speed-based evaluation: `TrainController.tick()` observes `current_speed_kmh` written by the previous tick's `on_physics_updated(...)` and transitions accordingly. This is the only mechanism for manual-mode STANDBY → RUNNING_MOVING.

## Minimal Open Tabs While Implementing

- `design_new_implementation_index.md`
- `design_new.md`
- the file you are actively editing
- one nearby caller or consumer for the contract you are changing

## When You Need To Decide On The Fly

1. Check this file for the canonical home.
2. Read the matching section in `design_new.md`.
3. If the issue is purely local implementation detail, decide locally.
4. If the issue changes ownership, payload shape, lifecycle, callback order, or failure semantics, patch `design_new.md` before continuing.
5. Add a one-line note below so the new decision is easy to relocate later.

## Implementation Decision Log

- 2026-04-27: Added compact implementation index; `design_new.md` remains authoritative.
- 2026-04-27: Hard conflicting physical entry is unrecoverable in v1; no automatic resume workflow is defined.
- 2026-04-27: Head-entry route progress is not depart gated; only stop completion is depart gated.
- 2026-04-27: Equal-metric outer tie-break is `tile_count`, then `walk_length_cm`, then `candidate_rank`, then final dead-heat `end_a`.
- 2026-04-27: The "one extra attempt" rule means immediate next waypoint plus at most one additional waypoint beyond it; no stored flag.
- 2026-04-29: `pending_cancel_request` is the controller-owned handoff for explicit non-movement cancel requests, including operator/admin cancel and EB; movement-derived deviation cancels inline during Phase 3 report drain, and only Phase 3 commits `PathDispatcher.cancel_path(...)`.
- 2026-04-29: Completion `event_time` is sampled from the current system clock when Phase 3 calls `PathDispatcher.try_complete_active_path(...)`.
- 2026-04-29: Phase 1 starts by correcting current physical position through covered-waypoint normalization and may continue the same pass after skip/correction before arming the first uncovered target.
- 2026-04-29: `power_off_cmd()` clears `ato_running` immediately in Phase 2, while route/path teardown and OFF transition still wait for Phase 3 cleanup.
- 2026-04-29: Phase 2 starts from cached lookahead when available; `TrainController.on_physics_updated(...)` performs the regular post-physics lookahead refresh that feeds Phase 3 and the next tick.
- 2026-04-29: When Phase 1 decides a live path must be revoked, it only refreshes `pending_cancel_request` and returns; `PathDispatcher.cancel_path(...)` later releases future reservations/locks and clears controller authority before clearing the train's active-path marker.
- 2026-04-30: Route edits, retargeting, and path-id mismatches use cancel-and-replan: Phase 1 latches cancel, Phase 3 commits it, and a later eligible Phase 1 replans from current physical progress with a brand-new path.
- 2026-04-30: `receive_path_plan(...)` clears cached lookahead immediately; the next Phase 2 rebuilds it on demand from the new authority projection.
- 2026-04-30: Dwell entry happens on the next eligible Phase 1 after arrival is recorded, while dwell-complete / `finish_dwell()` / terminal route removal are owned by the same train's later Phase 3 cleanup pass.
- 2026-04-30: `NO_PATH` is only a no-live-authority state, and pass-through targets are served by route progress/covered-waypoint handling rather than by an overshot-pass-through deviation rule.
- 2026-04-30: Operator-issued EB is `TrainController.activate_emergency_cmd()`, queued in the operator-command drain between `set_handle/set_manual_power_percent` and `release_emergency_cmd()`. ATC/no-authority EB still raises from inside `TrainController.tick()`. Both paths write `pending_cancel_request` with `EB` for Phase 3 commit through the same dispatcher cancel-and-release path used by other cancellation kinds.
- 2026-04-30: Phase 3 ordered steps: drain queued reports → completion check → dwell-complete → stopped-recovery cleanup (consume `RouteRun.path_cancelled` and/or `TrainController.eb_released` when controller is `IDLE` / `STANDBY`, `received_path_plan is None`, EB and power-off latches clear) → consume `pending_cancel_request` → consume `pending_route_abort` → consume `pending_power_off`. Stopped-recovery cleanup is a multi-tick latch.
- 2026-04-30: Cancellation and completion use the same canonical terminal-teardown order: release future block reservations → release future switch/flank locks → clear controller authority (`receive_path_plan(None)`) → for completion call `on_path_complete()` → mark terminal status → clear `active_path_id_by_train` → call route callback.
- 2026-04-30: `Block.reserve_for_path(train_id, direction)` accepts either compass `exit_edge` (N/E/S/W) or block-relative `forward`/`reverse`; the block converts via `ordered_tiles` + `index0_forward_entry_edge` and stores the compass `exit_edge` in `Block.reserved_direction`.
- 2026-04-30: `SimulationController.add_train(...)` is sync, all-or-nothing, and serialized between ticks; it is the one accepted exception to the phase-only-mutation rule.
- 2026-04-30: `TrainController.activate_emergency_cmd()` has no precondition except `eb_active == False` — emergency must be available at any time, any state, including `OFF`. `release_emergency_cmd()` requires `power_on`, `eb_active`, and stopped.
- 2026-04-30: `travel_direction` derives from cab orientation (cab faces forward); writers are `set_cab(...)` and `depart()` auto-cab-selection; cleared to `None` when no cab is selected.
- 2026-04-30: Phase 1 dwell-pending detection: if `Route.tasks[next_index].task_kind == "dwell"`, treat dwell as the active route step. Dwell timing uses `RouteRun.task_runs[dwell_index].arrived_at` (set when the preceding stop arrival is recorded). `dwell_target` and `arrived_at_platform_time` are removed; data lives in `task_runs[i]`.
- 2026-04-30: Operator-command drain slot 3 splits into 3a `set_mode(...)` then 3b `set_manual_mode_type(...)` (latter requires `control_mode == "manual"`).
- 2026-04-30: Route-edit / reconciliation cancel kind selection: operator-driven retargeting and operator-edited route swaps use `OPERATOR_CANCELLED`; system-detected invariant violations and non-operator-initiated reconciliation use `REVOKED`.
- 2026-04-30: Phase 1 is a state-machine. It may only run create_path / extend_path when the controller is `IDLE` / `STANDBY` (or, for extend, on a still-live `received_path_plan`). While the train is still braking under no-authority or EB, Phase 1 produces no authority and waits for a later tick.
- 2026-04-30: Phase 3 head-advance drain is a callback-by-callback consumption — each `RouteExecutor.handle_train_advanced(...)` callback compares its single reported block advance against the current next waypoint and consumes at most ONE waypoint. Later queued callbacks in the same drain discover later waypoints. The callback must not call `_arm_next_target(...)`, write `task_runs[i].resolved_target`, or create/extend authority; Phase 1 arms the next target later.
- 2026-04-30: `add_train(...)` is the sole entry point for writing the initial `TrainController.current_span`, including persistence-bootstrap; no separate span-initialization helper exists.
- 2026-05-02: Major v1 design refactor: (1) `RouteTask`/`RouteTaskRun` replace `Waypoint`/`RouteRunResolvedTarget`/parallel timing fields; (2) `RouteRun.active_path_id` removed — use `received_path_plan.path_id` directly; (3) `received_path_plan` trimmed to `path_id`, `resource_sequence`, `authority_end_block_id` only; (4) latch renames: `pending_stopped_recovery`→`path_cancelled`, `pending_eb_release_recovery`→`eb_released`, `pending_departure_request`→`departure_requested`, `pending_dwell_entry_request`→`dwell_entry_requested`; (5) `_find_next_waypoint_target` merged into `_arm_next_target`; (6) EB and power-off are independent latches (no override/upgrade); (7) `atc_limit_reason_code` enum added: `authority_stop`, `caution`, `obstruction`, `no_authority`, `no_cab`, `wrong_direction_cab`, `eb`.
- 2026-04-30: `set_cab(...)` clears `departure_ready_path_id` (cab change invalidates depart acceptance); `set_mode(...)` does NOT clear it (mode is not part of the depart-acceptance contract).
- 2026-04-30: `cancellation_kind` (and `RouteRun.current_route_status.pending_cancellation_kind`) are diagnostics only. Planning, routing, and recovery branching is driven by controller state, latches (`eb_active`, `pending_power_off`), `RouteRun` presence, and current span coverage — never by reading the cancellation enum.
