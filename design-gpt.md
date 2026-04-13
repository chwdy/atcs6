# ATCS6 Code-Derived Design Notes

This document is derived from the current codebase, not from `DESIGN.md`.
Its purpose is to describe the actual entity responsibilities and runtime flow so
logic issues can be reviewed against one consistent picture.

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
- `route`: a higher-level travel plan made of platform waypoints.
- `block`: an occupancy and reservation unit.
- `track tile`: physical rail geometry on the grid.

## Design Direction From Review

> **Reading convention:** Unmarked text describes the **current implementation** — what the code does today. Lines beginning with `TODO:` describe the **desired change**.
>
> **How to assemble the new model:** Each `TODO:` block **replaces** the current-state block immediately above it. To understand the target design, mentally substitute every `TODO:` block for its corresponding current block; sections with no `TODO:` block are unchanged in both models. Reading current text and `TODO:` blocks as additive or layered produces a false picture — the `TODO:` block is the authoritative replacement, not a supplement. Contradictions should be evaluated against the assembled new model (current text with `TODO:` substitutions applied), not against the current text alone.

Conversation summary and current ownership split:

- `TrainController` currently owns most live operational state: ATC/ATO/manual mode, power, EB, handle position, `target_speed`, ATC diagnostics, lookahead, and ATO runtime fields.
- `Train` currently owns physical and persistent state, but it also mirrors some live control and UI-facing fields such as `target_speed`, `manual_target_speed`, `speed_limit_reason`, and ATO destination info.
- Manual control currently centers on handle commands plus train-side `manual_target_speed`; there is no separate controller-owned voltage submode.
- `TrainPhysics` and `BlockPowerController` currently read controller or train speed targets when converting control state into movement and voltage.

TODO: desired ownership split:
- `TrainController` should own live operational state: ATC/ATO/manual mode, handle position, future manual voltage-mode inputs, and the canonical live lookahead data series. "Target speed" and "speed-limit reason" should not be stored as standalone fields anywhere; they should be the value and reason metadata at position 0 of the lookahead series.
- `Train` should own persistent and physical state only: identity, consist, schedule/route/path assignment, position, and actual `current_speed`. Live ATO/ATC state should not be mirrored on `Train`.
- `manual_target_speed` should be removed entirely. With the new handle plus power-percentage manual model there is no per-train target speed to store, so no compatibility shim is needed.
- Manual control should have two controller-owned submodes:
  - `simulation`: user drives with handle notches; the handle command flows to `TrainPhysics`, which integrates it into speed change over time.
  - `voltage`: user drives with percentage power; the controller passes raw percentage power to `TrainPhysics`, which converts it via train/profile calibration data such as `SpeedProfile.power_speed_curve`.
- `TrainPhysics` and `BlockPowerController` should not decide manual-mode policy. They should execute whichever command the controller produces and perform command-to-speed conversion and voltage emission.
- Percentage-power mapping data should live with train calibration, most naturally `SpeedProfile.power_speed_curve` or an equivalent train-attached profile, and be read by physics.

## Layered Architecture

Each layer acts as an **executor** for the layer above and a **planner** for the layer below.

| Layer | Executes plan from | Plans for |
| --- | --- | --- |
| `RouteExecutor` | Operator / schedule | `PathDispatcher` |
| `PathDispatcher` | `RouteExecutor` | `TrainController` |
| `TrainController` | `PathDispatcher` | `TrainPhysics` |
| `TrainPhysics` | `TrainController` | Track / network |

Current responsibilities:

- `RouteExecutor` decides which platform waypoint is next and requests a path to it. It does not know about blocks or switches.
- `PathDispatcher` reserves path resources, locks switches, manages movement authority, and receives progress/completion feedback from physics.
- `TrainController` calculates ATC safety limits, ATO/manual commands, and the active handle selection for the current train state.
- `TrainPhysics` executes the controller command by integrating acceleration, advancing tile position, updating block occupancy, and updating voltage output.

Current information flow:

- **Downward (planning):** route waypoint -> dispatcher path/resources -> controller handle -> physics acceleration.
- **Upward (feedback):** physics block transitions -> dispatcher path progress/completion -> route waypoint advancement.
- The controller currently pulls some path-derived data from the dispatcher on demand while a path is active.

TODO: desired layered responsibilities:
- `PathDispatcher` should execute the route plan by acquiring blocks, locking switches, and managing movement authority, and it should plan for the controller by providing path tiles, speed limits, and the exact terminal stop position.
- `TrainController` should execute the path plan by calculating ATC safety limits, ATO/manual commands, and an active handle or future power command. It should plan for physics by producing the active control command for the current mode.
- `TrainPhysics` should remain an executor only; it should not decide manual-mode policy.
- `PathDispatcher` should pass `stop_position_cm` to the controller when a path is created so the controller can brake to the correct stop target before the path completes.
- The path should become the single source of truth for controller authority while it is active. Cancelling or replacing a path in `PathDispatcher` should be sufficient to give the controller a clean slate.

## Core Runtime Loop

`SimulationController.update(delta_seconds)` is the top-level runtime step:

1. Advance `simulation_time`.
2. Run `RouteExecutor.update(delta_seconds)`.
3. For each train, call `_update_train(train, delta_seconds)`.
4. Update signals from blocks.
5. Consume dispatcher completion events and hand them to the route executor.

TODO: desired phase structure:
### Phase 1 - Setup (top-down planning)

1. Advance `simulation_time`.
2. Run `RouteExecutor.update(delta_seconds)`. This decides which waypoint/path is active for each train and calls `PathDispatcher.create_path()` when a new path is needed.
3. After path creation, `PathDispatcher` should call `controller.receive_path_plan(path_plan)` directly on the assigned train's `TrainController`. The plan should include path tiles, block speed limits, and `stop_position_cm`.

### Phase 2 - Execute (downward)

1. For each train, call `_update_train(train, delta_seconds)`:
   - Tick `TrainController`.
   - Tick `TrainPhysics`.

### Phase 3 - Feedback (bottom-up)

1. For each train, pass physics resource-entry transitions to `PathDispatcher`.
2. If `on_train_progress_from_physics()` detects a completed path, `PathDispatcher._complete_path_on_full_stop()` should release remaining resources, call `controller.on_path_complete()`, and call `route_executor.handle_path_completion()` directly.
3. Update signals from blocks after all dispatcher feedback has been applied.

TODO: dispatcher feedback should move out of `_update_train()` and into a separate pass after all trains have ticked physics so path state for all trains updates consistently in the same phase.

Concrete calls:

- `self.route_executor.update(delta_seconds)`.
- `self._update_train(train, delta_seconds)` for each `train` in `network.trains.values()`.
- `self.network.update_signals_from_blocks()`.
- `self.path_dispatcher.get_and_clear_completion_events()`.
- For each completion event with `train_id`, `path_id`, and `end_tile`, call `self.route_executor.handle_path_completion(train_id, path_id, _end_tile=end_tile, _event_time=event.get("time"))`.

TODO: desired `SimulationController.update()` calls:
- `self.route_executor.update(delta_seconds)` remains the setup entry point.
- `self._update_train(train, delta_seconds)` remains the per-train execute step.
- Add a separate feedback pass after all trains have run physics, for example `self._run_dispatcher_feedback(train)` for each train with an assigned path.
- `self.network.update_signals_from_blocks()` should run after dispatcher feedback so signals reflect all block-state changes, including path completion.
- Remove `self.path_dispatcher.get_and_clear_completion_events()` and the loop that calls `route_executor.handle_path_completion()`. Completion should be synchronous inside `PathDispatcher._complete_path_on_full_stop()`.

Consuming dispatcher completion events means:

- `PathDispatcher._complete_path_on_head_arrival(path, train)` calls `_record_completion_event(path, train.position.head_tile)` when it completes a path.
- `_record_completion_event()` appends a dict to `PathDispatcher.completed_path_events` with `train_id`, `path_id`, `end_tile`, and `time`.
- `SimulationController.update()` later calls `get_and_clear_completion_events()`, which returns that queued list and clears the queue.
- Each returned event is handed to `RouteExecutor.handle_path_completion(...)`.
- `RouteExecutor.handle_path_completion()` only handles the event if the train still exists and `train.assigned_path_id == path_id`.
- When it handles the event, `RouteExecutor.handle_path_completion()` clears the train's path fields, clears route waiting state, optionally checks waypoint arrival, clears the controller's ATO path/destination fields, and calls `controller.transition_to_standby()`.
- Current dispatcher completion code also clears the train path fields and may transition the shared controller before the event is consumed, so the route-executor event handler can return `False` if `train.assigned_path_id` has already been cleared.

TODO: desired path completion notification flow:
- Rename `_complete_path_on_head_arrival()` to `_complete_path_on_full_stop()`.
- `PathDispatcher._complete_path_on_full_stop(path, train)` should call `controller.on_path_complete()` and then call `route_executor.handle_path_completion(train_id, path_id, end_tile, event_time)` directly.
- `PathDispatcher` should hold a reference to `RouteExecutor`, injected at construction or passed in.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()` from `PathDispatcher`.
- Path completion should only be recorded after the train is fully stopped at the terminal stop point. No completion event should fire while the train is still braking or creeping inside the destination block.
- After clearing path fields, route-level waypoint and dwell logic should decide whether the train upgrades into `RUNNING_AT_STATION` or remains in `STANDBY`.

For each train, `_update_train()` does this:

1. Get or create a `TrainController`.
2. Get or create a `TrainPhysics`.
3. Sync train OFF/non-OFF into controller `power_on`.
4. Initialize controller state from train state once.
5. Tick controller first.
6. Tick physics second.
7. Pass physics block-entry transitions to `PathDispatcher`.
8. Ask dispatcher to process path progress/completion.
9. Sync controller state and speed-limit diagnostics back to the train.

TODO: desired `_update_train()` flow:
1. Get or create a `TrainController`.
2. Get or create a `TrainPhysics`.
3. Sync train OFF/non-OFF into controller `power_on`.
4. Initialize controller state from train state once.
5. Tick controller.
6. Tick physics.
7. Sync only the train-owned physical or persistent state back to `Train`.
8. Move dispatcher feedback out into a separate Phase 3 pass after all trains have run physics.

Concrete calls in `_update_train()`:

- `_get_or_create_controller(train)`.
- `_get_or_create_physics(train)`.
- `controller.tick(delta_seconds)`.
- `physics.tick(delta_seconds)`.
- For each block in `physics.last_block_transitions["entered"]`, call `path_dispatcher.on_train_advances_from_physics(train.assigned_path_id, block_id)`.
- Then call `path_dispatcher.on_train_progress_from_physics(train.assigned_path_id)`.
- Finally assign `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.

TODO: desired `_update_train()` calls:
- Keep `_get_or_create_controller(train)`, `_get_or_create_physics(train)`, `controller.tick(delta_seconds)`, and `physics.tick(delta_seconds)`.
- Stop writing `train.target_speed`, `train.speed_limit_reason`, and `train.ato_destination_block_id` here. Consumers that want live target-speed data should read controller lookahead at distance 0 instead.
- Replace block-only feedback with resource-entry feedback after all trains have ticked physics, so dispatcher updates can consume both blocks and switches in `resource_sequence` order.

Asking the dispatcher to process path progress/completion means:

- `on_train_advances_from_physics(path_id, entered_block_id)` updates `path.current_block_index` when the train enters a block in `path.block_sequence`.
- If the entered block is the final block, `on_train_advances_from_physics()` sets `path.status = PathStatus.CLEARING`.
- `on_train_progress_from_physics(path_id)` checks the active path and current train position.
- If the path is `CLEARING`, the train is stopped, and the head is in the destination block, it calls `_complete_path_on_head_arrival(path, train)`.
- If the train head tile equals the last path tile, it also calls `_complete_path_on_head_arrival(path, train)`.
- Otherwise it marks already-cleared earlier blocks in `path.blocks_cleared` and releases switches for those blocks with `_release_switches_for_block(path, block_id)` when safe.

TODO: desired dispatcher progress/completion flow:
- `on_train_advances_from_physics(path_id, resource_id)` should update `path.current_resource_index` when the train enters a resource in `path.resource_sequence`.
- Entering the final block should move the path into `CLEARING`, but it should not complete the path yet.
- `on_train_progress_from_physics(path_id)` should complete the path only after the train is fully stopped at the terminal stop point.
- Switch release should be driven by tail-vacate detection in `TrainPhysics`, not by dispatcher tile-index checks.

Syncing controller state and speed-limit diagnostics back to the train means:

- `TrainController.tick(delta_seconds)` calculates controller-owned runtime values such as `state`, `target_speed`, `current_block_speed_limit`, `clearance_ahead_cm`, `atc_speed_limit`, and `speed_limit_reason`.
- ATC diagnostics are produced inside `TrainController.tick()` through `_calculate_lookahead()`, `_get_current_block_speed_limit()`, `_calculate_clearance_ahead()`, and `_calculate_atc_speed_limit()`.
- `_calculate_atc_speed_limit()` can set reasons such as braking/clearance distance, yellow signal, or block speed limit.
- `SimulationController._update_train()` then copies the UI-facing subset back to the train with `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.
- `TrainController.tick()` also writes `train.target_speed` and `train.speed_limit_reason` directly near the end of the tick, but the simulation sync repeats it so the `Train` object matches the shared controller.

TODO: desired controller/train sync:
- `TrainController.tick(delta_seconds)` should continue to own ATC, ATO, and manual-control runtime calculations, but target speed and speed-limit reason should move into the canonical controller lookahead payload instead of standalone scalars.
- `Train` should keep only physical and persistent state in sync from the simulation loop.
- `PathDispatcher.create_path()` should push a path plan directly into the controller, and path cancellation or completion should clear that controller-owned authority through controller methods rather than train-side mirrors.

Current tick-order dependency:
- The existing update order already depends on route work happening before per-train controller and physics work, and on dispatcher completion feedback happening after physics has produced new position data.

TODO: desired tick-order dependency:
- Phase 1 must run before Phase 2 because the controller should read path tiles that already exist.
- Phase 2 must run before Phase 3 because the dispatcher should read positions that physics has just produced.
- The one-tick lag between a physics action and the route-level reaction remains acceptable so long as ATO safety margins absorb it.

## Units

Current code uses mixed physical units:

- Train speed: `km/h`.
- Speed profile acceleration/deceleration/friction: `km/h/s`.
- Train dataclass `acceleration` and `max_braking_rate`: documented in-code as `m/s^2`, mostly used by older train methods.
- Train position distance: `cm`.
- Tile, block, platform length: `cm`.
- Runtime delta: seconds.
- `kmh_to_cm_per_sec()` and `get_speed_conversion_factor()` apply model scale when converting speed to distance.

Review anchor:

- Logic that mixes train methods using `max_braking_rate` with controller/physics methods using `SpeedProfile.deceleration_rate` should be checked carefully because the units differ.

## TrackSegment

`TrackSegment` stores physical rail geometry:

- `track_bits`: 6-bit geometry for NS, EW, NE, NW, SE, SW.
- `dead_end_ports`: derived/maintained buffer-stop edges.
- `_switch_state`: 0 for main, 1 for diverge.
- `block_section_id`, `signal_ids`, `switch_id`: cross-links to higher-level infrastructure.
- `locked_by_path`, `locked_state`, `path_direction`: dispatcher path locks.

Derived geometry:

- `edges`: all physical edge ports plus dead-end ports.
- `switch_points`: entry edges with at least two possible exits.
- `is_switch`: true when `switch_points` exists.
- `track_type`, `connections`, `is_valid`: derived from `track_bits`.

Traversal:

- `get_exits(entry_edge, use_switch_state=True)` returns exit edge strings.
- Non-switch track returns all geometric exits.
- Switch track filters exits by active switch state.
- If a train enters a switch from an edge that is not part of the active segment, exits are empty.
- `get_entry_for_exit()` searches the inverse of `get_exits()` and is used for backward/tail tracing.

Path locking:

- `lock_for_path(path_id, state)` only works on switch tiles.
- Locking sets switch state and records the owning path.
- A switch already locked by another path cannot be locked.
- A switch already locked by the same path in the same state is idempotent.
- `unlock_for_path(path_id)` only clears locks owned by that path.

TODO: desired path locking:
- Remove `TrackSegment.locked_by_path`, `locked_state`, and the single-owner lock methods once switch locking is owned entirely by the `Switch` entity.
- Keep `path_direction` on `TrackSegment`; it is routing geometry, not lock state.
- Replace the single-owner track lock model with the multi-owner `Switch.locks` model described below.

Review anchors:

- Switch state is global per tile, not per entry edge.
- Diverge-only switches use bit-order selection for state 0/state 1.
- Tail tracing depends on `get_entry_for_exit()` and current switch state. If a switch changes while a train spans it, tail reconstruction can be affected unless the path lock prevents that.

## Switch Entity

`Switch` is metadata for a switch tile:

- Stores switch ID, tile position, main/diverge length and radius, type, name, notes.
- Has its own `locked_by_path` and `locked_state`.
- Reads current physical switch state from the network track tile.
- `request_state_change(new_state, network)` rejects changes when locked and delegates the actual state to the track tile.

Review anchors:

- There are lock fields on both `TrackSegment` and `Switch`. Dispatcher code primarily locks the `TrackSegment`. Treat track lock as the operational lock unless code is changed to synchronize both.

TODO: desired switch entity:
- `Switch` should become a first-class resource entity at the same level as a block.
- Add `switch_id: str` as a unique identifier with the same role as a block ID.
- Replace single-owner `locked_by_path` / `locked_state` fields with `locks: Dict[str, int]`, mapping `path_id -> locked_state`.
- `acquire_lock(path_id, state)` should add a lock entry if the switch is free or all existing entries have the same state, and reject conflicting states.
- `release_lock(path_id)` should remove only that path's entry; the switch should be free when the dict is empty.
- Compatible locks from multiple paths holding the same state should be explicitly valid.
- Add `flank_protection: List[Tuple[str, int]]` on the switch entity and reserve `length_cm: float = 0.0` for future physical switch length.
- Migrate call sites from `track.lock_for_path()` / `track.unlock_for_path()` and `track.is_locked_by_path()` to switch-entity lock methods.

TODO: desired switch review anchors:
- `is_locked()` and `can_lock(state)` should check the `locks` dict, not a single field.
- Once `Switch.locks` is the sole authority, `TrackSegment` lock fields should be removed rather than "kept in sync".

## Flank Protection Computation

Flank protection is currently carried as path-specific protection data, such as `flank_protection_switches` on `Path`, and acquired by `PathDispatcher` during path creation.

TODO: desired flank protection computation:
- Compute flank-protection relationships at the network level and store them on each switch entity instead of on individual paths.
- Use a global setting such as `FLANK_PROTECTION_DISTANCE_CM = 5` when searching for nearby switches that require mutual locking.
- At switch placement time, search the network graph outward from the switch tile, determine which neighbour states would create a flank conflict, and append `(neighbour_switch_id, blocking_state)` to `flank_protection`.
- Recompute nearby flank-protection relationships whenever topology changes.
- Compatible same-state locks from parallel paths should remain valid even when flank protection is in play.

## Block

`Block` is the stored block state:

- `state`: `FREE`, `RESERVED`, `OCCUPIED`, or `CAUTION`.
- `occupied_by`: train occupying or assigned to the block.
- `reserved_by_path`: path owning a reservation.
- `reserved_direction`: N/E/S/W path direction through the block.
- `disabled`: block cannot be used.
- `length_cm`, `min_radius_cm`, `max_speed_kmh`: physical and speed constraints.

Block methods:

- `can_enter(train_id)` accepts `FREE`, or `RESERVED` by the same train via `occupied_by`.
- `occupy(train_id)` calls `can_enter()` and sets `OCCUPIED`.
- `set_to_caution()` only transitions `OCCUPIED` to `CAUTION` and clears `occupied_by`.
- `clear()` only transitions `CAUTION` to `FREE`.
- `reserve_for_path(path_id, train_id, direction)` reserves a free block, reuses same-path reservations, or attaches a path to a block occupied by the same train.
- `release_path_reservation()` only releases `RESERVED` blocks owned by a path.
- `is_available_for_path(path_id, train_id)` accepts free, same-path reserved, or same-train occupied blocks.

Review anchors:

- `Block.reserve_for_path()` stores `train_id` in `occupied_by` even while state is `RESERVED`.
- `Block.set_to_caution()` clears `occupied_by`, but `BlockManager.set_block_to_caution()` keeps it. These two APIs do not preserve identical data.

TODO: desired block model:
- Treat `reserved_by_path` as the dispatcher's resource lock and `occupied_by` as physical train presence only.
- In `RESERVED`, `occupied_by` should be `None`; in `CAUTION`, `occupied_by` should also be `None` because the train has already left.
- A block should be reservable before a train arrives, remain reserved while the train is inside, and stay reserved through `CAUTION` until the path releases it back to `FREE`.
- Because acquisition decisions need to check all blocks at once, `BlockManager` should be the sole mutator and `Block` should remain a data container.

TODO: clarify `CAUTION` vs reservation independence:

- `CAUTION` is an independent safety state indicating a physically adjacent block is occupied; it is orthogonal to path reservation.
- A block can be simultaneously `CAUTION` and `reserved_by_path` (path still holds it for a future or in-progress train), or `CAUTION` with no reservation (train has passed and path is done).
- The `OCCUPIED → CAUTION → FREE` lifecycle is driven purely by physical train movement through `BlockManager`; it does not depend on whether a path reservation exists.
- Path reservation (`reserved_by_path`) is released by the dispatcher on path completion or cancellation, as a separate operation from the occupancy state machine.
- Correct line 350 above: a block in `CAUTION` is NOT necessarily still path-reserved; reservation and occupancy state are two independent dimensions.

TODO: desired block methods:

- `can_enter(path_id)` should accept `FREE`, or `RESERVED` where `reserved_by_path == path_id`. A train never moves between blocks without an active path, so `path_id` is the natural entry credential and removes the `train_id` lookup indirection.
- Remove `Block.set_to_caution()`. In the new design all `OCCUPIED → CAUTION` transitions are owned by `BlockManager`; callers use `block_manager.release_block()`. A `Block`-level method that changes occupancy state bypasses the manager's single-serialization guarantee.
- `reserve_for_path(path_id, direction)` should drop the `train_id` parameter and stop storing `train_id` in `occupied_by` during `RESERVED`.
- `release_path_reservation()` should remain a `RESERVED`-only release.

TODO: desired block review anchors:
- Remove the current `occupied_by`-while-`RESERVED` behavior from `Block.reserve_for_path()`.
- Remove `Block.set_to_caution()` entirely; the `OCCUPIED → CAUTION` transition in the new design always goes through `BlockManager.release_block()`.
- Update `BlockManager.set_block_to_caution()` to clear `occupied_by` on `OCCUPIED → CAUTION` to match the new ownership model (train presence is physical, not reservation-tied).

## BlockManager

`BlockManager` is intended as the operational block-state authority.

Key transitions:

- `occupy_block(block_id, train_id)`:
  - `FREE -> OCCUPIED`.
  - `RESERVED -> OCCUPIED`, unless reserved for a different train.
  - `OCCUPIED -> OCCUPIED` is idempotent for the same train.
  - `CAUTION` and other-train `OCCUPIED` are rejected.

- `set_block_to_caution(block_id)`:
  - `OCCUPIED -> CAUTION`.
  - `CAUTION -> CAUTION` is idempotent.
  - Keeps `occupied_by` for tracking.

- `clear_block(block_id)`:
  - `CAUTION -> FREE`.
  - `OCCUPIED -> FREE` as manual override/cleanup.
  - `RESERVED -> FREE` for cancellation.
  - `FREE -> FREE` idempotent.
  - Clears `occupied_by`, `reserved_by_path`, and `reserved_direction`.

- `release_block(block_id, train_id=None)`:
  - Validates train ownership when `train_id` is supplied.
  - Advances exactly one state based on current state:
  - `OCCUPIED -> CAUTION`.
  - `CAUTION -> FREE`.
  - `RESERVED -> FREE`.
  - `FREE -> FREE`.

Review anchors:

- The manager has both staged release (`release_block`) and direct clear (`clear_block`). Callers must choose intentionally.
- `clear_block()` can directly clear `OCCUPIED`, so it should be treated as an override path, not a normal train-vacated path.

TODO: desired block-manager model:
- `BlockManager` should become the database-style lock manager for block resources and the single serialization point for all-or-nothing acquisition.
- Keep `occupy_blocks(block_ids, train_id)` as the existing atomic occupy helper.
- Add `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` to replace one-by-one block reservation with atomic reservation.
- Add `release_path_blocks(block_ids, path_id)` to replace scattered direct `block.release()` calls during completion and cancellation.
- Update `set_block_to_caution()` to clear `occupied_by` so it matches the block ownership model.
- Treat `release_block()` as the intended entry point for normal train-vacated and path-cancellation transitions, and keep `clear_block()` as an override or cleanup path.
- `BlockManager` should continue to hold a reference to `Network.blocks` rather than maintaining a separate block registry.

TODO: desired `release_block()` behaviour (CAUTION neighbour-free logic):

- When `TrainPhysics` detects a tail-vacate, it calls `block_manager.release_block(block_id, train_id)` instead of `set_block_to_caution()` directly.
- `release_block()` sets the vacated block to `CAUTION`.
- After setting the block to `CAUTION`, `BlockManager` checks all blocks that are currently in `CAUTION` state and advances each one to `FREE` if it is both non-reserved (`reserved_by_path` is `None`) and non-occupied (`occupied_by` is `None`).
- This means the `CAUTION → FREE` transition is driven by the manager on each `release_block()` call, not by a separate periodic scan or direct `clear_block()` call from physics.

## Path And PathDispatcher

`Path` is a reserved dispatcher movement authority:

- `path_tiles`: ordered track tiles.
- `block_sequence`: ordered block IDs.
- `switch_positions`: required switch state by tile.
- `train_id`: train assigned to the path.
- `current_block_index`: head's block index in `block_sequence`.
- `blocks_cleared`: blocks fully behind the train.
- `flank_protection_switches`, `overlap_blocks`, `approach_locked_blocks`: protection data.

TODO: desired path model:
- Replace `block_sequence` and `switch_positions` with `resource_sequence`, an ordered list of block IDs and switch IDs interleaved in traversal order.
- Replace `current_block_index` with `current_resource_index`.
- Replace `blocks_cleared` with `resources_cleared`.
- Remove `flank_protection_switches` from `Path`; flank protection should live on the switch entity and be acquired automatically when the switch is locked.

`PathDispatcher.create_path()` flow:

1. `find_paths(start_block_id, end_block_id, train_id)` enumerates candidate `PathSegment`s.
2. Generate a path ID.
3. Validate each candidate.
4. Acquire path resources for the first valid candidate.
5. Extend path tiles through the destination block.
6. Create `Path(status=SET, train_id=train_id, ...)`.
7. Store it in `active_paths`.
8. Copy `block_sequence` to `train.reserved_blocks` when a train is supplied.

TODO: desired `create_path()` flow:
1. Keep the same high-level candidate-search and validation flow.
2. Build `resource_sequence` in traversal order so both blocks and switches are tracked in one sequence.
3. After storing the path in `active_paths`, copy block IDs from `resource_sequence` to `train.reserved_blocks` when needed.
4. Push a path plan to the controller after successful creation so the controller receives path tiles, per-block speed limits, and `stop_position_cm`.

Resource acquisition:

- Reserve every block with `Block.reserve_for_path(path_id, train_id, direction)`.
- Set `track.path_direction` for path tiles.
- Lock required switches by calling `track.lock_for_path(path_id, state)`.
- Lock flank-protection switches when available.
- If any acquisition fails, reserved blocks, path directions, and switch locks are rolled back.

TODO: desired resource acquisition:
- Acquire switches first, then blocks, so switch conflicts fail before any blocks are reserved.
- For each switch in path order, call `switch.acquire_lock(path_id, required_state)` and immediately acquire any `switch.flank_protection` locks.
- Acquire blocks atomically through `BlockManager.reserve_blocks_for_path(...)`.
- On failure, roll back in reverse acquisition order.
- Replace direct `block.release()` calls in completion and cancellation with `BlockManager.release_path_blocks(...)`.
- If partial-path handling is kept, make its behavior explicit through a single failure-mode setting instead of scattering policy across acquisition code.

Physics-driven progress:

- `TrainPhysics` detects newly entered blocks.
- `SimulationController` calls `PathDispatcher.on_train_advances_from_physics(path_id, block_id)`.
- Dispatcher updates `current_block_index` to the entered block.
- Entering the final block changes path status to `CLEARING`.
- `on_train_progress_from_physics(path_id)` can complete the path when:
  - path is `CLEARING`, train is stopped, and head is in the destination block, or
  - head tile equals the last path tile.
- Completion is performed by `_complete_path_on_head_arrival(path, train)`.
- Completed paths release non-occupied resources, clear path directions, unlock switches, call `_record_completion_event(path, train.position.head_tile)`, clear train path fields, update the shared controller when available, and remove the path from `active_paths`.
- `get_and_clear_completion_events()` is the handoff point from dispatcher to `SimulationController`.

Review anchors (current model only — all of these are superseded by the TODO block below):

- The head-tile-equals-last-path-tile condition (second completion trigger above) does **not** require the train to be stopped. It is replaced by `_complete_path_on_full_stop`, which requires a full stop before completing.
- `_complete_path_on_head_arrival` accesses the controller indirectly via `network.power_controller._sim_controller.get_controller()` — a circular reference through `BlockPowerController`. This entire chain is TODO to remove once `_complete_path_on_full_stop` calls `controller.on_path_complete()` directly.
- `train.ato_destination_block_id` is cleared inside `_complete_path_on_head_arrival` directly. In the new model this field is removed from `Train`; destination state lives in the controller path plan.
- `_complete_path_on_head_arrival` itself is TODO to remove; it is replaced by `_complete_path_on_full_stop` in the new model.

TODO: desired physics-driven progress:
- `TrainPhysics` should detect newly entered blocks and tail-vacated switches.
- `SimulationController` should pass resource-entry transitions to `PathDispatcher.on_train_advances_from_physics(path_id, resource_id)`.
- Dispatcher progress should update `current_resource_index`.
- Entering the final block should move the path into `CLEARING`, but it should not complete the path.
- Completion should be performed by `_complete_path_on_full_stop(path, train)` only after the train is fully stopped at the terminal stop point.
- Completed paths should release non-occupied resources, clear path directions, unlock switches, remove the path from `active_paths`, call `controller.on_path_complete()`, and then call `route_executor.handle_path_completion(...)` directly.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()`.
- When creating a path, include `stop_position_cm` in the controller path plan so the controller brakes to the exact stop point before the path is released.
- Drive switch release from tail-vacate detection in `TrainPhysics`, not from `_release_switches_for_block()`.
- Route any normal block release through `block_manager.release_block()` so `OCCUPIED -> CAUTION -> FREE` remains a single-manager state machine.

Path extension:

- `extend_path(path_id, new_end_block_id)` only extends forward.
- Rejects extension to a block already in the path.
- Finds extension paths from current end to the new block.
- Validates and acquires the extension resources.
- Appends new tiles and new blocks, skipping duplicated connection/start entries.

TODO: desired path extension:
- Keep forward-only extension.
- When the path model switches to `resource_sequence`, append new tiles and new resources while skipping duplicated connection/start entries.

Cancellation:

- `cancel_path(path_id, force=False)` marks status `CANCELLED`.
- Non-approach-locked blocks are cleared unless physically occupied by the train.
- Switches are released unless physically under the train or in an approach-locked block.
- Path directions are cleared only if all approach locks are released or `force=True`.
- Path remains in `active_paths` if approach-locked resources remain.

TODO: desired cancellation:
- Keep the same high-level cancellation rules, but release owned blocks through `BlockManager.release_path_blocks(...)` instead of direct `block.release()` calls.
- Clear controller authority by calling `controller.receive_path_plan(None)` when a live path is cancelled.

Review anchors:

- A newly created path is `SET`, but normal physics progress can update it even if it never transitions to `ACTIVE`.
- Some resource release paths call `clear_block()` directly, so check whether those cases can encounter physically occupied blocks.
- `current_block_index` uses the first index of the entered block in `block_sequence`; repeated block IDs in a complex path would need careful review.

TODO: desired path review anchors:
- `resource_sequence` replaces both `block_sequence` and `switch_positions`, so existing callers iterating those fields need updating.
- `current_resource_index` uses the first index of the entered resource in `resource_sequence`; repeated resource IDs in a complex path need careful review.
- Switch zero-length means approach-lock, overlap, and braking-distance calculations remain unchanged; switches in `resource_sequence` simply contribute 0 cm.
- `FLANK_PROTECTION_DISTANCE_CM` should be a global config, not a per-path setting.
- Multi-lock switches must allow the same state to be acquired by more than one path.
- `PathDispatcher._release_blocks_behind_train()` is effectively dead production code if block release stays physics-driven.

## Route And RouteExecutor

`Route` is a high-level waypoint plan:

- `waypoints`: ordered platform waypoints.
- `mode`: `ONE_OFF`, `CIRCULAR`, or `ROUND_TRIP`.
- `assigned_train_ids`: trains using the route.
- Runtime fields: `current_waypoint_index`, `direction`.

`Waypoint`:

- Targets a platform.
- Has dwell/timing rules.
- Can allow flexible platform selection.
- Can constrain platform side and travel direction policy.

`RouteExecutor.update()`:

1. Collect active route/train pairs.
2. Create or retrieve a per-train `RouteExecutionState`.
3. Resolve target platform for the current waypoint.
4. If train is `STANDBY` or `IDLE`, request path and depart.
5. If train is `RUNNING_MOVING`, check waypoint arrival.
6. If train is `RUNNING_AT_STATION`, check dwell completion.
7. If train is `RUNNING_WAITING` and waiting for path, retry path request.
8. Manual trains only get arrival checks.

Concrete `RouteExecutor.update(delta_time)` call chain:

- `update(delta_time)` increments `RouteExecutor.simulation_time`.
- `update(delta_time)` calls `_get_active_route_train_pairs()`.
- `_get_active_route_train_pairs()` scans active `network.routes`, route `assigned_train_ids`, and the backward-compatible `train.assigned_route_id` fallback.
- For each `(train, route)` pair, `update(delta_time)` calls `_execute_route_for_train(train, route)`.
- `_execute_route_for_train()` creates or reuses `RouteExecutionState`.
- `_execute_route_for_train()` calls `route.get_waypoint_at(exec_state.current_waypoint_index)`.
- If no waypoint exists, `_execute_route_for_train()` calls `_handle_route_complete_for_train(train, route, exec_state)`.
- `_execute_route_for_train()` calls `_resolve_target_platform(train, waypoint, exec_state)` and stores the result in `exec_state.resolved_platform_id`.
- If the train state is `STANDBY` or `IDLE`, `_execute_route_for_train()` calls `_try_depart_to_waypoint(train, route, exec_state)`.
- If the train state is `RUNNING_MOVING`, `_execute_route_for_train()` calls `_check_waypoint_arrival(train, route, exec_state)`.
- If the train state is `RUNNING_AT_STATION`, `_execute_route_for_train()` calls `_check_dwell_complete(train, route, exec_state)`.
- If the train state is `RUNNING_WAITING` and `exec_state.waiting_for_path` is true, `_execute_route_for_train()` calls `_try_depart_to_waypoint(train, route, exec_state)` again.
- If the train state is `MANUAL`, `_execute_route_for_train()` calls `_check_waypoint_arrival(train, route, exec_state)`.

Departure to waypoint:

- Resolve target platform and platform block.
- Determine train current block.
- If already in target block, call `controller.depart()` when applicable.
- Otherwise request a dispatcher path.
- On successful path:
  - Set train `current_path`, `path_index`, `assigned_path_id`.
  - Set controller and train `ato_destination_block_id`.
  - Call `controller.depart()` if train is ready.
- On failure, set route error and call `controller.wait_for_path()`.

TODO: desired departure-to-waypoint flow:
- Keep the same waypoint resolution and path-request flow.
- On successful path, set `train.current_path`, `train.path_index`, and `train.assigned_path_id`.
- Set `controller.ato_path_id`, but stop setting destination-block info directly on either `Train` or `TrainController`.
- Deliver destination block and exact stop position inside the controller path plan pushed by `PathDispatcher`.
- Keep `controller.depart()` as the way movement begins.

Concrete departure calls:

- `_try_depart_to_waypoint()` calls `_get_controller(train)` to get or create a `TrainController`.
- `_try_depart_to_waypoint()` calls `route.get_waypoint_at(exec_state.current_waypoint_index)`.
- It calls `_get_platform(target_platform_id)`, `_get_platform_block_id(target_platform_id)`, and `_get_current_block_id(train)`.
- If platform lookup or platform block lookup fails, it calls `route.set_error(...)`, `controller.wait_for_path(...)`, and sets `exec_state.waiting_for_path = True`.
- If current block lookup fails, it calls `controller.wait_for_path("Train is not in a block")` and sets `exec_state.waiting_for_path = True`; it does not set a route error in the current code.
- If current block already equals target block, it calls `route.clear_error()`, clears `exec_state.waiting_for_path`, and calls `controller.depart()` for `STANDBY` or `IDLE` trains.
- If a dispatcher exists, it calls `path_dispatcher.create_path(start_block_id=current_block_id, end_block_id=target_block_id, train_id=train.train_id)`.
- On successful `PathResult`, it calls `_convert_path_to_train_path(path_result.path)`, sets `train.current_path`, `train.path_index`, and `train.assigned_path_id`, sets `controller.ato_destination_block_id` and `train.ato_destination_block_id`, and then calls `controller.depart()` for `STANDBY` or `IDLE` trains.
- On failed `PathResult`, it calls `route.set_error(...)`, `controller.wait_for_path(error_msg)`, and sets `exec_state.waiting_for_path = True`.
- If no dispatcher exists, it calls `controller.depart()` for `STANDBY` or `IDLE` trains without reserving a path.
- `controller.depart()` delegates to `controller.ato_go()` in ATO mode, or directly sets `RUNNING_MOVING` in manual mode after precondition checks.

TODO: desired concrete departure calls:
- After `_convert_path_to_train_path(path_result.path)`, set `train.current_path`, `train.path_index`, and `train.assigned_path_id` as before.
- Set `controller.ato_path_id`, but do not set `train.ato_destination_block_id`.
- Let `PathDispatcher` push destination block and `stop_position_cm` to the controller as part of the path plan.
- Keep the fallback behavior where `controller.depart()` is called directly if no dispatcher exists.

Arrival and dwell:

- Arrival is checked by whether train is at the resolved platform.
- On first arrival, record arrival time and call `controller.arrive_at_station()` for non-manual trains.
- Dwell completion uses `Waypoint.should_depart()`.
- When there are more waypoints, call `controller.depart_from_station(True)`, then the next update will request the next path.
- When route completes, unassign train, clear execution state, and for non-manual trains call `controller.depart_from_station(False)`.

Concrete arrival/dwell calls:

- `_check_waypoint_arrival()` calls `route.get_waypoint_at(...)` and `_is_train_at_platform(train, target_platform_id)`.
- On first arrival, `_check_waypoint_arrival()` sets `exec_state.arrived_at_platform_time`, sets `exec_state.last_waypoint_platform_id`, and calls `_get_controller(train).arrive_at_station()` for non-manual trains.
- `_check_waypoint_arrival()` then calls `_check_dwell_complete(train, route, exec_state)`.
- `_check_dwell_complete()` calls `waypoint.should_depart(time_at_platform, route_start_time, self.simulation_time)`.
- If dwell/timing says depart, `_check_dwell_complete()` calls `_advance_to_next_waypoint(route, exec_state)`.
- `_advance_to_next_waypoint()` updates `exec_state.current_waypoint_index`, `exec_state.direction`, `exec_state.resolved_platform_id`, and the backward-compatible `route.current_waypoint_index` / `route.direction` fields according to `RouteMode.ONE_OFF`, `RouteMode.CIRCULAR`, or `RouteMode.ROUND_TRIP`.
- If another waypoint remains, `_check_dwell_complete()` calls `_get_controller(train).depart_from_station(has_more_waypoints=True)`.
- If no waypoint remains, `_check_dwell_complete()` calls `_handle_route_complete_for_train(train, route, exec_state)`.
- `_handle_route_complete_for_train()` calls `_get_controller(train)`, `route.unassign_train(train.train_id)`, clears `train.assigned_route_id` when it matches, removes `execution_states[train.train_id]`, calls `controller.depart_from_station(has_more_waypoints=False)` for non-manual trains, and calls `route.deactivate()` for completed one-off routes with no assigned trains.

Dispatcher completion notification handling:

- `SimulationController.update()` calls `RouteExecutor.handle_path_completion(train_id, path_id, _end_tile, _event_time)` after reading dispatcher events.
- `handle_path_completion()` ignores the event unless the train exists and `train.assigned_path_id == path_id`.
- It clears `train.assigned_path_id`, `train.current_path`, `train.path_index`, `train.reserved_blocks`, and `train.ato_destination_block_id`.
- It clears `exec_state.waiting_for_path` when an execution state exists.
- If the train still has an active assigned route, it calls `_check_waypoint_arrival(train, route, exec_state)`.
- It calls `_get_controller(train)`, clears `controller.ato_destination_block_id` and `controller.ato_path_id`, and calls `controller.transition_to_standby()`.
- It returns `True` when it handled the event, otherwise `False`.

TODO: desired dispatcher completion handling:
- `PathDispatcher._complete_path_on_full_stop()` should call `RouteExecutor.handle_path_completion(train_id, path_id, _end_tile, _event_time)` directly after calling `controller.on_path_complete()`.
- `handle_path_completion()` should continue to ignore mismatched or stale path IDs.
- It should clear `train.assigned_path_id`, `train.current_path`, `train.path_index`, and `train.reserved_blocks`, but no longer clear `train.ato_destination_block_id` because that field should be removed.
- `controller.transition_to_standby()` should be removed from `handle_path_completion()` once `controller.on_path_complete()` owns that state transition.
- Controller destination-block/path teardown should move into `controller.on_path_complete()`, leaving `RouteExecutor` responsible only for route-level arrival and dwell logic.

Review anchors:

- Route execution calls `controller.depart()` rather than directly starting physics.
- For same-block target, `controller.depart()` is called without creating a dispatcher path.
- Route completion assumes the train is in `RUNNING_AT_STATION` if `depart_from_station(False)` should succeed.

## Train Entity

`Train` stores the train's persistent and UI-facing state:

- Consist and length.
- `current_speed` in km/h.
- `position` as `TrainPosition`.
- Schedule and route/path IDs.
- `target_speed`, `manual_target_speed`, `speed_limit_reason`.
- Cab/direction fields.
- ATO/destination flags.

TODO: desired train ownership:
- Keep `current_speed` train-owned because `TrainPhysics` updates it together with position and occupancy.
- Remove train-side mirrors for `target_speed` and `speed_limit_reason`; those should become reads from controller lookahead at distance 0.
- Remove `manual_target_speed` entirely; the new manual model should not keep a per-train target speed field.
- Remove `train.ato_destination_block_id`; destination block and stop position should travel inside the controller path plan pushed by `PathDispatcher`.
- Keep `Train` focused on physical and persistent state only, and move live operational diagnostics to `TrainController`.

`TrainPosition` tracks both ends:

- End A tile, distance, entry edge.
- End B tile, distance, entry edge.
- `leading_end`: A or B.
- `occupied_tiles` and `occupied_blocks`.
- Computed head/tail properties depend on `leading_end`.
- `facing_direction` is inferred from head entry edge.

Train state helpers:

- `is_stopped()` uses `current_speed <= 0.1 km/h`.
- `get_effective_direction()` uses facing direction or its opposite for manual reverse.
- `get_occupied_blocks(network)` derives blocks from `position.occupied_tiles` when possible.

Train state methods exist on both `Train` and `TrainController`.

Review anchor:

- Runtime control should treat `TrainController` as the source of truth for operational transitions during simulation, because `SimulationController` syncs controller state back to train after each tick.

## TrainController

`TrainController` is the onboard/ATC/ATO authority for one train.

Important state:

- `power_on`.
- `eb_active`.
- `atc_warning_active`.
- `active_cab`.
- `control_mode`: `ato` or `manual`.
- `state`: mirrors train state.
- `target_speed`.
- `handle_position`.
- `ato_running`, `ato_destination_block_id`, `ato_path_id`.
- `clearance_ahead_cm`.
- `atc_speed_limit`.
- `current_block_speed_limit`.
- `lookahead`.
- `ato_info`.
- `handle_reason`.

TODO: desired controller state:
- Add `manual_mode_type`, such as `simulation` or `voltage`.
- Add `manual_power_percent` for voltage-mode manual control.
- Keep `ato_path_id`, but derive `ato_destination_block_id` from a controller-owned `received_path_plan` instead of storing it independently.
- Add `received_path_plan` as the controller inbox for the active path plan pushed by `PathDispatcher`.
- Keep `lookahead`, but make it the canonical cached series used by ATC, ATO, UI, and monitor consumers.
- Remove standalone scalar fields such as `target_speed`, `speed_limit_reason`, and `effective_limit` once those values are represented through lookahead-at-distance-0 reads.

Controller tick order:

1. If no train, set target speed to zero and return.
2. If power is off, set controller/train to `OFF`, target zero, return.
3. If emergency brake is active:
  - Set handle to `EB`.
  - Set target speed zero.
  - If already stopped, clear EB and move to `STANDBY`.
  - Return.
4. Calculate block lookahead.
5. If no cab is selected, force ATC limit to zero.
6. Otherwise:
  - Read current block speed limit.
  - Calculate clearance ahead.
  - Calculate ATC speed limit and reason.
7. Apply ATC overspeed logic:
  - Overspeed over 10 km/h triggers EB and returns.
  - Overspeed over 5 km/h sets warning.
8. Calculate ATO info.
9. In ATO mode:
  - If `ato_running`, calculate handle with `_ato_calculate_handle()`.
  - If not running, choose braking handle toward zero and transition to standby once stopped.
10. In manual mode:
  - If current speed is over ATC speed limit, override handle with braking.
11. Compute `effective_limit = min(train.max_speed, atc_speed_limit, current_block_speed_limit)`.
12. Set target speed to zero if ATO is not running; otherwise use `effective_limit`.
13. Sync target speed and reason to the train.
14. Run path-backed completion/waiting state checks.

TODO: desired controller tick order:
1. If no train, return immediately.
2. If power is off, set controller/train to `OFF` and return.
3. If emergency brake is active:
  - Set handle to `EB` and early-exit the tick while preserving path and route state.
  - Stay in the early-exit loop until the train has stopped.
  - Require explicit `release_emergency_cmd()` to clear `eb_active`.
  - After release, transition to `STANDBY`.
4. Mark cached lookahead data stale for the new tick.
5. If no cab is selected, force ATC limit to zero and suppress the lookahead payload for that tick.
6. Otherwise ensure the canonical lookahead payload is available:
  - With a received path plan, lookahead should extend along the path to `stop_position_cm`.
  - Without a path plan, lookahead should extend only through the occupied block boundary.
7. Build ATC state from the cached lookahead payload, including structured reason metadata.
8. Apply ATC overspeed logic.
9. Calculate ATO info.
10. In ATO mode:
  - If `ato_running`, calculate handle with `_ato_calculate_handle()`.
  - If not running, choose braking handle toward zero and transition to standby once stopped.
11. In manual mode, write a controller-owned command and let physics convert it to speed:
  - In `simulation` submode, store the driver's handle notch in `handle_position` and allow ATC to override with braking.
  - In `voltage` submode, store the driver's percentage power in `manual_power_percent` and let physics map it through `SpeedProfile.power_speed_curve`.
12. Stop computing standalone `effective_limit` and `target_speed` scalars; consumers should read controller lookahead at distance 0 instead.
13. Remove controller-side path completion checks such as standalone path completion or path-backed completion helpers.
14. Let waiting and completion decisions come from post-physics dispatcher feedback.

ATC speed limit:

- `_calculate_atc_speed_limit()` uses clearance ahead and `SpeedProfile.deceleration_rate`.
- `calculate_max_safe_speed()` computes maximum speed that can stop within `(clearance - safety_margin)`.
- Yellow signals can reduce speed to `YELLOW_SIGNAL_SPEED_KMH`.
- Current block `max_speed_kmh` can reduce speed.

TODO: desired ATC speed limit:
- Stop reading signal state directly for ATC restriction selection; CAUTION-block speed limits should already express the same restriction through ordinary block limits.
- In manual mode with a received path plan, include `_calculate_authority_boundary_limit()` so the effective ATC limit enforces a stop at `stop_position_cm`.
- Treat ATC reason reporting as structured metadata on the lookahead/curve payload, not only as a free-form controller string.

ATO handle calculation:

- `_ato_calculate_handle()` builds monitor lookahead and speed curves.
- If the current curve point says stop, it calculates a direct braking notch.
- Otherwise it evaluates candidate handles by simulating forward and comparing with the operational speed curve.
- It checks predicted speed against ATC limit and can override with braking.
- It rate-limits handle movement to reduce jitter, except EB always bypasses rate limiting.

TODO: desired ATO handle calculation:
- Stop rebuilding monitor lookahead inside `_ato_calculate_handle()` and read the canonical cached controller lookahead payload instead.
- If the sampled operational speed curve at distance `0` is already at or below the stop threshold, calculate a direct braking notch immediately.
- Replace the current multi-handle forward simulation wording with a two-point sampling approach:
  - sample the operational speed curve at distance `0`
  - sample one future point at a delta-scaled horizon such as `speed_cm_s * self._tick_delta * LOOKAHEAD_TICKS`
  - combine those samples with the active ATC and block caps to choose power, coast, or brake
- Once the two-point horizon is in place, remove the old fixed-horizon wording and any special near-stop fast-path that only exists to compensate for the old horizon logic.

Speed curves:

- `calculate_speed_curves()` samples ahead at `CURVE_SAMPLE_RESOLUTION_CM`.
- It creates restrictions for path end and block speed limits.
- It does not use signal restrictions for speed control; signals are display-oriented in this CBTC-style model.
- It uses full service braking for ATC curve.
- It uses `SpeedProfile.operational_decel_ratio` for ATO curve.
- ATO operational curve is kept below ATC by `ATO_SPEED_MARGIN_KMH`.

TODO: desired speed curves:
- Build a unified restriction set for the full clearance/authority area and take the minimum of all relevant curves and local limits at each sample point.
- Carry source metadata on each `lookahead.speed_curves` datapoint, such as `atc_limit_reason_code` and `atc_limit_reason_detail`.
- Preserve metadata for which restriction category produced the current minimum ATC limit so monitor/debug UI can explain the displayed number.

Commands:

- `power_on_cmd()` and `power_off_cmd()` control power mode.
- `emergency_stop_cmd()` sets EB.
- `release_emergency_cmd()` clears EB only when stopped.
- `set_cab()` changes active cab only when safe, and handles physical orientation details.
- `set_mode()` switches between ATO and manual only when stopped.
- `set_handle()` is for manual control.
- `ato_go()` requires ATO mode, power on, and `IDLE` or `STANDBY`; it can auto-select/correct cab from path direction, then sets `RUNNING_MOVING` and `ato_running=True`.
- `ato_stop()` sets `ato_running=False`; subsequent ticks brake to standby.

TODO: desired driver/operator inputs:
- Keep `set_handle()` as the active manual-driving command for `simulation` submode manual control.
- Add `set_manual_power_percent()` as the active driver input for `voltage` submode manual control.

TODO: desired dispatcher/route-executor inputs:
- Add `receive_path_plan(path_plan)`, called by `PathDispatcher` after successful `create_path()` and with `None` on cancellation.
- Add `on_path_complete()`, called by `PathDispatcher._complete_path_on_full_stop()` when the train has fully stopped at the terminal stop point.
- Keep `depart()`, `wait_for_path(reason)`, `arrive_at_station()`, and `depart_from_station(has_more_waypoints)` as route-executor-facing controller commands.
- Replace `transition_to_standby()` as the completion hook with `on_path_complete()` once completion teardown is controller-owned.

TODO: desired read accessors:
- Add `get_lookahead()` as the canonical accessor for UI and debug consumers. Any consumer asking for target speed or speed-limit reason should sample this payload at distance `0`.

Current manual mode and movement-authority boundary:
- Manual control currently uses `set_handle()` plus train-side `manual_target_speed`.
- ATC currently reacts to clearance, signal, and block limits, but it does not enforce an explicit path-end authority boundary in manual mode.

TODO: desired manual mode and movement-authority boundary:
- In `simulation` submode, the driver should call `set_handle()`, the controller should store `handle_position`, and `TrainPhysics` should integrate it into speed while ATC can still override with braking.
- In `voltage` submode, the driver should call `set_manual_power_percent()`, the controller should store `manual_power_percent`, and `TrainPhysics` should convert it through `SpeedProfile.power_speed_curve`.
- Remove `manual_target_speed` entirely; no hidden shim or compatibility field should remain.
- In manual mode with a received path plan, add `_calculate_authority_boundary_limit()` so the controller brakes to the pushed `stop_position_cm`.
- Without a received path plan, the current-block boundary should remain the fallback authority limit.

Current completion ownership:
- Dispatcher completion is currently event-driven through `SimulationController`, while `TrainController.tick()` still contains path-backed completion and waiting checks.

TODO: desired completion ownership and timing:
- Make `PathDispatcher` the sole owner of path completion detection.
- Let `TrainController` react to dispatcher-owned completion through `on_path_complete()`, but stop letting the controller decide independently that a path-backed run is complete.
- Remove helper logic such as `path_backed_run_active` / `_is_standalone_path_complete()` once dispatcher-driven completion is authoritative.
- Tighten the invariant so path completion is emitted only after the train is fully stopped at the terminal stop point.

Review anchors:

- In the current tick order, ATO handle calculation happens before `target_speed` is updated for this tick. `_ato_calculate_handle()` mostly uses curves and ATC fields, but any direct use of `self.target_speed` during handle calculation should be reviewed.
- No-cab state sets ATC speed limit to zero; ATO start should therefore ensure cab selection before movement.
- Controller and train both have ATO-related fields. Controller is the active runtime source, while train fields are used for persistence/UI and route integration. TODO: remove the runtime fallback read `self.ato_destination_block_id or getattr(train, "ato_destination_block_id", None)` from controller ATO logic; in the new model destination state comes exclusively from `received_path_plan`.

TODO: desired controller review anchors:
- There should be no `self.target_speed` field once lookahead-at-distance-0 becomes the canonical source of target speed.
- Controller-owned live control state and train-owned physical/persistent state should be cleanly separated.

## TrainPhysics

`TrainPhysics` moves one train and reconciles block occupancy.

Physics tick:

1. Get train and controller.
2. If controller power is off, set occupied block voltage to zero and return.
3. Get speed profile.
4. Convert controller handle to acceleration in `km/h/s`.
5. `new_speed = previous_speed + acceleration * delta`.
6. Clamp lower bound to zero.
7. If braking and under stopped threshold, clamp to zero.
8. If handle is neutral, apply speed profile friction.
9. Apply air resistance if enabled.
10. Set `train.current_speed = new_speed`.
11. Convert speed to movement distance in cm using model-scale conversion.
12. Move the train if distance is positive.
13. Update block occupancy.
14. Periodically validate position/block sync.
15. Update voltage output.

Movement:

- `_move_train()` advances the head end according to `leading_end`.
- `_advance_end()` handles tile transitions using the current tile length and `TrackSegment.get_exits()` through `_calculate_exit_edge()`.
- If there is no valid exit or no next tile, speed is set to zero and the end stays at the boundary.
- `_update_tail_position()` traces backward from head by train length.
- `_update_occupied_tiles()` traces from tail to head and records all covered tiles.

Block occupancy:

- `_get_blocks_for_train()` derives blocks from occupied tiles.
- `_update_block_occupancy()` compares new occupied blocks to `_last_occupied_blocks`.
- Newly entered blocks call `block_manager.occupy_block()`.
- Newly vacated blocks call `block_manager.set_block_to_caution()` and are tracked in `_caution_blocks`.
- Caution blocks clear to free later when the train no longer occupies them and clearing conditions are met.
- `TrainPosition.occupied_blocks` is updated from the derived block set.

TODO: desired block occupancy:
- Route normal tail-vacated transitions through `block_manager.release_block(block_id, train_id)` so `OCCUPIED -> CAUTION -> FREE` stays in one state machine.
- Replace direct `clear_block()` calls for normal CAUTION clearing with another `release_block()` step.

TODO: desired tail-vacate flow in `TrainPhysics`:

- Remove direct calls to `block_manager.set_block_to_caution()` and `block_manager.clear_block()` for normal vacate transitions.
- On tail-vacate, call `block_manager.release_block(block_id, train_id)` only; let `BlockManager` own both the `OCCUPIED → CAUTION` and the subsequent `CAUTION → FREE` steps.
- `_caution_blocks` tracking in `TrainPhysics` becomes unnecessary once `BlockManager` handles the full release lifecycle internally.

Voltage:

- `OFF`: 0 V.
- `IDLE`, `STANDBY`, `RUNNING_WAITING`, `RUNNING_AT_STATION`: lighting voltage.
- `RUNNING_MOVING`: voltage from `speed_profile.speed_to_voltage(controller.target_speed)`.
- `MANUAL`: voltage from `speed_profile.speed_to_voltage(train.manual_target_speed)`.
- EB overrides to 0 V.

TODO: desired voltage behavior:
- Stop reading `controller.target_speed` and `train.manual_target_speed` once those standalone fields are removed.
- For any running state, derive voltage from `train.current_speed` through `SpeedProfile.speed_to_voltage()`.
- Make voltage emission independent of control mode; it should not matter whether the current speed came from ATO, simulation-submode handle, or voltage-submode power percentage.

Review anchors:

- Physics does not clamp speed to target speed; it relies on controller handle selection to accelerate/brake.
- Movement is geometry-driven, not strictly path-index-driven. It uses switch state and track exits. Path locks are therefore important.
- If no movement occurs because speed is zero, block occupancy still updates.

TODO: desired physics review anchor:
- `TrainPhysics` should not own manual-mode policy; it should execute controller commands and maintain physical state only.

## SpeedProfile

`SpeedProfile` maps command speed to voltage and provides motion rates:

- `speed_voltage_curve`: calibration points `(speed_kmh, voltage)`.
- `min_start_voltage`: minimum movement voltage.
- `lighting_voltage`: voltage for stopped lit states.
- `acceleration_rate`: km/h/s.
- `deceleration_rate`: km/h/s.
- `emergency_brake_rate`: km/h/s.
- `friction_rate`: km/h/s.
- `operational_decel_ratio`: comfort braking fraction for ATO curve.
- Air resistance settings: enabled flag, drag coefficient, reference speed.

`speed_to_voltage()`:

- Returns 0 for non-positive speed.
- Returns start voltage below first calibration point.
- Interpolates in the curve range.
- Clamps above the last calibration point.

Review anchor:

- `SpeedProfile` is not a speed limit object; train maximum speed and block maximum speed are separate constraints.

TODO: desired speed-profile addition:
- Add power-to-speed mapping for manual voltage mode to train/profile calibration data, for example `power_speed_curve` or an equivalent interpolation helper.

## Signals

Signals are linked to tracks and blocks:

- `Signal.detection_direction` is the direction trains travel toward.
- `approach_direction` is the opposite direction for display.
- `RailwayNetwork.update_signals_from_blocks()` recalculates signal states from block state.
- Controller speed curves intentionally do not use signals as speed restrictions.

Review anchors:

- Red/yellow signal semantics and ATC speed enforcement are not equivalent in the current design. Blocks are the primary safety authority.
- If signals are expected to enforce stops, that would be a design change from the current CBTC-style speed curve comments.

## Power

`BlockPowerController` owns voltage output:

- `TrainPhysics` normally calls `set_block_voltage()` for occupied blocks.
- Emergency stop can set all block voltages to zero.
- Voltage is per block, not per tile.

Review anchor:

- Multiple trains occupying the same block would create a last-writer-wins voltage situation unless prevented by block occupancy/path logic.

TODO: desired power-controller role:
- Keep `BlockPowerController` focused on converting speed-oriented state to voltage only.
- Do not let it own manual-mode state or read `manual_target_speed`.

## Data Flow To UI

`Train.to_dict()` exports:

- `id`, `length`, `length_cm`, `speed`, `max_speed`.
- state, cab, route/path IDs, target speed, wait/speed-limit reason.
- `position` with end A/end B data.
- occupied tile rendering data.
- live or cached `current_path`.
- lookahead tile info when network and path data are supplied.

TODO: desired `Train.to_dict()` payload:
- Export only physical and persistent train state.
- Remove `target_speed`, `speed_limit_reason`, `ato_destination_block_id`, ATO info, lookahead data, and other live control diagnostics from `Train.to_dict()`.
- Read live operational diagnostics from `TrainController.to_dict()` instead.

`TrainController.to_dict()` exports:

- power, EB, active cab, control mode, travel direction.
- controller state, target speed, handle position.
- ATO state and ATO info.
- ATC clearance and speed limit.
- lookahead points.
- current speed and stopped flag from the train.
- `handle_reason`.

TODO: desired `TrainController.to_dict()` payload:
- Export all live operational state, including `handle_position`, `manual_mode_type`, and `manual_power_percent`.
- Export ATO state using `ato_destination_block_id` derived from `received_path_plan`.
- Export received path-plan data such as `stop_position_cm` and per-block speed limits when present.
- Expose the full canonical lookahead payload as the single source of truth for current target speed, ATC limit, speed-limit reason, and forward curve rendering.

Review anchor:

- UI code must distinguish `train.speed` from controller `current_speed`; they refer to the same physical value but are exposed under different keys.

TODO: desired UI data-flow anchor:
- Treat controller payload as the source of truth for live operational/manual state, and train payload as the source for physical/persistent state.
- A UI asking "what is the train's target speed right now?" should call `controller.get_lookahead()` and sample at distance `0`.

## End-To-End ATO Path Run

Typical route-controlled run:

1. Route is active and assigned to train.
2. RouteExecutor resolves the next platform waypoint.
3. RouteExecutor finds target platform block and current train block.
4. PathDispatcher creates a path and reserves blocks/switches.
5. RouteExecutor stores `assigned_path_id` on the train and sets `ato_destination_block_id` directly on both `train` and `controller`. (TODO: stop writing `ato_destination_block_id` to either entity; `PathDispatcher` should push destination block and `stop_position_cm` to `TrainController` via the path plan instead.)
6. RouteExecutor calls `controller.depart()`.
7. In ATO mode, `depart()` delegates to `ato_go()`.
8. `ato_go()` selects/corrects cab if needed and sets `RUNNING_MOVING`, `ato_running=True`.
9. Each simulation tick:
  - Controller calculates lookahead, ATC speed limit, ATO handle, target speed.
  - Physics changes speed, moves train, updates block occupancy, sets voltage.
  - Dispatcher consumes entered-block transitions and may complete the path.
10. On path completion, dispatcher records an event.
11. SimulationController passes event to RouteExecutor.
12. RouteExecutor checks platform arrival and dwell.
13. Dwell completion advances to the next waypoint and the cycle repeats.

TODO: desired end-to-end ATO run:
1. Route is active and assigned to the train.
2. In Phase 1 setup, `RouteExecutor` resolves the next platform waypoint and calls `PathDispatcher.create_path()`.
3. `RouteExecutor` stores `assigned_path_id` on the train and the controller receives destination block plus `stop_position_cm` through the pushed path plan.
4. Each simulation tick runs in setup, execute, and feedback phases:
  - setup: route/path planning
  - execute: controller lookahead, ATC, and ATO/manual command generation, followed by physics movement
  - feedback: dispatcher resource progress, release, and completion checks
5. On path completion, `PathDispatcher._complete_path_on_full_stop()` releases remaining resources, calls `controller.on_path_complete()`, and calls `RouteExecutor.handle_path_completion()` directly.
6. `RouteExecutor.handle_path_completion()` clears train path fields and hands route-level arrival/dwell logic to `_check_waypoint_arrival()`.
7. Dwell continues through `RUNNING_AT_STATION` and `Waypoint.should_depart()`.
8. When dwell completes, `RouteExecutor` advances the waypoint index and the cycle repeats.

## Logic Review Checklist

Use this checklist when reviewing train-running behavior:

- Is the train powered and cab-selected before movement?
- Is there an assigned dispatcher path when ATO is expected to run?
- Does the train have `ato_destination_block_id` set when path-end stopping matters? (TODO: field is removed from `Train` in the new model; replace with: "Does the controller's `received_path_plan` contain `stop_position_cm`?")
- Does controller `ato_running` match train route/path state?
- Is the current block's `max_speed_kmh` the intended speed authority?
- Are station speed limits expected to affect ATC/ATO? Current controller comments say no; use block limits.
- Are signal states expected to enforce speed? Current speed-curve comments say no; blocks enforce movement safety.
- Can a block be cleared directly through `clear_block()` while a train still physically occupies it?
- Can a path contain repeated blocks? If yes, review `current_block_index` behavior.
- Can a train span a switch while the switch is unlocked? If yes, tail tracing can become questionable.
- Does ATO handle calculation need the target speed computed before handle selection in the same tick?
- Are there cases where route executor starts movement without a dispatcher path, such as already being in the target block?
- Are controller fields and train fields being kept in sync when API commands bypass the simulation loop?

TODO: desired review checklist:
- Is the controller receiving a path plan with `stop_position_cm` when path-end stopping matters?
- Is controller lookahead the only source of target speed and speed-limit reason?
- In manual mode, does ATC enforce the end of the assigned path as a hard stop through `_calculate_authority_boundary_limit()`?
- Can any path-complete event still fire while the train is braking or creeping toward the terminal stop point?
- Is `PathDispatcher._release_blocks_behind_train()` dead production code, or does it still need to be wired into runtime behavior?
- Does `_ato_calculate_handle()` read only the current tick's canonical lookahead payload, not stale scalar fields?

Current verified notes:
- Switches release immediately on tail-vacate, not on full stop. `TrainPhysics` calls `switch.release_lock(path_id)` when the train tail fully vacates a switch tile.
- `assigned_path_id` is cleared by `RouteExecutor.handle_path_completion()`, not by `PathDispatcher`, so the old event-consumption race is avoided once completion is routed synchronously.
