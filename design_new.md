# ATCS6 Desired Design Model

This document defines the target refactor contracts and changed cross-component behavior for the current ATCS6 codebase.
It is intended to guide an engineer refactoring the existing implementation, not to replace the code as a complete standalone implementation spec.
Unchanged entity-local fields, helper internals, and serialization details continue to follow the current code unless this document states otherwise.
If current code conflicts with a changed contract stated here, this document wins.

## Scope

Primary files reviewed:

- `simulation.py`
- `train.py`
- `train_controller.py`
- `train_physics.py`
- `block.py`
- `block_manager.py`
- `path.py`
- `path_dispatcher.py`
- `route_executor.py`
- `train_route.py`
- `track.py`
- `switch.py`
- `network.py`
- `speed_profile.py`
- `block_power.py`

Terminology:

- `path`: a dispatcher-managed reserved movement authority between blocks.
- `route`: a higher-level travel plan made of stop / `pass_through` waypoints that ultimately resolve to concrete block targets.
- `block`: an occupancy and reservation unit.
- `track tile`: physical rail geometry on the grid.
- `platform`: the concrete physical stopping place on the track. Dispatcher targets and stop positions are always expressed against a concrete block traversal frame, never against a logical station group; when a waypoint resolves through station/platform data, the platform supplies the linked blocks and traversal context used to derive that block target.
- `station`: a logical grouping of one or more platforms used only for route-level stop selection. When a route targets a station-level stop, `RouteExecutor` must first resolve one concrete platform for that attempt and then derive the dispatcher request contract from that resolved platform.

## Design Direction

**Ownership split**

- `TrainController` should own live operational state: ATC/ATO/manual mode, handle position, future manual `power_percent`-mode inputs, and the canonical live lookahead data series. "Target speed" and "speed-limit reason" should not be stored as standalone fields anywhere; they should be the value and reason metadata at position 0 of the lookahead series.
- `TrainController` should also own live power/cab state: `power_on`, active cab selection, and any live direction-selection inputs tied to that active cab. After the runtime 1:1 `Train` <-> `TrainController` pairing is established, those values are not re-synced from `Train` on every tick.
- `Train` should own persistent and physical state only: identity, consist, schedule/route assignment, position, and actual `current_speed`. Live ATO/ATC/path-runtime state should not be mirrored on `Train`.
- `manual_target_speed` should be removed entirely. With the new handle plus power-percentage manual model there is no per-train target speed to store, so no compatibility shim is needed.
- Manual control should have two controller-owned submodes:
  - `simulation`: user drives with handle notches; the handle command flows to `TrainPhysics`, which integrates it into speed change over time.
  - `power_percent`: user drives with percentage power; the controller passes raw percentage power to `TrainPhysics`, which converts it via train/profile calibration data such as `SpeedProfile.power_speed_curve`.
- `TrainPhysics` and `BlockPowerController` should not decide manual-mode policy. They should execute whichever command the controller produces and perform command-to-speed conversion and voltage emission.
- Percentage-power mapping data should live in `SpeedProfile.power_speed_curve` as part of train calibration and be read by physics.

## Layered Architecture

**Layered responsibilities**

- `PathDispatcher` should execute the route plan by acquiring blocks, locking switches, and managing movement authority, and it should plan for the controller by providing the dispatcher-owned authority structure plus the exact terminal stop position.
- `PathDispatcher` should remain the sole owner of dispatcher path records. `TrainController.received_path_plan` is only a pushed view of the currently active authority, not the source of truth.
- `PathDispatcher` should keep each path record keyed by `path_id` in a dispatcher-owned registry such as `paths_by_id[path_id]`.
- `PathDispatcher` should also keep a single active-authority index such as `active_path_id_by_train[train_id]`. Each train has at most one active authority path at a time, and the controller inbox mirrors only that newest active path.
- Cancelled or completed paths may remain in `paths_by_id[path_id]` for teardown bookkeeping and history after controller authority has been cleared, but they are not active authority and are never mirrored into `TrainController.received_path_plan`. In v1, `teardown_pending` means only the immediate dispatcher-owned authority teardown is still running: clear reservation ownership, clear controller authority, clear `active_path_id_by_train[train_id]`, and finish any dispatcher bookkeeping that belongs to the old authority. Physical train motion after completion/cancellation is **not** blocked on `teardown_pending`: `TrainPhysics` keeps moving the train according to live network geometry/state, and any later occupancy/release callbacks are resource-local aftermath rather than retained authority. `PathDispatcher.create_path(...)` must reject a same-train request only when that train still has an `ACTIVE` path; once the old path is no longer active, later creation is governed only by ordinary resource conflicts.
- Physics-facing dispatcher callbacks should identify the moving train by `train_id` only. Active progress callbacks resolve the current authority from `active_path_id_by_train[train_id]`. After authority has been cleared, later physical release must not depend on a retained teardown row surviving: block release is driven by `block_id` plus `direction` through `BlockManager`, and switch release can resolve pending-claim teardown directly from the `Switch` resource's path-scoped `locks` collection for that `switch_id` by removing only `release_pending` claims owned by that `train_id`.
- `TrainController` should execute the path plan by calculating ATC safety limits, ATO/manual commands, and an active handle or future power command. It should plan for physics by producing the active control command for the current mode.
- `TrainPhysics` should remain an executor only; it should not decide manual-mode policy.
- `PathDispatcher` should pass the full target contract to the controller whenever a path is created or extended: the requested target (`requested_destination_block_id`, `requested_destination_direction`, `requested_stop_position_cm`) plus the current authorized target (`authority_end_block_id`, `authority_stop_position_cm`). The controller should brake to the current authorized stop while `RouteExecutor` may compare requested vs authorized targets to see whether the live authority already reaches the current requested target carried by that path. The controller should derive speed restrictions from lookahead over the network and dispatcher authority structure, not receive per-block speed limits as separate pushed data.
- The dispatcher-owned path record should be the single source of truth for controller authority while it is active. Cancelling or replacing a path in `PathDispatcher` should be sufficient to give the controller a clean slate.

## Core Runtime Loop

**Phase structure**

### Phase 1 - Setup (top-down planning)

1. Advance `simulation_time`.
2. Run `RouteExecutor.update(delta_seconds)`. This decides which waypoint/path is active for each train and calls `PathDispatcher.create_path()` when a new path is needed. A route-managed train should already have its 1:1 `TrainController` by this point; `RouteExecutor` must ensure the controller exists before it reads controller state or asks `PathDispatcher` to push authority.
3. After path creation, `PathDispatcher` should call `controller.receive_path_plan(path_plan)` directly on that assigned train's existing `TrainController`. The pushed payload must follow the canonical **received_path_plan schema**, and its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**, so controller/UI consumers have complete geometry and ordering after train-side path mirrors are removed and do not have to infer the traversal frame later.

### Phase 2 - Execute (downward)

1. For each train, call `_update_train(train, delta_seconds)`:
   - Tick `TrainController`.
   - Tick `TrainPhysics`.

### Phase 3 - Feedback (bottom-up)

1. Within `TrainPhysics.tick(delta_seconds)`, report physics resource-entry transitions directly to `PathDispatcher`.
2. Within `TrainPhysics.tick(delta_seconds)`, report physics tail-vacated resource transitions directly to `PathDispatcher` as typed resource-release events.
3. If `PathDispatcher.on_train_progress_from_physics()` detects a completed path, `PathDispatcher._complete_path_on_full_stop()` should release remaining already-vacated resources, call `controller.on_path_complete()`, clear controller authority via `controller.receive_path_plan(None)`, and then call `route_executor.handle_path_completion()` directly with the completion context it needs (`train_id`, `path_id`, `authority_end_block_id`, `authority_stop_position_cm`, `end_tile`, `event_time`). Path completion must not force-unlock a block or switch that is still physically occupied by the train.
4. Update signals from blocks after all dispatcher feedback has been applied.

**`SimulationController.update()` calls**

- `self.route_executor.update(delta_seconds)` remains the setup entry point.
- `self._update_train(train, delta_seconds)` remains the per-train execute step.
- `SimulationController` should stay an orchestrator only: advance time, call `RouteExecutor.update(delta_seconds)`, call `_update_train(train, delta_seconds)` for each train, and then update signals. It should not forward resource-entry or tail-vacate events itself.
- `SimulationController` should own one shared `PathDispatcher` instance for all trains. `TrainPhysics.tick(delta_seconds)` should report ordered resource-entry transitions and typed tail-vacated resource-release events directly to that shared dispatcher, and `PathDispatcher` should resolve the relevant active or teardown path from its own registries rather than by reading the controller inbox.
- `self.network.update_signals_from_blocks()` should run after dispatcher feedback so signals reflect all block-state changes, including path completion.
- Remove `self.path_dispatcher.get_and_clear_completion_events()` and the loop that calls `route_executor.handle_path_completion()`. Completion should be synchronous inside `PathDispatcher._complete_path_on_full_stop()`.

**Path completion notification flow**

- Remove `_complete_path_on_head_arrival()` and implement a new `_complete_path_on_full_stop()` method. This is not a rename: the trigger changes (head-arrival or head-tile-equals-last-path-tile → full stop only), the notification chain changes (event-queue append → synchronous direct calls to `controller.on_path_complete()` and `route_executor.handle_path_completion()`), and the method body changes accordingly.
- `PathDispatcher._complete_path_on_full_stop(path, train)` should call `controller.on_path_complete()` first so the controller can react to completion while the pushed path plan is still visible, then `PathDispatcher` should clear controller authority by calling `controller.receive_path_plan(None)`, and then it should call `route_executor.handle_path_completion(...)` directly using the canonical completion payload from the **route path-event callback schema**. `RouteExecutor` should treat dispatcher completion as authoritative; it must still compare the callback `path_id` against `exec_state.active_path_id` and ignore stale callbacks, but it does not need a second exact-stop evaluation or live-controller-state check. Completion teardown may drop reservation ownership for already-vacated resources, and it may force-clear `CAUTION` during terminal completion teardown, but it must not force-clear any `OCCUPIED` resource the train still physically occupies.
- `PathDispatcher` should hold a reference to `RouteExecutor`, and it should resolve `TrainController` instances through a shared controller registry keyed by `train_id`. The registry is owned by `SimulationController` and exposed through a public train-ID-based accessor such as `SimulationController.get_or_create_controller(train_id)`; `PathDispatcher`, `RouteExecutor`, and other layers go through that accessor rather than passing whole `Train` entities around just to find a controller or keeping private `_get_or_create_controller` helpers or circular back-references.
- Wiring pattern: `SimulationController` is initialized first and is the canonical shared runtime owner. It then constructs `PathDispatcher` and `RouteExecutor` with a reference to itself so both components can use the same controller registry and shared runtime services through `SimulationController`. They may still hold direct references to each other after construction, but `SimulationController` is the common home and is created before either of them.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()` from `PathDispatcher`.
- Path completion should only be recognized after the train is fully stopped at the terminal stop point. No completion callback should run while the train is still braking or creeping inside the destination block.
- After dispatcher-owned completion teardown, route-level waypoint and dwell logic should decide whether the train enters dwell (`RUNNING_AT_STATION`) or remains in `STANDBY`.

**`_update_train()` flow**

1. Get or create a `TrainController`.
2. Get or create a `TrainPhysics`.
3. On first pairing only, initialize any controller-owned bootstrap values that still come from persisted train data, then establish the runtime 1:1 `Train` <-> `TrainController` pairing for that `train_id`.
5. Tick controller.
6. Tick physics.
7. Sync only the train-owned physical or persistent state back to `Train`.
8. Do not add a separate simulation-owned dispatcher feedback pass. `TrainPhysics.tick()` should report progress and typed tail-vacate events directly to `PathDispatcher`.

**`_update_train()` calls**

- Keep `get_or_create_controller(train_id)` (the public `SimulationController` accessor that owns the controller registry), `_get_or_create_physics(train)`, `controller.tick(delta_seconds)`, and `physics.tick(delta_seconds)`. `RouteExecutor` and `PathDispatcher` both reach the registry through the same public train-ID-based accessor rather than keeping private `_get_or_create_controller` helpers.
- A newly created controller may bind only to train-owned persistent/physical state needed for that 1:1 pairing. In the desired model `Train` does not carry runtime authority fields, so bootstrap must not seed `received_path_plan`, `assigned_path_id`, `ato_path_id`, or `ato_destination_block_id` from `Train`. For route-managed trains, `RouteExecutor` should already have ensured the controller exists before any Phase 1 authority push; `get_or_create_controller(train_id)` in `_update_train()` remains a fallback for non-route flows and recovery.
- If `PathDispatcher` already has active authority for that `train_id` when the controller is created, dispatcher should re-push `received_path_plan` from its own registry after the controller exists; controller bootstrap must not recreate that authority from train-side mirrors. `SimulationController.get_or_create_controller(train_id)` is responsible only for creating the controller object and registering the `train_id -> controller` binding. If it needs persisted/bootstrap train data to initialize that controller, `SimulationController` resolves the `Train` entity internally from `train_id`; callers still use the train-ID-based accessor. After that binding exists, `SimulationController` should call a dispatcher-owned recovery hook such as `PathDispatcher.on_controller_available(train_id)`. That hook is the canonical late-controller recovery entry point: it looks up `active_path_id_by_train[train_id]`, rebuilds the canonical **received_path_plan schema** from dispatcher state, and pushes it into the newly available controller.
- Stop writing `train.target_speed`, `train.speed_limit_reason`, `train.ato_destination_block_id`, and `train.state` here. Consumers that want live target-speed data should read controller lookahead at distance 0 instead; consumers that want live operational state should read `TrainController.state` directly.
- Do not re-sync live power/cab state from `Train` on every tick. If compatibility/bootstrap persistence still stores those values on `Train`, they may be imported once when the controller is first created; after that point `TrainController` is the live owner.
- Replace block-only feedback with resource-entry feedback emitted directly from `TrainPhysics.tick()`, so dispatcher updates can consume both blocks and switches in `resource_sequence` order.
- In the same direct `TrainPhysics -> PathDispatcher` reporting path, emit typed tail-vacated resource-release events so `PathDispatcher` can release a vacated resource according to its resource type (`block` vs `switch`).

**Dispatcher progress/completion flow**

- `on_train_advances_from_physics(train_id, resource_type, resource_id)` should resolve the relevant dispatcher-owned path record for that train and update `path.current_resource_index` when the train head enters a resource in `path.resource_sequence`. `resource_type` is required on every resource-event callback so dispatcher never has to infer it from a bare ID; the globally-unique-ID-namespace alternative is not adopted.
- When the entered resource is a block, that same dispatcher-owned entry handoff must call `BlockManager.occupy_blocks([block_id], train_id)`. In the desired model this is the only normal runtime path that marks a block physically `OCCUPIED`.
- When the entered resource is a switch, the same dispatcher-owned entry handoff should set `Switch.occupied_by = train_id` for that `switch_id`. Switch physical occupancy is separate from reservation/lock ownership.
- Physical occupancy updates still happen even when no `ACTIVE` path remains for that train. If `active_path_id_by_train[train_id]` is already clear because the old authority was completed or cancelled, `PathDispatcher.on_train_advances_from_physics(...)` must still apply the resource-typed physical entry update (`BlockManager.occupy_blocks(...)` or `Switch.occupied_by = train_id`) and simply skip path-progress / completion bookkeeping because there is no active authority row left to update.
- In the desired model there should be no separate `CLEARING` lifecycle state. A live dispatcher-owned authority path stays `ACTIVE` until it is either `COMPLETED` or `CANCELLED`; entering the final block/resource only updates progress and completion predicates.
- Add `PathDispatcher.on_resource_tail_vacated_from_physics(train_id, resource_id, resource_type, direction=None)` as the typed tail-release handoff from `TrainPhysics.tick()`. `direction` is required for `resource_type == "block"` and ignored for switches. Active progress continues to resolve through `PathDispatcher`'s registries rather than from `TrainController.received_path_plan`, but tail-release logic after authority teardown must resolve from the resource itself so it still works after the controller inbox has been cleared:
  - if `resource_type == "block"`, dispatcher should route the release through `BlockManager.on_block_tail_vacated(block_id, train_id, direction)`
  - if `resource_type == "switch"`, dispatcher should clear `Switch.occupied_by` and release the switch resource through the `Switch` entity plus any flank-protection teardown resolved by `train_id` and `source_switch_id`, not by `path_id`
- `on_train_progress_from_physics(train_id)` should resolve the relevant dispatcher-owned path record for that train and complete the path only after the train is fully stopped at the terminal stop point.
- Switch release should be driven by tail-vacate detection in `TrainPhysics`, not by dispatcher tile-index checks.
- Implementation note: every caller that still passes `path_id` into dispatcher progress/completion callbacks must be migrated to the train-ID-only callback contract. Active progress resolution belongs to `PathDispatcher`, but delayed release of a resource that is still physically occupied after authority teardown must resolve from the resource itself (`BlockManager` for blocks, `Switch.occupied_by` / `Switch.locks` for switches) rather than requiring a retained teardown row just to rediscover ownership. On the switch side, that means tail-vacate removes only `release_pending` claims for the vacating `train_id`; it does not need the old `path_id`.

**Controller/train sync**

- `TrainController.tick(delta_seconds)` should continue to own ATC, ATO, and manual-control runtime calculations, but target speed and speed-limit reason should move into the canonical controller lookahead payload instead of standalone scalars.
- `Train` should keep only physical and persistent state in sync from the simulation loop.
- `PathDispatcher.create_path()` should push a path plan directly into the controller, and path cancellation or completion should clear that controller inbox through `PathDispatcher` as its sole writer rather than through train-side mirrors or controller self-mutation.

**Tick-order dependency**

- Phase 1 must run before Phase 2 because the controller should read path tiles that already exist.
- Phase 2 must run before Phase 3 because the dispatcher should read positions that physics has just produced.
- The one-tick lag between a physics action and the route-level reaction remains acceptable so long as ATO safety margins absorb it.

## Units

**Units contract**

- The unit-mixing hazard between `Train.max_braking_rate` (`m/s^2`) and `SpeedProfile.deceleration_rate` (`km/h/s`) persists in the desired model. Resolve it at exactly one boundary: `TrainPhysics` performs the `m/s^2` ↔ `km/h/s` conversion when it reads profile rates, so all controller-facing deceleration math stays in a single unit system. No other layer reinterprets the raw fields.
- The desired model uses one canonical stopped predicate everywhere: `Train.is_stopped()` with `STOPPED_SPEED_THRESHOLD_KMH = 0.1`. Path completion, same-block already-arrived bypass, `RUNNING_WAITING` entry, `TrainController.power_off_cmd()`, `TrainController.set_cab()`, `TrainController.set_mode()`, and `TrainController.release_emergency_cmd()` must all use this same helper instead of introducing layer-specific stop thresholds.

## Canonical Entity Index

**Entity list**

- `TrackSegment`
- `Switch`
- `Block`
- `BlockManager`
- `Path`
- `PathDispatcher`
- `Route`
- `RouteExecutionState`
- `Train`
- `TrainController`
- `TrainPhysics`
- `SpeedProfile`

**Canonical shared contracts**

- `Path` field schema is defined once in the Path section and every producer/consumer should refer back to it by name instead of reconstructing the field set piecemeal.
- `Path.path_tiles` tile-entry schema is defined once in the Path section and every controller/UI/physics geometry projection should refer back to it by name.
- `Path.resource_sequence` entry schema is defined once in the Path section and every controller/UI/physics projection should refer back to it by name.
- `TrainController.state` state-machine table is defined once in the TrainController section and every caller should use that same state set and transition meaning.
- `TrainController.received_path_plan` schema is defined once in the TrainController section and every producer/consumer should refer back to it by name.
- `RouteExecutor` path-event callback schema (`RouteExecutor.handle_path_completion(...)` / `RouteExecutor.handle_path_cancellation(...)`) is defined once in the Route section and every callback chain should refer back to it by name.

## TrackSegment

**Path locking**

- Remove `TrackSegment.locked_by_path`, `locked_state`, and the single-owner lock methods once switch locking is owned entirely by the `Switch` entity.
- Remove `path_direction` from `TrackSegment` in the desired model. Temporary reservation direction belongs at the block/path-plan level: `Block.reserved_direction` is the dispatcher-owned block reservation direction, and `Path.resource_sequence[*].entry_edge` / `exit_edge` plus nested `path_tiles[*].entry_edge` / `exit_edge` carry the canonical per-resource and per-tile traversal direction. `TrackSegment` should not store a second live routing-direction field.
- Replace the single-owner track lock model with the multi-owner `Switch.locks` model described below.
- `TrackSegment` keeps static geometry, classification, and `switch_id`, but it is not the authoritative store for the active switch state. If the segment is a switch, callers resolve the active segment from the owning `Switch.state` in `network.switches` via `switch_id`. `_switch_state` on `TrackSegment` is removed. The old single-owner track-level path lock is not migrated — per-resource `Block.reserved_by_path` / `Block.reserved_for_train` and multi-owner `Switch.locks` play the equivalent role, so there is no transitional phase.

**Track-segment role and traversal**

- The retained `TrackSegment` schema in v1 is explicit:
  - persistent fields kept on `TrackSegment`: `x`, `y`, `track_bits`, `dead_end_ports`, `signal_ids`, `station_id`, `block_section_id`, `switch_id`, `length`, `curve_radius`, `wear_level`, `last_maintenance`, `maintenance_count`
  - computed read kept on `TrackSegment`: `track_type`
  - removed live runtime fields: `_switch_state`, `occupied_by`, `reserved_by`, `locked_by_path`, `locked_state`, `path_direction`
- `TrackSegment` does not carry a separate static `traffic_direction_policy` field in v1. Temporary reservation/travel direction is represented by `Block.reserved_direction`, while finer-grained traversal frame data lives on `Path.resource_sequence[*]` and nested `path_tiles[*]`.
- `TrackSegment.station_id` remains a legacy field name in the current codebase, but in the desired model it refers to membership in the concrete physical platform entity on that tile, not to a logical multi-platform station group. Logical station-group selection remains route-level only.
- `TrackSegment` is not the authoritative store for live switch state or switch locks. Remove `_switch_state`, `locked_by_path`, and `locked_state` outright; the desired model does not keep them. Switch state ownership lives on `Switch`, and lock ownership is expressed through `Switch.locks` (for switches) and `Block.reserved_by_path` / `Block.reserved_for_train` (for blocks).
- If the segment classification says the tile is a switch, `TrackSegment.switch_id` should point to the authoritative `Switch` entity in the network entity database. Live switch state and switch lock ownership should live only on that `Switch` entity.
- Split planning-time traversal enumeration from live-state traversal reads.
- Live traversal helpers in the desired model:
  - `TrackSegment.get_active_exits(entry_edge, resolver) -> List[str]`
  - `TrackSegment.get_active_entry_for_exit(exit_edge, resolver) -> Optional[str]`
  where `resolver` is the network or an equivalent switch-state resolver that can read `Switch.state` via `switch_id`.
- Planning-time helper in the desired model:
  - `TrackSegment.enumerate_traversals(entry_edge) -> List[(exit_edge, required_switch_state)]`
  This helper enumerates every legal geometric traversal through the tile independent of the switch's current live state. For an ordinary non-switch tile, it yields the one legal `(exit_edge, None)` pair. For a switch tile, it yields the legal `(exit_edge, required_switch_state)` pairs that dispatcher path search may acquire.
- `TrackSegment.get_active_exits(...)` and `TrackSegment.get_active_entry_for_exit(...)` map one edge through the currently active segment selected by `Switch.state`. `TrackSegment.enumerate_traversals(...)` is the planner-facing geometry contract and must not consult live switch alignment.
- For Y-shape switch geometry in v1, branch-to-branch traversal is invalid. `TrackSegment.enumerate_traversals(...)` must not yield branch-to-branch options.
- `PathDispatcher` path construction/extension and BFS switch derivation use `TrackSegment.enumerate_traversals(...)`. `TrainPhysics._advance_end()`, tail tracing, and other live movement readers use the active-state helpers. Switch traversal never reads track-stored live state.

**Track-segment invariants**

- Switch-integrity while a train spans a switch: in the desired model, the combination of `Switch.occupied_by` plus the still-held `Switch.locks` entries prevents a switch from flipping out from under the spanning train. A global path lock is no longer required for this guarantee.
- Tail tracing still uses the live-state entry/exit helper, but it now resolves through the live `Switch.state` per the directed resolver contract in **Track-segment role and traversal** rather than through a separately persisted track-tile field.

## Switch Entity

**Switch entity**

- `Switch` should become a first-class resource entity at the same level as a block.
- The retained `Switch` schema in v1 is explicit:
  - identity/location: `switch_id`, `x`, `y`
  - retained physical/configuration fields from current code: `main_length_cm`, `diverge_length_cm`, `main_radius_cm`, `diverge_radius_cm`, `switch_type`, `name`, `notes`
  - new authoritative live/runtime fields: `state`, `occupied_by`, `locks`, `flank_protection`
  - reserved future physical field: `length_cm = 0.0`
  - removed single-owner lock fields: `locked_by_path`, `locked_state`
- Add `state: int` as the authoritative physical switch state. In the desired model the code-defined values are retained explicitly: `0 = MAIN`, `1 = DIVERGE`. When traversal code needs the active switch segment, it should resolve that state from the `Switch` entity rather than keeping a second authoritative live state copy on `TrackSegment`.
- Add `occupied_by: Optional[str]` as the switch resource's physical-occupancy field. `occupied_by` means a train is physically spanning the switch tile right now; it is set on switch-resource entry and cleared on tail-vacate. Reservation/lock ownership remains separate.
- Replace single-owner `locked_by_path` / `locked_state` fields with `locks`, keyed by a claim identity that can distinguish multiple flank claims sourced from different main switches. In v1:
  - a main-usage claim is keyed by `(path_id, claim_role)` with `claim_role == "main"`
  - a flank-protection claim is keyed by `(path_id, claim_role, source_switch_id)` with `claim_role == "flank"`
  Each lock claim stores exactly `path_id`, `claim_role`, `train_id`, `locked_state`, and `release_pending: bool`. When `claim_role == "flank"`, the claim also stores `source_switch_id`, the main switch whose acquisition created that flank-protection claim.
- `acquire_lock(path_id, claim_role, train_id, state, source_switch_id=None)` should first ensure the physical switch state matches `state`; if the current state differs, it should attempt to change the switch into `state` before recording the lock claim.
- Every successful `Switch.state` mutation path, whether it came from dispatcher lock acquisition or from another switch-state command path, must notify `BlockManager.on_switch_state_changed(switch_id)` after the state write succeeds. `Switch` remains the owner of the state mutation itself; `BlockManager` owns the downstream CAUTION reevaluation.
- `acquire_lock(path_id, claim_role, train_id, state, source_switch_id=None)` should be idempotent only for an already-recorded claim on that same canonical claim key with the same `locked_state`, and succeed immediately in that case.
- `acquire_lock(path_id, claim_role, train_id, state)` should allow multiple paths to hold lock claims on the same switch when those claims all require the same physical `state`. This includes a same-train later path adding a new compatible claim after the old path left `ACTIVE` while the train still physically spans the switch.
- `acquire_lock(path_id, claim_role, train_id, state)` must fail if any existing claim on that switch, pending or active, requires a different physical `state`. A same-train later path that needs a different `state` must wait until the old pending claim is gone after tail-vacate.
- While `occupied_by is not None`, the switch may only gain additional compatible lock claims. Dispatcher completion/cancellation must not delete the physically spanning train's old claim immediately; it marks that old canonical claim `release_pending = True` and leaves it in place until tail-vacate.
- `release_lock(path_id, claim_role, source_switch_id=None)` should remove that claim immediately only when the switch is not physically occupied by the same train. If `occupied_by == claim.train_id`, it should set `release_pending = True` on that claim instead of deleting it.
- Add `release_pending_claims_for_train(train_id)` to remove only claims on that switch owned by `train_id` whose `release_pending` flag is already true. `PathDispatcher.on_resource_tail_vacated_from_physics(train_id, switch_id, "switch")` uses this after clearing `Switch.occupied_by`. If the vacated switch was the main switch that originally created flank claims on neighbouring switches, dispatcher must also use each flank claim's stored `source_switch_id` to release only the flank claims created from that main switch, leaving flank claims sourced from any other still-active main switch intact.
- In the desired model, `Switch.locks` is a multi-owner lock collection, but physical occupancy remains single-owner: only one train may have `Switch.occupied_by` at a time.
- Add `flank_protection: List[Tuple[str, int]]` on the switch entity and reserve `length_cm: float = 0.0` for future physical switch length. In v1 each tuple is exactly `(neighbour_switch_id, blocking_state)`, where `blocking_state` is the neighbour state that would create the flank conflict if that neighbour were set toward this switch.
- Migrate call sites from `track.lock_for_path()` / `track.unlock_for_path()` and `track.is_locked_by_path()` to switch-entity lock methods.

**Switch invariants**

- `is_locked()` and `can_lock(state)` should check the `(path_id, claim_role)`-keyed lock-claim collection, not a single field. `can_lock(state)` means "is there a compatible lock state for this switch right now," not "is there only one owner." Pending claims still count for compatibility: a `release_pending` claim that requires a different `state` continues to block a conflicting acquisition until the later tail-vacate callback removes it.
- Once `Switch.locks` is the sole authority, `TrackSegment` lock fields should be removed rather than "kept in sync".

## Flank Protection Computation

**Flank protection computation**

- Compute flank-protection relationships at the network level and store them on each switch entity instead of on individual paths.
- Use one canonical global setting `FLANK_PROTECTION_DISTANCE_CM = 5.0` when searching for nearby switches that require mutual locking.
- At switch placement time, search the network graph outward from the switch tile, determine which neighbour states would create a flank conflict, and append `(neighbour_switch_id, blocking_state)` to `flank_protection`.
- Recompute nearby flank-protection relationships whenever topology changes.
- In v1, flank protection uses the same `Switch.locks` compatibility rule as ordinary switch use: multiple paths may hold compatible same-state lock claims, but physical occupancy is still single-owner and no lock set may demand conflicting physical states. Flank-protection claims follow the same `(path_id, claim_role, train_id, locked_state, release_pending)` contract as primary switch claims, using `claim_role = "flank"` instead of `"main"`, and each flank claim additionally stores `source_switch_id`, the main switch whose acquisition created that claim. Stored flank-protection data remains `(neighbour_switch_id, blocking_state)`; the acquisition-time `safe_state` is derived from it as `1 - blocking_state` because v1 switches are binary.

## Block

**Block model**

- Replace the stored `state: BlockState` field with three underlying primitive fields and make `state` a computed read-only property:
  - `occupied_by: Optional[str]` — physical train presence (non-None means a train is in the block).
  - `_is_caution: bool` — physical safety-margin flag (True when a train just vacated and the next block ahead is still occupied).
  - `reserved_by_path: Optional[str]` — dispatcher reservation ownership for the one active path that currently holds the block reservation.
  - `reserved_direction: Optional[str]` — direction reserved by the dispatcher through the block (N/E/S/W).
  - `reserved_for_train: Optional[str]` — the train ID the reservation is intended for; separate from `occupied_by` (physical presence) so that `BlockManager.occupy_blocks()`'s RESERVED-entry safety check stays effective after `occupied_by` is removed from the RESERVED state.
  - `caution_direction: Optional[str]` — direction of the departing train that triggered `CAUTION` (N/E/S/W). Cleared when `_is_caution` is cleared, either by `_reevaluate_caution_blocks()` during normal state-machine progression, or force-cleared by `release_path_block_reservations(completed=True)` on path completion with train stopped.
- The computed `state` property should use priority order: OCCUPIED > CAUTION > RESERVED > FREE.
  - Return `OCCUPIED` if `occupied_by is not None`.
  - Return `CAUTION` if `_is_caution is True`.
  - Return `RESERVED` if `reserved_by_path is not None`.
  - Return `FREE` otherwise.
- The intended computed-state table is:
  - physical `occupied_by is not None` -> `OCCUPIED`
  - physically free + `_is_caution is True` -> `CAUTION`
  - physically free + not caution + `reserved_by_path is not None` -> `RESERVED`
  - physically free + not caution + not reserved -> `FREE`
- This makes composite physical+reservation states directly representable without contradictions:
  - OCCUPIED + `reserved_by_path` set (start-block scenario): `state` reads `OCCUPIED`; reservation is visible and clearable as a plain field write with no state check.
  - CAUTION + `reserved_by_path` set (path holds block through safety margin): `state` reads `CAUTION`; reservation is likewise clearable unconditionally.
- Treat `reserved_by_path` as the dispatcher's resource lock and `occupied_by` as physical train presence only.
- `occupied_by` should always be `None` in `RESERVED` and `CAUTION`; the dispatcher sets `reserved_by_path`, not `occupied_by`, to claim a block.
- Every `Block` must also expose one immutable internal traversal order over its member geometry, initialized when the block is created and never rewritten by runtime switching or path acquisition. For a direct block target, `requested_destination_direction == "forward"` means movement in the increasing direction of that block-local order, and `"reverse"` means movement in the decreasing direction of that same order.
- Because acquisition decisions need to check all blocks at once, `BlockManager` should be the sole mutator and `Block` should remain a data container.

**`CAUTION` vs reservation independence**

- `CAUTION` is an independent safety state indicating the block ahead (in the departing train's direction) is still occupied; it is orthogonal to path reservation.
- A block can be simultaneously `CAUTION` and `reserved_by_path` (path still holds it for a future or in-progress train), or `CAUTION` with no reservation (train has passed and path is done). With the computed-state model both combinations are natively representable — the `state` property returns `CAUTION` in both cases, and `reserved_by_path` can be cleared by a plain field write with no state guard.
- The `OCCUPIED → CAUTION → FREE` lifecycle is driven purely by physical train movement through `BlockManager`; it does not depend on whether a path reservation exists.
- Path reservation (`reserved_by_path`) is released by the dispatcher on path completion or cancellation, as a separate operation from the occupancy state machine.
- `CAUTION` is directional: `caution_direction` records the direction the departing train was moving. The `CAUTION → FREE` check must only look at the adjacent block in `caution_direction`, not at all neighbours. Without this constraint a following train entering the block immediately behind would incorrectly prevent a `CAUTION` block from clearing, because the `OCCUPIED` neighbour check would match the follower rather than the train ahead that triggered the caution.

**Block methods**

- Remove `Block.can_enter()` from the physical-occupancy path. In the desired model, path reservation checks and physical occupancy are separate concerns: reservation eligibility is checked during `PathDispatcher` / `BlockManager.reserve_blocks_for_path(...)`, while `BlockManager.occupy_blocks([block_id], train_id)` records physical train entry only (single-block calls go through the same atomic helper with a one-item list).
- Remove `Block.occupy()`. With `BlockManager` as the sole mutator, `Block.occupy()` is dead code — `BlockManager.occupy_blocks(...)` writes `block.occupied_by` directly for every block in its list. Keeping `Block.occupy()` would leave a parallel write path that bypasses `BlockManager`'s serialization guarantee.
- Remove `Block.set_to_caution()`. In the new design all `OCCUPIED → CAUTION` transitions are owned by `BlockManager`; callers use `block_manager.on_block_tail_vacated()`. A `Block`-level method that changes occupancy state bypasses the manager's single-serialization guarantee.
- `reserve_for_path(train_id, direction)` (signature changed: `path_id` is removed from the block-level method, both `train_id` and `direction` are required) should stop storing `train_id` in `occupied_by` and instead store it in `reserved_for_train`. `occupied_by` is physical train presence only and must remain `None` while in `RESERVED`. Path membership is recorded separately by `BlockManager.reserve_blocks_for_path(...)` writing `block.reserved_by_path` directly; the block-level reservation field write does not carry `path_id`. Because this method is now a plain field write with no state check, it naturally handles the occupied-start-block case: if the block is already `OCCUPIED` by the assigned train, setting `reserved_by_path` (by `BlockManager`) and `reserved_for_train` (by this method) is additive — `occupied_by` is left unchanged and the computed `state` remains `OCCUPIED`. No special branch for the occupied case is needed.
- `release_path_reservation()` should clear `reserved_by_path`, `reserved_direction`, and `reserved_for_train` only, with no state guard. It must NOT touch `caution_direction` or `_is_caution` — those are physical-lifecycle fields owned exclusively by `BlockManager`. In the computed-state model the method is just a plain field clear, so it works regardless of the current physical state (`OCCUPIED`, `CAUTION`, `RESERVED`, or `FREE`). The old `state == RESERVED` guard is removed because it was the reason OCCUPIED+reserved and CAUTION+reserved blocks had no callable teardown path.
- `Block.reserve_for_path()` and `Block.release_path_reservation()` are **internal helpers** only; all callers go through `BlockManager.reserve_blocks_for_path()` and `BlockManager.release_path_block_reservations()`. They are not part of the public `Block` API.

**Block invariants**

- Remove `Block.occupy()` entirely — dead code with `BlockManager` as sole mutator.
- Remove `Block.set_to_caution()` and `Block.clear()` entirely; those physical transitions are owned by `BlockManager`.
- Remove `Block.can_enter()` outright. It is not kept for reservation-read use either; callers that need to check whether a block is reservable go through `BlockManager.reserve_blocks_for_path(...)` and let the acquisition result report failure.
- Remove `Block.is_available_for_path()` outright. Path-acquisition validation lives in `BlockManager.reserve_blocks_for_path(...)`; there is no per-block predicate method in the desired API.
- `Block.reserve_for_path()` and `Block.release_path_reservation()` are kept as internal helpers called only through `BlockManager` methods, not by any other caller.
- Remove the `occupied_by`-while-`RESERVED` behavior from `Block.reserve_for_path()`; use `reserved_for_train` instead.
- `BlockManager.occupy_blocks(block_ids, train_id)` is the sole physical-entry writer; there is no separate `occupy_block(block_id, train_id)` method. Single-block physical entry calls the same helper with a one-item list. Its RESERVED-entry check (applied per block inside the atomic call) must switch from `block.occupied_by != train_id` to `block.reserved_for_train != train_id`, because `occupied_by` is `None` in `RESERVED` in the new model.

## BlockManager

**Block-manager model**

- `BlockManager` should become the database-style lock manager for block resources and the single serialization point for atomic block acquisition, whether the dispatcher is attempting a full-route all-or-nothing reservation or a best-effort prefix reservation.
- Keep `occupy_blocks(block_ids, train_id)` as the existing atomic occupy helper.
- Add `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` to replace one-by-one block reservation with atomic reservation.
- `directions` in `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` should be a mapping `{block_id: direction}` using the path's default start -> end traversal order. `PathDispatcher` owns building that mapping from the chosen path geometry before calling `BlockManager`.
- `reserve_blocks_for_path(...)` must first check eligibility for every block in the candidate block set before writing any fields: a block is eligible when it is FREE, already reserved by the same `path_id`, or physically OCCUPIED by the assigned `train_id` (start-block case). If any block in that candidate set fails eligibility the entire call is rejected and no state changes are made. `PathDispatcher` owns choosing whether that candidate set is the full requested route (`all_or_nothing`) or the longest best-effort prefix it wants to attempt atomically. This removes the `Block.is_available_for_path()` predicate entirely: it checked `occupied_by == train_id` for RESERVED blocks — a condition that is always False once `occupied_by` is removed from RESERVED state — and its responsibilities are absorbed into this manager-level eligibility check.
- `reserve_blocks_for_path(...)` should set `block.reserved_by_path` directly (the `path_id` stays at the manager level, not in the block-method signature) and set `block.reserved_direction` and `block.reserved_for_train` for each block by calling `Block.reserve_for_path(train_id, direction)`. It should allow the assigned train's currently occupied start block to become path-reserved without clearing physical occupancy, because path creation may begin while the train is already in the start block. In the computed-state model, setting `block.reserved_by_path` on an already-occupied start block does not change the computed `state` (still `OCCUPIED`) but records the reservation so it can be cleared later through `release_path_block_reservations`. No special branch for the occupied start block is needed — `reserve_for_path` is a plain field write that works on any physical state.
- Add `release_path_block_reservations(block_ids, path_id, completed=False)` to release reservation ownership; it replaces scattered direct `block.release()` calls during completion and cancellation without modifying physical occupancy state. This manager method must clear only blocks that both belong to that path's dispatcher-owned reservation set and currently satisfy `block.reserved_by_path == path_id`; a mismatched block must be ignored or treated as a release failure rather than blindly cleared. Because `Block.release_path_reservation()` is now a plain field clear with no state guard, this method can call it for each matching owned block regardless of whether the block is currently `OCCUPIED`, `CAUTION`, or `RESERVED`. This includes the train's currently occupied start or terminal block when that block was reserved for the same train: reservation ownership and physical occupancy are independent, so an occupied block can still be unreserved safely. When `completed=True` (path completed with train fully stopped at terminal), it additionally force-clears `_is_caution` and `caution_direction` on any still-CAUTION matching owned blocks, freeing them immediately so that trailing-train gaps shrink to the terminal block only. When `completed=False` (path cancelled mid-flight), physical CAUTION state is left untouched — the train is still moving and the safety margin must be preserved.
- Remove `set_block_to_caution()` and replace it with `on_block_tail_vacated(block_id, train_id, direction)`: this method clears `block.occupied_by`, sets `block._is_caution = True`, records `block.caution_direction = direction`, then calls `_reevaluate_caution_blocks()` to cascade any blocks that can already advance to `FREE`.
- Treat `on_block_tail_vacated()` as the only normal train-vacated physical transition entry point.
- Keep `release_path_block_reservations(...)` as reservation teardown for path completion and cancellation.
- Keep `release_block()` as a manual/admin reservation-cleanup path only: it may clear `reserved_by_path`, `reserved_direction`, and `reserved_for_train`, but it must not touch `occupied_by`, `_is_caution`, or `caution_direction`. Remove `clear_block()` from the desired model. Manual/admin cleanup must not be part of the routine path-completion flow.
- `BlockManager` should continue to hold a reference to `Network.blocks` rather than maintaining a separate block registry.

**`on_block_tail_vacated()` behaviour (directional CAUTION evaluation)**

- Legacy direct `TrainPhysics -> BlockManager.on_block_tail_vacated(...)` calls must be removed in the desired model. `TrainPhysics` reports normal tail-vacated resources only through `PathDispatcher.on_resource_tail_vacated_from_physics(...)`, and `PathDispatcher` is the only normal runtime caller that invokes `BlockManager.on_block_tail_vacated(block_id, train_id, direction)` for block resources. `BlockManager` replaces `set_block_to_caution()` with this method.
- `BlockManager` does not independently decide that a block resource should be released; that instruction comes from `PathDispatcher` / physics-driven resource release. `train_id` is passed through for bookkeeping/history and optional diagnostics, not as a second ownership gate that can block the normal physical `OCCUPIED -> CAUTION -> FREE` transition once release has already been instructed.
- `on_block_tail_vacated()` clears `block.occupied_by`, sets `block._is_caution = True`, and sets `block.caution_direction = direction`. With the computed-state model the block's `state` property now returns `CAUTION`.
- After updating the vacated block, `BlockManager` should call `_reevaluate_caution_blocks()` to cascade any blocks that can already advance to `FREE`.
- `_reevaluate_caution_blocks()` iterates all blocks currently in `CAUTION`. In the normal reevaluation path, a `CAUTION` block should advance to `FREE` only while it can still resolve a reachable forward protection chain in `block.caution_direction` and the first forward block on that reachable chain is NOT `OCCUPIED`. "Forward block" is defined precisely: start from the vacated block's forward traversal in `block.caution_direction`, follow live tile connectivity through any intervening switch tile using the current `Switch.state`, and continue until the next block resource is reached. That next reachable block resource is the only occupancy check used for the `CAUTION -> FREE` decision; blocks behind the vacated block or off the currently selected switch branch are ignored. This prevents a following train (entering from behind) from incorrectly keeping a caution block alive, and it also means a later switch throw can invalidate the old protected chain.
- If `_reevaluate_caution_blocks()` cannot resolve any reachable forward-adjacent block in `block.caution_direction`, or if live connectivity no longer reaches the block that was previously being protected, there is nothing ahead left to protect. In that case it should clear `_is_caution` and `caution_direction` immediately, so the computed state becomes `RESERVED` if `reserved_by_path` is still set, otherwise `FREE`.
- When a CAUTION block clears, set `block._is_caution = False` and `block.caution_direction = None`. The block's computed `state` then re-evaluates to `RESERVED` if `reserved_by_path` is still set, or `FREE` if it is not.
- Reevaluation is triggered by manager-owned connectivity changes, not by a periodic scan: at minimum `BlockManager.on_block_tail_vacated()` must call `_reevaluate_caution_blocks()`, and every successful `BlockManager.on_switch_state_changed(switch_id)` call must also trigger `_reevaluate_caution_blocks()`.
- `reserved_by_path` is NOT checked as a condition for `CAUTION → FREE`. The physical safety margin and the dispatcher reservation are independent: a block can become physically clear of `CAUTION` while still holding a `reserved_by_path` value. In that case the computed `state` becomes `RESERVED`, not `FREE`, until the dispatcher separately clears the reservation via `release_path_block_reservations()`.
- Explicit exception: `BlockManager.release_path_block_reservations(..., completed=True)` may force-clear `_is_caution` and `caution_direction` during terminal completion teardown for owned blocks. That is a completion-time teardown rule, not part of the normal neighbour-based reevaluation path.

## Path And PathDispatcher

**Path model**

- Reduce the persisted dispatcher path lifecycle to three states only: `ACTIVE`, `COMPLETED`, and `CANCELLED`. The current lifecycle value is stored on `Path` as `status: PathStatus`, where `PathStatus` is the enum `{ACTIVE, COMPLETED, CANCELLED}`. `status` is initialized to `ACTIVE` on successful `create_path(...)` and is the field transitioned by completion and cancellation.
- Keep the core identity / routing fields on `Path`: `path_id`, `status`, optional `priority`, `requested_destination_block_id`, `requested_destination_direction`, `requested_stop_position_cm`, `authority_end_block_id`, `authority_stop_position_cm`, `train_id`, and `path_tiles`.
- `priority` is an optional integer dispatcher arbitration field. `RouteExecutor` may supply it or leave it blank. When the dispatcher chooses among competing requests in the same planning pass, higher `priority` values are attempted first and therefore claim shared resources first. Among equal-priority direction-compatible candidates within one request, candidate selection may be arbitrary in v1; routes that need deterministic path choice should constrain geometry with waypoint design rather than relying on dispatcher tie-breaking.
- `path_tiles` should remain the ordered physical tile list for the authority, and its default order is train tail -> current authority end. `Path.path_tiles` and every `resource_sequence[*].path_tiles` slice use the canonical **path_tile entry schema** below; shared contracts must not use bare `(x, y)` tuples.
- Remove `PENDING`, `SETTING`, `SET`, and `CLEARING` from the desired model. Resource acquisition is synchronous inside `create_path()`, and terminal-approach behavior should be represented by progress/completion predicates rather than a second live status.
- Replace `block_sequence` and `switch_positions` with `resource_sequence`, an ordered list of path-step records interleaving blocks and switches in traversal order. The per-entry schema is defined canonically in the **resource_sequence entry schema** subsection immediately below and must not be restated divergently elsewhere.

**Path field schema**

This is the single canonical field set for the dispatcher-owned `Path` entity in v1.

| Field | Type | Meaning |
| --- | --- | --- |
| `path_id` | `str` | Unique dispatcher path identifier |
| `status` | `PathStatus` | One of `ACTIVE`, `COMPLETED`, `CANCELLED` |
| `priority` | `Optional[int]` | Dispatcher arbitration priority |
| `train_id` | `str` | Assigned train |
| `path_tiles` | `List[path_tile]` | Full ordered train-tail -> authority-end geometry |
| `resource_sequence` | `List[resource_sequence entry]` | Ordered interleaved block/switch resources |
| `current_resource_index` | `int` | `sequence_index` of the resource where the head currently is |
| `requested_destination_block_id` | `str` | Caller's current requested block target carried by this live path |
| `requested_destination_direction` | `str` | Route-owned traversal constraint at the requested target; used for compatibility/filtering, not exact stop-frame geometry |
| `requested_stop_position_cm` | `float` | Requested stop in the dispatcher-resolved terminal frame of `requested_destination_block_id` |
| `authority_end_block_id` | `str` | Current live authority end block |
| `authority_stop_position_cm` | `float` | Current live stop in the dispatcher-resolved terminal frame of `authority_end_block_id` |
| `teardown_pending` | `bool` | Immediate dispatcher-owned teardown in progress only |

- `Path` is the entity name for the dispatcher-owned resource-sequence authority record itself. Shared contracts must refer back to this table instead of rediscovering the field set from scattered prose.
- In a stored live path, `resource_sequence[-1]` must always be a `block` entry. A v1 path may contain switch resources internally, but its live authority end is always expressed by `authority_end_block_id` / `authority_stop_position_cm` on a block resource, never on a terminal switch.

**path_tile entry schema**

This is the single canonical definition of one tile step inside `Path.path_tiles` and inside each `resource_sequence[*].path_tiles` slice.

- Each `path_tile` entry carries exactly:
  - `x`
  - `y`
  - `entry_edge`
  - `exit_edge`
  - `resource_sequence_index` — the parent `Path.resource_sequence[*].sequence_index`.
- `entry_edge` and `exit_edge` are required on every tile entry; shared contracts must not infer them later from neighboring coordinates.
- For same-block / same-tile synthetic movement, tile-entry `entry_edge` and `exit_edge` follow the train's intended forward cab direction for that move. If the move stays inside a single tile and no cab is selected, dispatcher must reject the request rather than inventing a traversal frame.

**resource_sequence entry schema**

This is the single canonical definition of a `resource_sequence` entry. Every site that describes the controller path plan, `get_path_tiles()`, the `to_dict()` exports, or any other projection of authority geometry refers back to this schema by name rather than restating it.

- Each `resource_sequence` entry carries exactly:
  - `sequence_index` — position of the entry in the path's train-tail -> authority-end traversal order.
  - `resource_type` — `"block"` or `"switch"`.
  - `resource_id` — stable network resource ID for the block or switch.
  - `path_tiles` — the contiguous ordered slice of canonical `path_tile` entries belonging to that resource, in train-tail -> authority-end traversal order. Every nested tile entry in this slice has `resource_sequence_index == sequence_index`.
  - `entry_edge` — the directed traversal-frame entry edge for that resource in train-tail -> authority-end traversal order.
  - `exit_edge` — the directed traversal-frame exit edge for that resource in train-tail -> authority-end traversal order.
- No other fields are required in v1.
- Ordered tiles alone are not enough for same-block / same-tile movement because a single tile can be traversed in either direction and the stop frame changes with the chosen entry side; `entry_edge` and `exit_edge` are required fields on every entry.
- `entry_edge` must equal the first nested `path_tiles[0].entry_edge`, and `exit_edge` must equal the last nested `path_tiles[-1].exit_edge`; they are stored redundantly so resource-level consumers can read the traversal frame without unpacking the whole tile slice.
- For switch resources, the required switch state is not stored as a fixed field on the entry. It is derived at acquisition time from the planner-facing `TrackSegment.enumerate_traversals(entry_edge)` contract on the switch tile, using the `(entry_edge, exit_edge)` pair selected for that resource's `path_tiles` slice. This derivation replaces storing `switch_positions` as a separate dict on the path.
- These per-resource tile slices plus explicit resource/tile entry-exit edges are the geometry contract for controller/UI consumers after `Train.current_path` is removed.
- Replace `current_block_index` with `current_resource_index`, which should track the `sequence_index` of the resource where the train head is currently located, not the first matching raw resource ID.
- Remove `blocks_cleared`; the desired model does not need a stored `resources_cleared` replacement in v1.
- Remove `flank_protection_switches` from `Path`; flank protection should live on the switch entity and be acquired automatically when the switch is locked.
- Add the requested target fields on `Path`: `requested_destination_block_id`, `requested_destination_direction`, and `requested_stop_position_cm`. `requested_destination_direction` is the caller's route-owned traversal constraint for the requested target. It is used to filter compatible terminal candidates and to match retained authority, but it is not itself the exact stop frame. `requested_stop_position_cm` is always resolved and stored by `PathDispatcher` before the path record is finalized: if the caller provided an exact route/platform stop it is normalized into the dispatcher-resolved terminal frame for `requested_destination_block_id`; otherwise dispatcher fills the fallback value from `PathDispatcher.resolve_stop_target(...)`.
- Add the current authorized target fields on `Path`: `authority_end_block_id` and `authority_stop_position_cm`. These are the live authority end the controller brakes to right now.
- `requested_stop_position_cm` and `authority_stop_position_cm` are both measured in centimeters as offsets counted back from a forward boundary in the stored traversal frame, but each value is interpreted in the exact directed frame of its companion block:
  - `requested_stop_position_cm` is interpreted in the traversal frame of `requested_destination_block_id`
  - `authority_stop_position_cm` is interpreted in the traversal frame of `authority_end_block_id`
  For a live path, those frames come from explicit directed geometry on the relevant terminal `resource_sequence` block entries (`path_tiles`, `entry_edge`, `exit_edge`). For a same-block / same-tile synthetic authority, that traversal frame must still be explicit in the path contract; the spec must not rely on readers inferring direction from tile coordinates alone or from `requested_destination_direction` alone.
- When the live authority already reaches the requested destination, the authorized target matches the requested target. When the live authority is a best-effort truncated prefix, the requested target fields remain unchanged while `authority_end_block_id` / `authority_stop_position_cm` describe the temporary stop at the current authority boundary.
- Remove overlap / approach-lock data from `Path` in the desired model; they are not part of the new design and must not be retained for teardown policy, cancellation gating, or history in v1. Add `teardown_pending: bool` as an explicit non-lifecycle marker for the **immediate authority-teardown phase only**. It covers clearing reservation ownership, clearing controller authority, clearing `active_path_id_by_train[train_id]`, and finishing any dispatcher bookkeeping that belongs to the old authority. Set `teardown_pending = True` immediately when an `ACTIVE` path transitions to `COMPLETED` or `CANCELLED`, and set it back to `False` as soon as that immediate teardown pass is complete. Later physical movement or occupancy updates after authority clear are not part of `teardown_pending`.
- `PathDispatcher` should treat each `Path` record as the authoritative dispatcher-owned database entry, keyed by `path_id` in `paths_by_id[path_id]`, with `active_path_id_by_train[train_id]` holding the one active authority path for that train. The controller inbox is only a pushed projection of that current authority.
- Dispatcher-owned path records should remain available while active and, after termination, as retained `CANCELLED` or `COMPLETED` records for history/inspection. Cleanup/retention policy may be separate, but authority teardown must not depend on the controller inbox surviving.
- `PathDispatcher`'s retained path database should be the authoritative historical store for every path it has created. After `active_path_id_by_train[train_id]` has been cleared, active progress callbacks resolve only against the new active authority, while delayed release or occupancy reporting for a block or switch that remains under the train resolves from the resource itself (`BlockManager` state for blocks, `Switch.occupied_by` / `Switch.locks` for switches). `create_path(...)` must reject while that train still has an `ACTIVE` path, but once the old path is no longer active retained history rows do not by themselves block creation; only ordinary live resource conflicts remain. Compatible same-state switch claims for the new path may coexist with `release_pending` claims from the old path until tail-vacate removes the old claims.
- In the desired model, `train_id` is mandatory for every dispatcher authority path, including route-driven, headless, and manually requested paths. Frontend/API callers must be updated to supply it; anonymous authority paths are out of scope for this model. Path membership on reserved resources is tracked through `Block.reserved_by_path` (written by `BlockManager.reserve_blocks_for_path(...)`) and `Switch.locks` (keyed by `(path_id, claim_role)`, with each claim storing `path_id`, `claim_role`, `train_id`, `locked_state`, and `release_pending`); the block-level `Block.reserve_for_path(train_id, direction)` method itself no longer takes `path_id`.
- There is no v1 compatibility adapter for legacy path-request payloads. Repo callers, REST handlers, and UI/API layers must migrate directly to the new `create_path(...)` / `extend_path(...)` contract rather than expecting dispatcher to reinterpret old request shapes.

**`create_path()` flow**

1. Candidate search should enumerate direction-compatible resource sequences from the train's current occupied span to `requested_destination_block_id`, reject any candidate that uses repeated resources or reaches the terminal block in a way that does not satisfy `requested_destination_direction`, and then attempt acquisition in candidate order under the chosen `acquisition_mode`.
2. Add a canonical dispatcher-owned helper such as `PathDispatcher.resolve_stop_target(train_id, requested_destination_block_id, terminal_frame, requested_stop_position_cm=None) -> resolved_stop_position_cm`, where `terminal_frame` is the exact directed stop frame for the requested target (`path_tiles`, `entry_edge`, `exit_edge`). For an ordinary live path, dispatcher derives that frame from the chosen terminal `resource_sequence` block entry. For same-block / same-tile normalization, dispatcher derives the needed local frame from the `train_id`'s current occupied blocks and, when the move stays inside a single tile, the currently selected cab direction. When the requested target was resolved from a multi-block platform, `terminal_frame` must use the terminal block selected from that platform's linked blocks on the far side of travel in the resolved traversal direction.
3. `PathDispatcher.create_path(...)` should take `requested_destination_block_id`, `train_id`, `requested_destination_direction`, optional `requested_stop_position_cm`, optional `priority: int`, and `acquisition_mode`; it should not require a caller-supplied `start_block_id` or a second opaque waypoint/platform stop-context payload. Dispatcher derives the start from the `train_id`'s current occupied span.
4. `PathDispatcher.create_path(...)` should also take an explicit `acquisition_mode` parameter of type `PathDispatcher.AcquisitionMode`, an enum owned by `PathDispatcher` with v1 values `all_or_nothing` and `best_effort`.
5. Candidate search must become direction-aware. `find_paths(...)` or the equivalent dispatcher pathfinder entry point must reject candidates whose terminal traversal does not satisfy `requested_destination_direction`, and same-block / same-tile synthetic path construction must use `requested_destination_direction` only as a constraint while dispatcher still derives the exact local terminal frame from explicit geometry. For a direct `block_id` target, `requested_destination_direction` is interpreted against that block's immutable internal traversal order: `"forward"` means the increasing direction of that block-local order and `"reverse"` means the decreasing direction. If route/waypoint resolution logic cannot determine a usable route-level traversal constraint for the current waypoint, or if dispatcher cannot build one unique terminal frame for the chosen move, path creation must fail rather than guessing.
6. When route/waypoint resolution logic already knows the exact stop target, `RouteExecutor` should pass that exact `requested_stop_position_cm` into `PathDispatcher.create_path(...)`, including same-block repositioning and any other stop where the route plan wants a precise marker inside the destination block.
7. If the caller does not supply `requested_stop_position_cm`, `PathDispatcher` should resolve it through `resolve_stop_target(...)` from `requested_destination_block_id` plus the chosen `terminal_frame` and dispatcher-owned terminal safety policy before the path is finalized. The desired v1 policy uses two canonical global settings:
   - `PATH_STOP_TRACK_MARGIN_CM = 25.0` for an ordinary track-end or non-platform authority end
   - `PATH_STOP_PLATFORM_MARGIN_CM = 5.0` for a platform/station stop
   `PathDispatcher.resolve_stop_target(...)` is the only resolver for the stored requested stop: it chooses which margin applies by checking whether `requested_destination_block_id` currently has a platform on it in network data, uses the explicit `terminal_frame` to define the forward boundary of that target, and stores the stop as the offset counted back from that forward boundary in the resolved traversal frame. A route-defined `stop_position_cm` is not an absolute coordinate; it is a stopping marker that must be interpreted in the resolved forward travel frame of the target. If the same physical platform is being served from the reverse side, dispatcher reverses that marker into the reverse-directed terminal frame before storing it. If the caller supplied an exact `requested_stop_position_cm`, dispatcher uses that same counting rule to normalize it into the path frame and stores the normalized offset directly. If the caller omitted it, dispatcher stores the selected fallback margin directly as that offset-from-forward-boundary value. For same-block / same-tile normalization, dispatcher uses the `train_id`'s current occupied blocks and currently selected cab direction to disambiguate the local traversal frame. The resolved value is stored on the `Path` as `requested_stop_position_cm`.
8. Allow `create_path(...)` for same-block repositioning when the train's current occupied span already includes `requested_destination_block_id`. Argument naming is consistent throughout the desired model: `requested_destination_block_id` is the caller's current requested target passed into `create_path(...)` / `extend_path(...)`, and `authority_end_block_id` is the current live-authority end stored on the `Path`.
9. For same-block repositioning, `RouteExecutor` still passes `requested_destination_block_id`, `requested_destination_direction`, optional exact `requested_stop_position_cm`, and `train_id`. `PathDispatcher.create_path(...)` owns deciding what tiles/resources must be assigned based on the train's current occupied span and the destination stop target. If the required movement stays on ordinary track tiles inside one block, dispatcher may build a synthetic local path with one block resource whose `path_tiles` slice covers the local movement. If that movement stays inside a single tile, dispatcher must still use `requested_destination_direction` plus the train's currently selected cab direction to validate and disambiguate the local traversal frame. If those disagree, or if no cab is selected for this same-tile edge case, dispatcher must reject the request with a defined failure reason such as `"same-tile synthetic movement requires a selected cab direction"` rather than guessing. If route/waypoint resolution logic has an explicit stop window, it should pass `requested_stop_position_cm`; otherwise the dispatcher fallback rule above applies.
10. In `all_or_nothing` mode, if the full requested candidate path cannot be acquired, roll back and return failure; no live path is created.
11. In `best_effort` mode, if the full requested candidate cannot be acquired, `PathDispatcher` should truncate the authority to the furthest forward resource prefix it can acquire on that candidate and still create one live path. That truncated live path keeps the same `path_id`, preserves the requested target fields, sets `authority_end_block_id` to the last acquired block in the current authority, and computes `authority_stop_position_cm` at that temporary authority boundary using the same traversal-frame counting rule. `create_path(...)` succeeds only when that acquisition added at least one resource to the new live `resource_sequence`; if nothing can be acquired for the new path, the result is failure.
    - Special case: if the furthest acquirable prefix ends on a `switch` resource, dispatcher must truncate again to the previous `block` resource before storing the live path, so the stored live authority is always block-terminated.
    - If no resource can be acquired for the new live path, `PathDispatcher.create_path(..., acquisition_mode=best_effort)` must fail rather than creating a zero-growth holding authority seeded only from the train's current covered span.
    - Exception: if the train head is currently on a switch resource and no forward block beyond that switch can be acquired, `PathDispatcher.create_path(..., acquisition_mode=best_effort)` must fail rather than storing a head-on-switch holding authority. A natural-language failure reason such as `"cannot create best-effort authority while head is on a switch"` is sufficient in v1.
12. Allocate the new `path_id` before switch acquisition so `Switch.locks` and flank-protection claims can be keyed by that same path ID. Once the chosen acquisition result succeeds, create the dispatcher-owned path record directly in `ACTIVE` state and push the controller path plan immediately. There should be no persisted intermediate `SET` state in the desired model.
13. `PathDispatcher.create_path(...)` seeds `resource_sequence` from the train's already-occupied resource span for every occupied-start case, not only the one-resource synthetic case. The canonical initialization rule is: order the sequence from the tail-most occupied resource toward the authority end, then set `current_resource_index` to the resource where the train head is currently located. As a worked example: for same-block repositioning, if dispatcher builds that one-resource synthetic move, initialize both the sequence and the head index to that sole resource. No extra movement-start guard is needed: if the train is already stopped inside the waypoint stop window and the route/waypoint already defines the exact stop target, `RouteExecutor` may bypass path creation entirely; otherwise completion still requires a later full stop at the requested target, so same-block movement still goes through ordinary `create_path(...)`. If the train's current span or required movement would still include another resource (for example a switch that the train continues to cover), `PathDispatcher.create_path(...)` should expand the assigned tiles/resources itself and build a normal multi-resource authority from the train's current occupied span to the target stop point; `RouteExecutor` does not compute or pass those tiles directly.
14. Build `resource_sequence` in traversal order so both blocks and switches are tracked in one sequence.
15. Store both target contracts on the `Path`: the resolved requested target (`requested_destination_block_id`, `requested_destination_direction`, `requested_stop_position_cm`) and the current authorized target (`authority_end_block_id`, `authority_stop_position_cm`).
16. Before activating the new path, reject the request if `active_path_id_by_train[train_id]` already references an `ACTIVE` path. In the desired model a train may have only one active authority plan at a time: callers must use `extend_path(...)` to grow that same live path, or wait until the old path has transitioned out of `ACTIVE` before calling `create_path(...)` again. A retained `CANCELLED` / `COMPLETED` row or a retained teardown/history row does not by itself block `create_path(...)`; only ordinary resource conflicts still apply.
17. Store the new dispatcher-owned path record in `paths_by_id[path_id]` and set `active_path_id_by_train[train_id] = path_id`.
18. Do not copy reserved-block state onto `Train`; reserved resources remain dispatcher-owned on the `Path` record and in the controller-facing authority projection.
19. Push the canonical **received_path_plan schema** to the controller after successful creation. Its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**.
20. The pushed plan must preserve an explicit traversal frame. `requested_stop_position_cm` is interpreted in the frame of `requested_destination_block_id`, and `authority_stop_position_cm` is interpreted in the frame of `authority_end_block_id`; readers must not try to recover either frame later from tile coordinates alone.

**`create_path()` / `extend_path()` return contract**

- `PathDispatcher.create_path(...)` and `PathDispatcher.extend_path(...)` return a small result object with exactly `success: bool`, `path_id: Optional[str]`, `resource_sequence: Optional[List[resource_sequence entry]]`, and `reason: Optional[str]`.
- On success, `path_id` is required, `resource_sequence` is required, `reason = None`, and `resource_sequence` is the full current live `resource_sequence` after that create/extend operation completes. `RouteExecutor` uses this returned geometry to decide whether the live planning chain now covers the next waypoint and therefore whether `target_waypoint_index` should advance.
- Success means that operation added at least one resource to the live `resource_sequence`. A create-path call that acquires nothing, or an extend-path call that appends nothing, is a failure in v1 rather than a successful no-op.
- On failure, `success = False`, `path_id = None`, `resource_sequence = None`, and `reason` is required.
- `reason` is human-readable diagnostic text only. Callers may display or log it, but they must not branch on exact string equality in v1.

**Resource acquisition**

- Acquire switches first, then blocks, so switch conflicts fail before any blocks are reserved.
- For each switch in path order, call `switch.acquire_lock(path_id, "main", train_id, required_state)` and immediately acquire any `switch.flank_protection` locks. Each `flank_protection` entry `(neighbour_switch_id, blocking_state)` is acquired as `neighbour_switch.acquire_lock(path_id, "flank", train_id, safe_state)`, where `safe_state` is the neighbour's state whose active segment does **not** point toward the primary switch's track — i.e. the state that keeps the neighbour's diverging path aimed away, preventing a head-on approach. `safe_state` is computable from switch geometry: it is the neighbour state whose selected branch exit edge is not the edge facing the primary switch. In v1, because switches are binary, `safe_state` is simply the opposite of the neighbour's `blocking_state` recorded at placement time. Every acquired flank claim must store `source_switch_id = switch.switch_id` so later switch tail-vacate teardown can release only the flank claims sourced from that main switch.
- Acquire blocks atomically through `BlockManager.reserve_blocks_for_path(...)`.
- Resource acquisition should write `Block.reserved_direction` through `BlockManager.reserve_blocks_for_path(...)` for every reserved block. Tile-level traversal direction is carried by `resource_sequence[*].entry_edge` / `exit_edge` and nested `path_tiles[*].entry_edge` / `exit_edge`; there is no separate `TrackSegment.path_direction` field in the desired model.
- Distinguish retained geometry from temporary reservation direction: v1 does not define a separate track-level `traffic_direction_policy` field. `Block.reserved_direction` is the dispatcher-owned temporary reservation direction for the block, and the path plan geometry carries the finer-grained per-tile traversal frame inside that block.
- On failure, roll back in reverse acquisition order, including switch locks and block reservations. There is no separate tile-level `path_direction` assignment to clear.
- Replace direct `block.release()` calls in completion and cancellation with `BlockManager.release_path_block_reservations(...)`.
- In v1, block reservations remain single-train/single-path resources even though `Switch.locks` can have multiple compatible owners. `Block.reserved_direction` therefore has at most one active value per reserved block and should be cleared when that block reservation is released.
- `create_path(..., acquisition_mode: PathDispatcher.AcquisitionMode)` and `extend_path(..., acquisition_mode: PathDispatcher.AcquisitionMode)` must share the same `PathDispatcher.AcquisitionMode` enum, with two v1 behaviors:
  - `AcquisitionMode.all_or_nothing`: dispatcher acquires only the full requested authority or returns failure after full rollback
  - `AcquisitionMode.best_effort`: dispatcher may truncate to the furthest forward acquirable resource prefix, but the acquisition of that chosen prefix is still atomic
- In `best_effort` mode, neither `PathDispatcher.create_path(...)` nor `PathDispatcher.extend_path(...)` may report success when no new resource can be acquired. A create with zero acquired resources fails, and an extend that appends no new resource to the existing live path also fails.
- The special head-on-switch case remains a failure because v1 does not allow storing a head-on-switch live authority end.
- In all acquisition modes, the stored live authority end must be block-terminated. A same-tick acquisition result that reaches a switch but no following block is not a valid stored live authority in v1; dispatcher must roll that trailing switch/flank acquisition back before storing or pushing the path.

**Physics-driven progress**

- `TrainPhysics` should detect newly entered blocks, newly entered switches, tail-vacated blocks, and tail-vacated switches.
- The runtime should use one shared `PathDispatcher` instance for all trains. `TrainPhysics` should report ordered resource-entry transitions directly to `PathDispatcher.on_train_advances_from_physics(train_id, resource_type, resource_id)`, letting that shared dispatcher resolve the active path from `active_path_id_by_train`. The `resource_type` argument must be carried on every resource-event callback (entry, tail-vacate, and any other resource-release event), not just the entry callback, so dispatcher never has to infer type from a bare ID. Delayed release of a block or switch that remains physically occupied after authority teardown resolves from the resource itself, not from a retained teardown record.
- Entry ownership is resource-typed:
  - on block entry, `PathDispatcher.on_train_advances_from_physics(...)` must call `BlockManager.occupy_blocks([block_id], train_id)`; this is the only normal runtime path that writes `Block.occupied_by`
  - on switch entry, `PathDispatcher.on_train_advances_from_physics(...)` must set `Switch.occupied_by = train_id`
- After movement, occupancy reconciliation, and tail-vacate reporting inside each `TrainPhysics.tick(...)`, `TrainPhysics` should call `PathDispatcher.on_train_progress_from_physics(train_id)` once for that train even if no new resource was entered on that tick, so full-stop completion can be evaluated without a simulation-owned forwarding pass.
- Dispatcher progress should update `current_resource_index` using the path-step `sequence_index`, not by searching for the first matching raw `resource_id`.
- A live path should remain `ACTIVE` after creation, through intermediate resources, and after entering the final block/resource. Entering the terminal resource should not change lifecycle state by itself.
- Completion should be performed by `_complete_path_on_full_stop(path, train)` only when all of the following are true:
  - `path.status == ACTIVE`
  - `path.authority_end_block_id == path.requested_destination_block_id`
  - `path.authority_stop_position_cm == path.requested_stop_position_cm`
  - the train head has reached the terminal resource and the train is fully stopped within the canonical stop window at that authority stop. In v1, this means `RailwayNetwork.project_stop_frame_delta(...)` has been evaluated against the authority target and `abs(delta_cm) <= 5.0`, not merely that the train is somewhere inside `authority_end_block_id`
- A best-effort truncated path must not complete just because the train has stopped at the current authority boundary while the current authorized target is still short of the requested target. In that case the same path remains `ACTIVE`, the controller may enter `RUNNING_WAITING` under the still-live authority, and `RouteExecutor` should keep trying `extend_path()` on that same `path_id`.
- Completed paths should release block reservations for all owned blocks through `BlockManager.release_path_block_reservations(..., completed=True)` immediately, even if the terminal block is still physically occupied by the train at the moment of completion, because reservation ownership and physical occupancy are separate concerns. That reservation teardown may force-clear terminal `CAUTION`, but it must not force-clear any physical `OCCUPIED` block early. On this `ACTIVE -> COMPLETED` transition, dispatcher should set `path.teardown_pending = True`, release any switch claims and flank-protection claims whose `Switch.occupied_by` is already `None`, mark the completed path's switch claims `release_pending = True` when `Switch.occupied_by == train_id`, clear block reservation direction only through `BlockManager.release_path_block_reservations(...)`, call `controller.on_path_complete()`, clear controller authority by calling `controller.receive_path_plan(None)`, clear `active_path_id_by_train[train_id]`, and then set `path.teardown_pending = False` once that immediate teardown pass is finished. The retained record may remain in `paths_by_id` for history, but later physical release of a switch or block still under the train is not part of `teardown_pending` and resolves from the resource itself. When that later tail-vacate callback finally fires, it clears `Switch.occupied_by` and removes only `release_pending` claims for that `train_id`, leaving any newer compatible claim for a later path by the same train intact. `RouteExecutor` treats the dispatcher callback as authoritative completion and does not re-check exact stop position itself.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()`.
- When creating or extending a path, resolve and store the requested target contract first, then set the current authorized target contract and include both in the controller path plan:
  - if the live authority already reaches the requested target, set `authority_end_block_id == requested_destination_block_id` and `authority_stop_position_cm == requested_stop_position_cm`
  - if the live authority is still truncated, keep the requested target unchanged and compute `authority_end_block_id` / `authority_stop_position_cm` for the current temporary end of `resource_sequence`
- Drive resource release from tail-vacate detection in `TrainPhysics`, but let `TrainPhysics` report each vacated resource directly to `PathDispatcher` as a typed resource-release event. `PathDispatcher` should inspect `resource_type` and release it accordingly:
  - vacated blocks go through `BlockManager.on_block_tail_vacated(...)`
  - vacated switches go through the `Switch` entity after `PathDispatcher` resolves the vacating `train_id` for that `switch_id` from dispatcher / switch state (`clear Switch.occupied_by`, then `release_pending_claims_for_train(train_id)` on that switch plus release of any neighbouring flank claims whose stored `source_switch_id == switch_id`)
- Route any normal block release through `block_manager.on_block_tail_vacated()` so `OCCUPIED -> CAUTION -> FREE` remains a single-manager state machine. Manual/admin cleanup stays out of the routine completion flow; in the desired model that cleanup uses `release_block()` only because `clear_block()` is removed.

**Path extension**

- Keep forward-only extension.
- When the path model switches to `resource_sequence`, append new tiles and new resources while skipping duplicated connection/start entries.
- Reject any extension that would cause a resource to appear twice in the combined path.
- `PathDispatcher.extend_path(...)` should take the same argument contract as `create_path(...)`, except that it identifies the existing live path by `path_id`: `path_id`, `requested_destination_block_id`, `requested_destination_direction`, optional `requested_stop_position_cm`, and `acquisition_mode`. The live path's current `authority_end_block_id` is only the current authority end; the path's stored requested target fields remain the caller's final waypoint target.
- `PathDispatcher.extend_path(...)` must return `success=False` with a required `reason` when `path_id` does not exist or when the referenced path is not currently `ACTIVE`.
- `PathDispatcher.extend_path(...)` preserves the live path's existing `priority`; it does not take or recompute a second priority value.
- In `all_or_nothing` mode, `extend_path(...)` should either acquire the full requested suffix to `requested_destination_block_id` or leave the existing path unchanged.
- In `best_effort` mode, `extend_path(...)` should keep the same `path_id` and append the furthest forward acquirable suffix it can add to the current path. The `resource_sequence` therefore grows in place on the same live path instead of creating a new path.
- While the path is still truncated short of the caller's current requested target, keep the stored requested target unchanged, set `authority_end_block_id` to the current last acquired block, and recompute `authority_stop_position_cm` at the current temporary authority boundary.
- Once `extend_path(...)` finally reaches the current requested target block carried by that path, set `authority_end_block_id = requested_destination_block_id`, set `authority_stop_position_cm = requested_stop_position_cm`, update the dispatcher-owned `Path` record, and repush the refreshed path plan through `controller.receive_path_plan(...)` if that path is still the train's active authority.
- In `best_effort` mode, if an extension attempt cannot add any new resource at all on that tick, it is a failure result rather than a successful no-op. The existing live path remains in place, but the return contract reports `success = False`.

**Cancellation**

- Release owned blocks through `BlockManager.release_path_block_reservations(...)` instead of direct `block.release()` calls. In the desired v1 model, cancellation teardown clears reservation ownership only; it never clears physical occupancy.
- Path cancellation or revocation is owned entirely by `PathDispatcher`. Cancelling a live path should clear controller authority by calling `controller.receive_path_plan(None)` and should not be synthesized by `RouteExecutor` or `TrainController`.
- Power-off uses this same dispatcher cancellation workflow. When a train is powered off, higher layers must cancel any live path through `PathDispatcher`, clear controller authority, and release reservations through the ordinary cancellation path rather than inventing a separate OFF-specific teardown path.
- On the `ACTIVE -> CANCELLED` transition, `PathDispatcher` should set `path.teardown_pending = True`, clear block reservation ownership, clear controller authority, and clear `active_path_id_by_train[train_id]`. A block reservation may be cleared even when the train still physically occupies that block; reservation ownership and physical occupancy are independent. Switch-lock teardown is different: if `Switch.occupied_by is None`, dispatcher removes that cancelled path's claims immediately; if `Switch.occupied_by == train_id`, dispatcher marks that path's claims `release_pending = True` and leaves them in place until the later tail-vacate callback. A physically occupied switch can add compatible new claims for a later path by the same train, but it cannot drop the old pending claim early. Once the immediate teardown pass is complete, set `path.teardown_pending = False`; at that point a same-train new path is allowed, subject to ordinary live resource conflicts from any still-occupied blocks or switches.
- Reservation / physical state ownership in v1 should be read literally:
  - dispatcher-owned reservation state: `Block.reserved_by_path`, `Block.reserved_for_train`, `Block.reserved_direction`, `Switch.locks`, flank-protection lock ownership
  - physics-owned physical state: `Block.occupied_by`, `Block._is_caution`, `Block.caution_direction`
  - dispatcher-mediated physical state, written by `PathDispatcher` on physics-reported resource-entry / tail-vacate events rather than directly by `TrainPhysics`: `Switch.occupied_by`
  - a block or switch has at most one physical `occupied_by` train at a time (single-train physical ownership). A single train may hold multiple path records layered over that physical ownership; path membership is tracked separately by `PathDispatcher` on `Path` records, not by duplicating `occupied_by` per path.
  - cancellation / completion teardown may clear block reservation state immediately, but it may clear switch-lock state only for switches that are no longer physically occupied
  - only physical entry / tail-vacate callbacks may clear or set physics-owned or dispatcher-mediated physical occupancy state
  - if any reservation / lock residue was intentionally retained until physical release, the later tail-vacate callback may clear that residue together with the physical occupancy update
- Once `received_path_plan` is cleared while the train is moving, the essential event is "authority removed." `PathDispatcher` does not set EB directly. On the next `TrainController.tick()`, the controller rebuilds lookahead with no live authority and therefore extends it only to the occupied block boundary. The controller must command ordinary braking toward zero at that boundary using the no-authority ATC limit derived from that rebuilt lookahead. If the no-authority braking / overspeed check determines service braking is insufficient to keep the train within that current block, it escalates into EB. If ordinary braking succeeds and the train reaches a full stop with `eb_active == False`, `TrainController.tick()` transitions it to `STANDBY`; that stopped controller state is the "fresh stopped no-authority state" that `RouteExecutor` waits for before retrying the waypoint. This is distinct from ordinary `RUNNING_WAITING`, which requires the same path to still exist while clearance inside that live authority has dropped to zero. While the train is braking under this no-authority condition, `TrainPhysics` still advances it according to speed and the current live network state. Any later block/switch occupancy or release callbacks are still reported normally, but cancellation does not authorize a physically occupied switch to drop its lock early.
- After cancelling a live path, `PathDispatcher` should notify `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)`. This notification is mandatory but informational: `RouteExecutor` should inspect whether that cancelled path actually affects its current waypoint / assigned route state and must not treat the old path as finished.

**Path invariants**

- In the desired model, old `PENDING` and `SETTING` disappear entirely because acquisition is synchronous inside `create_path()`, and old `SET`, old `ACTIVE`, and old `CLEARING` collapse into a single live dispatcher state: `ACTIVE`. Whether the train has started moving, entered the terminal resource, or is stopped at the stop point should be represented by controller state, `current_resource_index`, and explicit completion predicates, not by separate path lifecycle values.
- `resource_sequence` replaces both `block_sequence` and `switch_positions`, so existing callers iterating those fields need updating.
- `current_resource_index` should track `sequence_index`, not the first occurrence of a raw resource ID.
- For v1, repeated resources in a single path are invalid and should be rejected during both `create_path()` and `extend_path()`.
- Switch resources contribute `0 cm` to forward-distance accumulation. Any helper that sums per-resource physical length should therefore add `0` for `resource_type == "switch"` and continue the cumulative axis across the surrounding block geometry.
- `FLANK_PROTECTION_DISTANCE_CM` should be a global config, not a per-path setting.
- In v1, switch locks are a multi-owner compatibility set: multiple paths may hold compatible same-state lock claims on the same switch, but conflicting required states are not allowed and physical occupancy stays single-owner.
- Remove `PathDispatcher._release_blocks_behind_train()` in the desired model. Once block release is fully owned by `BlockManager.on_block_tail_vacated()` triggered from physics, the dispatcher no longer needs a separate behind-train sweep.

## Route And RouteExecutor

**Route data**

| Field | Type | Meaning |
| --- | --- | --- |
| `route_id` | `str` | Unique route identifier |
| `name` | `str` | Display name |
| `waypoints` | `List[Waypoint]` | Ordered route definition |
| `mode` | `RouteMode` | One of `ONE_OFF`, `CIRCULAR`, `ROUND_TRIP` |
| `assigned_train_ids` | `List[str]` | Trains assigned to the route |
| `is_active` | `bool` | Route execution enabled |
| `started_at` | `Optional[float]` | Simulation time when activated |
| `last_error` | `Optional[str]` | Last route/path error |
| `error_time` | `Optional[float]` | Simulation time of that error |
| `current_waypoint_index` | `int` | Backward-compat / UI summary only; not authoritative for multi-train execution |
| `direction` | `int` | Backward-compat / UI summary only; not authoritative for multi-train execution |
| `default_departure_mode` | `"auto" \| "manual"` | Ordinary departure policy |
| `auto_depart_after_eb` | `bool` | Post-EB restart override |
| `acquisition_mode` | `PathDispatcher.AcquisitionMode` | Default route path-acquisition policy |

- Keep the persisted route-definition fields plus the backward-compat route-level runtime index/direction summary fields.
- A route may be assigned to multiple trains in v1. Each assigned train owns its own `RouteExecutionState` and therefore its own waypoint cursor, current traversal direction, active path correlation key, dwell timing, and resolved stop target cache. Shared route execution state across trains is not allowed.
- Because multi-train assignment is supported, `RouteExecutionState` is the only canonical runtime progress store. `Route.current_waypoint_index` and `Route.direction` remain backward-compat / UI summary fields only; they must not drive execution logic, persistence of per-train progress, or callback correlation.
- Add `default_departure_mode` with two v1 values: `auto` and `manual`. This is the ordinary route-level default for whether `RouteExecutor` automatically issues `controller.depart()` once authority exists and the next leg is ready.
- Add `auto_depart_after_eb: bool = False` at route level. This is a separate override for post-EB restart only: when a train returns to `STANDBY` after `release_emergency_cmd()`, `auto_depart_after_eb = False` suppresses automatic restart even if the ordinary departure mode is `auto`.
- Add `acquisition_mode` at route level as the policy for how this route wants dispatcher authority to be acquired (`all_or_nothing` or `best_effort`). In v1, `RouteExecutor` reads that field directly for both `create_path(...)` and later `extend_path(...)` retries on the same planning chain; no second per-train acquisition-mode runtime field is introduced.
- Define one per-train runtime structure, `ResolvedWaypointTarget`, owned by `RouteExecutionState`, carrying exactly `requested_destination_block_id`, `requested_destination_direction`, and `route_defined_stop_position_cm`. This is a route-owned request cache for the current `target_waypoint_index`, not a dispatcher mirror. `RouteExecutor` writes it from route/station/platform/block resolution. `PathDispatcher` remains the sole owner of dispatcher-resolved stop-target normalization and therefore the sole owner of `Path.requested_stop_position_cm`.

**Route-mode semantics**

- `RouteMode.ONE_OFF`: the train is unassigned from the route after it progresses through the full waypoint list.
- `RouteMode.CIRCULAR`: waypoint progression wraps `A -> B -> C -> A -> ...`.
- `RouteMode.ROUND_TRIP`: waypoint progression reflects at the ends, `A -> B -> C -> B -> A -> ...`.

**Waypoint data**

Retained current-code enums used by waypoint/route data:

- `RouteMode`: `ONE_OFF`, `CIRCULAR`, `ROUND_TRIP`
- `TimingMode`: `NONE`, `RELATIVE`, `ABSOLUTE`
- `TravelDirectionPolicy`: `PRESERVE`, `EITHER`

| Field | Type | Applies | Meaning |
| --- | --- | --- | --- |
| `kind` | `"stop" \| "pass_through"` | all | Waypoint behavior discriminator |
| `station_id` | `Optional[str]` | primarily `stop` | Logical station-group target when route planning stops by station |
| `platform_id` | `Optional[str]` | primarily `stop` | Preferred or fixed concrete platform target |
| `block_id` | `Optional[str]` | all | Direct concrete block target when this waypoint does not resolve through a platform |
| `stop_position_cm` | `Optional[float]` | `stop` | Exact stop point when the route defines one |
| `dwell_time_sec` | `float` | `stop` | Planned dwell time; use `0` for a terminal stop with no dwell |
| `flexible_platform` | `bool` | `stop` | Allow compatible platform substitution |
| `travel_direction_policy` | `TravelDirectionPolicy` | all | Approach-direction policy |
| `timing_mode` | `TimingMode` | `stop` | Departure timing mode |
| `target_time` | `Optional[float]` | `stop` | Relative/absolute timing target |
| `departure_mode` | `Optional["auto" \| "manual"]` | usually `stop` | Optional departure override |

- Split the current overloaded waypoint concept into two explicit kinds in the target model: `Waypoint.kind == "pass_through"` and `Waypoint.kind == "stop"`.
- A `pass_through` waypoint is still a live route/planning target, but it is not a dwell/stop target. It may be traversed at speed, does not define a stop window, and does not invoke arrival/dwell logic.
- A terminal waypoint must not be `pass_through`. If the train must stop and reverse immediately with no dwell, that terminal waypoint is still `Waypoint.kind == "stop"` with `dwell_time_sec = 0`.
- A `stop` waypoint is the current platform-stop concept. The arrival, same-block repositioning, stop-window, and dwell rules in this section apply only to `Waypoint.kind == "stop"`.
- For target resolution, `Waypoint.kind == "stop"` and `Waypoint.kind == "pass_through"` use the same resolution chain. The difference is behavioral: `pass_through` still resolves to a concrete block/direction target, but it does not invoke stop-window or dwell logic.
- Station/platform distinction is explicit in the desired model:
  - `Station` is a logical grouping of platforms used by route planning and platform selection.
  - `Platform` is the concrete physical stop. When a waypoint resolves through station/platform data, dispatcher path requests, block IDs, directions, and stop positions are always derived from one resolved concrete platform, never from a station group directly.
  - `exec_state.resolved_platform_id` is that per-train concrete platform resolution for the current waypoint attempt when resolution goes through a platform. For a direct block-target waypoint it remains `None`.
- Define one canonical platform-local travel frame for every concrete `Platform`. The platform exposes an ordered local traversal axis from its first stop position to its last stop position. "Forward" means increasing platform order on that axis; "reverse" means decreasing platform order. This platform-local frame is used for compatibility checks and route-level traversal intent only; it is not the exact dispatcher stop frame.
- `requested_destination_direction` is the route-owned traversal constraint at the requested target. In the stored resolved target contract it is always one concrete value, `forward` or `reverse`, in the resolved local order of the target block. When a waypoint resolves through a concrete platform, that local order is the platform's canonical traversal axis. When a waypoint targets a direct block, `RouteExecutor` resolves one concrete `forward` / `reverse` value against that block's immutable internal traversal order created with the block itself and never rewritten later. The route definition may allow either direction, but the stored resolved target for one attempt must not remain `either`.
- Define the exact dispatcher terminal frame separately. The terminal frame is the explicit directed geometry used for stop-position normalization and stop-window checks: `path_tiles`, `entry_edge`, and `exit_edge` for the relevant stop block. When a live path exists, dispatcher derives that frame from the chosen terminal `resource_sequence` block entry. Stop math must never infer that frame from `requested_destination_direction` alone.
- Resolve waypoint targets through one canonical chain: `station -> platform -> block`. If the waypoint already specifies a concrete `platform_id`, resolution starts at `platform`. If it specifies a direct `block_id`, resolution skips station/platform lookup and targets that block directly.
- When a route/waypoint targets a logical station rather than a fixed platform, `RouteExecutor` must resolve one concrete platform before path creation. In v1, platform availability is all-or-nothing across the whole platform: a platform is available only when all blocks linked to that platform are free of live-path reservation and not physically occupied by another train. Blocks already occupied by the same train on its own current same-block / already-arrived handling do not disqualify that platform for that train. Deterministic tie-breaking is not required.
- Flexible platform selection in v1 is:
  - if the waypoint names no preferred `platform_id`, choose any compatible available platform in the station group
  - if the waypoint names a preferred `platform_id` and it is compatible and available, use it
  - if the waypoint names a preferred `platform_id`, it is not available, and `flexible_platform == True`, choose any other compatible available platform in the same station group
  - if the waypoint names a preferred `platform_id`, it is not available, and `flexible_platform == False`, fail the waypoint attempt rather than silently substituting another platform
- `TravelDirectionPolicy.PRESERVE` applies only to flexible platform substitution. It means that if the originally preferred/default platform for this waypoint attempt implies one concrete traversal direction, any substitute platform must preserve that same direction. If there is no resolvable original/preferred direction for that attempt, `PRESERVE` falls back to `EITHER` rather than inventing a direction preference.
- After a concrete platform is resolved, `RouteExecutor` must gather all blocks linked to that platform and resolve one terminal target block from the train's approach side. The canonical rule is: determine the travel direction through the platform for this attempt, then choose the block on the far side of travel as `requested_destination_block_id`. That block is the dispatcher-facing target block for both `stop` and `pass_through` waypoints that resolve through that platform. If a caller-supplied block lies in the middle of the platform, `RouteExecutor` still normalizes it to that far-side block before calling `PathDispatcher`.
- After that resolution, `RouteExecutor` derives the per-attempt `ResolvedWaypointTarget` (`requested_destination_block_id`, `requested_destination_direction`, `route_defined_stop_position_cm`) for the current waypoint, passes any route/waypoint-defined exact `stop_position_cm` through unchanged as `route_defined_stop_position_cm`, and passes only that resolved request contract into `PathDispatcher`. It does not resolve fallback `stop_position_cm` itself.
- In v1, every waypoint must provide exactly one concrete targeting field among `block_id`, `platform_id`, and `station_id`. Supplying more than one is a route-definition error; supplying none is also a route-definition error. No extra type-discriminator field is needed because the mutually exclusive field set is itself the discriminator.
- A `Waypoint.kind == "stop"` may therefore target either:
  - a fixed concrete `platform_id`, or
  - a logical `station_id`, in which case `RouteExecutor` resolves `exec_state.resolved_platform_id` before path creation
  - a direct concrete `block_id`, in which case no platform resolution is required
  If more than one of `block_id`, `platform_id`, or `station_id` is present, the route definition is invalid. If none of them is present for a stop waypoint, the route definition is invalid.
- Keep platform targeting, dwell/timing rules, platform-selection flexibility, and direction policy.
- Remove platform-side constraints from the target model in v1. Compatibility depends only on travel-direction compatibility plus current availability; there is no separate `required_platform_side` field or fallback rule.
- Add explicit stop-policy data used by route arrival and same-block repositioning for `Waypoint.kind == "stop"`:
  - the resolved destination block for the waypoint attempt
  - an optional exact stop target such as `stop_position_cm` when the route/platform plan defines one directly
- Add an explicit route-owned traversal-constraint field, `requested_destination_direction`, on the per-attempt `ResolvedWaypointTarget` for every waypoint. Same-block / same-tile repositioning must not guess direction from geometry alone; `RouteExecutor` must resolve this constraint from the route/platform definition before path creation, while dispatcher still derives the exact stop frame from explicit terminal geometry.
- If the waypoint targets a logical `station_id` or concrete `platform_id`, `requested_destination_block_id` and `requested_destination_direction` are resolved only after `RouteExecutor` has picked a concrete `resolved_platform_id` for that train's current attempt and then resolved the far-side terminal block for that platform traversal. If the waypoint targets a direct `block_id`, that block becomes `requested_destination_block_id` directly and `resolved_platform_id` stays `None`. Any exact `stop_position_cm` defined on the route/waypoint is then passed through unchanged; fallback stop resolution still belongs to `PathDispatcher`.
- Add optional `departure_mode` as a per-waypoint override of `Route.default_departure_mode`. When unset, the waypoint inherits the route default.
- Use one global stop-window tolerance of `5 cm` around the resolved `stop_position_cm` for `Waypoint.kind == "stop"`; do not carry a second per-waypoint tolerance field in v1.
- When the route does not provide an exact `stop_position_cm`, `PathDispatcher` should resolve one from `requested_destination_block_id` plus its own exact terminal frame and global stop-margin settings. The desired v1 settings are `PATH_STOP_TRACK_MARGIN_CM = 25.0` and `PATH_STOP_PLATFORM_MARGIN_CM = 5.0`, and dispatcher chooses between them by checking whether `requested_destination_block_id` currently has a platform on it in network data.
- Margin choice is block-level only in v1: if `requested_destination_block_id` has any platform on it, use `PATH_STOP_PLATFORM_MARGIN_CM`; otherwise use `PATH_STOP_TRACK_MARGIN_CM`. Platform side or multiple-platform identity inside that same block does not introduce a second fallback rule for margin selection in v1.
- `Waypoint.stop_position_cm` is not an absolute map location. It is a stop marker interpreted as a distance counted backward from the terminal boundary in the resolved direction of travel through the target. When the same physical platform is served from the reverse side, dispatcher reverses that marker into the reverse-directed terminal frame before storing `requested_stop_position_cm`.
- For `Waypoint.kind == "stop"`, waypoint arrival / completion is not simply "the train is in the waypoint block". The target condition is "the train is associated with the resolved waypoint target and is fully stopped within the canonical stop window of the resolved `stop_position_cm` for that stop." In v1, that stop window is `abs(delta_cm) <= 5.0` using `RailwayNetwork.project_stop_frame_delta(...)`.
- The waypoint stop-window definition should be the single source for both the no-movement already-arrived bypass and the `requested_stop_position_cm` used for a normal or synthetic dispatcher path to that waypoint. `PathDispatcher.resolve_stop_target(train_id, requested_destination_block_id, terminal_frame, requested_stop_position_cm=None)` is the canonical resolver when the route does not provide an exact stop. Route-defined exact stops override that fallback.
- Add one shared stop-frame conversion helper such as `RailwayNetwork.project_stop_frame_delta(head_tile, head_entry_edge, head_distance_cm, destination_block_id, stop_frame, stop_position_cm) -> delta_cm`. Here `stop_frame` is the exact directed stop frame (`path_tiles`, `entry_edge`, `exit_edge`) for the destination block. It uses tile lengths and traversal geometry to project the live train head and the resolved stop target onto the same forward axis, then returns their separation in centimeters on that shared axis. In v1, every stop-window and exact-stop completion check uses the same rule: the train is "at the stop" iff `abs(delta_cm) <= 5.0`. Callers must not branch on the sign of `delta_cm`; only the absolute-distance test is canonical in v1. `PathDispatcher._complete_path_on_full_stop()` uses the live path's terminal `resource_sequence` frame for this helper, and the same-block stop-window/bypass predicate may use an equivalent explicit local frame only when the route/waypoint already defines the exact stop target locally.

**Waypoint timing-mode semantics**

- `TimingMode.NONE`: ordinary route-plan execution. After arrival, the train dwells for `dwell_time_sec` and then may advance/depart under the route's departure policy.
- `TimingMode.RELATIVE`: `target_time` is a departure timestamp on the simulation clock, measured in seconds from simulation start (`0` at simulation start). `dwell_time_sec` is not a second controlling timer in this mode; the train dwells only until that departure timestamp. If the train arrives late (simulation time already past `target_time`), it departs immediately after the arrival/dwell-entry step with no additional dwell.
- `TimingMode.ABSOLUTE`: `target_time` is a wall-clock/system-time departure timestamp. In v1, store it as an absolute Unix timestamp in seconds. `dwell_time_sec` is not a second controlling timer in this mode; the train dwells only until that departure timestamp. If the train arrives late (system time already past `target_time`), it departs immediately after the arrival/dwell-entry step with no additional dwell.
- `Waypoint.should_depart(...)` should take `time_at_platform_sec`, `current_time_sec`, and `timing_mode` in v1. `RouteExecutor._check_dwell_complete()` is responsible for selecting `current_time_sec` from the correct clock for that waypoint: simulation time for `TimingMode.NONE` and `TimingMode.RELATIVE`, wall-clock Unix time for `TimingMode.ABSOLUTE`.

**Route-definition validation**

- A valid route definition must contain at least one `Waypoint.kind == "stop"`.
- A valid route definition must contain at least two `Waypoint.kind == "stop"` waypoints in total.
- A route made only of `pass_through` waypoints is invalid.
- The terminal waypoint must be `Waypoint.kind == "stop"`.
- Every `Waypoint.kind == "stop"` must identify a valid stop target: either a concrete `block_id`, a concrete `platform_id`, or a logical `station_id` that can be resolved to a platform at runtime.
- `RouteExecutor` should reject or refuse to activate any route definition that violates these rules rather than trying to infer missing stop semantics at runtime.

**Route-execution state**

| Field | Type | Meaning |
| --- | --- | --- |
| `train_id` | `str` | Assigned train |
| `route_id` | `str` | Owning route |
| `current_waypoint_index` | `int` | Last finished waypoint (`-1` before the first completed waypoint) |
| `target_waypoint_index` | `int` | Furthest waypoint already requested/covered by the current live planning chain |
| `direction` | `int` | Effective traversal direction |
| `route_start_time` | `Optional[float]` | Route activation timestamp / bookkeeping |
| `arrived_at_platform_time` | `Optional[float]` | Dwell timing |
| `waiting_for_path` | `bool` | Pre-departure create-path failure flag only |
| `last_waypoint_platform_id` | `Optional[str]` | Last completed stop platform |
| `resolved_platform_id` | `Optional[str]` | Current platform resolution |
| `resolved_target` | `Optional[ResolvedWaypointTarget]` | Current route-owned request cache for `target_waypoint_index` |
| `active_path_id` | `Optional[str]` | Current live-path correlation key |

- Keep `current_waypoint_index`, add `target_waypoint_index`, and keep `direction`, `resolved_platform_id`, `resolved_target`, `arrived_at_platform_time`, and `last_waypoint_platform_id`.
- Initialize both waypoint cursors to `-1` before the first completed waypoint. The invariant is `current_waypoint_index <= target_waypoint_index`.
- `current_waypoint_index` is the route-progress cursor: it always marks the last waypoint the train has actually finished.
- `target_waypoint_index` is the planning cursor: it marks the furthest waypoint already requested from dispatcher by the current live create/extend chain. When there is no live armed target beyond current progress, `target_waypoint_index == current_waypoint_index`.
- The next waypoint eligible to be newly requested from dispatcher is at most `target_waypoint_index + 1`; route planning must not skip farther ahead in one request.
- `resolved_platform_id` and `resolved_target` cache the current planning target at `target_waypoint_index`, not the last finished waypoint. The next waypoint to check for in-motion pass-through consumption is always `current_waypoint_index + 1`.
- Add `active_path_id: Optional[str]` as the route-owned correlation key for the current live dispatcher path being used by this route execution. It is set when `RouteExecutor` adopts a newly created path or retained authority, stays unchanged across `extend_path(...)` on that same live path, and is cleared when that path completes for this route, is cancelled for this route, or route execution is torn down. A single live path may span multiple chained `pass_through` waypoints before the downstream `stop` it is currently targeting.
- Keep a pre-departure retry flag such as `waiting_for_path` only for "path request failed before movement began" handling.
- Set `waiting_for_path = True` only when pre-departure platform lookup, current-block lookup, or dispatcher path creation fails before any authority exists.
- Do not use `waiting_for_path` for a live best-effort truncated path. That case still has active authority; the route should keep the same path alive and retry `extend_path()` on it rather than dropping into the pre-authority failure bucket.
- Clear `waiting_for_path` on successful path creation, on the same-block already-arrived bypass, on authoritative path completion, and when `RouteExecutor.handle_path_cancellation(...)` consumes a cancellation notification for the route's current live path.
- Do not add a separate cancellation-recovery sub-state for v1. When a live path is cancelled, `PathDispatcher` clears controller authority and the controller enters the no-authority braking path against the current occupied-block boundary. `RouteExecutor` should wait until the train reaches a fresh stopped no-authority state before retrying the waypoint. If that no-authority stop escalates into EB, the train remains blocked until a manual `release_emergency_cmd()` transitions it back to `STANDBY` and latches a controller-owned `post_eb_restart_pending` flag; only after that stopped recovery point should ordinary `RouteExecutor` reachability + departure logic for the same waypoint run again.
- Do not add a separate retained-authority standby sub-state. When a train is in `STANDBY` and `received_path_plan` still exists, `RouteExecutor` should treat that as "authority retained, awaiting fresh departure decision" and resolve it through ordinary departure policy rather than through a new route-execution state enum. If the stop was caused by EB, the train must remain in `STANDBY` until a fresh explicit departure decision; it must not auto-resume from retained authority.

**Route-executor state reads**

- `RouteExecutor` should read live operational state from `TrainController`, not from mirrored runtime fields on `Train`.
- Departure, retry, arrival, dwell, and waiting decisions should use controller state as the operational source of truth.
- `Train` may still expose persisted or UI-facing state, but route-control decisions should not depend on it being a live mirror.
- In the desired model, manual driving is `control_mode == manual`, not a separate operational state. A route-managed manual train still uses ordinary `STANDBY` / `IDLE` / `RUNNING_*` states and dispatcher authority; the difference from ATO is how movement begins and is driven, not whether `RouteExecutor` owns the waypoint plan.

**Concrete `RouteExecutor.update(delta_time)` calls**

- `update(delta_time)` should continue to increment `RouteExecutor.simulation_time`, scan active route/train pairs, and call `_execute_route_for_train(train, route)`.
- `_execute_route_for_train()` should continue to create or reuse `RouteExecutionState`. When `target_waypoint_index == current_waypoint_index`, it should arm the next planning target by setting `target_waypoint_index = current_waypoint_index + 1`, resolving that waypoint through the canonical `station -> platform -> block` chain, and storing the result in `exec_state.resolved_platform_id` / `exec_state.resolved_target`.
- Before any create/extend decision, `exec_state.resolved_target` is the route-owned request contract for `target_waypoint_index`: the waypoint target block, the route-owned `requested_destination_direction` constraint, and the optional route-defined exact stop marker `route_defined_stop_position_cm`. It must NOT resolve fallback `stop_position_cm` itself. `PathDispatcher` is the single owner of stop-position resolution: `RouteExecutor` passes the route/waypoint-defined numeric stop marker when the route plan has one, and passes `None` otherwise. Dispatcher resolves the stored target inside `create_path(...)` / `extend_path(...)` via `PathDispatcher.resolve_stop_target(...)`, using the chosen terminal frame and, for same-block / same-tile normalization, the `train_id`'s current occupied blocks plus current cab direction.
- `_execute_route_for_train()` should ensure the train's `TrainController` exists before it reads controller state or calls `PathDispatcher.create_path(...)`. In the desired model a route-managed train should already have a controller by the time authority is assigned in Phase 1. `SimulationController` owns only the 1:1 binding between `train_id` and `TrainController`; it does not construct or push path plans.
- The operational state checks inside `_execute_route_for_train()` should read controller state rather than `train.state`.
- If controller state is `OFF`, `_execute_route_for_train()` must treat that train as unavailable for route execution on this tick. It must not create, extend, reuse, or rely on retained dispatcher authority for that train, must not auto-power-on the train, and should record a route/path error such as "train is powered off". In v1, power-off is also a route-teardown condition for that train: `RouteExecutor` should unassign the train from the route, cancel any live path through the ordinary `PathDispatcher` cancellation workflow, clear the controller path plan through that workflow, and stop route execution for that train without advancing the waypoint as completed. `TrainPhysics` continues to run independently; power-off kills the onboard controller, not world physics.
- If controller state is `STANDBY` or `IDLE`, `_execute_route_for_train()` should call `_try_depart_to_waypoint(train, route, exec_state)`.
- If controller state is `RUNNING_MOVING`, `_execute_route_for_train()` should not poll for stop-waypoint arrival; normal stop-waypoint arrival now comes only from dispatcher-authoritative path completion. `Waypoint.kind == "pass_through"` progression is the one exception that is still geometry-based while moving: `RouteExecutor` should resolve waypoint `current_waypoint_index + 1` on demand whenever that index is already covered by the current planning chain (`current_waypoint_index + 1 <= target_waypoint_index`). When the train head first enters that next pass-through waypoint's resolved block in the required resolved direction, `RouteExecutor` should treat that pass-through waypoint as consumed and set `current_waypoint_index` to that waypoint index immediately without entering dwell.
- If controller state is `RUNNING_AT_STATION`, `_execute_route_for_train()` should call `_check_dwell_complete(train, route, exec_state)`.
- If controller state is `RUNNING_WAITING`, `_execute_route_for_train()` should treat the route as on hold and should not request a brand-new path while `received_path_plan` is still present. If that still-live path's authorized target (`authority_end_block_id`, `authority_stop_position_cm`) is still short of its requested target (`requested_destination_block_id`, `requested_stop_position_cm`), `_execute_route_for_train()` should keep retrying `PathDispatcher.extend_path(path_id=existing_path_id, requested_destination_block_id=exec_state.resolved_target.requested_destination_block_id, requested_destination_direction=exec_state.resolved_target.requested_destination_direction, requested_stop_position_cm=exec_state.resolved_target.route_defined_stop_position_cm, acquisition_mode=route.acquisition_mode)` on that same path.
- `RouteExecutor` does not call `PathDispatcher.resolve_stop_target(...)` directly at any point; that helper is dispatcher-internal. Route callers pass a route-defined numeric `route_defined_stop_position_cm` when they have one, or `None` to let dispatcher resolve the fallback against the chosen terminal frame.
- `_try_depart_to_waypoint()` is the route executor's normal "make progress toward the current waypoint" entry point. The same function should be used for the initial departure attempt, for `STANDBY` with retained authority, and for retries after `PathDispatcher` has cancelled/revoked the previous path once the train has reached a fresh stopped no-authority state. If the cancellation stop escalated into EB, that retry point occurs only after a manual release has returned the controller to `STANDBY` or `IDLE`; active-path completion still comes back from `PathDispatcher`, not from `_execute_route_for_train()` polling for end-of-path.
- `RouteExecutor` should resolve one effective departure policy for the current waypoint each time it considers departure:
  - start from `Route.default_departure_mode`
  - override with `Waypoint.departure_mode` when present
  - if `TrainController.post_eb_restart_pending` is true, apply `Route.auto_depart_after_eb` as the separate final gate

- **Departure-to-waypoint flow**

- `RouteExecutor` owns waypoint targeting and departure policy; `PathDispatcher` owns stop-target resolution and authority acquisition. The departure flow is: resolve waypoint block/direction, optionally reuse retained authority when the cached requested target matches, otherwise request or extend dispatcher authority, and only then decide whether to call `TrainController.depart()`.
- The stop-window, same-block repositioning, and dwell-entry rules in this subsection apply only to `Waypoint.kind == "stop"`. `Waypoint.kind == "pass_through"` does not call arrival/dwell helpers; it constrains route geometry only.
- `Waypoint.kind == "pass_through"` is planned incrementally toward later route entries using the same block/direction resolution rules as a stop waypoint. It is still a live route target in the planning chain, but it is not intended to become the stopping end of authority. `target_waypoint_index` is the furthest waypoint already covered by the current live planning chain, while `current_waypoint_index + 1` is the next waypoint eligible for in-motion consumption. No create/extend request may target farther than `target_waypoint_index + 1`.
- For a non-stop waypoint specifically, `RouteExecutor` should keep requesting `extend_path(...)` toward `target_waypoint_index + 1` until the same live path reaches a downstream actual `Waypoint.kind == "stop"` or the next extend attempt fails. The non-stop behavior comes from authority being extended beyond the pass-through block before the train reaches it: the live authority end moves past that waypoint, so the controller keeps a positive allowed speed there and the train does not stop at the pass-through target. If that downstream extension has not yet succeeded, the train still obeys the current authority end and may stop at the current boundary.
- `RouteExecutor` owns waypoint-chain progression on the route side. `PathDispatcher` grows authority geometry; it does not own waypoint progression. After any successful downstream `create_path(...)` or `extend_path(...)`, `RouteExecutor` should inspect the returned `resource_sequence` and advance `target_waypoint_index` only when that returned live authority newly covers the next waypoint in order. For chained pass-through waypoints, this comparison repeats one waypoint at a time as the same path is extended downstream.
- If the train is already stopped within the waypoint stop window in the target block, do not call `controller.depart()`. Treat the waypoint as already arrived and call the same arrival/dwell entry path used after normal path completion. This bypass is only valid when the route/waypoint already defines the exact stop target locally.
- If the train is in the target block but not yet at the waypoint stop window, request a synthetic same-block path with the route/waypoint-defined `route_defined_stop_position_cm` when one exists, or `None` otherwise so dispatcher resolves the fallback. ATO must not move the train within the same block without a dispatcher plan.
- `RouteExecutor` should not resolve stop targets at all. For path creation/extension, it passes through whatever numeric `route_defined_stop_position_cm` the route/waypoint defines, or `None` when it has no exact value. `PathDispatcher` is the sole caller of `PathDispatcher.resolve_stop_target(...)`: it does the fallback resolution inside `create_path(...)` and `extend_path(...)`, stores the result on `Path.requested_stop_position_cm`, and then computes the live `authority_stop_position_cm` from the current reachable end. There is no special same-block dispatcher entrypoint; same-block movement still uses ordinary `create_path(...)`. The route/waypoint resolution step must still provide `requested_destination_direction` as a route-level constraint; dispatcher must not guess it from same-block geometry, and must not treat it as the exact stop frame by itself.
- `RouteExecutor` should use `Route.acquisition_mode` for both `create_path(...)` and any later `extend_path(...)` retries while it is still pursuing that same planning chain. `AcquisitionMode.best_effort` is therefore a route policy choice, not a one-off branch invented mid-chain.
- For a `pass_through` waypoint, the create/extend chain is still block-based: the current per-attempt resolved target still carries `requested_destination_block_id` and `requested_destination_direction` just like a stop waypoint. The difference is not the path API shape; the difference is that once the train head enters that waypoint block in the required direction, `RouteExecutor` consumes the waypoint immediately and continues pursuing the same live path toward later route entries.
- If `TrainController.received_path_plan` already exists and still matches the current planning target, `_try_depart_to_waypoint()` must not request a new path. In v1, the retained-authority match predicate compares the existing plan against the route-owned `exec_state.resolved_target`: the existing plan and that cache must have the same `requested_destination_block_id` and the same `requested_destination_direction`. If `exec_state.resolved_target.route_defined_stop_position_cm is not None`, the existing plan's `requested_stop_position_cm` must also match that same explicit stop marker under the resolved block/direction counting rule. If the route did not define an exact stop marker, dispatcher-owned fallback stop resolution stays dispatcher-internal and the route-side match does not add a second stop-value equality check.
- A manual cab reversal or other direction-selection change does not invalidate retained authority by itself. The existing `received_path_plan` remains valid, but any attempted departure or motion inconsistent with the retained plan's directed authority geometry must be blocked by controller/ATC authority enforcement until a matching authority exists.
- If `create_path(..., acquisition_mode=best_effort)` returns a live path whose current authorized target is still short of the route's current planning target, that is not a failure. It is a degraded but valid live authority: let the controller treat it as normal movement authority toward the current authorized end, and let `RouteExecutor` keep retrying `extend_path()` on the same `path_id` toward that same route-owned `exec_state.resolved_target` on later ticks. If the route originally had an exact numeric stop, the cached value matches it; if the route did not define one, `exec_state.resolved_target.route_defined_stop_position_cm` stays `None` and dispatcher keeps owning fallback stop resolution. A best-effort request only counts as success when the returned `resource_sequence` actually grew; a zero-growth attempt is a failure result.
- On successful path, do not write train-side path cache fields from `RouteExecutor`. `PathDispatcher` owns the authoritative path-progress view; any controller/UI-facing path tiles or path-progress reads should be pushed/read as a projection of dispatcher state rather than as a train mirror.
- Do not set destination-block fields directly on `Train` or `TrainController` from `RouteExecutor`; controller authority comes only from the pushed path plan.
- `PathDispatcher` owns `controller.received_path_plan`; `controller.assigned_path_id` should be only a derived read of `received_path_plan.path_id`, so `RouteExecutor` does not write that field.
- On successful path creation, `RouteExecutor` should copy that returned `path_id` into `exec_state.active_path_id`. It should not overwrite `exec_state.resolved_target` from the pushed plan because that cache remains route-owned. When `_try_depart_to_waypoint()` reuses retained authority, it should copy the retained `received_path_plan.path_id` into `exec_state.active_path_id` before branching on departure policy.
- After any successful `extend_path(...)` that returns a grown `resource_sequence`, `RouteExecutor` must compare that returned geometry against the next unresolved waypoint in order and advance `target_waypoint_index` by exactly one only when the new live authority now covers that next waypoint. It then resolves that newly armed waypoint from route/platform/block data and updates `exec_state.resolved_platform_id` / `exec_state.resolved_target` from route state. The pushed plan remains the controller's authority view, but the route-side target cache is not recomputed from dispatcher payloads.
- Deliver the canonical **received_path_plan schema** inside the controller path plan pushed by `PathDispatcher`. Its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**.
- Keep `controller.depart()` as the way movement begins once a valid path plan exists, whether that plan was just created or retained across an EB stop.
- Ordinary departure policy (`auto` vs `manual`) controls whether `RouteExecutor` issues `controller.depart()` automatically after authority exists. `Route.auto_depart_after_eb` separately controls the post-EB retained-authority restart case and defaults to `false`.

**Concrete departure calls**

- Every `RouteExecutor` access to a train's controller goes through `SimulationController.get_or_create_controller(train.train_id)` (the shared public accessor) rather than a private `_get_controller` helper or any direct dictionary lookup.
- If current block already equals target block and the train is already stopped inside the waypoint stop window, bypass `controller.depart()` and enter `RouteExecutor._enter_waypoint_arrival(train, route, exec_state)`. This bypass is only allowed when the route/waypoint already defines the exact stop target; otherwise same-block handling should request an ordinary same-block path.
- If current block already equals target block but the train is not yet inside the waypoint stop window, call `path_dispatcher.create_path(...)` with `train_id`, `requested_destination_direction`, and `requested_stop_position_cm=exec_state.resolved_target.route_defined_stop_position_cm` for a synthetic local path. There is no same-block ATO movement without dispatcher authority.
- For that decision, `RouteExecutor` does not compute a resolved stop position locally. It passes the route/waypoint-defined numeric `route_defined_stop_position_cm` (or `None`) and the waypoint's `requested_destination_direction` into `PathDispatcher.create_path(...)`. `PathDispatcher` resolves any fallback value via `PathDispatcher.resolve_stop_target(...)` and stores the result on the new `Path` record. There is no extra same-block dispatcher entrypoint: same-block movement still uses ordinary `create_path(...)`. The global `5 cm` tolerance is applied only when the route/waypoint already provides the exact stop target locally. The bypass predicate and stop-window comparison still use the shared stop-frame conversion helper, and dispatcher still receives `requested_destination_direction` explicitly as the route-level constraint for any same-block / same-tile synthetic move.
- When the current attempt uses `AcquisitionMode.best_effort`, compare the live path's current authorized target against the live path's cached requested target each tick. If the live path is still short of that requested target, keep calling `path_dispatcher.extend_path(path_id=existing_path_id, requested_destination_block_id=exec_state.resolved_target.requested_destination_block_id, requested_destination_direction=exec_state.resolved_target.requested_destination_direction, requested_stop_position_cm=exec_state.resolved_target.route_defined_stop_position_cm, acquisition_mode=route.acquisition_mode)` on that same `path_id`; do not synthesize a new path.
- If `_try_depart_to_waypoint()` sees retained authority already present on the controller for this waypoint, do not call `path_dispatcher.create_path(...)` again. Reuse the existing authority and branch only on departure policy:
  - if auto departure is allowed for this case, call `controller.depart()`
  - if manual confirmation is required, leave the train in `STANDBY` with the retained authority intact
- Remove the train-side `current_path` / `path_index` write from `RouteExecutor`; `PathDispatcher` should own the authoritative path-progress view and expose any controller/UI-facing path tiles or path-progress display as a pushed/read-only projection.
- Remove the `train.assigned_path_id` write from `RouteExecutor`; `controller.assigned_path_id` should be derived from the path plan that `PathDispatcher` pushes.
- Do not set `controller.ato_path_id` or any destination field directly from `RouteExecutor`.
- Let `PathDispatcher` push the canonical **received_path_plan schema** to the controller as part of the path plan. Its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**.
- Remove the route-controlled fallback where `controller.depart()` is called directly without dispatcher authority; route-driven movement should require a dispatcher path plan.
- Remove the route-facing `TrainController.wait_for_path(reason)` command in the desired model. The controller should infer "no path authority" from an empty `received_path_plan`; higher layers do not need a separate command to tell it that the path is gone.
- If initial path creation fails before departure in `all_or_nothing` mode, `RouteExecutor` should record a route/path error, set `exec_state.waiting_for_path = True`, and keep the train in `STANDBY` or `IDLE`; the train never had authority, so no special controller waiting command is needed.
- In `best_effort` mode, only "no authority at all could be created" is a creation failure. A truncated but live path is not a failure path; the train may run under that current authority and later wait at the current authority boundary while `RouteExecutor` keeps retrying `extend_path()` on the same `path_id`.
- If `PathDispatcher` cancels/revokes a live path mid-run, it should clear controller authority by calling `controller.receive_path_plan(None)`, forcing the controller into no-authority braking against the current occupied-block boundary.
- After the cancellation-driven no-authority stop has finished, ordinary `_try_depart_to_waypoint()` logic for the same waypoint should run again. If that stop escalated into EB, this retry point is only after a manual release has returned the controller to `STANDBY`:
  - if reachable, let `PathDispatcher` acquire a fresh path
  - if unreachable or path creation fails, use the ordinary pre-departure route/path error path instead of introducing a separate recovery-only state machine
- When a path still exists after an EB stop (retained-authority standby), fresh departure does not require creating a new path. It requires only a new `controller.depart()` decision over the retained authority, subject to the resolved departure policy and `Route.auto_depart_after_eb`, gated by the controller-owned `post_eb_restart_pending` flag when the stop came from EB.

**Arrival detection**

- Stop using `RouteExecutor._is_train_at_platform()` as the normal arrival gate after dispatcher-owned path completion. In the desired model, `PathDispatcher._complete_path_on_full_stop()` already guarantees the train has stopped within `5 cm` of the resolved final stop for the current waypoint target block, so path completion is the authoritative arrival trigger for that stop.
- Delete `RouteExecutor._check_waypoint_arrival()` from the desired model and replace it with one route-owned arrival-entry helper such as `RouteExecutor._enter_waypoint_arrival(train, route, exec_state)`.
- Keep a waypoint stop-window predicate only for the pre-departure same-block bypass decision: if the train is already in the target block, is fully stopped, and is already inside the waypoint stop window, `RouteExecutor` should treat that as already arrived without creating a path.
- Remove `TrainController.arrive_at_station()` in the desired model and implement a new route-executor-facing `TrainController.enter_station_dwell()` command instead. This is not a rename: route-level dwell entry should happen only after a confirmed stopped arrival, either from dispatcher-confirmed path completion or from the no-movement same-block bypass where `RouteExecutor` has already determined the train is stopped inside the waypoint stop window. `TrainController.enter_station_dwell()` should be valid from dispatcher-completed post-path `STANDBY` and from the same-block no-movement already-arrived bypass. ATO movement itself still requires a dispatcher path.
- On first arrival — whether from dispatcher-reported full stop or from the same-block stop-window bypass — `RouteExecutor._enter_waypoint_arrival(...)` should set `exec_state.arrived_at_platform_time`, set `exec_state.current_waypoint_index = exec_state.target_waypoint_index`, set `exec_state.target_waypoint_index = exec_state.current_waypoint_index`, set `exec_state.last_waypoint_platform_id = exec_state.resolved_platform_id` when the completed waypoint was platform-derived and `None` otherwise, clear `exec_state.waiting_for_path`, clear `exec_state.active_path_id`, clear `exec_state.resolved_target`, and call `controller.enter_station_dwell()` to transition into `RUNNING_AT_STATION`. For dispatcher-completed arrivals, `arrived_at_platform_time` comes from callback `event_time`; for the same-block no-movement bypass it comes from the current `RouteExecutor.simulation_time`. This dwell-entry path applies to both ATO and manual trains; the manual/ATO difference is departure behavior only. The helper must not assume a preceding path completion event; the same arrival/dwell entry path is used in both cases.
- `_check_dwell_complete()` calls `waypoint.should_depart(time_at_platform, current_time_for_mode, waypoint.timing_mode)`, where `current_time_for_mode` is chosen from the correct clock for that timing mode.
- If dwell/timing says depart, `_check_dwell_complete()` calls `_advance_to_next_waypoint(route, exec_state)`.
- `_advance_to_next_waypoint()` does not mark arrival; that already happened in `_enter_waypoint_arrival(...)`. Instead it prepares the next leg by updating `exec_state.direction` according to `RouteMode.ONE_OFF`, `RouteMode.CIRCULAR`, or `RouteMode.ROUND_TRIP`, resetting `exec_state.target_waypoint_index = exec_state.current_waypoint_index`, and clearing `exec_state.resolved_platform_id` so the next leg will resolve a fresh planning target on the following tick. If backward-compat summary fields `route.current_waypoint_index` / `route.direction` are still exported, they are updated only as non-authoritative UI/compatibility mirrors and must not be treated as the canonical progress state when multiple trains are assigned to the same route.
- If another waypoint remains, `_check_dwell_complete()` calls `SimulationController.get_or_create_controller(train.train_id).finish_dwell(has_more_waypoints=True)`.
- If no waypoint remains, `_check_dwell_complete()` calls `_handle_route_complete_for_train(train, route, exec_state)`.
- `_handle_route_complete_for_train()` resolves the controller through `SimulationController.get_or_create_controller(train.train_id)`, calls `route.unassign_train(train.train_id)`, clears `train.assigned_route_id` when it matches, removes `execution_states[train.train_id]`, clears `exec_state.resolved_target`, calls `controller.finish_dwell(has_more_waypoints=False)` for any route-managed train still in dwell (including manual trains), and calls `route.deactivate()` for completed one-off routes with no assigned trains.
- `finish_dwell(has_more_waypoints: bool)` is a **dwell-exit state-transition command only**, named `finish_dwell` rather than `depart_from_station` because it does not start movement. It transitions the controller out of `RUNNING_AT_STATION`:
  - `has_more_waypoints=True` → `STANDBY` (route still has pending legs; cab stays selected).
  - `has_more_waypoints=False` → also `STANDBY` (route complete; cab stays selected because `IDLE` is reserved for the no-cab-selected state). `RouteExecutor` is still responsible for clearing route assignment and removing the execution-state entry; those are route-level bookkeeping, not controller state.
  `finish_dwell(...)` must not touch `received_path_plan` (only `PathDispatcher` writes it; authority for the completed leg was already cleared by `PathDispatcher._complete_path_on_full_stop()` before dwell began), must not call `depart()` / `ato_go()`, and must not initiate physics movement. Movement for the next leg is started on the following tick by `RouteExecutor._try_depart_to_waypoint()` → `PathDispatcher.create_path()` → `controller.depart()`. The next leg's automatic departure depends on the resolved `departure_mode`; in `manual` mode the route remains armed but stopped in `STANDBY` until the operator issues `depart()`.

**Dispatcher completion handling**

**route path-event callback schema**

- This is the single canonical callback contract for `PathDispatcher -> RouteExecutor` path-terminal notifications.
- The callback payloads are exactly:
  - completion context: `train_id`, `path_id`, `authority_end_block_id`, `authority_stop_position_cm`, `end_tile`, `event_time`
  - cancellation context: `train_id`, `path_id`, `reason`
  `RouteExecutor` uses `exec_state.active_path_id` as its route-owned correlation key for these callbacks and must not rediscover path identity from torn-down controller state.
- In that completion context:
  - `end_tile` is the train head tile coordinate at the instant `PathDispatcher._complete_path_on_full_stop()` recognizes the full-stop completion event
  - `event_time` is the simulation timestamp recorded at that same instant and is used as the dwell-entry arrival time; later dwell-departure checks choose their own current clock based on `Waypoint.timing_mode`
- `PathDispatcher._complete_path_on_full_stop()` should call `controller.on_path_complete()` first, then clear controller authority by calling `controller.receive_path_plan(None)`, and then call `RouteExecutor.handle_path_completion(...)` directly using the canonical completion payload from this schema.
- `handle_path_completion()` should treat dispatcher completion as authoritative and should not perform a second exact-stop or live-controller-state check. It should first compare the callback `path_id` against `exec_state.active_path_id`; if they differ, the callback is stale for this route and should be ignored.
- It should clear only route-executor-owned bookkeeping such as `exec_state.waiting_for_path`. Active path tiles, reserved resources, and other authority residue should not be stored or cleared on `Train` by `RouteExecutor` in the desired model. It should no longer clear `train.ato_destination_block_id` (that field should be removed), `train.assigned_path_id` (that field moves to the controller and is cleared by `PathDispatcher`), or `train.reserved_blocks` (reserved resource ownership stays dispatcher-owned).
- `PathDispatcher` clears controller authority by calling `controller.receive_path_plan(None)` as part of path teardown after `controller.on_path_complete()` but before calling `handle_path_completion()`. The derived reads `controller.assigned_path_id`, `controller.ato_path_id`, and `controller.ato_destination_block_id` then disappear automatically.
- `controller.transition_to_standby()` should be removed from `handle_path_completion()`. `controller.on_path_complete()` owns the post-path stop transition and should leave the train in `STANDBY`; `RouteExecutor` then decides whether to immediately enter dwell.
- Controller destination-block/path teardown should remain dispatcher-owned through `controller.receive_path_plan(None)`; `controller.on_path_complete()` reacts to that completion while `RouteExecutor` remains responsible for deciding whether the completed stop becomes station dwell (`RUNNING_AT_STATION`) or remains non-dwell `STANDBY`.
- `handle_path_completion()` must not need to query live train/controller path state after dispatcher teardown. It should use the completion context passed in by `PathDispatcher` plus route-executor-owned state only.
- If the completed stop matches the current route target block/stop contract, `handle_path_completion()` should immediately call `RouteExecutor._enter_waypoint_arrival(..., arrival_time=event_time)` before returning. There must not be an inter-tick window where the train sits in bare `STANDBY` at a stop and a later `RouteExecutor.update()` misreads that post-path stopped state as a fresh departure opportunity. The helper uses dispatcher-confirmed completion as truth plus the completion context passed in by `PathDispatcher`; it does not re-check the exact stop position from train/controller state.
- Add `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)` as the cancellation-side counterpart. It should first compare the callback `path_id` against `exec_state.active_path_id`; if they differ, the callback is stale for this route and should be ignored. For the matching live path, it should clear `exec_state.waiting_for_path`, clear `exec_state.active_path_id`, set `exec_state.target_waypoint_index = exec_state.current_waypoint_index`, clear `exec_state.resolved_target`, clear `exec_state.resolved_platform_id`, record route/path error context when relevant, and must NOT advance the waypoint, synthesize arrival, or treat the cancelled path as completed.
- Completion handling may observe a train still physically occupying the terminal block or switch footprint. That is not a contradiction: `RouteExecutor` treats dispatcher completion as authoritative arrival, while physical resource release still waits for the later tail-vacate callbacks.

## Train Entity

**Train ownership**

- Keep `current_speed` train-owned because `TrainPhysics` updates it together with position and occupancy.
- Remove train-side mirrors for `target_speed` and `speed_limit_reason`; those should become reads from controller lookahead at distance 0.
- Remove `manual_target_speed` entirely; the new manual model should not keep a per-train target speed field.
- Remove `train.state`; operational state lives entirely on `TrainController.state` and must not be mirrored on `Train`. Every consumer that wants operational state should read `controller.state` directly.
- Move live power/cab ownership to `TrainController`. If train serialization still keeps compatibility/bootstrap copies of power or cab fields, those are persistence/bootstrap inputs only; they are not the live runtime source of truth after the controller is created.
- Remove `train.assigned_path_id`; the controller-side derived read `TrainController.assigned_path_id` (from `received_path_plan.path_id`) is the only runtime source.
- Remove `train.ato_destination_block_id`; destination block and stop position should travel inside the controller path plan pushed by `PathDispatcher`.
- Remove train-side `current_path` and `path_index`; active path tiles and path-progress reads should come from a controller/UI projection owned by `PathDispatcher` instead of a train mirror written by `RouteExecutor`.
- Remove `train.reserved_blocks` as runtime authority residue in the desired model; reserved block/resource ownership should stay on dispatcher path records and controller-facing projections, not on `Train`.
- Keep `Train` focused on physical and persistent state only, and move live operational diagnostics to `TrainController`.

**Review anchor**

- `TrainController` remains the source of truth for operational transitions. The rationale inverts: controller is source of truth because train-side operational mirrors (`target_speed`, `speed_limit_reason`, `ato_destination_block_id`, etc.) are removed and nothing overwrites the controller from train fields. The conclusion is the same in both models; the supporting mechanism is different.

## TrainController

**Controller state**

- `TrainController` owns live power/cab state in the desired model: `power_on`, active cab selection, and the live direction selection implied by that cab. These are not re-synced from `Train` on every tick.
- Add `manual_mode_type`, such as `simulation` or `power_percent`.
- Add `manual_power_percent` for `power_percent`-mode manual control. In v1 this is a positive-only `0..100` voltage-style command rather than a signed traction/brake command.
- Add `post_eb_restart_pending: bool` as a latched flag that marks "the last transition into `STANDBY` came from an emergency-brake release and the next departure decision is the special post-EB restart case." The flag is cleared by `TrainController.depart()` after a successful restart acceptance; `RouteExecutor` does not clear it.
- Define the `IDLE` / `STANDBY` trigger explicitly: `IDLE` is the controller's state when the train is powered on but no cab is selected. In `IDLE`, the train has no manually selected forward/backward direction because no cab is active. A controller enters `IDLE` from `OFF` when power is turned on with no cab selected. Selecting a cab transitions the controller immediately to `STANDBY`. Route authority may still be assigned while the controller is `IDLE`: in ATO mode, `depart()` / `ato_go()` may auto-select the cab from the directed authority geometry carried by `received_path_plan.resource_sequence` and then begin movement; in manual mode, assigning authority while `IDLE` is allowed but the train does not move until a cab is selected manually. The unsupported exception is same-tile synthetic movement with no cab selected: dispatcher rejects that request before any authority is pushed.
- Keep manual driving as `control_mode == manual`; do not keep a separate `MANUAL` operational state in the desired model.
- Add `assigned_path_id: Optional[str]` only as a computed read-only property derived from `received_path_plan.path_id`. It replaces `train.assigned_path_id` as the controller-facing runtime read, but it should not be stored or written independently.
- Add `received_path_plan` as the controller inbox for the active path plan pushed by `PathDispatcher`.
- `received_path_plan` is a projection written only by `PathDispatcher`; it is not the source of truth for dispatcher path ownership, and `TrainController` should not mutate it directly.
- Exception: same-tile synthetic movement without a selected cab remains unsupported in v1. `PathDispatcher.create_path(...)` should reject that request before a plan is pushed; `TrainController` does not infer or invent the cab direction for that case after the fact.

**Controller retained field schema**

| Field | Type | Meaning |
| --- | --- | --- |
| `state` | controller state enum | Canonical operational state (`OFF`, `IDLE`, `STANDBY`, `RUNNING_MOVING`, `RUNNING_WAITING`, `RUNNING_AT_STATION`) |
| `power_on` | `bool` | Live controller-owned power state |
| `cab` | `Optional[str]` | Active cab selection (`"A"`, `"B"`, or `None`) |
| `control_mode` | `"ato" \| "manual"` | High-level driving mode selector |
| `manual_mode_type` | `"simulation" \| "power_percent"` | Manual-mode submode selector |
| `travel_direction` | `Optional[str]` | Live direction selection implied by the active cab or manual stopped input |
| `handle_position` | controller handle enum/value | Active handle command for ATO and manual `simulation` mode |
| `manual_power_percent` | `float` | Active manual driver command for manual `power_percent` mode |
| `eb_active` | `bool` | Emergency-brake latch |
| `ato_running` | `bool` | Whether ATO is actively running under the current mode/authority |
| `post_eb_restart_pending` | `bool` | Latched post-EB restart gate |
| `received_path_plan` | `Optional[received_path_plan]` | Controller's read-only active-authority projection written only by `PathDispatcher` |
| `lookahead` | canonical lookahead payload cache | Cached lookahead data for the current tick |

- The table above is the canonical retained controller field set for v1. If a field is not in this table or in a later canonical payload schema, it should not remain as an independently written live controller field.

**Controller state machine**

| State | Meaning | Canonical entry triggers | Canonical exits |
| --- | --- | --- | --- |
| `OFF` | Power off | `TrainController.power_off_cmd()` from a stopped powered state; startup default | `TrainController.power_on_cmd()` |
| `IDLE` | Powered on, no cab selected | `TrainController.power_on_cmd()` with no cab; `TrainController.set_cab(None)` from a stopped state | `TrainController.set_cab("A" or "B")`; `TrainController.depart()` may auto-select a cab in ATO with live authority |
| `STANDBY` | Stopped, cab selected, not dwelling, not waiting | cab selection while stopped; post-path `TrainController.on_path_complete()`; successful no-authority stop after cancellation/revocation; `TrainController.release_emergency_cmd()`; `TrainController.finish_dwell(...)` | `TrainController.depart()` / movement start; `TrainController.enter_station_dwell()`; `TrainController.set_cab(None)`; `TrainController.power_off_cmd()` |
| `RUNNING_MOVING` | Moving or actively commanded under authority/manual driving | successful `TrainController.depart()` / movement start | `RUNNING_WAITING` when stopped under the same live authority with zero clearance; `STANDBY` on post-path completion or no-authority stop; `RUNNING_AT_STATION` only through route-owned dwell entry after confirmed arrival |
| `RUNNING_WAITING` | Stopped while the same live authority still exists but current clearance inside that authority is zero | `TrainController.tick()` when the train is stopped, `received_path_plan` is still present, and the distance-0 ATC limit is at or below the stopped threshold | back to `RUNNING_MOVING` when distance-0 clearance becomes positive again under the same authority; authority loss follows the no-authority braking path instead of staying in `RUNNING_WAITING` |
| `RUNNING_AT_STATION` | Route-managed dwell at a confirmed stop waypoint | `TrainController.enter_station_dwell()` after dispatcher-confirmed stop completion or same-block already-arrived bypass | `TrainController.finish_dwell(...)` |

- The state machine above is the single canonical operational-state contract. Shared logic must not introduce additional live route/runtime state enums for path completion, retained-authority standby, or cancellation recovery in v1.
- Stopped-only operator commands (`power_off_cmd()`, `set_cab(...)`, `set_mode(...)`, and manual stopped-direction selection) may be issued from any physically stopped controller state. In v1 they disengage any active ATO run and land in the command-defined stopped target state (`OFF`, `IDLE`, or `STANDBY`); they do not cause automatic movement just because clearance later returns or dwell later finishes.

**Power/cab/mode command contracts**

- `TrainController.power_on_cmd()` is the explicit `OFF -> powered` transition. It is valid only when power is currently off. If no cab is selected it transitions to `IDLE`; if a cab is already selected it transitions directly to `STANDBY`.
- `TrainController.power_off_cmd()` is valid only while the train is stopped. It transitions the controller to `OFF`, clears the active cab selection, disengages any active ATO run (`ato_running = False`), and is the trigger for higher-layer route/dispatcher teardown: successful power-off must cause the train to be unassigned from its route and any live path to be cancelled through the ordinary `PathDispatcher` cancellation workflow. The controller turning off does not stop `TrainPhysics`; world physics continues to run.
- `TrainController.set_cab(cab)` is valid only while the train is stopped. It disengages any active ATO run (`ato_running = False`) but does not create motion or clear retained authority. Setting `cab` to `"A"` or `"B"` transitions the controller to `STANDBY`; setting `cab = None` transitions the controller to `IDLE`.
- `TrainController.set_mode(mode)` is valid only while the train is stopped. It switches between `ato` and `manual`, disengages any active ATO run (`ato_running = False`), and does not create or clear dispatcher authority. `manual` is a control mode, not a separate operational state in the desired model: after a stopped mode switch the controller remains `STANDBY` when a cab is selected, otherwise `IDLE`.
- `TrainController.set_travel_direction(direction)` is a manual-mode stopped-only input. It does not clear retained authority and does not start movement. If the selected direction is inconsistent with the directed authority geometry carried by `received_path_plan.resource_sequence`, the retained plan remains valid but authority enforcement must block movement in that inconsistent direction until a matching authority exists.

**received_path_plan schema**

- This is the single canonical schema for the controller's active path-plan payload. Every producer/consumer should refer back to this subsection by name instead of restating the field list elsewhere.
- `received_path_plan` carries exactly `path_id`, `resource_sequence`, `requested_destination_block_id`, `requested_destination_direction`, `requested_stop_position_cm`, `authority_end_block_id`, and `authority_stop_position_cm` in v1. It is the controller's only active path-plan payload. `resource_sequence` uses the canonical **resource_sequence entry schema**, and each nested `path_tiles` slice uses the canonical **path_tile entry schema**, so the controller can extend lookahead along the actual geometry without reading `Train.current_path` and without inferring stop-frame direction from tile coordinates alone. `requested_destination_direction` remains the route-owned target traversal constraint; the exact stop frames come from explicit directed geometry on the relevant terminal `resource_sequence` entries.
- `requested_stop_position_cm` is the resolved requested stop in the exact dispatcher-resolved terminal frame for `requested_destination_block_id`. `authority_stop_position_cm` is the stop the controller must brake to right now in the exact terminal frame for `authority_end_block_id`. When the live authority already reaches the current requested target carried by the path, those two values match; when the path is a best-effort truncated authority, `authority_stop_position_cm` is the temporary current boundary stop while `requested_stop_position_cm` remains the requested stop for that same path target.
- Treat `assigned_path_id`, `ato_path_id`, and `ato_destination_block_id` as computed reads derived from `received_path_plan`, not as independently written runtime fields. The alias mapping is explicit in v1:
  - `assigned_path_id := received_path_plan.path_id`
  - `ato_path_id := received_path_plan.path_id`
  - `ato_destination_block_id := received_path_plan.authority_end_block_id`
  When `PathDispatcher` clears authority by calling `controller.receive_path_plan(None)`, all three disappear automatically.
- Keep `lookahead`, but make it the canonical cached series used by ATC, ATO, UI, and monitor consumers.
- Remove standalone scalar fields such as `target_speed` and `effective_limit` once those values are represented through lookahead-at-distance-0 reads. Keep the top-level exported `speed_limit_reason` only as a compatibility projection for current UI/monitor consumers; its canonical source remains the distance-0 lookahead speed-curve metadata.

**canonical lookahead payload**

- `TrainController.get_lookahead()` is the single canonical exported lookahead payload for ATC, ATO, UI, and monitor consumers. Every producer/consumer should refer back to this subsection by name instead of restating the field list elsewhere.
- In v1, `get_lookahead()` carries exactly:
  - top level: `train_id`, `head_tile`, `head_distance_cm`, `travel_direction`, `cab`, `speed_limit_reason`, `lookahead`
  - nested `lookahead`: `blocks`, `signals`, `switches`, `stations`, `clearance_cm`, `safety_margin_cm`, `target_stop_cm`, `speed_curves`
- `head_tile` is the current train head tile coordinate or `None` when unavailable. `head_distance_cm`, `clearance_cm`, `safety_margin_cm`, and `target_stop_cm` are centimeters. `travel_direction` is the train's current forward travel direction in the same frame used by movement/traversal. `cab` is the currently selected cab or `None`.
- `blocks`, `signals`, `switches`, and `stations` are ordered forward projections for UI/debug consumers. They are informational read models only; control decisions come from the same payload's clearance and speed-curve data.
- `lookahead.stations` keeps the current compatibility payload name in v1, but each entry refers to a concrete physical platform ahead on the track, not to a logical multi-platform station group.
- Each `lookahead.blocks` entry carries exactly:
  - `id`
  - `name`
  - `state`
  - `start_cm`
  - `end_cm`
  - `length_cm`
  - `tile_count`
  - `speed_limit_kmh`
- Each `lookahead.signals` entry carries exactly:
  - `id`
  - `name`
  - `state`
  - `distance_cm`
- Each `lookahead.switches` entry carries exactly:
  - `x`
  - `y`
  - `name`
  - `position`
  - `distance_cm`
  - `locked`
  where `position` is `"main"` or `"diverging"` for the live switch alignment at that tile, and `locked` is the exported boolean monitor view of whether that switch is currently held by path locking.
- Each `lookahead.stations` entry carries exactly:
  - `station_id`
  - `name`
  - `distance_cm`
  - `length_cm`
  - `tile_count`
  - `top_speed_kmh`
- `station_id` in this compatibility payload is the current exported identifier for that concrete physical platform object on the track. It is not a logical station-group ID.
- Each `lookahead.speed_curves` entry carries exactly:
  - `distance_cm`
  - `atc_limit`
  - `operational_limit`
  - optional `atc_limit_reason_code`
  - optional `atc_limit_reason_detail`
  The limit fields are in `km/h`.
- `target_stop_cm` is the live current stop target in the same forward distance axis as the `speed_curves[*].distance_cm` samples and the stop-frame projection helper. With a live path it corresponds to the current authority stop; without a live path it corresponds to the current no-authority block-boundary stop.
- Consumers asking "what is the target speed right now?" sample `lookahead.speed_curves` at distance `0`: `operational_limit` at distance `0` is the controller target speed, and `atc_limit` plus its reason metadata at distance `0` is the controller ATC limit/reason. The top-level `speed_limit_reason` field remains part of the exported v1 payload shape for compatibility with current monitor/UI consumers.
- The empty/no-cab/no-train form must preserve this same top-level and nested key shape, with empty lists and zero distances instead of returning a different payload shape. If a compatibility producer cannot compute one of the nested stop/safety fields on an empty payload, it should still emit the key with `0.0` rather than omitting it.

**Controller tick order**

1. If no train, return immediately.
2. If power is off, set controller to `OFF` and return.
3. If emergency brake is active:
  - Set handle to `EB` and early-exit the tick while preserving path and route state.
  - Stay in the early-exit loop until the train has stopped.
  - Require explicit `release_emergency_cmd()` to clear `eb_active`.
  - After release, transition to `STANDBY` and set `post_eb_restart_pending = True`.
4. Mark cached lookahead data stale for the new tick.
5. If no cab is selected, force ATC limit to zero and return the canonical empty lookahead payload for that tick.
6. Otherwise ensure the canonical lookahead payload is available:
  - With a received path plan, lookahead should extend along the path to `authority_stop_position_cm`.
  - Without a path plan, lookahead should extend only through the forward boundary of the current occupied block in the train's current travel frame. For a train spanning multiple blocks, "forward" means the side where the train/path would continue next, not the trailing side.
7. Build ATC state from the cached lookahead payload, including structured reason metadata.
8. Apply ATC overspeed logic.
9. Calculate ATO info.
10. In ATO mode:
  - If `ato_running`, calculate handle with `_ato_calculate_handle()`.
  - If not running, choose braking handle toward zero and transition to standby once stopped.
11. In manual mode, write a controller-owned command and let physics convert it to speed:
  - In `simulation` submode, store the driver's handle notch in `handle_position` and allow ATC to override with braking.
  - In `power_percent` submode, store the driver's percentage power in `manual_power_percent` and let physics map it through `SpeedProfile.power_speed_curve`. In v1 this value is `0..100`, positive only, and represents applied track/motor voltage rather than signed traction vs brake effort.
   - At any given tick there is only one authoritative operating input for the active mode. In ATO and manual `simulation` mode, the controller/physics speed solution is authoritative and `power_percent` / `track_voltage` are derived from that operating point. In manual `power_percent` mode, `manual_power_percent` is authoritative and `speed_kmh` / `track_voltage` are derived from that command path. Reducing `manual_power_percent` is how the driver backs off in this mode; service braking / EB remain controller safety overrides rather than negative power-percent values.
12. Stop computing standalone `effective_limit` and `target_speed` scalars; consumers should read controller lookahead at distance 0 instead.
13. Remove controller-side path completion checks such as standalone path completion or path-backed completion helpers.
14. Let waiting and completion decisions come from dispatcher-owned feedback emitted during or immediately after `TrainPhysics.tick(...)`, not from a separate `SimulationController` forwarding pass.
15. `RUNNING_WAITING` is controller-owned only. To enter it: the ATC limit derived from the canonical lookahead payload (at distance 0) is at or below the stopped-speed threshold, the train has fully stopped, and `received_path_plan` is still present. In the desired model `RUNNING_WAITING` means "stopped while the same path still exists, but current clearance inside that path is zero."
16. Do not enter `RUNNING_WAITING` solely because the driver manually stopped the train or because EB brought the speed to zero. A driver-commanded manual stop under live authority should remain in `RUNNING_MOVING`; zero speed alone does not change the operational state. Those cases become waiting only if the controller recomputes the same live authority and still finds zero-speed clearance at distance 0.
17. `TrainController.tick()` is also the only component that clears `RUNNING_WAITING`: when the same authority still exists and the lookahead-derived allowed speed at distance 0 becomes positive again, the controller may return to `RUNNING_MOVING` according to mode-specific restart logic.
18. A best-effort truncated path stopping at its current authority boundary is a normal source of `RUNNING_WAITING`, not completion. In that case the same `received_path_plan.path_id` remains live while `RouteExecutor` keeps retrying `extend_path()` on it.
19. Do not enter `RUNNING_WAITING` for an EB-caused stop. If `received_path_plan` is cleared while the train is moving, treat that as path-authority loss rather than ordinary waiting. The essential event is "authority removed": `PathDispatcher` clears `received_path_plan(None)`, then on the next tick the controller rebuilds lookahead with no live authority and therefore only to the occupied block boundary. The controller must command ordinary braking toward zero at that current-block boundary using the no-authority ATC limit derived from that rebuilt lookahead. If the no-authority braking / overspeed check determines service braking is insufficient to keep the train within that current block, it escalates to EB. If ordinary braking succeeds and the train reaches a full stop with `eb_active == False`, transition to `STANDBY`. This is the cancellation/revocation path, not `RUNNING_WAITING`.
20. After `release_emergency_cmd()`, transition to `STANDBY` even if `received_path_plan` still exists. That is the retained-authority standby case: the authority remains available, but restarting requires a fresh explicit `depart()` decision rather than automatic controller self-resume.

**ATC speed limit**

- Stop reading signal state directly for ATC restriction selection. In the desired model, CAUTION restriction should come from a single controller-owned global cap such as `CAUTION_SPEED_LIMIT_KMH`, applied when `TrainController` samples a block whose computed `Block.state` is `CAUTION`; signals remain display state derived from blocks rather than an independent ATC input.
- In manual mode with a received path plan, include `_calculate_authority_boundary_limit()` so the effective ATC limit enforces the current authority end correctly: brake to `authority_stop_position_cm` in the current live authority frame.
- Treat ATC reason reporting as structured metadata on the lookahead/curve payload, not only as a free-form controller string.

**ATO handle calculation**

- Stop rebuilding monitor lookahead inside `_ato_calculate_handle()` and read the canonical cached controller lookahead payload instead.
- If the sampled operational speed curve at distance `0` is already at or below the stop threshold, calculate a direct braking notch immediately.
- Replace the current multi-handle forward simulation wording with a two-point sampling approach:
  - sample the operational speed curve at distance `0`
  - sample one future point at a delta-scaled horizon such as `speed_cm_s * self._tick_delta * LOOKAHEAD_TICKS`
  - combine those samples with the active ATC and block caps to choose power, coast, or brake
- Once the two-point horizon is in place, remove the old fixed-horizon wording and any special near-stop fast-path that only exists to compensate for the old horizon logic.

**Speed curves**

- Build a unified restriction set for the full clearance/authority area and take the minimum of all relevant curves and local limits at each sample point.
- Carry source metadata on each `lookahead.speed_curves` datapoint, such as `atc_limit_reason_code` and `atc_limit_reason_detail`.
- Preserve metadata for which restriction category produced the current minimum ATC limit so monitor/debug UI can explain the displayed number.

**Driver/operator inputs**

- Keep `set_handle()` as the active manual-driving command for `simulation` submode manual control.
- Add `set_manual_power_percent()` as the active driver input for `power_percent` submode manual control.

**Dispatcher/route-executor inputs**

- Add `receive_path_plan(path_plan)`, called by `PathDispatcher` after successful `create_path()`, after any `extend_path()` that appends one or more new resources to the live authority on the same `path_id`, and with `None` on cancellation or completion teardown. `received_path_plan` should mirror only the active authority path referenced by `active_path_id_by_train[train_id]`.
- `path_plan` must follow the canonical **received_path_plan schema**. Its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**; the controller must not be asked to recover stop-frame direction later from tile coordinates alone.
- In v1, the controller path plan always carries both requested and authorized targets. `requested_stop_position_cm` is the resolved stop for the path's current requested target, while `authority_stop_position_cm` is the live stop the controller should brake to on this tick.
- `path_plan` should not carry a second lifecycle status enum in the desired model. For the controller, movement authority is binary: `received_path_plan is not None` means there is live authority, and `received_path_plan is None` means there is not.
- Add `on_path_complete()`, called by `PathDispatcher._complete_path_on_full_stop()` when the train has fully stopped at the terminal stop point. `on_path_complete()` should transition the controller into post-path `STANDBY`, but it must NOT clear `received_path_plan` itself. `PathDispatcher`, as the sole owner/writer of the pushed path plan, clears authority immediately after the callback by calling `controller.receive_path_plan(None)`. `on_path_complete()` must not enter station dwell by itself; `RouteExecutor.handle_path_completion()` decides from the route plan whether the completed stop becomes dwell.
- Remove `arrive_at_station()` in the desired model and replace it with a new route-executor-facing `enter_station_dwell()` command, used only after a confirmed stopped arrival: either dispatcher-confirmed full stop on a path, or the explicit same-block no-movement bypass where `RouteExecutor` has already determined the train is stopped inside the waypoint stop window. `TrainController.enter_station_dwell()` must accept dispatcher-completed post-path `STANDBY` and same-block already-arrived entry in the desired model.
- Remove `wait_for_path(reason)` from the desired controller API. Missing path authority is represented entirely by `received_path_plan is None`.
- Keep `depart()`, `enter_station_dwell()`, and `finish_dwell(has_more_waypoints: bool)` as route-executor-facing controller commands. `finish_dwell(...)` is the end-of-dwell state transition (see the Route-executor section for full semantics); both branches land in `STANDBY`, neither clears `received_path_plan`, and neither starts movement. Cab selection is preserved across route completion — `IDLE` is reserved for the no-cab-selected state only.
- Manual trains should also enter dwell through `enter_station_dwell()` on confirmed arrival. The difference from ATO is departure behavior only: `TrainController.depart()` in manual mode transitions into movement directly and does not call `TrainController.ato_go()`.
- `PathDispatcher` should call `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)` whenever a live path is cancelled or revoked. In v1 this notification is informational: after controller authority is cleared and the train has reached a fresh stopped no-authority state, `RouteExecutor` should check whether that cancelled path actually affects its current waypoint / assigned route state and then use ordinary reachability + path-request logic for recovery. If the no-authority stop escalated into EB, this recovery point is only after a manual `release_emergency_cmd()` has returned the controller to `STANDBY`.
- `RouteExecutor` should not set controller path or destination fields directly; controller authority should be derived entirely from `receive_path_plan(...)`.
- Replace `transition_to_standby()` and `arrive_at_station()` as completion hooks with `on_path_complete()` plus route-owned dwell-entry once completion teardown is dispatcher-owned and the controller reacts through callbacks only.
- `release_emergency_cmd()` should only clear EB when the train is already stopped, transition the controller to `STANDBY`, and latch `post_eb_restart_pending = True`.
- `TrainController.depart()` is also the fresh-restart command for retained-authority standby. If `received_path_plan` still exists after an EB stop, route/operator logic should reuse that authority and call `depart()` rather than synthesizing a new path. A successful new departure decision clears `post_eb_restart_pending`.

**route/dispatcher command contracts**

- `TrainController.receive_path_plan(path_plan)` is an atomic full replacement of the controller's active authority projection. `PathDispatcher` uses it both to push a brand-new path and to refresh the same `path_id` after extension; `None` clears the projection immediately. It is valid while the train is stopped or moving because it is a projection update, not a physics state transition.
- `TrainController.depart()` is the movement-start command for an existing authority/mode decision; it does not create dispatcher authority itself. A successful fresh departure decision clears `post_eb_restart_pending`. In ATO, calling `depart()` from `IDLE` with a live path may auto-select the required cab from the directed authority geometry in `received_path_plan.resource_sequence` before movement begins, except for the unsupported same-tile no-cab case that dispatcher rejects earlier. In route-driven movement, `depart()` without a live `received_path_plan` must not start ATO motion.
- `TrainController.on_path_complete()` is valid only after `PathDispatcher._complete_path_on_full_stop()` confirms a full stop at the authority end. It transitions the controller to post-path `STANDBY`, must not clear `received_path_plan`, and must not enter dwell itself.
- `TrainController.enter_station_dwell()` is valid only after confirmed stopped arrival: either dispatcher-confirmed path completion for a station stop or the explicit same-block no-movement already-arrived bypass. It transitions the controller to `RUNNING_AT_STATION` and must not create movement or mutate `received_path_plan`.
- `TrainController.finish_dwell(has_more_waypoints)` is valid only from `RUNNING_AT_STATION`. It transitions back to `STANDBY`, preserves cab selection, does not call `depart()`, and does not mutate `received_path_plan`.

**Read accessors**

- Add `get_lookahead()` as the canonical accessor for UI and debug consumers. Any consumer asking for target speed or speed-limit reason should sample this payload at distance `0`.
- Keep `get_path_tiles()` as the canonical read-only controller projection of authority geometry. It is derived from `received_path_plan.resource_sequence` and returns the ordered train-tail -> authority-end tile list for the active authority. In v1, each returned tile entry carries the canonical **path_tile entry schema** plus `resource_type` and `resource_id` copied from the owning `resource_sequence` entry. `TrainPhysics` and UI consumers may read this projection, but they must not read `Train.current_path`. `TrainController.get_path_tiles()` must not collapse to bare deduplicated coordinates, because the traversal frame is required to choose exits on ambiguous geometry and to interpret same-block / same-tile stop positions correctly.
- Add one shared neighbor/edge helper such as `RailwayNetwork.get_connection_edges(tile_a, tile_b) -> Optional[Tuple[str, str]]`, which answers two questions at once: are the two tiles neighbors, and if so, which edge on each tile connects to the other. `TrainPhysics` and traversal helpers should use this instead of re-deriving neighbor-edge pairs ad hoc.

**Manual mode and movement-authority boundary**

- In `simulation` submode, the driver should call `set_handle()`, the controller should store `handle_position`, and `TrainPhysics` should integrate it into speed while ATC can still override with braking.
- In `power_percent` submode, the driver should call `set_manual_power_percent()`, the controller should store `manual_power_percent`, and `TrainPhysics` should convert it through `SpeedProfile.power_speed_curve`.
- Remove `manual_target_speed` entirely; no hidden shim or compatibility field should remain.
- In manual mode with a received path plan, add `_calculate_authority_boundary_limit()` so the controller brakes to the current authority end at `authority_stop_position_cm`.
- Without a received path plan, the current-block boundary should remain the fallback authority limit.

**Completion ownership and timing**

- Make `PathDispatcher` the sole owner of path completion detection.
- Let `TrainController` react to dispatcher-owned completion through `on_path_complete()`, but stop letting the controller decide independently that a path-backed run is complete.
- Keep `PathDispatcher` as the sole writer of `TrainController.received_path_plan`; `on_path_complete()` is a reaction hook, not an inbox-mutating hook.
- Remove helper logic such as `path_backed_run_active` / `_is_standalone_path_complete()` once dispatcher-driven completion is authoritative.
- Tighten the invariant so path completion is emitted only after the train is fully stopped at the terminal stop point.

**Controller invariants**

- There should be no `self.target_speed` field once lookahead-at-distance-0 becomes the canonical source of target speed.
- Controller-owned live control state and train-owned physical/persistent state should be cleanly separated.
- Remove the runtime fallback read `self.ato_destination_block_id or getattr(train, "ato_destination_block_id", None)` from controller ATO logic; in the new model destination state comes exclusively from `received_path_plan`.
- `SimulationController.get_or_create_controller(train_id)` should preserve the 1:1 train/controller pairing without recreating authority from train-side runtime mirrors. Its job is only "make sure a controller object exists and is registered for this `train_id`." If authority already exists, `SimulationController` may notify `PathDispatcher` that the binding is now available, and `PathDispatcher` re-pushes the live plan from dispatcher state after the controller is available. `SimulationController` must not construct or write the plan payload itself.

## TrainPhysics

**Movement-authority input in `TrainPhysics`**

- `TrainPhysics` should continue to own physical integration (`current_speed`, position, occupancy, tail tracing), but it should not own path identity or dispatcher authority state.
- To choose exits on ambiguous geometry, `TrainPhysics` normally needs a controller-provided, read-only authority-geometry projection derived from `TrainController.received_path_plan.resource_sequence`. The minimum movement data is the ordered path tiles returned by `TrainController.get_path_tiles()`, plus enough local direction context to choose the correct next edge through the current tile.
- `TrainPhysics` does **not** need dispatcher ownership fields such as `path_id` to step the train, and it does not need requested/authorized stop-position fields for physical integration. Those stop-position fields belong to controller braking logic.
- `TrainPhysics` should obtain that geometry projection through `TrainController.get_path_tiles()`. `Train.current_path` should not remain a movement input in the desired model.
- Exception for cancellation/revocation braking: if `PathDispatcher` has already cleared `received_path_plan` to revoke authority while the train is still rolling, `TrainPhysics` must still keep moving the train according to speed and the current live network state. That post-revocation movement relies on the physical `Switch.state` / live track geometry that exists in the network at that moment, not on retained authority locks or a second copy of path ownership surviving in the controller inbox.
- Once live switch state ownership moves to `Switch`, `TrainPhysics._advance_end()` and tail tracing must call the updated `TrackSegment` traversal helpers using the resolver input defined by the `TrackSegment` helper contract above.

**Block occupancy**

- Route normal tail-vacated transitions through `PathDispatcher.on_resource_tail_vacated_from_physics()`, which routes block releases through `BlockManager.on_block_tail_vacated()` so `OCCUPIED -> CAUTION -> FREE` stays in one state machine.
- `TrainPhysics` must not emit a block tail-vacate callback just because the tail left one tile of that block. `PathDispatcher` may call `BlockManager.on_block_tail_vacated()` only after `TrainPhysics` knows the train no longer occupies **any** tile in that block.
- Replace direct `clear_block()` calls for normal CAUTION clearing with the cascade inside `on_block_tail_vacated()` via `_reevaluate_caution_blocks()`, not a separate `release_block()` step.

**Tail-vacate flow in `TrainPhysics`**

- Remove direct calls to `block_manager.set_block_to_caution()` and `block_manager.clear_block()` for normal vacate transitions.
- On tail-vacate, emit a typed resource-release event to `PathDispatcher.on_resource_tail_vacated_from_physics(train_id, resource_id, resource_type, direction)`. For blocks, `TrainPhysics` must emit the callback only after it knows the train no longer occupies any tile in that block; tile-level vacate is not enough. For switches, tile-level vacate of the switch resource is sufficient. `TrainPhysics` obtains `resource_id` from the same tile-lookup mechanism already used for entry detection: `track.block_section_id` for blocks, `track.switch_id` for switch tiles (both fields are on `TrackSegment` and available from `network.tracks.get(tile)`). `TrainPhysics` should derive `direction` from its own movement basis for that vacated traversal: the entry/exit edges and local movement vector it already used to move through the resource. `PathDispatcher` routes the release according to `resource_type`: blocks go through `BlockManager.on_block_tail_vacated(block_id, train_id, direction)` so `OCCUPIED → CAUTION → FREE` stays in one state machine; switches go through the `Switch` entity by clearing `Switch.occupied_by` and then performing any train-ID-based lock/flank teardown that still applies.
- `_caution_blocks` tracking in `TrainPhysics` becomes unnecessary once `BlockManager` handles the full release lifecycle internally; remove that legacy field and its associated maintenance logic in the desired model.

**Voltage behavior**

- Stop reading `controller.target_speed` and `train.manual_target_speed` once those standalone fields are removed.
- Keep the terminology explicit and simple: `track_voltage`, `power_percent`, and `speed_kmh` are three different values in the desired model.
- `TrainPhysics` should pass the current `track_voltage` value to `BlockPowerController` as the software-to-hardware output layer for block voltage emission.
- The active mode determines which value is authoritative. In ATO and manual `simulation` mode, `speed_kmh` is the controller/physics source of truth and `power_percent` / `track_voltage` are derived from that current operating point. In manual `power_percent` mode, `manual_power_percent` is the source of truth and `speed_kmh` / `track_voltage` are derived from that commanded power level.
- In `power_percent` manual mode, `TrainPhysics` should use the profile's voltage/power/speed calibration directly rather than reading removed scalar target-speed fields. The calibration must be usable in both directions so physics can convert between the authoritative value for the active mode and the derived outputs it needs to maintain. These mappings are owned by `SpeedProfile` and should be documented exactly this way:
  - `track_voltage <-> power_percent`
  - `power_percent <-> speed_kmh`
- When `TrainPhysics` needs `track_voltage`, it derives it from the current authoritative mode input: `speed_kmh -> power_percent -> track_voltage` in ATO and manual `simulation` mode, or `manual_power_percent -> track_voltage` in manual `power_percent` mode.
- Do not introduce extra names for these concepts in the desired model. Keep the doc wording in terms of voltage, power percent, and speed, and keep the mappings distinct.

**Physics invariants**

- `TrainPhysics` should not own manual-mode policy; it should execute controller commands and maintain physical state only.
- `TrainPhysics` no longer relies on a separate track-level path-lock field. The equivalent role is now played by `Block.reserved_by_path` / `Block.reserved_for_train` on the block side and `Switch.locks` on the switch side. Movement remains geometry-driven; switch integrity during traversal is enforced by the combination of `Switch.occupied_by` plus the still-held compatible `Switch.locks` set rather than by a global track-level path lock. This is not a transitional/migrated version of the old field — the mechanism is different, so no transitional phase is needed.

## SpeedProfile

`SpeedProfile` carries motion-rate data plus per-train calibration in the vocabulary the rest of the system should use directly: voltage, power percent, and speed. The calibration defines bidirectional mappings between `track_voltage` and `power_percent`, and between `power_percent` and steady running `speed_kmh`. Keep those mappings explicit in the doc instead of introducing extra terminology.

- Keep these fields on `SpeedProfile`:
  - `profile_id`: unique profile identifier.
  - `name`: display name for UI/debug/profile selection.
  - `startup_voltage`: minimum `track_voltage` that produces movement. Below this threshold the train remains stopped.
  - `max_voltage`: top of the usable `track_voltage` range for the first mapping stage.
  - `power_speed_curve`: stored calibration mapping `(power_percent, speed_kmh)` for the second stage.
  - `lighting_voltage`: voltage used for stopped lit states.
  - `acceleration_rate`: service acceleration rate in `km/h/s`.
  - `deceleration_rate`: service braking rate in `km/h/s`.
  - `emergency_brake_rate`: emergency-brake deceleration rate in `km/h/s`.
  - `friction_rate`: passive/coasting deceleration rate in `km/h/s`.
  - `operational_decel_ratio`: comfort-braking fraction used when ATO builds operational braking curves.
  - `air_resistance_enabled`: whether the drag model is active.
  - `drag_coefficient`: drag strength used by the drag model.
  - `reference_speed_kmh`: reference speed used to scale the drag model.
- Remove `speed_voltage_curve` and `min_start_voltage` from the desired model. They belong to the old one-stage speed-to-voltage design and must not remain in the target schema.

**SpeedProfile constraint**

- `SpeedProfile` is still not a speed limit object under the desired model. It expresses motion-rate data and per-train calibration (`startup_voltage`, `max_voltage`, `power_speed_curve`), not any form of speed ceiling. Train maximum speed and block maximum speed remain separate, independently enforced constraints.

**Speed-profile addition**

- Keep the desired-model calibration wording in three explicit values: `track_voltage`, `power_percent`, and `speed_kmh`.
- The calibration has two bidirectional mappings and both should be stated directly in the doc:
  1. `track_voltage <-> power_percent`
  2. `power_percent <-> speed_kmh`
- `startup_voltage` and `max_voltage` define the voltage range used by the first mapping. Below `startup_voltage`, the train does not move. `max_voltage` is the top of the usable range. In v1, `track_voltage <-> power_percent` is linear across that usable range, so reversing from `power_percent` back to `track_voltage` uses the same linear interpolation.
- `power_speed_curve` is the stored `(power_percent, speed_kmh)` mapping used by the second step. `power_percent` is the intermediate normalized control value between voltage and speed; it is not a second name for either of them. In v1 this may be linear by default, but it is still stored explicitly as profile calibration data rather than inferred ad hoc elsewhere. Reverse lookup from `speed_kmh` to `power_percent` uses the inverse interpolation of this same stored curve.
- Acceleration to reach the speed implied by that mapping is still governed by the physics model and motion-rate fields, not by `power_speed_curve` itself.
- Every train should have a default `SpeedProfile` carrying these calibration fields so the `power_percent` manual mode always has a defined voltage/power/speed mapping, even before per-train tuning is customized.

## Signals

**Signals**

- Signals remain display-only in the desired model. CAUTION handling moves to `TrainController.CAUTION_SPEED_LIMIT_KMH` (enforced via the lookahead / ATC path), not to signal aspects. `RailwayNetwork.update_signals_from_blocks()` still produces aspect values for the UI, but no controller or physics layer consumes them as a speed restriction.

## Power

**Power-controller role**

- Keep `BlockPowerController` focused on the software-to-hardware boundary only: it accepts a voltage number and applies it to block outputs.
- `TrainPhysics` computes `track_voltage` from the current authoritative operating input for the active mode: `speed_kmh -> power_percent -> track_voltage` in ATO and manual `simulation` mode, or `manual_power_percent -> track_voltage` in manual `power_percent` mode. `BlockPowerController` applies that already-computed value to the hardware-facing block outputs.
- Do not let it own manual-mode state or read `manual_target_speed`.

## Data Flow To UI

**`Train.to_dict()` payload**

- Export only physical and persistent train state.
- Remove `state`, `target_speed`, `speed_limit_reason`, `assigned_path_id`, `ato_destination_block_id`, `current_path`, `path_index`, ATO info, lookahead data, and other live control diagnostics from `Train.to_dict()`.
- Read live operational diagnostics from `TrainController.to_dict()` instead.

**`TrainController.to_dict()` payload**

- Export all live operational state, including `handle_position`, `manual_mode_type`, and `manual_power_percent`.
- Export ATO state using `ato_destination_block_id` derived from `received_path_plan.authority_end_block_id`.
- Export `received_path_plan` when present using the canonical **received_path_plan schema**. This is the sole controller/UI source of truth for the active path plan, and its nested authority geometry must continue to follow the canonical **resource_sequence entry schema** and **path_tile entry schema**.
- Do not make `TrainController.to_dict()` the owner of live path-progress state in v1. `PathDispatcher` remains the sole owner of `current_resource_index`; if UI later needs that progress projection, expose it separately as a dispatcher-owned read model rather than copying ownership into `TrainController`.
- Expose the full canonical lookahead payload as the single source of truth for current target speed, ATC limit, speed-limit reason, and forward curve rendering.

**UI data-flow anchor**

- Treat controller payload as the source of truth for live operational/manual state, and train payload as the source for physical/persistent state.
- A UI asking "what is the train's target speed right now?" should call `controller.get_lookahead()` and sample at distance `0`.

## End-To-End ATO Path Run

**End-to-end path-authority handoff**

- `PathDispatcher` should push the canonical **received_path_plan schema** via the controller path plan. Its nested authority geometry must follow the canonical **resource_sequence entry schema** and **path_tile entry schema**. `controller.assigned_path_id` should be only a derived read of that plan's `path_id`.
- `RouteExecutor` should ensure the train's `TrainController` exists before this path-authority handoff happens in Phase 1.
- `RouteExecutor` should not write `assigned_path_id` or `ato_destination_block_id` directly in the desired model.

**End-to-end ATO run**

1. Route is active and assigned to the train.
2. In Phase 1 setup, `RouteExecutor` resolves the next concrete waypoint target through the canonical `station -> platform -> block` chain (or directly from `block_id`), together with the route-owned `requested_destination_direction` constraint for serving that target.
3. If the train is already stopped within the waypoint stop window and the route/waypoint already defines the exact stop target for that resolved block target, `RouteExecutor` treats the waypoint as arrived and calls the same arrival/dwell entry path used after normal path completion.
4. If the train is in the target block but not yet at the stop point, `RouteExecutor` requests a synthetic same-block path with the route-defined `route_defined_stop_position_cm` when one exists, or `None` otherwise so dispatcher resolves the fallback.
5. Otherwise `RouteExecutor` calls `PathDispatcher.create_path()` normally, passing `requested_destination_block_id`, `train_id`, `requested_destination_direction`, `requested_stop_position_cm=route_defined_stop_position_cm` when the route plan defines an exact stop target for the resolved block target, choosing `acquisition_mode=route.acquisition_mode`, and letting `PathDispatcher` resolve a fallback safe stop target from the chosen terminal frame otherwise.
6. `PathDispatcher` establishes controller authority by pushing the canonical **received_path_plan schema**. Its nested authority geometry uses the canonical **resource_sequence entry schema** and **path_tile entry schema**, and `controller.assigned_path_id` is read back as a derived view of that plan's `path_id`. `RouteExecutor` does not write `assigned_path_id`.
7. If the route uses `best_effort` and the current live path is still short of its current requested target, `RouteExecutor` keeps retrying `PathDispatcher.extend_path()` on that same `path_id`, always passing the same route-owned request contract from `exec_state.resolved_target` (`requested_destination_block_id`, `requested_destination_direction`, and `route_defined_stop_position_cm`). The current authority stays live while `resource_sequence` grows in place; the controller may enter `RUNNING_WAITING` at the current authority boundary between successful extensions.
8. Each simulation tick runs in setup, execute, and feedback phases:
   - setup: route/path planning
   - execute: controller lookahead, ATC, and ATO/manual command generation, followed by physics movement
   - feedback: dispatcher resource progress, release, and completion checks driven directly by `TrainPhysics` resource-entry and tail-vacate reporting
9. On path completion, `PathDispatcher._complete_path_on_full_stop()` releases remaining already-vacated resources, calls `controller.on_path_complete()`, clears controller authority by calling `controller.receive_path_plan(None)`, and then calls `RouteExecutor.handle_path_completion()` directly with the completion context it needs (`train_id`, `path_id`, `authority_end_block_id`, `authority_stop_position_cm`, `end_tile`, `event_time`). Any block or switch still physically occupied by the train remains held until its later tail-vacate callback.
10. `RouteExecutor.handle_path_completion()` clears only route-owned bookkeeping and hands route-level arrival/dwell logic to `RouteExecutor._enter_waypoint_arrival(...)`, which enters dwell when the route plan says this completed stop is a station stop. It does not re-check the exact stop position from live train/controller state; dispatcher completion plus the passed completion context are authoritative.
11. Dwell continues through `RUNNING_AT_STATION` and `Waypoint.should_depart(time_at_platform_sec, current_time_sec, timing_mode)`.
12. When dwell completes, `RouteExecutor` advances the waypoint index and the cycle repeats.

**End-to-end manual-arrival note**

- Manual trains use the same dispatcher-confirmed arrival and `RouteExecutor._enter_waypoint_arrival(...)` dwell-entry path as ATO trains. The only difference is departure behavior: `TrainController.depart()` in manual mode transitions directly into movement and does not call `TrainController.ato_go()`.

**End-to-end retained-authority restart note**

- If an EB release leaves the train in `STANDBY` while the same `received_path_plan` still exists, the next fresh departure reuses that retained authority. `RouteExecutor` must not create a duplicate path in that case; it should issue `controller.depart()` only when the resolved departure policy allows it. `TrainController.post_eb_restart_pending` is the explicit gate that makes `Route.auto_depart_after_eb` apply; once the fresh departure decision is made, clear that flag.

## Logic Review Checklist

**Review checklist**

- Is the controller receiving the canonical **received_path_plan schema** when path-end stopping matters?
- Does each `resource_sequence` entry carry the ordered `path_tiles` slice using the canonical **path_tile entry schema**, with explicit `entry_edge` / `exit_edge` on both the resource entry and each nested tile entry, needed for controller lookahead and UI geometry after `Train.current_path` is removed?
- Is controller lookahead the only source of target speed and speed-limit reason?
- In manual mode, does ATC enforce the end of the assigned path as a hard stop through `_calculate_authority_boundary_limit()`?
- Can any path-complete event still fire while the train is braking or creeping toward the terminal stop point?
- Does `_ato_calculate_handle()` read only the current tick's canonical lookahead payload, not stale scalar fields?
- Does `RouteExecutor` read controller state rather than mirrored runtime state on `Train`?
- For same-block waypoints, does the route either treat the train as already arrived or create a synthetic same-block path rather than starting movement without dispatcher authority?
- For same-block / same-tile waypoint movement, does the request carry an explicit `requested_destination_direction`, and does dispatcher pathfinding reject ambiguous or wrong-direction candidates rather than guessing?
- Are repeated resources rejected during both `create_path()` and `extend_path()`?
- In `best_effort` mode, does the system keep one live `path_id`, extend `resource_sequence` in place, and avoid treating a truncated-but-live authority as a creation failure?
- Does `best_effort` creation fail instead of storing a live authority when the head is on a switch and no forward block can be acquired?
- Do dispatcher progress/completion/release callbacks all identify active authority by `train_id` rather than by caller-supplied `path_id`, with `PathDispatcher` resolving active progress from its own database rather than from physics or train mirrors, while delayed release of a still-occupied block or switch resolves from the resource itself after authority teardown?
- Are multi-train routes using per-train `RouteExecutionState` as the only canonical progress store, with any route-level `current_waypoint_index` / `direction` fields treated as compatibility summary only?
- Does the normal `CAUTION -> FREE` reevaluation require the forward neighbour (in `caution_direction`) to be non-occupied, while still allowing the explicit completion-time `force` exception in `release_path_block_reservations(..., completed=True)`? (`occupied_by` is always `None` in `CAUTION` — guaranteed by `on_block_tail_vacated()` clearing it — so a separate non-occupied check is vacuous. `reserved_by_path` is explicitly not a gating condition; if `_is_caution` has cleared but `reserved_by_path` remains set, the computed block state is `RESERVED`, not `FREE`.)
- Can any completion or teardown path still unlock a block or switch that the train physically occupies at full stop?
- After `release_emergency_cmd()`, does the system return to `STANDBY` and reuse retained authority without creating a duplicate path, while still honoring `departure_mode` and `auto_depart_after_eb`?
- Do flank-protection claims record `source_switch_id` so tail-vacate can release only the flank claims created from the vacated main switch?

**Notes**

- In the desired model, ordinary switch release happens when the train tail vacates the switch tile, not on full path completion. `TrainPhysics` reports the vacated resource to `PathDispatcher` as a typed resource-release event, and dispatcher releases it according to `resource_type`: blocks go through `BlockManager.on_block_tail_vacated(...)`, while switches clear `Switch.occupied_by`, remove that switch's own pending claims for the vacating train, and release only those neighbour flank claims whose stored `source_switch_id` matches the vacated main switch. Both completion and cancellation must respect physical occupancy: if a switch is still physically occupied, its lock stays in place until the later tail-vacate event.
- `assigned_path_id` moves from `Train` to `TrainController` as a read-only derived property of `received_path_plan.path_id`. `PathDispatcher` owns the underlying `received_path_plan`: it sets that plan when the active path is established, mirrors only the single active authority path referenced by `active_path_id_by_train[train_id]`, and clears it on path teardown by calling `controller.receive_path_plan(None)` before calling `RouteExecutor.handle_path_completion()`.
