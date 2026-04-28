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
| Who owns live runtime orchestration? | `Core Runtime Loop`, `SimulationController runtime surface` | `SimulationController` owns the shared runtime surface, registries, and `add_train(...)`. |
| Who owns live authority? | `Layered Architecture`, `Path And PathDispatcher` | `PathDispatcher` owns `Path` records and active-authority registries; `TrainController.received_path_plan` is only a pushed mirror. |
| Who owns live span and speed? | `Train Entity`, `TrainController`, `TrainPhysics` | `TrainController.current_span` and `TrainController.current_speed_kmh` are canonical; `TrainPhysics` only computes `PhysicsSpanUpdate`. |
| Who writes physical block occupancy? | `Dispatcher progress/completion flow`, `Block`, `BlockManager` | Normal runtime block entry goes only through `PathDispatcher.on_controller_progress(...)` -> `BlockManager.occupy_blocks(...)`. |
| Who writes physical switch occupancy/protection? | `Switch Entity`, `Dispatcher progress/completion flow` | Dispatcher-mediated resource-entry and tail-vacate logic owns `Switch.occupied_by` and `train_protection_claims`. |
| Who commits path completion? | `Path completion notification flow`, `Dispatcher progress/completion flow` | Controller reports `stop_complete`; dispatcher accepts it only when authority reaches requested target. |
| Who consumes pass-through waypoints? | `Canonical Route-Progress Events` | `RouteExecutor.handle_train_advanced(...)` consumes pass-through on head entry into the resolved block. |
| Is route progress depart gated? | `Dispatcher progress/completion flow`, `TrainController` | Stop completion is depart gated; head-entry route progress is not. |
| Who owns dwell entry/exit? | `Arrival detection`, `Dispatcher completion handling`, `TrainController` | `RouteExecutor` decides dwell entry and calls `TrainController.enter_station_dwell()` / `finish_dwell()`. |
| Where is the one-active-path rule? | `Layered Architecture`, `Path status state machine`, `create_path() flow` | One `ACTIVE` path per train; grow that same path with `extend_path(...)`. |
| Where is the hard-conflict behavior? | `Dispatcher progress/completion flow`, `Block`, `BlockManager` | Conflicting physical entry is unrecoverable in v1: emit fatal diagnostic, stop simulation advancement, clear involved trains' authority, cancel/remove involved route runs, require manual cleanup. |
| How are reservation-release mismatches handled? | `BlockManager` | Ignore the mismatched block and emit a warning/diagnostic. |
| What is the outer tie-break for equal candidates? | `create_path() flow`, `Route And RouteExecutor` | Lower `tile_count`, then lower `walk_length_cm`, then prefer `end_a` over `end_b`, then keep the shared helper's stable order. |
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
   - old completion-event queue is gone and dispatcher is the sole owner of live authority

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
   - `_find_next_waypoint_target(...)`
   - covered-waypoint normalization
   - `handle_train_advanced(...)`
   - completion/cancellation recovery
   Exit condition:
   - route progress comes only from the route-owned events defined in `design_new.md`

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
- `current_waypoint_index` is physical progress; `target_waypoint_index` is planning only.
- Pass-through progress is head-entry based and not depart gated.
- Stop completion is depart gated and dispatcher-committed only.
- Route-managed already-covered stop targets are skipped, not same-block routed.
- Hard conflicting physical entry is unrecoverable in v1 and has no automatic resume workflow.

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
- 2026-04-27: Equal-metric outer tie-break prefers `end_a` over `end_b`.
- 2026-04-27: The "one extra attempt" rule means immediate next waypoint plus at most one additional waypoint beyond it; no stored flag.