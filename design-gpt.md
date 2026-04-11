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

## Core Runtime Loop

`SimulationController.update(delta_seconds)` is the top-level runtime step:

1. Advance `simulation_time`.
2. Run `RouteExecutor.update(delta_seconds)`.
3. For each train, call `_update_train(train, delta_seconds)`.
4. Update signals from blocks.
5. Consume dispatcher completion events and hand them to the route executor.

Concrete calls:

- `self.route_executor.update(delta_seconds)`.
- `self._update_train(train, delta_seconds)` for each `train` in `network.trains.values()`.
- `self.network.update_signals_from_blocks()`.
- `self.path_dispatcher.get_and_clear_completion_events()`.
- For each completion event with `train_id`, `path_id`, and `end_tile`, call `self.route_executor.handle_path_completion(train_id, path_id, _end_tile=end_tile, _event_time=event.get("time"))`.

Consuming dispatcher completion events means:

- `PathDispatcher._complete_path_on_head_arrival(path, train)` calls `_record_completion_event(path, train.position.head_tile)` when it completes a path.
- `_record_completion_event()` appends a dict to `PathDispatcher.completed_path_events` with `train_id`, `path_id`, `end_tile`, and `time`.
- `SimulationController.update()` later calls `get_and_clear_completion_events()`, which returns that queued list and clears the queue.
- Each returned event is handed to `RouteExecutor.handle_path_completion(...)`.
- `RouteExecutor.handle_path_completion()` only handles the event if the train still exists and `train.assigned_path_id == path_id`.
- When it handles the event, `RouteExecutor.handle_path_completion()` clears the train's path fields, clears route waiting state, optionally checks waypoint arrival, clears the controller's ATO path/destination fields, and calls `controller.transition_to_standby()`.
- Current dispatcher completion code also clears the train path fields and may transition the shared controller before the event is consumed, so the route-executor event handler can return `False` if `train.assigned_path_id` has already been cleared.

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

Concrete calls in `_update_train()`:

- `_get_or_create_controller(train)`.
- `_get_or_create_physics(train)`.
- `controller.tick(delta_seconds)`.
- `physics.tick(delta_seconds)`.
- For each block in `physics.last_block_transitions["entered"]`, call `path_dispatcher.on_train_advances_from_physics(train.assigned_path_id, block_id)`.
- Then call `path_dispatcher.on_train_progress_from_physics(train.assigned_path_id)`.
- Finally assign `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.

Asking the dispatcher to process path progress/completion means:

- `on_train_advances_from_physics(path_id, entered_block_id)` updates `path.current_block_index` when the train enters a block in `path.block_sequence`.
- If the entered block is the final block, `on_train_advances_from_physics()` sets `path.status = PathStatus.CLEARING`.
- `on_train_progress_from_physics(path_id)` checks the active path and current train position.
- If the path is `CLEARING`, the train is stopped, and the head is in the destination block, it calls `_complete_path_on_head_arrival(path, train)`.
- If the train head tile equals the last path tile, it also calls `_complete_path_on_head_arrival(path, train)`.
- Otherwise it marks already-cleared earlier blocks in `path.blocks_cleared` and releases switches for those blocks with `_release_switches_for_block(path, block_id)` when safe.

Syncing controller state and speed-limit diagnostics back to the train means:

- `TrainController.tick(delta_seconds)` calculates controller-owned runtime values such as `state`, `target_speed`, `current_block_speed_limit`, `clearance_ahead_cm`, `atc_speed_limit`, and `speed_limit_reason`.
- ATC diagnostics are produced inside `TrainController.tick()` through `_calculate_lookahead()`, `_get_current_block_speed_limit()`, `_calculate_clearance_ahead()`, and `_calculate_atc_speed_limit()`.
- `_calculate_atc_speed_limit()` can set reasons such as braking/clearance distance, yellow signal, or block speed limit.
- `SimulationController._update_train()` then copies the UI-facing subset back to the train with `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.
- `TrainController.tick()` also writes `train.target_speed` and `train.speed_limit_reason` directly near the end of the tick, but the simulation sync repeats it so the `Train` object matches the shared controller.

Important authority split:

- `TrainController` decides commands, safety limits, ATO handle, operational state.
- `TrainPhysics` changes speed, tile position, block occupancy, and block voltage.
- `PathDispatcher` reserves path resources and receives progress from physics.
- `RouteExecutor` decides which waypoint/path should be active for a train.

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

Review anchor:

- There are lock fields on both `TrackSegment` and `Switch`. Dispatcher code primarily locks the `TrackSegment`. Treat track lock as the operational lock unless code is changed to synchronize both.

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

## Path And PathDispatcher

`Path` is a reserved dispatcher movement authority:

- `path_tiles`: ordered track tiles.
- `block_sequence`: ordered block IDs.
- `switch_positions`: required switch state by tile.
- `train_id`: train assigned to the path.
- `current_block_index`: head's block index in `block_sequence`.
- `blocks_cleared`: blocks fully behind the train.
- `flank_protection_switches`, `overlap_blocks`, `approach_locked_blocks`: protection data.

`PathDispatcher.create_path()` flow:

1. `find_paths(start_block_id, end_block_id, train_id)` enumerates candidate `PathSegment`s.
2. Generate a path ID.
3. Validate each candidate.
4. Acquire path resources for the first valid candidate.
5. Extend path tiles through the destination block.
6. Create `Path(status=SET, train_id=train_id, ...)`.
7. Store it in `active_paths`.
8. Copy `block_sequence` to `train.reserved_blocks` when a train is supplied.

Resource acquisition:

- Reserve every block with `Block.reserve_for_path(path_id, train_id, direction)`.
- Set `track.path_direction` for path tiles.
- Lock required switches by calling `track.lock_for_path(path_id, state)`.
- Lock flank-protection switches when available.
- If any acquisition fails, reserved blocks, path directions, and switch locks are rolled back.

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

Path extension:

- `extend_path(path_id, new_end_block_id)` only extends forward.
- Rejects extension to a block already in the path.
- Finds extension paths from current end to the new block.
- Validates and acquires the extension resources.
- Appends new tiles and new blocks, skipping duplicated connection/start entries.

Cancellation:

- `cancel_path(path_id, force=False)` marks status `CANCELLED`.
- Non-approach-locked blocks are cleared unless physically occupied by the train.
- Switches are released unless physically under the train or in an approach-locked block.
- Path directions are cleared only if all approach locks are released or `force=True`.
- Path remains in `active_paths` if approach-locked resources remain.

Review anchors:

- A newly created path is `SET`, but normal physics progress can update it even if it never transitions to `ACTIVE`.
- Some resource release paths call `clear_block()` directly, so check whether those cases can encounter physically occupied blocks.
- `current_block_index` uses the first index of the entered block in `block_sequence`; repeated block IDs in a complex path would need careful review.

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

Dispatcher completion event handling:

- `SimulationController.update()` calls `RouteExecutor.handle_path_completion(train_id, path_id, _end_tile, _event_time)` after reading dispatcher events.
- `handle_path_completion()` ignores the event unless the train exists and `train.assigned_path_id == path_id`.
- It clears `train.assigned_path_id`, `train.current_path`, `train.path_index`, `train.reserved_blocks`, and `train.ato_destination_block_id`.
- It clears `exec_state.waiting_for_path` when an execution state exists.
- If the train still has an active assigned route, it calls `_check_waypoint_arrival(train, route, exec_state)`.
- It calls `_get_controller(train)`, clears `controller.ato_destination_block_id` and `controller.ato_path_id`, and calls `controller.transition_to_standby()`.
- It returns `True` when it handled the event, otherwise `False`.

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

ATC speed limit:

- `_calculate_atc_speed_limit()` uses clearance ahead and `SpeedProfile.deceleration_rate`.
- `calculate_max_safe_speed()` computes maximum speed that can stop within `(clearance - safety_margin)`.
- Yellow signals can reduce speed to `YELLOW_SIGNAL_SPEED_KMH`.
- Current block `max_speed_kmh` can reduce speed.

ATO handle calculation:

- `_ato_calculate_handle()` builds monitor lookahead and speed curves.
- If the current curve point says stop, it calculates a direct braking notch.
- Otherwise it evaluates candidate handles by simulating forward and comparing with the operational speed curve.
- It checks predicted speed against ATC limit and can override with braking.
- It rate-limits handle movement to reduce jitter, except EB always bypasses rate limiting.

Speed curves:

- `calculate_speed_curves()` samples ahead at `CURVE_SAMPLE_RESOLUTION_CM`.
- It creates restrictions for path end and block speed limits.
- It does not use signal restrictions for speed control; signals are display-oriented in this CBTC-style model.
- It uses full service braking for ATC curve.
- It uses `SpeedProfile.operational_decel_ratio` for ATO curve.
- ATO operational curve is kept below ATC by `ATO_SPEED_MARGIN_KMH`.

Commands:

- `power_on_cmd()` and `power_off_cmd()` control power mode.
- `emergency_stop_cmd()` sets EB.
- `release_emergency_cmd()` clears EB only when stopped.
- `set_cab()` changes active cab only when safe, and handles physical orientation details.
- `set_mode()` switches between ATO and manual only when stopped.
- `set_handle()` is for manual control.
- `ato_go()` requires ATO mode, power on, and `IDLE` or `STANDBY`; it can auto-select/correct cab from path direction, then sets `RUNNING_MOVING` and `ato_running=True`.
- `ato_stop()` sets `ato_running=False`; subsequent ticks brake to standby.

Review anchors:

- In the current tick order, ATO handle calculation happens before `target_speed` is updated for this tick. `_ato_calculate_handle()` mostly uses curves and ATC fields, but any direct use of `self.target_speed` during handle calculation should be reviewed.
- No-cab state sets ATC speed limit to zero; ATO start should therefore ensure cab selection before movement.
- Controller and train both have ATO-related fields. Controller is the active runtime source, while train fields are used for persistence/UI and route integration.

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

Voltage:

- `OFF`: 0 V.
- `IDLE`, `STANDBY`, `RUNNING_WAITING`, `RUNNING_AT_STATION`: lighting voltage.
- `RUNNING_MOVING`: voltage from `speed_profile.speed_to_voltage(controller.target_speed)`.
- `MANUAL`: voltage from `speed_profile.speed_to_voltage(train.manual_target_speed)`.
- EB overrides to 0 V.

Review anchors:

- Physics does not clamp speed to target speed; it relies on controller handle selection to accelerate/brake.
- Movement is geometry-driven, not strictly path-index-driven. It uses switch state and track exits. Path locks are therefore important.
- If no movement occurs because speed is zero, block occupancy still updates.

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

## Data Flow To UI

`Train.to_dict()` exports:

- `id`, `length`, `length_cm`, `speed`, `max_speed`.
- state, cab, route/path IDs, target speed, wait/speed-limit reason.
- `position` with end A/end B data.
- occupied tile rendering data.
- live or cached `current_path`.
- lookahead tile info when network and path data are supplied.

`TrainController.to_dict()` exports:

- power, EB, active cab, control mode, travel direction.
- controller state, target speed, handle position.
- ATO state and ATO info.
- ATC clearance and speed limit.
- lookahead points.
- current speed and stopped flag from the train.
- `handle_reason`.

Review anchor:

- UI code must distinguish `train.speed` from controller `current_speed`; they refer to the same physical value but are exposed under different keys.

## End-To-End ATO Path Run

Typical route-controlled run:

1. Route is active and assigned to train.
2. RouteExecutor resolves the next platform waypoint.
3. RouteExecutor finds target platform block and current train block.
4. PathDispatcher creates a path and reserves blocks/switches.
5. RouteExecutor stores path data on train and sets ATO destination block.
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

## Logic Review Checklist

Use this checklist when reviewing train-running behavior:

- Is the train powered and cab-selected before movement?
- Is there an assigned dispatcher path when ATO is expected to run?
- Does the train have `ato_destination_block_id` set when path-end stopping matters?
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
