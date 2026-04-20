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
> **How to assemble the new model:** Each `TODO:` block **replaces** the current-state material for the same local subsection. In many places that is the prose immediately above the `TODO:` block; if a `Review anchors:` subsection sits between the current-state prose and the `TODO:` block, the `TODO:` still replaces the main subsection while any separate review-anchor `TODO:` replaces only the anchor list. Sections with no `TODO:` block are unchanged in both models. Reading current text and `TODO:` blocks as additive or layered produces a false picture — the `TODO:` block is the authoritative replacement, not a supplement. Contradictions should be evaluated against the assembled new model (current text with `TODO:` substitutions applied), not against the current text alone.

Conversation summary and current ownership split:

- `TrainController` currently owns most live operational state: ATC/ATO/manual mode, power, EB, handle position, `target_speed`, ATC diagnostics, lookahead, and ATO runtime fields.
- `Train` currently owns physical and persistent state, but it also mirrors some live control and UI-facing fields such as `target_speed`, `manual_target_speed`, `speed_limit_reason`, and ATO destination info.
- Manual control currently centers on handle commands plus train-side `manual_target_speed`; there is no separate controller-owned `power_percent` submode.
- `TrainPhysics` and `BlockPowerController` currently read controller or train speed targets when converting control state into movement and voltage.

TODO: desired ownership split:
- `TrainController` should own live operational state: ATC/ATO/manual mode, handle position, future manual `power_percent`-mode inputs, and the canonical live lookahead data series. "Target speed" and "speed-limit reason" should not be stored as standalone fields anywhere; they should be the value and reason metadata at position 0 of the lookahead series.
- `Train` should own persistent and physical state only: identity, consist, schedule/route assignment, position, and actual `current_speed`. Live ATO/ATC/path-runtime state should not be mirrored on `Train`.
- `manual_target_speed` should be removed entirely. With the new handle plus power-percentage manual model there is no per-train target speed to store, so no compatibility shim is needed.
- Manual control should have two controller-owned submodes:
  - `simulation`: user drives with handle notches; the handle command flows to `TrainPhysics`, which integrates it into speed change over time.
  - `power_percent`: user drives with percentage power; the controller passes raw percentage power to `TrainPhysics`, which converts it via train/profile calibration data such as `SpeedProfile.power_speed_curve`.
- `TrainPhysics` and `BlockPowerController` should not decide manual-mode policy. They should execute whichever command the controller produces and perform command-to-speed conversion and voltage emission.
- Percentage-power mapping data should live in `SpeedProfile.power_speed_curve` as part of train calibration and be read by physics.

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
- `PathDispatcher` should execute the route plan by acquiring blocks, locking switches, and managing movement authority, and it should plan for the controller by providing the dispatcher-owned authority structure plus the exact terminal stop position.
- `PathDispatcher` should remain the sole owner of dispatcher path records. `TrainController.received_path_plan` is only a pushed view of the currently active authority, not the source of truth.
- `PathDispatcher` should keep each path record keyed by `path_id` in a dispatcher-owned registry such as `paths_by_id[path_id]`.
- `PathDispatcher` should also keep a single active-authority index such as `active_path_id_by_train[train_id]`. Each train has at most one active authority path at a time, and the controller inbox mirrors only that newest active path.
- Cancelled or completed paths may remain in `paths_by_id[path_id]` for teardown bookkeeping and history after controller authority has been cleared, but they are not active authority and are never mirrored into `TrainController.received_path_plan`. In v1, `teardown_pending` means only the immediate dispatcher-owned authority teardown is still running: clear reservation ownership, clear controller authority, clear `active_path_id_by_train[train_id]`, and finish any dispatcher bookkeeping that belongs to the old authority. Physical train motion after completion/cancellation is **not** blocked on `teardown_pending`: `TrainPhysics` keeps moving the train according to live network geometry/state, and any later occupancy/release callbacks are resource-local aftermath rather than retained authority. `PathDispatcher.create_path(...)` must reject a same-train replacement only while that immediate authority teardown is still pending.
- Physics-facing dispatcher callbacks should identify the moving train by `train_id` only. Active progress callbacks resolve the current authority from `active_path_id_by_train[train_id]`. After authority has been cleared, later physical release must not depend on a retained teardown row surviving: block release is driven by `block_id` plus `direction` through `BlockManager`, and switch release can resolve the owning lock directly from the `Switch` resource's train-owned lock collection for that `switch_id`.
- `TrainController` should execute the path plan by calculating ATC safety limits, ATO/manual commands, and an active handle or future power command. It should plan for physics by producing the active control command for the current mode.
- `TrainPhysics` should remain an executor only; it should not decide manual-mode policy.
- `PathDispatcher` should pass the current authority-end contract to the controller whenever a path is created or extended: `authority_end_block_id` plus `stop_position_cm` when the live authority already reaches an exact stop target, or `authority_end_block_id` plus `authority_end_mode = path_end_boundary` when the live authority is only a temporary best-effort prefix. The controller should derive speed restrictions from lookahead over the network and dispatcher authority structure, not receive per-block speed limits as separate pushed data.
- The dispatcher-owned path record should be the single source of truth for controller authority while it is active. Cancelling or replacing a path in `PathDispatcher` should be sufficient to give the controller a clean slate.

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
2. Run `RouteExecutor.update(delta_seconds)`. This decides which waypoint/path is active for each train and calls `PathDispatcher.create_path()` when a new path is needed. A route-managed train should already have its 1:1 `TrainController` by this point; `RouteExecutor` must ensure the controller exists before it reads controller state or asks `PathDispatcher` to push authority.
3. After path creation, `PathDispatcher` should call `controller.receive_path_plan(path_plan)` directly on that assigned train's existing `TrainController`. The plan should include `path_id`, `resource_sequence`, current `authority_end_block_id`, current `stop_position_cm`, and `authority_end_mode`. Each `resource_sequence` entry in that pushed plan carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata — so controller/UI consumers have complete geometry and ordering after train-side path mirrors are removed and do not have to infer the traversal frame later.

### Phase 2 - Execute (downward)

1. For each train, call `_update_train(train, delta_seconds)`:
   - Tick `TrainController`.
   - Tick `TrainPhysics`.

### Phase 3 - Feedback (bottom-up)

1. Within `TrainPhysics.tick(delta_seconds)`, report physics resource-entry transitions directly to `PathDispatcher`.
2. Within `TrainPhysics.tick(delta_seconds)`, report physics tail-vacated resource transitions directly to `PathDispatcher` as typed resource-release events.
3. If `PathDispatcher.on_train_progress_from_physics()` detects a completed path, `PathDispatcher._complete_path_on_full_stop()` should release remaining already-vacated resources, call `controller.on_path_complete()`, clear controller authority via `controller.receive_path_plan(None)`, and then call `route_executor.handle_path_completion()` directly with the completion context it needs (`train_id`, `path_id`, `authority_end_block_id`, `stop_position_cm`, `end_tile`, `event_time`). Path completion must not force-unlock a block or switch that is still physically occupied by the train.
4. Update signals from blocks after all dispatcher feedback has been applied.

Concrete calls:

- `self.route_executor.update(delta_seconds)`.
- `self._update_train(train, delta_seconds)` for each `train` in `network.trains.values()`.
- `self.network.update_signals_from_blocks()`.
- `self.path_dispatcher.get_and_clear_completion_events()`.
- For each completion event with `train_id`, `path_id`, and `end_tile`, call `self.route_executor.handle_path_completion(train_id, path_id, _end_tile=end_tile, _event_time=event.get("time"))`.

TODO: desired `SimulationController.update()` calls:
- `self.route_executor.update(delta_seconds)` remains the setup entry point.
- `self._update_train(train, delta_seconds)` remains the per-train execute step.
- `SimulationController` should stay an orchestrator only: advance time, call `RouteExecutor.update(delta_seconds)`, call `_update_train(train, delta_seconds)` for each train, and then update signals. It should not forward resource-entry or tail-vacate events itself.
- `SimulationController` should own one shared `PathDispatcher` instance for all trains. `TrainPhysics.tick(delta_seconds)` should report ordered resource-entry transitions and typed tail-vacated resource-release events directly to that shared dispatcher, and `PathDispatcher` should resolve the relevant active or teardown path from its own registries rather than by reading the controller inbox.
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
- Remove `_complete_path_on_head_arrival()` and implement a new `_complete_path_on_full_stop()` method. This is not a rename: the trigger changes (head-arrival or head-tile-equals-last-path-tile → full stop only), the notification chain changes (event-queue append → synchronous direct calls to `controller.on_path_complete()` and `route_executor.handle_path_completion()`), and the method body changes accordingly.
- `PathDispatcher._complete_path_on_full_stop(path, train)` should call `controller.on_path_complete()` first so the controller can react to completion while the pushed path plan is still visible, then `PathDispatcher` should clear controller authority by calling `controller.receive_path_plan(None)`, and then it should call `route_executor.handle_path_completion(train_id, path_id, authority_end_block_id, stop_position_cm, end_tile, event_time)` directly. `RouteExecutor` should treat dispatcher completion as authoritative; it does not need a second path-ID validity check or a second exact-stop evaluation. Completion teardown may drop reservation ownership for already-vacated resources, and it may force-clear `CAUTION` during terminal completion teardown, but it must not force-clear any `OCCUPIED` resource the train still physically occupies.
- `PathDispatcher` should hold a reference to `RouteExecutor`, and it should resolve `TrainController` instances through a shared controller registry. The registry is owned by `SimulationController` and exposed as a public accessor `SimulationController.get_or_create_controller(train)`; `PathDispatcher`, `RouteExecutor`, and other layers go through that accessor rather than keeping private `_get_or_create_controller` helpers or circular back-references.
- Wiring pattern: `SimulationController` owns both `PathDispatcher` and `RouteExecutor`, constructs them in order, then injects each into the other via setter (late-binding) to close the bidirectional reference. Neither component takes the other as a constructor argument.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()` from `PathDispatcher`.
- Path completion should only be recognized after the train is fully stopped at the terminal stop point. No completion callback should run while the train is still braking or creeping inside the destination block.
- After dispatcher-owned completion teardown, route-level waypoint and dwell logic should decide whether the train enters dwell (`RUNNING_AT_STATION`) or remains in `STANDBY`.

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
4. Initialize controller state from train-owned persistent/physical state once, and establish the runtime 1:1 `Train` <-> `TrainController` pairing for that `train_id`.
5. Tick controller.
6. Tick physics.
7. Sync only the train-owned physical or persistent state back to `Train`.
8. Do not add a separate simulation-owned dispatcher feedback pass. `TrainPhysics.tick()` should report progress and typed tail-vacate events directly to `PathDispatcher`.

Concrete calls in `_update_train()`:

- `_get_or_create_controller(train)`.
- `_get_or_create_physics(train)`.
- `controller.tick(delta_seconds)`.
- `physics.tick(delta_seconds)`.
- For each block in `physics.last_block_transitions["entered"]`, call `path_dispatcher.on_train_advances_from_physics(train.assigned_path_id, block_id)`.
- Then call `path_dispatcher.on_train_progress_from_physics(train.assigned_path_id)`.
- Finally assign `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.

TODO: desired `_update_train()` calls:
- Keep `get_or_create_controller(train)` (the public `SimulationController` accessor that owns the controller registry), `_get_or_create_physics(train)`, `controller.tick(delta_seconds)`, and `physics.tick(delta_seconds)`. `RouteExecutor` and `PathDispatcher` both reach the registry through the same public accessor rather than keeping private `_get_or_create_controller` helpers.
- A newly created controller may bind only to train-owned persistent/physical state needed for that 1:1 pairing. In the desired model `Train` does not carry runtime authority fields, so bootstrap must not seed `received_path_plan`, `assigned_path_id`, `ato_path_id`, or `ato_destination_block_id` from `Train`. For route-managed trains, `RouteExecutor` should already have ensured the controller exists before any Phase 1 authority push; `get_or_create_controller(train)` in `_update_train()` remains a fallback for non-route flows and recovery.
- If `PathDispatcher` already has active authority for that `train_id` when the controller is created, dispatcher should re-push `received_path_plan` from its own registry after the controller exists; controller bootstrap must not recreate that authority from train-side mirrors. `SimulationController.get_or_create_controller(train)` is responsible only for creating the controller object and registering the `train_id -> controller` binding. After that binding exists, `SimulationController` may notify `PathDispatcher` that the controller is now available, but `PathDispatcher` remains the sole component that builds and pushes the plan payload.
- Stop writing `train.target_speed`, `train.speed_limit_reason`, `train.ato_destination_block_id`, and `train.state` here. Consumers that want live target-speed data should read controller lookahead at distance 0 instead; consumers that want live operational state should read `TrainController.state` directly.
- Replace block-only feedback with resource-entry feedback emitted directly from `TrainPhysics.tick()`, so dispatcher updates can consume both blocks and switches in `resource_sequence` order.
- In the same direct `TrainPhysics -> PathDispatcher` reporting path, emit typed tail-vacated resource-release events so `PathDispatcher` can release a vacated resource according to its resource type (`block` vs `switch`).

Asking the dispatcher to process path progress/completion means:

- `on_train_advances_from_physics(path_id, entered_block_id)` updates `path.current_block_index` when the train enters a block in `path.block_sequence`.
- If the entered block is the final block, `on_train_advances_from_physics()` sets `path.status = PathStatus.CLEARING`.
- `on_train_progress_from_physics(path_id)` checks the active path and current train position.
- If the path is `CLEARING`, the train is stopped, and the head is in the destination block, it calls `_complete_path_on_head_arrival(path, train)`.
- If the train head tile equals the last path tile, it also calls `_complete_path_on_head_arrival(path, train)`.
- Otherwise it marks already-cleared earlier blocks in `path.blocks_cleared` and releases switches for those blocks with `_release_switches_for_block(path, block_id)` when safe.

TODO: desired dispatcher progress/completion flow:
- `on_train_advances_from_physics(train_id, resource_type, resource_id)` should resolve the relevant dispatcher-owned path record for that train and update `path.current_resource_index` when the train enters a resource in `path.resource_sequence`. `resource_type` is required on every resource-event callback so dispatcher never has to infer it from a bare ID; the globally-unique-ID-namespace alternative is not adopted.
- When the entered resource is a switch, the same dispatcher-owned entry handoff should set `Switch.occupied_by = train_id` for that `switch_id`. Switch physical occupancy is separate from reservation/lock ownership.
- In the desired model there should be no separate `CLEARING` lifecycle state. A live dispatcher-owned authority path stays `ACTIVE` until it is either `COMPLETED` or `CANCELLED`; entering the final block/resource only updates progress and completion predicates.
- Add `PathDispatcher.on_resource_tail_vacated_from_physics(train_id, resource_id, resource_type, direction=None)` as the typed tail-release handoff from `TrainPhysics.tick()`. `direction` is required for `resource_type == "block"` and ignored for switches. Active progress continues to resolve through `PathDispatcher`'s registries rather than from `TrainController.received_path_plan`, but tail-release logic after authority teardown must resolve from the resource itself so it still works after the controller inbox has been cleared:
  - if `resource_type == "block"`, dispatcher should route the release through `BlockManager.on_block_tail_vacated(block_id, train_id, direction)`
  - if `resource_type == "switch"`, dispatcher should clear `Switch.occupied_by` and release the switch resource through the `Switch` entity plus any flank-protection teardown resolved by `train_id`, not by `path_id`
- `on_train_progress_from_physics(train_id)` should resolve the relevant dispatcher-owned path record for that train and complete the path only after the train is fully stopped at the terminal stop point.
- Switch release should be driven by tail-vacate detection in `TrainPhysics`, not by dispatcher tile-index checks.
- Add an explicit desired-model TODO in the implementation plan: every current caller that still passes `path_id` into dispatcher progress/completion callbacks must be migrated to the train-ID-only callback contract. Active progress resolution belongs to `PathDispatcher`, but delayed release of a resource that is still physically occupied after authority teardown must resolve from the resource itself (`BlockManager` for blocks, `Switch.occupied_by` / `Switch.locks` for switches) rather than requiring a retained teardown row just to rediscover ownership.

Syncing controller state and speed-limit diagnostics back to the train means:

- `TrainController.tick(delta_seconds)` calculates controller-owned runtime values such as `state`, `target_speed`, `current_block_speed_limit`, `clearance_ahead_cm`, `atc_speed_limit`, and `speed_limit_reason`.
- ATC diagnostics are produced inside `TrainController.tick()` through `_calculate_lookahead()`, `_get_current_block_speed_limit()`, `_calculate_clearance_ahead()`, and `_calculate_atc_speed_limit()`.
- `_calculate_atc_speed_limit()` can set reasons such as braking/clearance distance, yellow signal, or block speed limit.
- `SimulationController._update_train()` then copies the UI-facing subset back to the train with `train.state = controller.state`, `train.target_speed = controller.target_speed`, and `train.speed_limit_reason = controller.speed_limit_reason`.
- `TrainController.tick()` also writes `train.target_speed` and `train.speed_limit_reason` directly near the end of the tick, but the simulation sync repeats it so the `Train` object matches the shared controller.

TODO: desired controller/train sync:
- `TrainController.tick(delta_seconds)` should continue to own ATC, ATO, and manual-control runtime calculations, but target speed and speed-limit reason should move into the canonical controller lookahead payload instead of standalone scalars.
- `Train` should keep only physical and persistent state in sync from the simulation loop.
- `PathDispatcher.create_path()` should push a path plan directly into the controller, and path cancellation or completion should clear that controller inbox through `PathDispatcher` as its sole writer rather than through train-side mirrors or controller self-mutation.

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

TODO: desired-model units contract:
- The unit-mixing hazard between `Train.max_braking_rate` (`m/s^2`) and `SpeedProfile.deceleration_rate` (`km/h/s`) persists in the desired model. Resolve it at exactly one boundary: `TrainPhysics` performs the `m/s^2` ↔ `km/h/s` conversion when it reads profile rates, so all controller-facing deceleration math stays in a single unit system. No other layer reinterprets the raw fields.

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
- `TrackSegment` keeps static geometry, classification, and `switch_id`, but it is not the authoritative store for the active switch state. If the segment is a switch, callers resolve the active segment from the owning `Switch.state` in `network.switches` via `switch_id`. `_switch_state` on `TrackSegment` is removed. The old single-owner track-level path lock is not migrated — per-resource `Block.reserved_by_path` / `Block.reserved_for_train` and multi-owner `Switch.locks` play the equivalent role, so there is no transitional phase.

TODO: desired track-segment role and traversal:
- `TrackSegment` should keep static rail geometry, dead-end metadata, cross-links such as `switch_id`, static traffic policy such as `traffic_direction_policy`, temporary routing metadata such as `path_direction`, and a public computed classification property `track_type`.
- `TrackSegment` is not the authoritative store for live switch state or switch locks. Remove `_switch_state`, `locked_by_path`, and `locked_state` outright; the desired model does not keep them. Switch state ownership lives on `Switch`, and lock ownership is expressed through `Switch.locks` (for switches) and `Block.reserved_by_path` / `Block.reserved_for_train` (for blocks).
- If the segment classification says the tile is a switch, `TrackSegment.switch_id` should point to the authoritative `Switch` entity in the network entity database. Live switch state and switch lock ownership should live only on that `Switch` entity.
- `TrackSegment.get_exits(...)`, `TrackSegment.get_entry_for_exit(...)`, and `TrackSegment.get_required_switch_state(...)` should continue to be the traversal helpers used by higher layers, but when the tile is a switch they should resolve the active segment from the referenced `Switch.state` on demand via `switch_id` / `network.switches` rather than reading or mutating a second live state copy stored on the track tile.
- `TrainPhysics._advance_end()` and tail tracing should keep using `TrackSegment` traversal helpers, but those helpers must become switch-aware lookups over the referenced `Switch` entity instead of direct readers of track-stored live state.
- Helper contract in the desired model: `TrackSegment.get_exits(...)`, `TrackSegment.get_entry_for_exit(...)`, and `TrackSegment.get_required_switch_state(...)` accept either the network or an equivalent switch-state resolver so they can read `Switch.state` via `switch_id`. `PathDispatcher` path construction/extension, BFS switch derivation, `TrainPhysics._advance_end()`, tail tracing, and any other traversal callers all go through this resolver contract from day one; switch traversal never reads track-stored live state.

Review anchors (current model only):

- Switch state is global per tile, not per entry edge.
- Diverge-only switches use bit-order selection for state 0/state 1.
- Tail tracing depends on `get_entry_for_exit()` and current switch state. If a switch changes while a train spans it, tail reconstruction can be affected unless the path lock prevents that.

TODO: desired track-segment review anchors:
- Switch-integrity while a train spans a switch: in the desired model, the combination of `Switch.occupied_by` plus the still-held `Switch.locks` entries prevents a switch from flipping out from under the spanning train. A global path lock is no longer required for this guarantee.
- Tail tracing still uses `get_entry_for_exit()`, but it now resolves through the live `Switch.state` per the directed resolver contract at line 299 rather than through a separately persisted track-tile field.

## Switch Entity

`Switch` is metadata for a switch tile:

- Stores switch ID, tile position, main/diverge length and radius, type, name, notes.
- Has its own `locked_by_path` and `locked_state`.
- Reads current physical switch state from the network track tile.
- `request_state_change(new_state, network)` rejects changes when locked and delegates the actual state to the track tile.

Review anchors (current model only):

- There are lock fields on both `TrackSegment` and `Switch`. Dispatcher code primarily locks the `TrackSegment`. Treat track lock as the operational lock unless code is changed to synchronize both.

TODO: desired switch entity:
- `Switch` should become a first-class resource entity at the same level as a block.
- Keep `switch_id: str` as the unique identifier, and keep the tile location fields (`x`, `y`) so the entity remains anchored to the network graph.
- Keep the existing physical/configuration metadata that still belongs to the switch entity: branch-specific geometry/calibration such as `main_length_cm`, `diverge_length_cm`, `main_radius_cm`, `diverge_radius_cm`, plus descriptive metadata such as `switch_type`, `name`, and `notes`.
- Add `state: int` as the authoritative physical switch state. When traversal code needs the active switch segment, it should resolve that state from the `Switch` entity rather than keeping a second authoritative live state copy on `TrackSegment`.
- Add `occupied_by: Optional[str]` as the switch resource's physical-occupancy field. `occupied_by` means a train is physically spanning the switch tile right now; it is set on switch-resource entry and cleared on tail-vacate. Reservation/lock ownership remains separate.
- Replace single-owner `locked_by_path` / `locked_state` fields with `locks`, keyed by the `(train_id, locked_state)` combination.
- `acquire_lock(train_id, state)` should first ensure the physical switch state matches `state`; if the current state differs, it should attempt to change the switch into `state` before recording the lock entry.
- `acquire_lock(train_id, state)` should be idempotent for an already-recorded `(train_id, state)` entry and succeed immediately in that case.
- `acquire_lock(train_id, state)` should allow multiple trains to hold lock entries on the same switch when those entries all require the same physical `state`. A request that would require a different physical state from one already held must fail until the conflicting lock entries are released.
- `acquire_lock(train_id, state)` should also reject a request from the same train when that train already holds the switch in a different `state`. Same-train different-state requests must fail until the old state is released.
- While `occupied_by is not None`, the switch may only gain additional compatible lock entries; lock removal is deferred until the switch tail-vacates. A physically occupied switch can add compatible locks, but it cannot remove locks while a train is still on it.
- `release_lock(train_id)` should remove only entries owned by that train, and it must reject lock-removal requests while `occupied_by is not None`. Normal release happens after tail-vacate.
- In the desired model, `Switch.locks` is a multi-owner lock collection, but physical occupancy remains single-owner: only one train may have `Switch.occupied_by` at a time.
- Add `flank_protection: List[Tuple[str, int]]` on the switch entity and reserve `length_cm: float = 0.0` for future physical switch length.
- Migrate call sites from `track.lock_for_path()` / `track.unlock_for_path()` and `track.is_locked_by_path()` to switch-entity lock methods.

TODO: desired switch review anchors:
- `is_locked()` and `can_lock(state)` should check the `(train_id, state)` keyed lock collection, not a single field. `can_lock(state)` means "is there a compatible lock state for this switch right now," not "is there only one owner."
- Once `Switch.locks` is the sole authority, `TrackSegment` lock fields should be removed rather than "kept in sync".

## Flank Protection Computation

Flank protection is currently carried as path-specific protection data, such as `flank_protection_switches` on `Path`, and acquired by `PathDispatcher` during path creation.

TODO: desired flank protection computation:
- Compute flank-protection relationships at the network level and store them on each switch entity instead of on individual paths.
- Use a global setting such as `FLANK_PROTECTION_DISTANCE_CM = 5` when searching for nearby switches that require mutual locking.
- At switch placement time, search the network graph outward from the switch tile, determine which neighbour states would create a flank conflict, and append `(neighbour_switch_id, blocking_state)` to `flank_protection`.
- Recompute nearby flank-protection relationships whenever topology changes.
- In v1, flank protection uses the same `Switch.locks` compatibility rule as ordinary switch use: multiple trains may hold compatible same-state lock entries, but physical occupancy is still single-owner and no lock set may demand conflicting physical states.

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

Review anchors (current model only):

- `Block.reserve_for_path()` stores `train_id` in `occupied_by` even while state is `RESERVED`.
- `Block.set_to_caution()` clears `occupied_by`, but `BlockManager.set_block_to_caution()` keeps it. These two APIs do not preserve identical data.

TODO: desired block model:
- Replace the stored `state: BlockState` field with three underlying primitive fields and make `state` a computed read-only property:
  - `occupied_by: Optional[str]` — physical train presence (non-None means a train is in the block).
  - `_is_caution: bool` — physical safety-margin flag (True when a train just vacated and the next block ahead is still occupied).
  - `reserved_by_path: Optional[str]` — dispatcher reservation lock (unchanged).
  - `reserved_direction: Optional[str]` — direction reserved by the dispatcher through the block (N/E/S/W).
  - `reserved_for_train: Optional[str]` — the train ID the reservation is intended for; separate from `occupied_by` (physical presence) so that `BlockManager.occupy_block()`'s RESERVED-entry safety check stays effective after `occupied_by` is removed from the RESERVED state.
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
- Because acquisition decisions need to check all blocks at once, `BlockManager` should be the sole mutator and `Block` should remain a data container.

TODO: clarify `CAUTION` vs reservation independence:

- `CAUTION` is an independent safety state indicating the block ahead (in the departing train's direction) is still occupied; it is orthogonal to path reservation.
- A block can be simultaneously `CAUTION` and `reserved_by_path` (path still holds it for a future or in-progress train), or `CAUTION` with no reservation (train has passed and path is done). With the computed-state model both combinations are natively representable — the `state` property returns `CAUTION` in both cases, and `reserved_by_path` can be cleared by a plain field write with no state guard.
- The `OCCUPIED → CAUTION → FREE` lifecycle is driven purely by physical train movement through `BlockManager`; it does not depend on whether a path reservation exists.
- Path reservation (`reserved_by_path`) is released by the dispatcher on path completion or cancellation, as a separate operation from the occupancy state machine.
- `CAUTION` is directional: `caution_direction` records the direction the departing train was moving. The `CAUTION → FREE` check must only look at the adjacent block in `caution_direction`, not at all neighbours. Without this constraint a following train entering the block immediately behind would incorrectly prevent a `CAUTION` block from clearing, because the `OCCUPIED` neighbour check would match the follower rather than the train ahead that triggered the caution.

TODO: desired block methods:

- Remove `Block.can_enter()` from the physical-occupancy path. In the desired model, path reservation checks and physical occupancy are separate concerns: reservation eligibility is checked during `PathDispatcher` / `BlockManager.reserve_blocks_for_path(...)`, while `BlockManager.occupy_block(block_id, train_id)` records physical train entry only.
- Remove `Block.occupy()`. With `BlockManager` as the sole mutator, `Block.occupy()` is dead code — `BlockManager.occupy_block()` writes `block.occupied_by` directly. Keeping `Block.occupy()` would leave a parallel write path that bypasses `BlockManager`'s serialization guarantee.
- Remove `Block.set_to_caution()`. In the new design all `OCCUPIED → CAUTION` transitions are owned by `BlockManager`; callers use `block_manager.on_block_tail_vacated()`. A `Block`-level method that changes occupancy state bypasses the manager's single-serialization guarantee.
- `reserve_for_path(train_id, direction)` (signature changed: `path_id` is removed from the block-level method, both `train_id` and `direction` are required) should stop storing `train_id` in `occupied_by` and instead store it in `reserved_for_train`. `occupied_by` is physical train presence only and must remain `None` while in `RESERVED`. Path membership is recorded separately by `BlockManager.reserve_blocks_for_path(...)` writing `block.reserved_by_path` directly; the block-level reservation field write does not carry `path_id`. Because this method is now a plain field write with no state check, it naturally handles the occupied-start-block case: if the block is already `OCCUPIED` by the assigned train, setting `reserved_by_path` (by `BlockManager`) and `reserved_for_train` (by this method) is additive — `occupied_by` is left unchanged and the computed `state` remains `OCCUPIED`. No special branch for the occupied case is needed.
- `release_path_reservation()` should clear `reserved_by_path`, `reserved_direction`, and `reserved_for_train` only, with no state guard. It must NOT touch `caution_direction` or `_is_caution` — those are physical-lifecycle fields owned exclusively by `BlockManager`. In the computed-state model the method is just a plain field clear, so it works regardless of the current physical state (`OCCUPIED`, `CAUTION`, `RESERVED`, or `FREE`). The old `state == RESERVED` guard is removed because it was the reason OCCUPIED+reserved and CAUTION+reserved blocks had no callable teardown path.
- `Block.reserve_for_path()` and `Block.release_path_reservation()` are **internal helpers** only; all callers go through `BlockManager.reserve_blocks_for_path()` and `BlockManager.release_path_block_reservations()`. They are not part of the public `Block` API.

TODO: desired block review anchors:
- Remove `Block.occupy()` entirely — dead code with `BlockManager` as sole mutator.
- Remove `Block.set_to_caution()` and `Block.clear()` entirely; those physical transitions are owned by `BlockManager`.
- Remove `Block.can_enter()` outright. It is not kept for reservation-read use either; callers that need to check whether a block is reservable go through `BlockManager.reserve_blocks_for_path(...)` and let the acquisition result report failure.
- Remove `Block.is_available_for_path()` outright. Path-acquisition validation lives in `BlockManager.reserve_blocks_for_path(...)`; there is no per-block predicate method in the desired API.
- `Block.reserve_for_path()` and `Block.release_path_reservation()` are kept as internal helpers called only through `BlockManager` methods, not by any other caller.
- Remove the `occupied_by`-while-`RESERVED` behavior from `Block.reserve_for_path()`; use `reserved_for_train` instead.
- `BlockManager.occupy_block()` RESERVED-entry check must switch from `block.occupied_by != train_id` to `block.reserved_for_train != train_id`, because `occupied_by` is `None` in `RESERVED` in the new model.

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

Review anchors (current model only):

- The manager has both staged release (`release_block`) and direct clear (`clear_block`). Callers must choose intentionally.
- `clear_block()` can directly clear `OCCUPIED`, so it should be treated as an override path, not a normal train-vacated path.

TODO: desired block-manager model:
- `BlockManager` should become the database-style lock manager for block resources and the single serialization point for atomic block acquisition, whether the dispatcher is attempting a full-route all-or-nothing reservation or a best-effort prefix reservation.
- Keep `occupy_blocks(block_ids, train_id)` as the existing atomic occupy helper.
- Add `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` to replace one-by-one block reservation with atomic reservation.
- `directions` in `reserve_blocks_for_path(block_ids, path_id, train_id, directions)` should be a mapping `{block_id: direction}` using the path's default start -> end traversal order. `PathDispatcher` owns building that mapping from the chosen path geometry before calling `BlockManager`.
- `reserve_blocks_for_path(...)` must first check eligibility for every block in the candidate block set before writing any fields: a block is eligible when it is FREE, already reserved by the same `path_id`, or physically OCCUPIED by the assigned `train_id` (start-block case). If any block in that candidate set fails eligibility the entire call is rejected and no state changes are made. `PathDispatcher` owns choosing whether that candidate set is the full requested route (`all_or_nothing`) or the longest best-effort prefix it wants to attempt atomically. This removes the `Block.is_available_for_path()` predicate entirely: it checked `occupied_by == train_id` for RESERVED blocks — a condition that is always False once `occupied_by` is removed from RESERVED state — and its responsibilities are absorbed into this manager-level eligibility check.
- `reserve_blocks_for_path(...)` should set `block.reserved_by_path` directly (the `path_id` stays at the manager level, not in the block-method signature) and set `block.reserved_direction` and `block.reserved_for_train` for each block by calling `Block.reserve_for_path(train_id, direction)`. It should allow the assigned train's currently occupied start block to become path-reserved without clearing physical occupancy, because path creation may begin while the train is already in the start block. In the computed-state model, setting `block.reserved_by_path` on an already-occupied start block does not change the computed `state` (still `OCCUPIED`) but records the reservation so it can be cleared later through `release_path_block_reservations`. No special branch for the occupied start block is needed — `reserve_for_path` is a plain field write that works on any physical state.
- Add `release_path_block_reservations(block_ids, path_id, completed=False)` to release reservation ownership; it replaces scattered direct `block.release()` calls during completion and cancellation without modifying physical occupancy state. Because `Block.release_path_reservation()` is now a plain field clear with no state guard, this method can unconditionally call it on every block in the list regardless of whether the block is currently `OCCUPIED`, `CAUTION`, or `RESERVED`. This includes the train's currently occupied start or terminal block when that block was reserved for the same train: reservation ownership and physical occupancy are independent, so an occupied block can still be unreserved safely. When `completed=True` (path completed with train fully stopped at terminal), it additionally force-clears `_is_caution` and `caution_direction` on any still-CAUTION blocks in the list, freeing them immediately so that trailing-train gaps shrink to the terminal block only. When `completed=False` (path cancelled mid-flight), physical CAUTION state is left untouched — the train is still moving and the safety margin must be preserved.
- Remove `set_block_to_caution()` and replace it with `on_block_tail_vacated(block_id, train_id, direction)`: this method clears `block.occupied_by`, sets `block._is_caution = True`, records `block.caution_direction = direction`, then calls `_reevaluate_caution_blocks()` to cascade any blocks that can already advance to `FREE`.
- Treat `on_block_tail_vacated()` as the only normal train-vacated physical transition entry point.
- Keep `release_path_block_reservations(...)` as reservation teardown for path completion and cancellation.
- Keep `release_block()` as a manual/admin reservation-cleanup path only: it may clear `reserved_by_path`, `reserved_direction`, and `reserved_for_train`, but it must not touch `occupied_by`, `_is_caution`, or `caution_direction`. Remove `clear_block()` from the desired model. Manual/admin cleanup must not be part of the routine path-completion flow.
- `BlockManager` should continue to hold a reference to `Network.blocks` rather than maintaining a separate block registry.

TODO: desired `on_block_tail_vacated()` behaviour (directional CAUTION evaluation):

- Legacy direct `TrainPhysics -> BlockManager.on_block_tail_vacated(...)` calls must be removed in the desired model. `TrainPhysics` reports normal tail-vacated resources only through `PathDispatcher.on_resource_tail_vacated_from_physics(...)`, and `PathDispatcher` is the only normal runtime caller that invokes `BlockManager.on_block_tail_vacated(block_id, train_id, direction)` for block resources. `BlockManager` replaces `set_block_to_caution()` with this method.
- `BlockManager` does not independently decide that a block resource should be released; that instruction comes from `PathDispatcher` / physics-driven resource release. `train_id` is passed through for bookkeeping/history and optional diagnostics, not as a second ownership gate that can block the normal physical `OCCUPIED -> CAUTION -> FREE` transition once release has already been instructed.
- `on_block_tail_vacated()` clears `block.occupied_by`, sets `block._is_caution = True`, and sets `block.caution_direction = direction`. With the computed-state model the block's `state` property now returns `CAUTION`.
- After updating the vacated block, `BlockManager` should call `_reevaluate_caution_blocks()` to cascade any blocks that can already advance to `FREE`.
- `_reevaluate_caution_blocks()` iterates all blocks currently in `CAUTION`. In the normal reevaluation path, a `CAUTION` block should advance to `FREE` only when the block immediately adjacent in `block.caution_direction` is NOT `OCCUPIED`. Adjacency is checked in that one direction only; blocks in other directions are ignored. This prevents a following train (entering from behind) from incorrectly keeping a caution block alive — only the forward neighbour (the block the departing train moved into) determines whether the safety margin is still needed.
- When a CAUTION block clears, set `block._is_caution = False` and `block.caution_direction = None`. The block's computed `state` then re-evaluates to `RESERVED` if `reserved_by_path` is still set, or `FREE` if it is not.
- This means the `CAUTION → FREE` transition is driven by the manager on each `on_block_tail_vacated()` call, not by a separate periodic scan or direct `clear_block()` call from physics.
- `reserved_by_path` is NOT checked as a condition for `CAUTION → FREE`. The physical safety margin and the dispatcher reservation are independent: a block can become physically clear of `CAUTION` while still holding a `reserved_by_path` value. In that case the computed `state` becomes `RESERVED`, not `FREE`, until the dispatcher separately clears the reservation via `release_path_block_reservations()`.
- Explicit exception: `BlockManager.release_path_block_reservations(..., completed=True)` may force-clear `_is_caution` and `caution_direction` during terminal completion teardown for owned blocks. That is a completion-time teardown rule, not part of the normal neighbour-based reevaluation path.

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
- Reduce the persisted dispatcher path lifecycle to three states only: `ACTIVE`, `COMPLETED`, and `CANCELLED`.
- Keep the core identity / routing fields on `Path`: `path_id`, `priority`, `start_block_id`, `authority_end_block_id`, optional station metadata, `dispatcher_id`, `train_id`, and `path_tiles`.
- `path_tiles` should remain the ordered physical tile list for the authority, and its default order is start -> end.
- Remove `PENDING`, `SETTING`, `SET`, and `CLEARING` from the desired model. Resource acquisition is synchronous inside `create_path()`, and terminal-approach behavior should be represented by progress/completion predicates rather than a second live status.
- Replace `block_sequence` and `switch_positions` with `resource_sequence`, an ordered list of path-step records interleaving blocks and switches in traversal order.
- Each `resource_sequence` entry should carry at least `sequence_index`, `resource_type`, stable network `resource_id`, the ordered `path_tiles` slice belonging to that resource, and explicit traversal-frame metadata for that resource such as `entry_edge` and `exit_edge` (or an equivalent directed resolver contract). Ordered tiles alone are not enough for same-block / same-tile movement because a single tile can be traversed in either direction and the stop frame changes with the chosen entry side. Those per-resource tile slices plus traversal metadata are the geometry contract for controller/UI consumers after `Train.current_path` is removed. For switch resources, the required switch state is not stored as a fixed field; instead it is derived at acquisition time by calling `TrackSegment.get_required_switch_state(entry_edge, exit_edge, network_or_resolver)` on the switch tile, where the entry and exit edges are computed from the neighboring tiles in that resource's `path_tiles` slice (or the adjacent tiles in `Path.path_tiles` when needed). This helper must resolve live state through the referenced `Switch` entity and must not mutate `_switch_state`. This derivation replaces storing `switch_positions` as a separate dict on the path.
- Replace `current_block_index` with `current_resource_index`, which should track the active `sequence_index`, not the first matching raw resource ID.
- Remove `blocks_cleared`; the desired model does not need a stored `resources_cleared` replacement in v1.
- Remove `flank_protection_switches` from `Path`; flank protection should live on the switch entity and be acquired automatically when the switch is locked.
- Add `stop_position_cm: Optional[float]` as the current authority-end stop target stored on `Path` itself. When the current active authority already reaches the requested waypoint/platform target, `PathDispatcher` uses this field for full-stop completion detection, and the same resolved value is pushed to `TrainController.received_path_plan`. In the desired model, `stop_position_cm` is measured in centimeters from the **authority-entry boundary of the terminal resource in the chosen traversal frame**. For a normal multi-resource authority, that is the approach-side boundary of `authority_end_block_id` in the path's directed traversal. For a same-block / same-tile synthetic authority, that boundary must still be explicit in the path contract; the spec must not rely on readers inferring direction from tile coordinates alone. When a best-effort path is currently truncated short of the requested waypoint, `stop_position_cm = None` and the controller must instead treat the current end of `resource_sequence` as the temporary authority boundary.
- Add an explicit current authority-end mode on `Path`, such as `authority_end_mode = exact_stop | path_end_boundary`. `exact_stop` means the current active authority already reaches a resolved stop point in `authority_end_block_id`; `path_end_boundary` means the path is a best-effort truncated authority whose current end is only the temporary end of the currently acquired resources.
- Keep overlap / approach-lock data and timestamps on `Path` where still needed for teardown, safety policy, and retained-history inspection. Add `teardown_pending: bool` as an explicit non-lifecycle marker for the **immediate authority-teardown phase only**. It covers clearing reservation ownership, clearing controller authority, clearing `active_path_id_by_train[train_id]`, and finishing any dispatcher bookkeeping that belongs to the old authority. Set `teardown_pending = True` immediately when an `ACTIVE` path transitions to `COMPLETED` or `CANCELLED`, and set it back to `False` as soon as that immediate teardown pass is complete. Later physical movement or occupancy updates after authority clear are not part of `teardown_pending`.
- `PathDispatcher` should treat each `Path` record as the authoritative dispatcher-owned database entry, keyed by `path_id` in `paths_by_id[path_id]`, with `active_path_id_by_train[train_id]` holding the one active authority path for that train. The controller inbox is only a pushed projection of that current authority.
- Dispatcher-owned path records should remain available while active and, after termination, as retained `CANCELLED` or `COMPLETED` records for history/inspection. Cleanup/retention policy may be separate, but authority teardown must not depend on the controller inbox surviving.
- `PathDispatcher`'s retained path database should be the authoritative historical store for every path it has created. After `active_path_id_by_train[train_id]` has been cleared, active progress callbacks resolve only against the new active authority, while delayed release or occupancy reporting for a block or switch that remains under the train resolves from the resource itself (`BlockManager` state for blocks, `Switch.occupied_by` / `Switch.locks` for switches). A same-train replacement path is blocked only while `teardown_pending == True`; once the immediate teardown pass finishes, retained history rows no longer block replacement path creation and only ordinary live resource conflicts remain.
- In the desired model, `train_id` is mandatory for every dispatcher authority path, including route-driven, headless, and manually requested paths. Frontend/API callers must be updated to supply it; anonymous authority paths are out of scope for this model. Path membership on reserved resources is tracked through `Block.reserved_by_path` (written by `BlockManager.reserve_blocks_for_path(...)`) and `Switch.locks` (keyed by `train_id`, with dispatcher bookkeeping resolving which active path currently owns that train's locks); the block-level `Block.reserve_for_path(train_id, direction)` method itself no longer takes `path_id`.

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
2. Add a canonical dispatcher-owned helper such as `PathDispatcher.resolve_stop_target(requested_destination_block_id, travel_direction, path_geometry=None) -> stop_position_cm`.
3. `PathDispatcher.create_path(...)` should take `start_block_id`, `requested_destination_block_id`, `train_id`, `requested_destination_direction`, optional `requested_stop_position_cm`, and `acquisition_mode`; it should not require a second opaque waypoint/platform stop-context payload.
4. `PathDispatcher.create_path(...)` should also take an explicit `acquisition_mode` parameter of type `PathDispatcher.AcquisitionMode`, an enum owned by `PathDispatcher` with v1 values `all_or_nothing` and `best_effort`. The enum lives in `PathDispatcher` because `PathDispatcher` owns path calculation and resource acquisition; this mirrors how `DepartureMode` is owned by `RouteExecutor`.
5. Candidate search must become direction-aware. `find_paths(...)` or the equivalent dispatcher pathfinder entry point must reject candidates that reach the terminal resource in the wrong traversal direction, and same-block / same-tile synthetic path construction must use `requested_destination_direction` to decide which side of the resource is the authority-entry boundary. If route/platform logic cannot determine a unique terminal direction for the current waypoint, path creation must fail rather than guessing.
6. When route/platform logic already knows the exact stop target, `RouteExecutor` should pass that exact `requested_stop_position_cm` into `PathDispatcher.create_path(...)`, including same-block repositioning and any other platform stop where the route plan wants a precise marker inside the destination block.
7. If the caller does not supply `requested_stop_position_cm`, `PathDispatcher` should resolve it through `resolve_stop_target(...)` from `requested_destination_block_id` plus `requested_destination_direction` and dispatcher-owned terminal safety policy (global safety-margin fallback in v1) before the path is finalized. The resolved value is always stored in the terminal resource's directed traversal frame defined above.
8. Allow `create_path(start_block_id == requested_destination_block_id, ...)` for same-block repositioning. Argument naming is consistent throughout the desired model: `requested_destination_block_id` is the caller's requested final target passed into `create_path(...)` / `extend_path(...)`, and `authority_end_block_id` is the current live-authority end stored on the `Path`.
9. For same-block repositioning, `RouteExecutor` still passes `requested_destination_block_id`, `requested_destination_direction`, optional exact `requested_stop_position_cm`, and `train_id`. `PathDispatcher.create_path(...)` owns deciding what tiles/resources must be assigned based on the train's current occupied span and the destination stop target. If the required movement stays on ordinary track tiles inside one block, dispatcher may build a synthetic local path with one block resource whose `path_tiles` slice covers the local movement. If that movement stays inside a single tile, dispatcher must still use `requested_destination_direction` to choose the traversal frame and must reject the request if the direction is ambiguous. If route/platform logic has an explicit stop window, it should pass `requested_stop_position_cm`; otherwise the dispatcher fallback rule above applies.
10. In `all_or_nothing` mode, if the full requested candidate path cannot be acquired, roll back and return failure; no live path is created.
11. In `best_effort` mode, if the full requested candidate cannot be acquired, `PathDispatcher` should truncate the authority to the furthest forward resource prefix it can acquire on that candidate and still create one live path. That truncated live path keeps the same `path_id`, sets `authority_end_block_id` to the last acquired block in the current authority, sets `stop_position_cm = None`, and sets `authority_end_mode = path_end_boundary`.
12. Once the chosen acquisition result succeeds, create the dispatcher-owned path record directly in `ACTIVE` state and push the controller path plan immediately. There should be no persisted intermediate `SET` state in the desired model.
13. `PathDispatcher.create_path(...)` seeds `current_resource_index` from the train's already-occupied resource span for every occupied-start case, not only the one-resource synthetic case. As a worked example: for same-block repositioning, if dispatcher builds that one-resource synthetic move, initialize the path's progress index to that sole resource. No extra movement-start guard is needed: if the train is already stopped inside the waypoint stop window and the route/waypoint already defines the exact stop target, `RouteExecutor` may bypass path creation entirely; otherwise completion still requires a later full stop at the requested `stop_position_cm`, so same-block movement still goes through ordinary `create_path(...)`. If the train's current span or required movement would still include another resource (for example a switch that the train continues to cover), `PathDispatcher.create_path(...)` should expand the assigned tiles/resources itself and build a normal multi-resource authority from the train's current occupied span to the target stop point; `RouteExecutor` does not compute or pass those tiles directly.
14. Build `resource_sequence` in traversal order so both blocks and switches are tracked in one sequence.
15. Store the current `authority_end_mode` and the corresponding current `stop_position_cm` (or `None` for a truncated best-effort authority) on the `Path`.
16. Before activating the new path, reject the request if any retained row for the same `train_id` still has `teardown_pending == True`.
17. Store the new dispatcher-owned path record in `paths_by_id[path_id]` and set `active_path_id_by_train[train_id] = path_id`.
18. Do not copy reserved-block state onto `Train`; reserved resources remain dispatcher-owned on the `Path` record and in the controller-facing authority projection.
19. Push a path plan to the controller after successful creation so the controller receives `path_id`, `resource_sequence`, current `authority_end_block_id`, current `stop_position_cm`, and current `authority_end_mode`. Each pushed `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata.
20. The pushed plan must preserve an explicit traversal frame. `stop_position_cm` is interpreted in that stored frame; readers must not try to recover it later from tile coordinates alone.

Resource acquisition:

- Reserve every block with `Block.reserve_for_path(path_id, train_id, direction)`.
- Set `track.path_direction` for path tiles.
- Lock required switches by calling `track.lock_for_path(path_id, state)`.
- Lock flank-protection switches when available.
- If any acquisition fails, reserved blocks, path directions, and switch locks are rolled back.

TODO: desired resource acquisition:
- Acquire switches first, then blocks, so switch conflicts fail before any blocks are reserved.
- For each switch in path order, call `switch.acquire_lock(train_id, required_state)` and immediately acquire any `switch.flank_protection` locks.
- Acquire blocks atomically through `BlockManager.reserve_blocks_for_path(...)`.
- Resource acquisition should also set `track.path_direction` for the tiles owned by the path.
- Distinguish intrinsic traffic policy from temporary reservation direction: a track's static `traffic_direction_policy` may be bidirectional or fixed one-way, but `track.path_direction` is the temporary direction reserved by the active path, not the track's permanent traffic policy.
- A one-way track may only be reserved in the direction allowed by `traffic_direction_policy`; a bidirectional track may be reserved in either direction.
- On failure, roll back in reverse acquisition order, including switch locks, block reservations, and any `path_direction` assignments.
- Replace direct `block.release()` calls in completion and cancellation with `BlockManager.release_path_block_reservations(...)`.
- In v1, block reservations remain single-train/single-path resources even though `Switch.locks` can have multiple compatible owners. `track.path_direction` therefore still reflects only one active path's temporary reservation direction on a tile and should be cleared when that owning path releases the tile.
- `create_path(..., acquisition_mode: PathDispatcher.AcquisitionMode)` and `extend_path(..., acquisition_mode: PathDispatcher.AcquisitionMode)` must share the same `PathDispatcher.AcquisitionMode` enum, with two v1 behaviors:
  - `AcquisitionMode.all_or_nothing`: dispatcher acquires only the full requested authority or returns failure after full rollback
  - `AcquisitionMode.best_effort`: dispatcher may truncate to the furthest forward acquirable resource prefix, but the acquisition of that chosen prefix is still atomic
- In `best_effort` mode, if no forward resource beyond the train's currently covered authority can be added, do not create a second fallback path and do not replace the current one; leave the existing path unchanged and report "no extension yet" to the caller.

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

Review anchors (current model only):

- The head-tile-equals-last-path-tile condition (second completion trigger above) does **not** require the train to be stopped. *(TODO: superseded by desired physics-driven progress below — this trigger is replaced by `_complete_path_on_full_stop`, which requires a full stop.)*
- `_complete_path_on_head_arrival` accesses the controller indirectly via `network.power_controller._sim_controller.get_controller()` — a circular reference through `BlockPowerController`. *(TODO: superseded by desired physics-driven progress below — this chain is removed once `_complete_path_on_full_stop` calls `controller.on_path_complete()` directly.)*
- `train.ato_destination_block_id` is cleared inside `_complete_path_on_head_arrival` directly. *(TODO: superseded by desired physics-driven progress below — `train.ato_destination_block_id` is removed from `Train`; destination state lives in the controller path plan.)*
- `_complete_path_on_head_arrival` is the current path-completion method. *(TODO: superseded by desired physics-driven progress below — replaced by `_complete_path_on_full_stop`.)*

TODO: desired physics-driven progress:
- `TrainPhysics` should detect newly entered blocks, newly entered switches, tail-vacated blocks, and tail-vacated switches.
- The runtime should use one shared `PathDispatcher` instance for all trains. `TrainPhysics` should report ordered resource-entry transitions directly to `PathDispatcher.on_train_advances_from_physics(train_id, resource_type, resource_id)`, letting that shared dispatcher resolve the active path from `active_path_id_by_train`. The `resource_type` argument must be carried on every resource-event callback (entry, tail-vacate, and any other resource-release event), not just the entry callback, so dispatcher never has to infer type from a bare ID. Delayed release of a block or switch that remains physically occupied after authority teardown resolves from the resource itself, not from a retained teardown record.
- After movement, occupancy reconciliation, and tail-vacate reporting inside each `TrainPhysics.tick(...)`, `TrainPhysics` should call `PathDispatcher.on_train_progress_from_physics(train_id)` once for that train even if no new resource was entered on that tick, so full-stop completion can be evaluated without a simulation-owned forwarding pass.
- Dispatcher progress should update `current_resource_index` using the path-step `sequence_index`, not by searching for the first matching raw `resource_id`.
- A live path should remain `ACTIVE` after creation, through intermediate resources, and after entering the final block/resource. Entering the terminal resource should not change lifecycle state by itself.
- Completion should be performed by `_complete_path_on_full_stop(path, train)` only when all of the following are true:
  - `path.status == ACTIVE`
  - `path.authority_end_mode == exact_stop`
  - the train is fully stopped within `5 cm` of the resolved `stop_position_cm` for the target waypoint / platform stop, not merely somewhere in `authority_end_block_id`
  - `current_resource_index` shows the train has reached the terminal resource
- A best-effort truncated path with `authority_end_mode == path_end_boundary` must not complete just because the train has stopped at the current authority boundary. In that case the same path remains `ACTIVE`, the controller may enter `RUNNING_WAITING` under the still-live authority, and `RouteExecutor` should keep trying `extend_path()` on that same `path_id`.
- Completed paths should release block reservations for all owned blocks through `BlockManager.release_path_block_reservations(..., completed=True)` immediately, even if the terminal block is still physically occupied by the train at the moment of completion, because reservation ownership and physical occupancy are separate concerns. That reservation teardown may force-clear terminal `CAUTION`, but it must not force-clear any physical `OCCUPIED` block early. On this `ACTIVE -> COMPLETED` transition, dispatcher should set `path.teardown_pending = True`, release any switch locks and flank-protection locks whose `Switch.occupied_by` is already `None`, keep any switch lock whose `Switch.occupied_by == train_id` in place until its later tail-vacate callback, clear any remaining path directions that are no longer owned, call `controller.on_path_complete()`, clear controller authority by calling `controller.receive_path_plan(None)`, clear `active_path_id_by_train[train_id]`, and then set `path.teardown_pending = False` once that immediate teardown pass is finished. The retained record may remain in `paths_by_id` for history, but later physical release of a switch or block still under the train is not part of `teardown_pending` and resolves from the resource itself. When that later tail-vacate callback finally fires, it may clear both the physical occupancy fact and any reservation / lock residue that was intentionally kept until physical release. `RouteExecutor` treats the dispatcher callback as authoritative completion and does not re-check exact stop position itself.
- Remove `_record_completion_event()`, `completed_path_events`, and `get_and_clear_completion_events()`.
- When creating or extending a path, set the current authority-end contract on the `Path` first, then include that same contract in the controller path plan:
  - if `authority_end_mode == exact_stop`, populate `stop_position_cm` so the controller brakes to the exact stop point
  - if `authority_end_mode == path_end_boundary`, keep `stop_position_cm = None` so the controller brakes to the temporary current end of `resource_sequence`
- Drive resource release from tail-vacate detection in `TrainPhysics`, but let `TrainPhysics` report each vacated resource directly to `PathDispatcher` as a typed resource-release event. `PathDispatcher` should inspect `resource_type` and release it accordingly:
  - vacated blocks go through `BlockManager.on_block_tail_vacated(...)`
  - vacated switches go through the `Switch` entity after `PathDispatcher` resolves the owning `train_id` for that `switch_id` from dispatcher / switch state (`release_lock(train_id)` plus any flank-protection teardown)
- Route any normal block release through `block_manager.on_block_tail_vacated()` so `OCCUPIED -> CAUTION -> FREE` remains a single-manager state machine. Manual/admin cleanup stays out of the routine completion flow; in the desired model that cleanup uses `release_block()` only because `clear_block()` is removed.

Path extension:

- `extend_path(path_id, new_end_block_id)` only extends forward.
- Rejects extension to a block already in the path.
- Finds extension paths from current end to the new block.
- Validates and acquires the extension resources.
- Appends new tiles and new blocks, skipping duplicated connection/start entries.

TODO: desired path extension:
- Keep forward-only extension.
- When the path model switches to `resource_sequence`, append new tiles and new resources while skipping duplicated connection/start entries.
- Reject any extension that would cause a resource to appear twice in the combined path.
- `PathDispatcher.extend_path(...)` should take the same argument contract as `create_path(...)`, except that it replaces `start_block_id` with `path_id`: `path_id`, `requested_destination_block_id`, `requested_destination_direction`, optional `requested_stop_position_cm`, and `acquisition_mode`. The live path's current `authority_end_block_id` is only the current authority end; it is not enough to recover the caller's final waypoint target.
- In `all_or_nothing` mode, `extend_path(...)` should either acquire the full requested suffix to `requested_destination_block_id` or leave the existing path unchanged.
- In `best_effort` mode, `extend_path(...)` should keep the same `path_id` and append the furthest forward acquirable suffix it can add to the current path. The `resource_sequence` therefore grows in place on the same live path instead of creating a replacement path.
- While the path is still truncated short of the caller's requested final target, keep `authority_end_block_id` set to the current last acquired block, keep `stop_position_cm = None`, and keep `authority_end_mode = path_end_boundary`.
- Once `extend_path(...)` finally reaches the requested final target block, recompute the resolved exact `stop_position_cm` from `requested_destination_block_id`, `requested_destination_direction`, and optional `requested_stop_position_cm`, set `authority_end_mode = exact_stop`, update the dispatcher-owned `Path` record, and repush the refreshed path plan through `controller.receive_path_plan(...)` if that path is still the train's active authority.
- In `best_effort` mode, if an extension attempt cannot add any new resource at all on that tick, it should be a no-op on the existing path rather than a failure that tears down or replaces the current authority.

Cancellation:

- `cancel_path(path_id, force=False)` marks status `CANCELLED`.
- Non-approach-locked blocks are cleared unless physically occupied by the train.
- Switches are released unless physically under the train or in an approach-locked block.
- Path directions are cleared only if all approach locks are released or `force=True`.
- Path remains in `active_paths` if approach-locked resources remain.

TODO: desired cancellation:
- Release owned blocks through `BlockManager.release_path_block_reservations(...)` instead of direct `block.release()` calls. In the desired v1 model, cancellation teardown clears reservation ownership only; it never clears physical occupancy.
- Path cancellation or revocation is owned entirely by `PathDispatcher`. Cancelling a live path should clear controller authority by calling `controller.receive_path_plan(None)` and should not be synthesized by `RouteExecutor` or `TrainController`.
- On the `ACTIVE -> CANCELLED` transition, `PathDispatcher` should set `path.teardown_pending = True`, clear block reservation ownership, clear controller authority, and clear `active_path_id_by_train[train_id]`. A block reservation may be cleared even when the train still physically occupies that block; reservation ownership and physical occupancy are independent. Switch-lock teardown is different: if `Switch.occupied_by is not None`, dispatcher must keep the existing switch-lock entries in place until the later tail-vacate callback. A physically occupied switch can add compatible locks, but it cannot remove locks while a train is still on it. Once the immediate teardown pass is complete, set `path.teardown_pending = False`; at that point a same-train replacement path is allowed, subject to ordinary live resource conflicts from any still-occupied blocks or switches.
- Reservation / physical state ownership in v1 should be read literally:
  - dispatcher-owned reservation state: `Block.reserved_by_path`, `Block.reserved_for_train`, `Block.reserved_direction`, `Switch.locks`, flank-protection lock ownership
  - physics-owned physical state: `Block.occupied_by`, `Block._is_caution`, `Block.caution_direction`
  - dispatcher-mediated physical state, written by `PathDispatcher` on physics-reported resource-entry / tail-vacate events rather than directly by `TrainPhysics`: `Switch.occupied_by`
  - a block or switch has at most one physical `occupied_by` train at a time (single-train physical ownership). A single train may hold multiple path records layered over that physical ownership; path membership is tracked separately by `PathDispatcher` on `Path` records, not by duplicating `occupied_by` per path.
  - cancellation / completion teardown may clear block reservation state immediately, but it may clear switch-lock state only for switches that are no longer physically occupied
  - only physical entry / tail-vacate callbacks may clear or set physics-owned or dispatcher-mediated physical occupancy state
  - if any reservation / lock residue was intentionally retained until physical release, the later tail-vacate callback may clear that residue together with the physical occupancy update
- Once `received_path_plan` is cleared while the train is moving, the essential event is "authority removed." `PathDispatcher` does not set EB directly. On the next `TrainController.tick()`, the controller rebuilds lookahead with no live authority, the ATO / authority-end allowed speed at distance `0` collapses to `0`, and the controller's existing braking / EB logic escalates that no-authority condition into EB. The train then stops and remains in that stopped emergency-brake state until a manual `release_emergency_cmd()` transitions it to `STANDBY`. This is distinct from ordinary `RUNNING_WAITING`, which requires the same path to still exist while clearance inside that live authority has dropped to zero. While the train is braking under EB, `TrainPhysics` still advances it according to speed and the current live network state. Any later block/switch occupancy or release callbacks are still reported normally, but cancellation does not authorize a physically occupied switch to drop its lock early.
- After cancelling a live path, `PathDispatcher` should notify `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)`. This notification is mandatory but informational: `RouteExecutor` should inspect whether that cancelled path actually affects its current waypoint / assigned route state and must not treat the old path as finished.

Review anchors (current model only):

- A newly created path is `SET`, but normal physics progress can update it even if it never transitions to `ACTIVE`. *(TODO: superseded by desired path review anchors below — the desired model creates paths directly in `ACTIVE` and removes `SET`.)*
- Some resource release paths call `clear_block()` directly, so check whether those cases can encounter physically occupied blocks. *(TODO: superseded by desired physics-driven progress and cancellation/completion TODOs below — routine release moves to manager-owned tail-vacate plus reservation-only teardown.)*
- `current_block_index` uses the first index of the entered block in `block_sequence`; repeated block IDs in a complex path would need careful review. *(TODO: superseded by desired path review anchors below — `current_resource_index` replaces `current_block_index`, and repeated resources are rejected.)*

TODO: desired path review anchors:
- In the desired model, old `PENDING` and `SETTING` disappear entirely because acquisition is synchronous inside `create_path()`, and old `SET`, old `ACTIVE`, and old `CLEARING` collapse into a single live dispatcher state: `ACTIVE`. Whether the train has started moving, entered the terminal resource, or is stopped at the stop point should be represented by controller state, `current_resource_index`, and explicit completion predicates, not by separate path lifecycle values.
- `resource_sequence` replaces both `block_sequence` and `switch_positions`, so existing callers iterating those fields need updating.
- `current_resource_index` should track `sequence_index`, not the first occurrence of a raw resource ID.
- For v1, repeated resources in a single path are invalid and should be rejected during both `create_path()` and `extend_path()`.
- Switch zero-length means any calculations that still use switch physical length remain unchanged; switches in `resource_sequence` simply contribute `0 cm`. In the desired v1 cancellation flow, overlap / approach-lock data may be retained for history and future policy, but they do not block reservation teardown unless a later TODO explicitly reintroduces that behavior.
- `FLANK_PROTECTION_DISTANCE_CM` should be a global config, not a per-path setting.
- In v1, switch locks are a multi-owner compatibility set: multiple trains may hold compatible same-state lock entries on the same switch, but conflicting required states are not allowed and physical occupancy stays single-owner.
- Remove `PathDispatcher._release_blocks_behind_train()` in the desired model. Once block release is fully owned by `BlockManager.on_block_tail_vacated()` triggered from physics, the dispatcher no longer needs a separate behind-train sweep.

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

TODO: desired route data:
- Keep `waypoints`, `mode`, `assigned_train_ids`, and the route-level runtime index/direction fields.
- Add `default_departure_mode` with two v1 values: `auto` and `manual`. This is the ordinary route-level default for whether `RouteExecutor` automatically issues `controller.depart()` once authority exists and the next leg is ready.
- Add `auto_depart_after_eb: bool = False` at route level. This is a separate override for post-EB restart only: when a train returns to `STANDBY` after `release_emergency_cmd()`, `auto_depart_after_eb = False` suppresses automatic restart even if the ordinary departure mode is `auto`.

TODO: desired waypoint data:
- Keep platform targeting, dwell/timing rules, platform-selection flexibility, and direction policy.
- Add explicit stop-policy data used by route arrival and same-block repositioning:
  - the resolved destination block for the waypoint
  - an optional exact stop target such as `stop_position_cm` when the route/platform plan defines one directly
- Add an explicit waypoint traversal / arrival direction contract, such as `requested_destination_direction`, for every waypoint whose stop frame depends on approach side. Same-block / same-tile repositioning must not guess direction from geometry alone; `RouteExecutor` must either resolve this direction from the route/platform definition or treat the waypoint as ambiguous and fail path creation.
- Add optional `departure_mode` as a per-waypoint override of `Route.default_departure_mode`. When unset, the waypoint inherits the route default.
- Use one global stop-window tolerance of `5 cm` around the resolved `stop_position_cm`; do not carry a second per-waypoint tolerance field in v1.
- When the route does not provide an exact `stop_position_cm`, `PathDispatcher` should resolve one from `requested_destination_block_id` plus `requested_destination_direction` and its own global safety-margin fallback.
- Waypoint arrival / completion is not simply "the train is in the waypoint block". The target condition is "the train is associated with the resolved waypoint target and is fully stopped within `5 cm` of the resolved `stop_position_cm` for that stop."
- The waypoint stop-window definition should be the single source for both the no-movement already-arrived bypass and the `requested_stop_position_cm` used for a normal or synthetic dispatcher path to that waypoint. `PathDispatcher.resolve_stop_target(requested_destination_block_id, travel_direction, path_geometry=None)` is the canonical fallback resolver when the route does not provide an exact stop. Route-defined exact stops override that fallback.
- Add one shared direction-aware conversion helper for the bypass predicate and same-block stop-window checks: given the train's live position plus the destination block, compute the train's current stop-frame position in the same destination-block frame used by `stop_position_cm`. The no-movement bypass is only valid when the route/waypoint already defines the exact stop target; otherwise `RouteExecutor` should request an ordinary same-block `create_path(...)` and let `PathDispatcher` resolve the stop target internally.

`RouteExecutionState`:

- Tracks the current waypoint index and route direction.
- Stores resolved runtime platform data for the current waypoint.
- Stores arrival/dwell timing for the current waypoint.
- Stores retry/waiting state when path creation has not succeeded yet.

TODO: desired route-execution state:
- Keep `current_waypoint_index`, `direction`, `resolved_platform_id`, `arrived_at_platform_time`, and `last_waypoint_platform_id`.
- Keep a pre-departure retry flag such as `waiting_for_path` only for "path request failed before movement began" handling.
- Set `waiting_for_path = True` only when pre-departure platform lookup, current-block lookup, or dispatcher path creation fails before any authority exists.
- Do not use `waiting_for_path` for a live best-effort truncated path. That case still has active authority; the route should keep the same path alive and retry `extend_path()` on it rather than dropping into the pre-authority failure bucket.
- Clear `waiting_for_path` on successful path creation, on the same-block already-arrived bypass, on authoritative path completion, and when `RouteExecutor.handle_path_cancellation(...)` consumes a cancellation notification for the route's current live path.
- Do not add a separate cancellation-recovery sub-state for v1. When a live path is cancelled, `PathDispatcher` clears controller authority, the controller performs the EB-stop, and the train remains blocked in that stopped emergency-brake state until a manual `release_emergency_cmd()` transitions it back to `STANDBY` and latches a controller-owned `post_eb_restart_pending` flag; only after that manual release should ordinary `RouteExecutor` reachability + departure logic for the same waypoint run again from that fresh stopped state.
- Do not add a separate retained-authority standby sub-state. When a train is in `STANDBY` and `received_path_plan` still exists, `RouteExecutor` should treat that as "authority retained, awaiting fresh departure decision" and resolve it through ordinary departure policy rather than through a new route-execution state enum. If the stop was caused by EB, the train must remain in `STANDBY` until a fresh explicit departure decision; it must not auto-resume from retained authority.

`RouteExecutor.update()`:

1. Collect active route/train pairs.
2. Create or retrieve a per-train `RouteExecutionState`.
3. Resolve target platform for the current waypoint.
4. If train is `STANDBY` or `IDLE`, request path and depart.
5. If train is `RUNNING_MOVING`, check waypoint arrival.
6. If train is `RUNNING_AT_STATION`, check dwell completion.
7. If train is `RUNNING_WAITING`, treat the route as on hold under the existing path rather than retrying path creation.
8. Manual trains only get arrival checks.

TODO: desired route-executor state reads:
- `RouteExecutor` should read live operational state from `TrainController`, not from mirrored runtime fields on `Train`.
- Departure, retry, arrival, dwell, and waiting decisions should use controller state as the operational source of truth.
- `Train` may still expose persisted or UI-facing state, but route-control decisions should not depend on it being a live mirror.
- In the desired model, manual driving is `control_mode == manual`, not a separate operational state. A route-managed manual train still uses ordinary `STANDBY` / `IDLE` / `RUNNING_*` states and dispatcher authority; the difference from ATO is how movement begins and is driven, not whether `RouteExecutor` owns the waypoint plan.

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

TODO: desired concrete `RouteExecutor.update(delta_time)` calls:
- `update(delta_time)` should continue to increment `RouteExecutor.simulation_time`, scan active route/train pairs, and call `_execute_route_for_train(train, route)`.
- `_execute_route_for_train()` should continue to create or reuse `RouteExecutionState`, resolve the current waypoint, and store the resolved platform in `exec_state`.
- Before any create/extend decision, `_execute_route_for_train()` should resolve the per-tick waypoint authority contract it owns: the waypoint target block and `requested_destination_direction`. It must NOT resolve `stop_position_cm` itself. `PathDispatcher` is the single owner of stop-position resolution: `RouteExecutor` passes the route/waypoint-defined numeric `requested_stop_position_cm` when the route plan has one, and passes `None` otherwise. Dispatcher resolves the fallback from `requested_destination_block_id`, `requested_destination_direction`, and `path_geometry` inside `create_path(...)` / `extend_path(...)` via `PathDispatcher.resolve_stop_target(...)`.
- `_execute_route_for_train()` should ensure the train's `TrainController` exists before it reads controller state or calls `PathDispatcher.create_path(...)`. In the desired model a route-managed train should already have a controller by the time authority is assigned in Phase 1. `SimulationController` owns only the 1:1 binding between `train_id` and `TrainController`; it does not construct or push path plans.
- The operational state checks inside `_execute_route_for_train()` should read controller state rather than `train.state`.
- If controller state is `STANDBY` or `IDLE`, `_execute_route_for_train()` should call `_try_depart_to_waypoint(train, route, exec_state)`.
- If controller state is `RUNNING_MOVING`, `_execute_route_for_train()` should not poll for arrival; normal arrival now comes only from dispatcher-authoritative path completion.
- If controller state is `RUNNING_AT_STATION`, `_execute_route_for_train()` should call `_check_dwell_complete(train, route, exec_state)`.
- If controller state is `RUNNING_WAITING`, `_execute_route_for_train()` should treat the route as on hold and should not request a brand-new path while `received_path_plan` is still present. If that still-live path is a best-effort truncated authority whose current `authority_end_block_id` is short of the waypoint target, `_execute_route_for_train()` should keep retrying `PathDispatcher.extend_path(path_id=existing_path_id, requested_destination_block_id=waypoint_target_block_id, requested_destination_direction=waypoint.requested_destination_direction, requested_stop_position_cm=waypoint.stop_position_cm, acquisition_mode=best_effort)` on that same path.
- `RouteExecutor` does not call `PathDispatcher.resolve_stop_target(...)` directly at any point; that helper is dispatcher-internal. Route callers pass a route-defined numeric `requested_stop_position_cm` when they have one, or `None` to let dispatcher resolve the fallback.
- `_try_depart_to_waypoint()` is the route executor's normal "make progress toward the current waypoint" entry point. The same function should be used for the initial departure attempt, for `STANDBY` with retained authority, and for retries after `PathDispatcher` has cancelled/revoked the previous path, the train has stopped under EB, and a manual release has returned the controller to `STANDBY` or `IDLE`; active-path completion still comes back from `PathDispatcher`, not from `_execute_route_for_train()` polling for end-of-path.
- `RouteExecutor` should resolve one effective departure policy for the current waypoint each time it considers departure:
  - start from `Route.default_departure_mode`
  - override with `Waypoint.departure_mode` when present
  - if `TrainController.post_eb_restart_pending` is true, apply `Route.auto_depart_after_eb` as the separate final gate

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
- If the train is already stopped within the waypoint stop window in the target block, do not call `controller.depart()`. Treat the waypoint as already arrived and call the same arrival/dwell entry path used after normal path completion. This bypass is only valid when the route/waypoint already defines the exact stop target locally.
- If the train is in the target block but not yet at the waypoint stop window, request a synthetic same-block path with an explicit `stop_position_cm`. ATO must not move the train within the same block without a dispatcher plan.
- `RouteExecutor` should not resolve stop targets at all. For path creation/extension, it passes through whatever numeric `requested_stop_position_cm` the route/waypoint defines, or `None` when it has no exact value. `PathDispatcher` is the sole caller of `PathDispatcher.resolve_stop_target(...)`: it does the fallback resolution inside `create_path(...)` and `extend_path(...)` and stores the result on `Path.stop_position_cm`. There is no special same-block dispatcher entrypoint; same-block movement still uses ordinary `create_path(...)`. The route/waypoint resolution step must still provide `requested_destination_direction`; dispatcher must not guess it from same-block geometry.
- `RouteExecutor` should also choose one `PathDispatcher.AcquisitionMode` value for the current waypoint attempt and pass that same value into both `create_path(...)` and any later `extend_path(...)` retries while it is still pursuing that waypoint. The per-waypoint default lives in `RouteExecutor` (not in `Route` or `Waypoint`), analogous to how `RouteExecutor` owns `DepartureMode`; v1 default is `AcquisitionMode.all_or_nothing`, and `AcquisitionMode.best_effort` is selected only when a named `RouteExecutor` policy applies.
- If `TrainController.received_path_plan` already exists and still matches the current waypoint authority, `_try_depart_to_waypoint()` must not request a replacement path. That is the retained-authority standby case: reuse the existing authority and decide only whether to issue a fresh `controller.depart()` now or leave the train in `STANDBY` awaiting manual confirmation. `stop_position_cm` is interpreted in the existing path plan's stored traversal frame, carried by the resource metadata such as `entry_edge` / `exit_edge`; retained-authority matching must reuse that same frame rather than treating `stop_position_cm` as direction-free.
- If `create_path(..., acquisition_mode=best_effort)` returns a live path whose current `authority_end_block_id` is still short of the waypoint target block, that is not a failure. It is a degraded but valid live authority: let the controller treat it as normal movement authority toward the current path end, and let `RouteExecutor` keep retrying `extend_path()` on the same `path_id` toward the waypoint target on later ticks, passing the same requested final-target contract each time (`requested_destination_block_id`, `requested_destination_direction`, and `requested_stop_position_cm` — the route/waypoint-defined numeric value or `None`; `PathDispatcher` resolves the fallback).
- On successful path, do not write train-side path cache fields from `RouteExecutor`. `PathDispatcher` owns the authoritative path-progress view; any controller/UI-facing path tiles or path-progress reads should be pushed/read as a projection of dispatcher state rather than as a train mirror.
- Do not set destination-block fields directly on `Train` or `TrainController` from `RouteExecutor`; controller authority comes only from the pushed path plan.
- `PathDispatcher` owns `controller.received_path_plan`; `controller.assigned_path_id` should be only a derived read of `received_path_plan.path_id`, so `RouteExecutor` does not write that field.
- Deliver `path_id`, `resource_sequence`, current `authority_end_block_id`, current `stop_position_cm`, and `authority_end_mode` inside the controller path plan pushed by `PathDispatcher`. Each `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata.
- Keep `controller.depart()` as the way movement begins once a valid path plan exists, whether that plan was just created or retained across an EB/manual stop.
- Ordinary departure policy (`auto` vs `manual`) controls whether `RouteExecutor` issues `controller.depart()` automatically after authority exists. `Route.auto_depart_after_eb` separately controls the post-EB retained-authority restart case and defaults to `false`.

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
- Every `RouteExecutor` access to a train's controller goes through `SimulationController.get_or_create_controller(train)` (the shared public accessor) rather than a private `_get_controller` helper or any direct dictionary lookup.
- If current block already equals target block and the train is already stopped inside the waypoint stop window, bypass `controller.depart()` and enter `RouteExecutor._enter_waypoint_arrival(train, route, exec_state)`. This bypass is only allowed when the route/waypoint already defines the exact stop target; otherwise same-block handling should request an ordinary same-block path.
- If current block already equals target block but the train is not yet inside the waypoint stop window, call `path_dispatcher.create_path(...)` with the same start and end block, `train_id`, `requested_destination_direction`, and `requested_stop_position_cm` for a synthetic local path. There is no same-block ATO movement without dispatcher authority.
- For that decision, `RouteExecutor` does not compute a resolved stop position locally. It passes the route/waypoint-defined numeric `requested_stop_position_cm` (or `None`) and the waypoint's `requested_destination_direction` into `PathDispatcher.create_path(...)`. `PathDispatcher` resolves any fallback value via `PathDispatcher.resolve_stop_target(...)` and stores the result on the new `Path` record. There is no extra same-block dispatcher entrypoint: same-block movement still uses ordinary `create_path(...)`. The global `5 cm` tolerance is applied only when the route/waypoint already provides the exact stop target locally. The bypass predicate and stop-window comparison still use the shared stop-frame conversion helper, and dispatcher still receives `requested_destination_direction` explicitly for any same-block / same-tile synthetic move.
- When `acquisition_mode == best_effort`, compare the live path's current `authority_end_block_id` against the waypoint target block each tick. If the live path is still short of the waypoint, keep calling `path_dispatcher.extend_path(path_id=existing_path_id, requested_destination_block_id=waypoint_target_block_id, requested_destination_direction=waypoint.requested_destination_direction, requested_stop_position_cm=waypoint.stop_position_cm, acquisition_mode=best_effort)` on that same `path_id`; do not synthesize a replacement path.
- If `_try_depart_to_waypoint()` sees retained authority already present on the controller for this waypoint, do not call `path_dispatcher.create_path(...)` again. Reuse the existing authority and branch only on departure policy:
  - if auto departure is allowed for this case, call `controller.depart()`
  - if manual confirmation is required, leave the train in `STANDBY` with the retained authority intact
- Remove the train-side `current_path` / `path_index` write from `RouteExecutor`; `PathDispatcher` should own the authoritative path-progress view and expose any controller/UI-facing path tiles or path-progress display as a pushed/read-only projection.
- Remove the `train.assigned_path_id` write from `RouteExecutor`; `controller.assigned_path_id` should be derived from the path plan that `PathDispatcher` pushes.
- Do not set `controller.ato_path_id` or any destination field directly from `RouteExecutor`.
- Let `PathDispatcher` push `path_id`, `resource_sequence`, current `authority_end_block_id`, current `stop_position_cm`, and `authority_end_mode` to the controller as part of the path plan. Each `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata.
- Remove the route-controlled fallback where `controller.depart()` is called directly without dispatcher authority; route-driven movement should require a dispatcher path plan.
- Remove the route-facing `TrainController.wait_for_path(reason)` command in the desired model. The controller should infer "no path authority" from an empty `received_path_plan`; higher layers do not need a separate command to tell it that the path is gone.
- If initial path creation fails before departure in `all_or_nothing` mode, `RouteExecutor` should record a route/path error, set `exec_state.waiting_for_path = True`, and keep the train in `STANDBY` or `IDLE`; the train never had authority, so no special controller waiting command is needed.
- In `best_effort` mode, only "no authority at all could be created" is a creation failure. A truncated but live path is not a failure path; the train may run under that current authority and later wait at the current authority boundary while `RouteExecutor` keeps retrying `extend_path()` on the same `path_id`.
- If `PathDispatcher` cancels/revokes a live path mid-run, it should clear controller authority by calling `controller.receive_path_plan(None)`, forcing the controller to EB-stop and remain blocked until a manual `release_emergency_cmd()` returns it to `STANDBY`.
- After cancellation-driven EB stop has finished and a manual release has returned the controller to `STANDBY`, ordinary `_try_depart_to_waypoint()` logic for the same waypoint should run again:
  - if reachable, let `PathDispatcher` acquire a fresh path
  - if unreachable or path creation fails, use the ordinary pre-departure route/path error path instead of introducing a separate recovery-only state machine
- When a path still exists after an EB/manual stop (retained-authority standby), fresh departure does not require creating a new path. It requires only a new `controller.depart()` decision over the retained authority, subject to the resolved departure policy and `Route.auto_depart_after_eb`, gated by the controller-owned `post_eb_restart_pending` flag when the stop came from EB.

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

TODO: desired arrival detection:
- Stop using `RouteExecutor._is_train_at_platform()` as the normal arrival gate after dispatcher-owned path completion. In the desired model, `PathDispatcher._complete_path_on_full_stop()` already guarantees the train has stopped within `5 cm` of the resolved `stop_position_cm` for the current waypoint / platform target, so path completion is the authoritative arrival trigger for that stop.
- Delete `RouteExecutor._check_waypoint_arrival()` from the desired model and replace it with one route-owned arrival-entry helper such as `RouteExecutor._enter_waypoint_arrival(train, route, exec_state)`.
- Keep a waypoint stop-window predicate only for the pre-departure same-block bypass decision: if the train is already in the target block, is fully stopped, and is already inside the waypoint stop window, `RouteExecutor` should treat that as already arrived without creating a path.
- Remove `TrainController.arrive_at_station()` in the desired model and implement a new route-executor-facing `TrainController.enter_station_dwell()` command instead. This is not a rename: route-level dwell entry should happen only after a confirmed stopped arrival, either from dispatcher-confirmed path completion or from the no-movement same-block bypass where `RouteExecutor` has already determined the train is stopped inside the waypoint stop window. `TrainController.enter_station_dwell()` should be valid from dispatcher-completed post-path `STANDBY` and from the same-block no-movement already-arrived bypass. ATO movement itself still requires a dispatcher path.
- On first arrival — whether from dispatcher-reported full stop or from the same-block stop-window bypass — `RouteExecutor._enter_waypoint_arrival(...)` should set `exec_state.arrived_at_platform_time`, set `exec_state.last_waypoint_platform_id`, clear `exec_state.waiting_for_path`, and call `controller.enter_station_dwell()` to transition into `RUNNING_AT_STATION`. This dwell-entry path applies to both ATO and manual trains; the manual/ATO difference is departure behavior only. The helper must not assume a preceding path completion event; the same arrival/dwell entry path is used in both cases.
- `_check_dwell_complete()` calls `waypoint.should_depart(time_at_platform, route_start_time, self.simulation_time)`.
- If dwell/timing says depart, `_check_dwell_complete()` calls `_advance_to_next_waypoint(route, exec_state)`.
- `_advance_to_next_waypoint()` updates `exec_state.current_waypoint_index`, `exec_state.direction`, `exec_state.resolved_platform_id`, and `route.current_waypoint_index` / `route.direction` according to `RouteMode.ONE_OFF`, `RouteMode.CIRCULAR`, or `RouteMode.ROUND_TRIP`. These are three distinct concepts, not a backward-compat mirror: `exec_state.*` is ephemeral per-tick executor state, `route.current_waypoint_index` is durable route progress, and `route.direction` is the effective travel direction. Each is maintained independently; implementers must not collapse them into one field.
- If another waypoint remains, `_check_dwell_complete()` calls `_get_controller(train).depart_from_station()`.
- If no waypoint remains, `_check_dwell_complete()` calls `_handle_route_complete_for_train(train, route, exec_state)`.
- `_handle_route_complete_for_train()` calls `_get_controller(train)`, `route.unassign_train(train.train_id)`, clears `train.assigned_route_id` when it matches, removes `execution_states[train.train_id]`, calls `controller.depart_from_station()` for any route-managed train still in dwell (including manual trains), and calls `route.deactivate()` for completed one-off routes with no assigned trains.
- `depart_from_station()` returns the controller to `STANDBY` with no retained authority. It does not imply automatic movement and does not take a `has_more_waypoints` argument — route-level chaining decisions are made by `RouteExecutor`, not by this command. The next leg's automatic departure still depends on the resolved `departure_mode`; in `manual` mode the route remains armed but stopped until the operator issues `depart()`.

Dispatcher completion notification handling:

- `SimulationController.update()` calls `RouteExecutor.handle_path_completion(train_id, path_id, _end_tile, _event_time)` after reading dispatcher events.
- `handle_path_completion()` ignores the event unless the train exists and `train.assigned_path_id == path_id`.
- It clears `train.assigned_path_id`, `train.current_path`, `train.path_index`, `train.reserved_blocks`, and `train.ato_destination_block_id`.
- It clears `exec_state.waiting_for_path` when an execution state exists.
- If the train still has an active assigned route, it calls `_check_waypoint_arrival(train, route, exec_state)`.
- It calls `_get_controller(train)`, clears `controller.ato_destination_block_id` and `controller.ato_path_id`, and calls `controller.transition_to_standby()`.
- It returns `True` when it handled the event, otherwise `False`.

TODO: desired dispatcher completion handling:
- `PathDispatcher._complete_path_on_full_stop()` should call `controller.on_path_complete()` first, then clear controller authority by calling `controller.receive_path_plan(None)`, and then call `RouteExecutor.handle_path_completion(train_id, path_id, authority_end_block_id, stop_position_cm, _end_tile, _event_time)` directly.
- `handle_path_completion()` should treat dispatcher completion as authoritative and should not perform a second path-ID validity check.
- It should clear only route-executor-owned bookkeeping such as `exec_state.waiting_for_path`. Active path tiles, reserved resources, and other authority residue should not be stored or cleared on `Train` by `RouteExecutor` in the desired model. It should no longer clear `train.ato_destination_block_id` (that field should be removed), `train.assigned_path_id` (that field moves to the controller and is cleared by `PathDispatcher`), or `train.reserved_blocks` (reserved resource ownership stays dispatcher-owned).
- `PathDispatcher` clears controller authority by calling `controller.receive_path_plan(None)` as part of path teardown after `controller.on_path_complete()` but before calling `handle_path_completion()`. The derived reads `controller.assigned_path_id`, `controller.ato_path_id`, and `controller.ato_destination_block_id` then disappear automatically.
- `controller.transition_to_standby()` should be removed from `handle_path_completion()`. `controller.on_path_complete()` owns the post-path stop transition and should leave the train in `STANDBY`; `RouteExecutor` then decides whether to immediately enter dwell.
- Controller destination-block/path teardown should remain dispatcher-owned through `controller.receive_path_plan(None)`; `controller.on_path_complete()` reacts to that completion while `RouteExecutor` remains responsible for deciding whether the completed stop becomes station dwell (`RUNNING_AT_STATION`) or remains non-dwell `STANDBY`.
- `handle_path_completion()` must not need to query live train/controller path state after dispatcher teardown. It should use the completion context passed in by `PathDispatcher` (`path_id`, `authority_end_block_id`, `stop_position_cm`, `end_tile`, `event_time`) plus route-executor-owned state only.
- If the completed stop corresponds to the current waypoint platform, `handle_path_completion()` should immediately call `RouteExecutor._enter_waypoint_arrival(...)` before returning. There must not be an inter-tick window where the train sits in bare `STANDBY` at a station stop and a later `RouteExecutor.update()` misreads that post-path stopped state as a fresh departure opportunity. The helper uses dispatcher-confirmed completion as truth plus the completion context passed in by `PathDispatcher`; it does not re-check the exact stop position from train/controller state.
- Add `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)` as the cancellation-side counterpart. It should clear `exec_state.waiting_for_path` for the affected live path, record route/path error context when relevant, and must NOT advance the waypoint, synthesize arrival, or treat the cancelled path as completed.
- Completion handling may observe a train still physically occupying the terminal block or switch footprint. That is not a contradiction: `RouteExecutor` treats dispatcher completion as authoritative arrival, while physical resource release still waits for the later tail-vacate callbacks.

Review anchors (current model only):

- Route execution calls `controller.depart()` rather than directly starting physics.
- For same-block target, `controller.depart()` is called without creating a dispatcher path. (TODO: this is superseded by the departure-to-waypoint TODO above; same-block targets must go through a synthetic dispatcher path with `stop_position_cm` in the desired model. This anchor describes current behavior only.)

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
- Remove `train.state`; operational state lives entirely on `TrainController.state` and must not be mirrored on `Train`. Every consumer that wants operational state should read `controller.state` directly.
- Remove `train.assigned_path_id`; the controller-side derived read `TrainController.assigned_path_id` (from `received_path_plan.path_id`) is the only runtime source.
- Remove `train.ato_destination_block_id`; destination block and stop position should travel inside the controller path plan pushed by `PathDispatcher`.
- Remove train-side `current_path` and `path_index`; active path tiles and path-progress reads should come from a controller/UI projection owned by `PathDispatcher` instead of a train mirror written by `RouteExecutor`.
- Remove `train.reserved_blocks` as runtime authority residue in the desired model; reserved block/resource ownership should stay on dispatcher path records and controller-facing projections, not on `Train`.
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

Review anchor (current model only):

- Runtime control should treat `TrainController` as the source of truth for operational transitions during simulation. In the current model this holds because `SimulationController` syncs controller state back to train after each tick, so controller writes survive to the next tick.

TODO: desired review anchor:
- `TrainController` remains the source of truth for operational transitions. The rationale inverts: controller is source of truth because train-side operational mirrors (`target_speed`, `speed_limit_reason`, `ato_destination_block_id`, etc.) are removed and nothing overwrites the controller from train fields. The conclusion is the same in both models; the supporting mechanism is different.

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
- Add `manual_mode_type`, such as `simulation` or `power_percent`.
- Add `manual_power_percent` for `power_percent`-mode manual control.
- Add `post_eb_restart_pending: bool` as a latched flag that marks "the last transition into `STANDBY` came from an emergency-brake release and the next departure decision is the special post-EB restart case." The flag is cleared by `TrainController.depart()` after a successful restart acceptance; `RouteExecutor` does not clear it.
- Define the `IDLE` entry trigger explicitly: `IDLE` is the controller's state when the train is powered on but no cab is selected and no departure command is active. A controller enters `IDLE` from `OFF` when power is turned on; it leaves `IDLE` when a cab is selected and a departure command (`depart()` / `ato_go()`) is accepted (then `RUNNING_MOVING`) or when it reaches `STANDBY` via any of the existing triggers.
- Keep manual driving as `control_mode == manual`; do not keep a separate `MANUAL` operational state in the desired model.
- Add `assigned_path_id: Optional[str]` only as a computed read-only property derived from `received_path_plan.path_id`. It replaces `train.assigned_path_id` as the controller-facing runtime read, but it should not be stored or written independently.
- Add `received_path_plan` as the controller inbox for the active path plan pushed by `PathDispatcher`.
- `received_path_plan` is a projection written only by `PathDispatcher`; it is not the source of truth for dispatcher path ownership, and `TrainController` should not mutate it directly.
- `received_path_plan` should carry at least `path_id`, `resource_sequence`, `authority_end_block_id`, `stop_position_cm`, and `authority_end_mode`. It is the controller's only active path-plan payload. Each `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata — so the controller can extend lookahead along the actual geometry without reading `Train.current_path` and without inferring stop-frame direction from tile coordinates alone.
- `stop_position_cm` is optional in the pushed plan: it is present when `authority_end_mode == exact_stop`, and `None` when `authority_end_mode == path_end_boundary` because the current live authority is a best-effort truncated path whose temporary target is only the current end of `resource_sequence`.
- Treat `assigned_path_id`, `ato_path_id`, and `ato_destination_block_id` as computed reads derived from `received_path_plan`, not as independently written runtime fields. When `PathDispatcher` clears authority by calling `controller.receive_path_plan(None)`, all three disappear automatically.
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
2. If power is off, set controller to `OFF` and return.
3. If emergency brake is active:
  - Set handle to `EB` and early-exit the tick while preserving path and route state.
  - Stay in the early-exit loop until the train has stopped.
  - Require explicit `release_emergency_cmd()` to clear `eb_active`.
  - After release, transition to `STANDBY` and set `post_eb_restart_pending = True`.
4. Mark cached lookahead data stale for the new tick.
5. If no cab is selected, force ATC limit to zero and suppress the lookahead payload for that tick.
6. Otherwise ensure the canonical lookahead payload is available:
  - With a received path plan and `authority_end_mode == exact_stop`, lookahead should extend along the path to `stop_position_cm`.
  - With a received path plan and `authority_end_mode == path_end_boundary`, lookahead should extend only to the current end-of-authority boundary at the end of the current `resource_sequence`.
  - Without a path plan, lookahead should extend only through the occupied block boundary.
7. Build ATC state from the cached lookahead payload, including structured reason metadata.
8. Apply ATC overspeed logic.
9. Calculate ATO info.
10. In ATO mode:
  - If `ato_running`, calculate handle with `_ato_calculate_handle()`.
  - If not running, choose braking handle toward zero and transition to standby once stopped.
11. In manual mode, write a controller-owned command and let physics convert it to speed:
  - In `simulation` submode, store the driver's handle notch in `handle_position` and allow ATC to override with braking.
  - In `power_percent` submode, store the driver's percentage power in `manual_power_percent` and let physics map it through `SpeedProfile.power_speed_curve`.
12. Stop computing standalone `effective_limit` and `target_speed` scalars; consumers should read controller lookahead at distance 0 instead.
13. Remove controller-side path completion checks such as standalone path completion or path-backed completion helpers.
14. Let waiting and completion decisions come from dispatcher-owned feedback emitted during or immediately after `TrainPhysics.tick(...)`, not from a separate `SimulationController` forwarding pass.
15. `RUNNING_WAITING` is controller-owned only. To enter it: the ATC limit derived from the canonical lookahead payload (at distance 0) is at or below the stopped-speed threshold, the train has fully stopped, and `received_path_plan` is still present. In the desired model `RUNNING_WAITING` means "stopped while the same path still exists, but current clearance inside that path is zero."
16. Do not enter `RUNNING_WAITING` solely because the driver manually stopped the train or because EB brought the speed to zero. Those cases become waiting only if the controller recomputes the same live authority and still finds zero-speed clearance at distance 0.
17. `TrainController.tick()` is also the only component that clears `RUNNING_WAITING`: when the same authority still exists and the lookahead-derived allowed speed at distance 0 becomes positive again, the controller may return to `RUNNING_MOVING` according to mode-specific restart logic.
18. A best-effort truncated path stopping at its current authority boundary is a normal source of `RUNNING_WAITING`, not completion. In that case the same `received_path_plan.path_id` remains live while `RouteExecutor` keeps retrying `extend_path()` on it.
19. Do not enter `RUNNING_WAITING` for an EB-caused stop. If `received_path_plan` is cleared while the train is moving, treat that as path-authority loss rather than ordinary waiting. The essential event is "authority removed": `PathDispatcher` clears `received_path_plan(None)`, then on the next tick the controller rebuilds lookahead with no live authority, the ATO / authority-end allowed speed at distance `0` falls to `0`, and the existing controller braking / EB logic escalates that no-authority condition into EB. The controller then stays in that stopped emergency-brake state until a manual `release_emergency_cmd()` transitions it back to `STANDBY`. This is the cancellation/revocation path, not `RUNNING_WAITING`.
20. After `release_emergency_cmd()`, transition to `STANDBY` even if `received_path_plan` still exists. That is the retained-authority standby case: the authority remains available, but restarting requires a fresh explicit `depart()` decision rather than automatic controller self-resume.

ATC speed limit:

- `_calculate_atc_speed_limit()` uses clearance ahead and `SpeedProfile.deceleration_rate`.
- `calculate_max_safe_speed()` computes maximum speed that can stop within `(clearance - safety_margin)`.
- Yellow signals can reduce speed to `YELLOW_SIGNAL_SPEED_KMH`.
- Current block `max_speed_kmh` can reduce speed.

TODO: desired ATC speed limit:
- Stop reading signal state directly for ATC restriction selection. In the desired model, CAUTION restriction should come from a single controller-owned global cap such as `CAUTION_SPEED_LIMIT_KMH`, applied when `TrainController` samples a block whose computed `Block.state` is `CAUTION`; signals remain display state derived from blocks rather than an independent ATC input.
- In manual mode with a received path plan, include `_calculate_authority_boundary_limit()` so the effective ATC limit enforces the current authority end correctly: brake to `stop_position_cm` when `authority_end_mode == exact_stop`, or brake to the current path-end boundary when `authority_end_mode == path_end_boundary`.
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
- Add `set_manual_power_percent()` as the active driver input for `power_percent` submode manual control.

TODO: desired dispatcher/route-executor inputs:
- Add `receive_path_plan(path_plan)`, called by `PathDispatcher` after successful `create_path()`, after any `extend_path()` that changes the live authority on the same `path_id`, and with `None` on cancellation, replacement, or completion teardown. `received_path_plan` should mirror only the active authority path referenced by `active_path_id_by_train[train_id]`.
- `path_plan` should include `path_id`, `resource_sequence`, `authority_end_block_id`, `stop_position_cm`, and `authority_end_mode`. Each `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata. The controller must not be asked to recover stop-frame direction later from tile coordinates alone.
- In v1, `authority_end_mode == exact_stop` means `stop_position_cm` is populated and the controller should brake to that exact stop target; `authority_end_mode == path_end_boundary` means `stop_position_cm = None` and the controller should treat the current end of `resource_sequence` as the temporary authority boundary instead.
- `path_plan` should not carry a second lifecycle status enum in the desired model. For the controller, movement authority is binary: `received_path_plan is not None` means there is live authority, and `received_path_plan is None` means there is not.
- Add `on_path_complete()`, called by `PathDispatcher._complete_path_on_full_stop()` when the train has fully stopped at the terminal stop point. `on_path_complete()` should transition the controller into post-path `STANDBY`, but it must NOT clear `received_path_plan` itself. `PathDispatcher`, as the sole owner/writer of the pushed path plan, clears authority immediately after the callback by calling `controller.receive_path_plan(None)`. `on_path_complete()` must not enter station dwell by itself; `RouteExecutor.handle_path_completion()` decides from the route plan whether the completed stop becomes dwell.
- Remove `arrive_at_station()` in the desired model and replace it with a new route-executor-facing `enter_station_dwell()` command, used only after a confirmed stopped arrival: either dispatcher-confirmed full stop on a path, or the explicit same-block no-movement bypass where `RouteExecutor` has already determined the train is stopped inside the waypoint stop window. `TrainController.enter_station_dwell()` must accept dispatcher-completed post-path `STANDBY` and same-block already-arrived entry in the desired model.
- Remove `wait_for_path(reason)` from the desired controller API. Missing path authority is represented entirely by `received_path_plan is None`.
- Keep `depart()`, `enter_station_dwell()`, and `depart_from_station()` as route-executor-facing controller commands. `depart_from_station()` takes no `has_more_waypoints` argument; it returns the controller to `STANDBY` with no retained authority and makes no assumption about whether another waypoint will follow.
- Manual trains should also enter dwell through `enter_station_dwell()` on confirmed arrival. The difference from ATO is departure behavior only: `TrainController.depart()` in manual mode transitions into movement directly and does not call `TrainController.ato_go()`.
- `PathDispatcher` should call `RouteExecutor.handle_path_cancellation(train_id, path_id, reason)` whenever a live path is cancelled or revoked. In v1 this notification is informational: after controller authority is cleared, the train has stopped under EB, and a manual `release_emergency_cmd()` has returned the controller to `STANDBY`, `RouteExecutor` should check whether that cancelled path actually affects its current waypoint / assigned route state and then use ordinary reachability + path-request logic for recovery.
- `RouteExecutor` should not set controller path or destination fields directly; controller authority should be derived entirely from `receive_path_plan(...)`.
- Replace `transition_to_standby()` and `arrive_at_station()` as completion hooks with `on_path_complete()` plus route-owned dwell-entry once completion teardown is dispatcher-owned and the controller reacts through callbacks only.
- `release_emergency_cmd()` should only clear EB when the train is already stopped, transition the controller to `STANDBY`, and latch `post_eb_restart_pending = True`.
- `TrainController.depart()` is also the fresh-restart command for retained-authority standby. If `received_path_plan` still exists after an EB/manual stop, route/operator logic should reuse that authority and call `depart()` rather than synthesizing a replacement path. A successful new departure decision clears `post_eb_restart_pending`.

TODO: desired read accessors:
- Add `get_lookahead()` as the canonical accessor for UI and debug consumers. Any consumer asking for target speed or speed-limit reason should sample this payload at distance `0`.
- Add `get_path_tiles()` as a read-only controller projection derived from `received_path_plan.resource_sequence[*].path_tiles`. It returns the deduplicated tile list in forward traversal order. `TrainPhysics` and UI consumers may read this projection, but they must not read `Train.current_path`.
- Add one shared neighbor/edge helper such as `RailwayNetwork.get_connection_edges(tile_a, tile_b) -> Optional[Tuple[str, str]]`, which answers two questions at once: are the two tiles neighbors, and if so, which edge on each tile connects to the other. `TrainPhysics` and traversal helpers should use this instead of re-deriving neighbor-edge pairs ad hoc.

Current manual mode and movement-authority boundary:
- Manual control currently uses `set_handle()` plus train-side `manual_target_speed`.
- ATC currently reacts to clearance, signal, and block limits, but it does not enforce an explicit path-end authority boundary in manual mode.

TODO: desired manual mode and movement-authority boundary:
- In `simulation` submode, the driver should call `set_handle()`, the controller should store `handle_position`, and `TrainPhysics` should integrate it into speed while ATC can still override with braking.
- In `power_percent` submode, the driver should call `set_manual_power_percent()`, the controller should store `manual_power_percent`, and `TrainPhysics` should convert it through `SpeedProfile.power_speed_curve`.
- Remove `manual_target_speed` entirely; no hidden shim or compatibility field should remain.
- In manual mode with a received path plan, add `_calculate_authority_boundary_limit()` so the controller brakes to the current authority end: the pushed `stop_position_cm` when `authority_end_mode == exact_stop`, or the current path-end boundary when `authority_end_mode == path_end_boundary`.
- Without a received path plan, the current-block boundary should remain the fallback authority limit.

Current completion ownership:
- Dispatcher completion is currently event-driven through `SimulationController`, while `TrainController.tick()` still contains path-backed completion and waiting checks.

TODO: desired completion ownership and timing:
- Make `PathDispatcher` the sole owner of path completion detection.
- Let `TrainController` react to dispatcher-owned completion through `on_path_complete()`, but stop letting the controller decide independently that a path-backed run is complete.
- Keep `PathDispatcher` as the sole writer of `TrainController.received_path_plan`; `on_path_complete()` is a reaction hook, not an inbox-mutating hook.
- Remove helper logic such as `path_backed_run_active` / `_is_standalone_path_complete()` once dispatcher-driven completion is authoritative.
- Tighten the invariant so path completion is emitted only after the train is fully stopped at the terminal stop point.

Review anchors (current model only):

- In the current tick order, ATO handle calculation happens before `target_speed` is updated for this tick. `_ato_calculate_handle()` mostly uses curves and ATC fields, but any direct use of `self.target_speed` during handle calculation should be reviewed.
- No-cab state sets ATC speed limit to zero; ATO start should therefore ensure cab selection before movement.
- Controller and train both have ATO-related fields. Controller is the active runtime source, while train fields are used for persistence/UI and route integration.

TODO: desired controller review anchors:
- There should be no `self.target_speed` field once lookahead-at-distance-0 becomes the canonical source of target speed.
- Controller-owned live control state and train-owned physical/persistent state should be cleanly separated.
- Remove the runtime fallback read `self.ato_destination_block_id or getattr(train, "ato_destination_block_id", None)` from controller ATO logic; in the new model destination state comes exclusively from `received_path_plan`.
- `SimulationController.get_or_create_controller()` should preserve the 1:1 train/controller pairing without recreating authority from train-side runtime mirrors. Its job is only "make sure a controller object exists and is registered for this `train_id`." If authority already exists, `SimulationController` may notify `PathDispatcher` that the binding is now available, and `PathDispatcher` re-pushes the live plan from dispatcher state after the controller is available. `SimulationController` must not construct or write the plan payload itself.

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

TODO: desired movement-authority input in `TrainPhysics`:

- `TrainPhysics` should continue to own physical integration (`current_speed`, position, occupancy, tail tracing), but it should not own path identity or dispatcher authority state.
- To choose exits on ambiguous geometry, `TrainPhysics` normally needs a controller-provided, read-only authority-geometry projection derived from `TrainController.received_path_plan.resource_sequence`. The minimum movement data is the ordered path tiles (or an equivalent current-tile -> next-tile resolver) plus enough local direction context to choose the correct next edge through the current tile.
- `TrainPhysics` does **not** need dispatcher ownership fields such as `path_id` to step the train, and it does not need `stop_position_cm` for physical integration. `stop_position_cm` belongs to controller braking logic.
- `TrainPhysics` may obtain that geometry projection through `TrainController.get_path_tiles()` or an equivalent controller accessor. `Train.current_path` should not remain a movement input in the desired model.
- Exception for cancellation/revocation braking: if `PathDispatcher` has already cleared `received_path_plan` to revoke authority while the train is still rolling, `TrainPhysics` must still keep moving the train according to speed and the current live network state. That post-revocation movement relies on the physical `Switch.state` / live track geometry that exists in the network at that moment, not on retained authority locks or a second copy of path ownership surviving in the controller inbox.
- Once live switch state ownership moves to `Switch`, `TrainPhysics._advance_end()` and tail tracing must call the updated `TrackSegment` traversal helpers with the network or equivalent switch-state resolver required by the desired `TrackSegment` contract.

Block occupancy:

- `_get_blocks_for_train()` derives blocks from occupied tiles.
- `_update_block_occupancy()` compares new occupied blocks to `_last_occupied_blocks`.
- Newly entered blocks call `block_manager.occupy_block()`.
- Newly vacated blocks call `block_manager.set_block_to_caution()` and are tracked in `_caution_blocks`.
- Caution blocks clear to free later when the train no longer occupies them and clearing conditions are met.
- `TrainPosition.occupied_blocks` is updated from the derived block set.

TODO: desired block occupancy:
- Route normal tail-vacated transitions through `PathDispatcher.on_resource_tail_vacated_from_physics()`, which routes block releases through `BlockManager.on_block_tail_vacated()` so `OCCUPIED -> CAUTION -> FREE` stays in one state machine.
- `TrainPhysics` must not emit a block tail-vacate callback just because the tail left one tile of that block. `PathDispatcher` may call `BlockManager.on_block_tail_vacated()` only after `TrainPhysics` knows the train no longer occupies **any** tile in that block.
- Replace direct `clear_block()` calls for normal CAUTION clearing with the cascade inside `on_block_tail_vacated()` via `_reevaluate_caution_blocks()`, not a separate `release_block()` step.

TODO: desired tail-vacate flow in `TrainPhysics`:

- Remove direct calls to `block_manager.set_block_to_caution()` and `block_manager.clear_block()` for normal vacate transitions.
- On tail-vacate, emit a typed resource-release event to `PathDispatcher.on_resource_tail_vacated_from_physics(train_id, resource_id, resource_type, direction)`. For blocks, `TrainPhysics` must emit the callback only after it knows the train no longer occupies any tile in that block; tile-level vacate is not enough. For switches, tile-level vacate of the switch resource is sufficient. `TrainPhysics` obtains `resource_id` from the same tile-lookup mechanism already used for entry detection: `track.block_section_id` for blocks, `track.switch_id` for switch tiles (both fields are on `TrackSegment` and available from `network.tracks.get(tile)`). `TrainPhysics` should derive `direction` from its own movement basis for that vacated traversal: the entry/exit edges and local movement vector it already used to move through the resource. `PathDispatcher` routes the release according to `resource_type`: blocks go through `BlockManager.on_block_tail_vacated(block_id, train_id, direction)` so `OCCUPIED → CAUTION → FREE` stays in one state machine; switches go through the `Switch` entity by clearing `Switch.occupied_by` and then performing any train-ID-based lock/flank teardown that still applies.
- `_caution_blocks` tracking in `TrainPhysics` becomes unnecessary once `BlockManager` handles the full release lifecycle internally; remove that legacy field and its associated maintenance logic in the desired model.

Voltage:

- `OFF`: 0 V.
- `IDLE`, `STANDBY`, `RUNNING_WAITING`, `RUNNING_AT_STATION`: lighting voltage.
- `RUNNING_MOVING`: voltage from `speed_profile.speed_to_voltage(controller.target_speed)`.
- `MANUAL`: voltage from `speed_profile.speed_to_voltage(train.manual_target_speed)`.
- EB overrides to 0 V.

TODO: desired voltage behavior:
- Stop reading `controller.target_speed` and `train.manual_target_speed` once those standalone fields are removed.
- Keep the terminology explicit and simple: `track_voltage`, `power_percent`, and `speed_kmh` are three different values in the desired model.
- `TrainPhysics` should pass the current `track_voltage` value to `BlockPowerController` as the software-to-hardware output layer for block voltage emission.
- In `power_percent` manual mode, `TrainPhysics` should use the profile's voltage/power/speed calibration directly rather than reading removed scalar target-speed fields. Both mappings are owned by `SpeedProfile` and should be documented and named exactly this way:
  - `track_voltage -> power_percent`
  - `power_percent -> speed_kmh`
- Do not introduce extra names for these concepts in the desired model. Keep the doc wording in terms of voltage, power percent, and speed, and keep the mappings distinct.

Review anchors (current model only):

- Physics does not clamp speed to target speed; it relies on controller handle selection to accelerate/brake.
- Movement is geometry-driven, not strictly path-index-driven. It uses switch state and track exits. Path locks are therefore important.
- If no movement occurs because speed is zero, block occupancy still updates.

TODO: desired physics review anchor:
- `TrainPhysics` should not own manual-mode policy; it should execute controller commands and maintain physical state only.
- `TrainPhysics` no longer relies on a separate track-level path-lock field. The equivalent role is now played by `Block.reserved_by_path` / `Block.reserved_for_train` on the block side and `Switch.locks` on the switch side. Movement remains geometry-driven; switch integrity during traversal is enforced by the combination of `Switch.occupied_by` plus the still-held compatible `Switch.locks` set rather than by a global track-level path lock. This is not a transitional/migrated version of the old field — the mechanism is different, so no transitional phase is needed.

## SpeedProfile

`SpeedProfile` maps command/target speed to `track_voltage` (block detection signal) in the current implementation and provides motion rates.

TODO: desired-model SpeedProfile intro. In the desired model, `SpeedProfile` carries motion-rate data plus per-train calibration in the vocabulary the rest of the system should use directly: voltage, power percent, and speed. The calibration defines how voltage maps to `power_percent`, and how `power_percent` maps to steady running `speed_kmh`. Keep those two mappings explicit in the doc instead of introducing extra terminology.

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

TODO: desired-model SpeedProfile review anchor:
- `SpeedProfile` is still not a speed limit object under the desired model. It expresses motion-rate data and per-train calibration (`startup_voltage`, `max_voltage`, `power_speed_curve`), not any form of speed ceiling. Train maximum speed and block maximum speed remain separate, independently enforced constraints.

TODO: desired speed-profile addition:
- Keep the desired-model calibration wording in three explicit values: `track_voltage`, `power_percent`, and `speed_kmh`.
- The calibration has two mappings and both should be stated directly in the doc:
  1. `track_voltage -> power_percent`
  2. `power_percent -> speed_kmh`
- `startup_voltage` and `max_voltage` define the voltage range used by the first mapping. Below `startup_voltage`, the train does not move. `max_voltage` is the top of the usable range.
- `power_speed_curve` is the stored `(power_percent, speed_kmh)` mapping used by the second step. `power_percent` is the intermediate normalized control value between voltage and speed; it is not a second name for either of them. In v1 this may be linear by default, but it is still stored explicitly as profile calibration data rather than inferred ad hoc elsewhere.
- Acceleration to reach the speed implied by that mapping is still governed by the physics model and motion-rate fields, not by `power_speed_curve` itself.
- Every train should have a default `SpeedProfile` carrying these calibration fields so the `power_percent` manual mode always has a defined voltage/power/speed mapping, even before per-train tuning is customized.

## Signals

Signals are linked to tracks and blocks:

- `Signal.detection_direction` is the direction trains travel toward.
- `approach_direction` is the opposite direction for display.
- `RailwayNetwork.update_signals_from_blocks()` recalculates signal states from block state.
- Controller speed curves intentionally do not use signals as speed restrictions.

Review anchors:

- Red/yellow signal semantics and ATC speed enforcement are not equivalent in the current design. Blocks are the primary safety authority.
- If signals are expected to enforce stops, that would be a design change from the current CBTC-style speed curve comments.

TODO: desired-model signals:
- Signals remain display-only in the desired model. CAUTION handling moves to `TrainController.CAUTION_SPEED_LIMIT_KMH` (enforced via the lookahead / ATC path), not to signal aspects. `RailwayNetwork.update_signals_from_blocks()` still produces aspect values for the UI, but no controller or physics layer consumes them as a speed restriction.

## Power

`BlockPowerController` owns voltage output:

- `TrainPhysics` normally calls `set_block_voltage()` for occupied blocks.
- Emergency stop can set all block voltages to zero.
- Voltage is per block, not per tile.

Review anchor:

- Multiple trains occupying the same block would create a last-writer-wins voltage situation unless prevented by block occupancy/path logic.

TODO: desired power-controller role:
- Keep `BlockPowerController` focused on the software-to-hardware boundary only: it accepts a voltage number and applies it to block outputs.
- `TrainPhysics` computes `track_voltage` from live train motion / current speed, and `BlockPowerController` applies that already-computed value to the hardware-facing block outputs.
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
- Remove `state`, `target_speed`, `speed_limit_reason`, `assigned_path_id`, `ato_destination_block_id`, `current_path`, `path_index`, ATO info, lookahead data, and other live control diagnostics from `Train.to_dict()`.
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
- Export `received_path_plan` when present, including `path_id`, `resource_sequence`, `authority_end_block_id`, `stop_position_cm`, and `authority_end_mode`. This is the sole controller/UI source of truth for the active path plan. Each exported `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata.
- Do not make `TrainController.to_dict()` the owner of live path-progress state in v1. `PathDispatcher` remains the sole owner of `current_resource_index`; if UI later needs that progress projection, expose it separately as a dispatcher-owned read model rather than copying ownership into `TrainController`.
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
5. RouteExecutor stores `assigned_path_id` on the train and sets `ato_destination_block_id` directly on both `train` and `controller`.
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

TODO: desired end-to-end path-authority handoff:
- `PathDispatcher` should push `path_id`, `resource_sequence`, `authority_end_block_id`, `stop_position_cm`, and `authority_end_mode` via the controller path plan. Each `resource_sequence` entry carries the full `Path.resource_sequence` entry schema defined near line 508 — `sequence_index`, `resource_type`, `resource_id`, `path_tiles`, `entry_edge`, `exit_edge`, and any traversal metadata. `controller.assigned_path_id` should be only a derived read of that plan's `path_id`.
- `RouteExecutor` should ensure the train's `TrainController` exists before this path-authority handoff happens in Phase 1.
- `RouteExecutor` should not write `assigned_path_id` or `ato_destination_block_id` directly in the desired model.

TODO: desired end-to-end ATO run:
1. Route is active and assigned to the train.
2. In Phase 1 setup, `RouteExecutor` resolves the next platform waypoint.
3. If the train is already stopped within the waypoint stop window and the route/waypoint already defines the exact stop target, `RouteExecutor` treats the waypoint as arrived and calls the same arrival/dwell entry path used after normal path completion.
4. If the train is in the target block but not yet at the stop point, `RouteExecutor` requests a synthetic same-block path with explicit `requested_stop_position_cm`.
5. Otherwise `RouteExecutor` calls `PathDispatcher.create_path()` normally, passing `requested_destination_block_id`, `train_id`, `requested_destination_direction`, `requested_stop_position_cm` when the route plan defines an exact platform stop target, choosing `acquisition_mode` (`all_or_nothing` or `best_effort`), and letting `PathDispatcher` resolve a fallback safe stop target otherwise.
6. `PathDispatcher` establishes controller authority by pushing the path plan. The controller receives `path_id`, `resource_sequence`, current `authority_end_block_id`, current `stop_position_cm`, and `authority_end_mode` through that plan, with per-resource `path_tiles` slices and explicit traversal metadata such as `entry_edge` / `exit_edge` included in `resource_sequence`, and `controller.assigned_path_id` is read back as a derived view of the plan's `path_id`. `RouteExecutor` does not write `assigned_path_id`.
7. If the request mode is `best_effort` and the current live path is still short of the waypoint target, `RouteExecutor` keeps retrying `PathDispatcher.extend_path()` on that same `path_id`, always passing the same requested final-target contract (`requested_destination_block_id`, `requested_destination_direction`, and the same `requested_stop_position_cm` value). The current authority stays live while `resource_sequence` grows in place; the controller may enter `RUNNING_WAITING` at the current authority boundary between successful extensions.
8. Each simulation tick runs in setup, execute, and feedback phases:
   - setup: route/path planning
   - execute: controller lookahead, ATC, and ATO/manual command generation, followed by physics movement
   - feedback: dispatcher resource progress, release, and completion checks driven directly by `TrainPhysics` resource-entry and tail-vacate reporting
9. On path completion, `PathDispatcher._complete_path_on_full_stop()` releases remaining already-vacated resources, calls `controller.on_path_complete()`, clears controller authority by calling `controller.receive_path_plan(None)`, and then calls `RouteExecutor.handle_path_completion()` directly with the completion context it needs (`train_id`, `path_id`, `authority_end_block_id`, `stop_position_cm`, `end_tile`, `event_time`). Any block or switch still physically occupied by the train remains held until its later tail-vacate callback.
10. `RouteExecutor.handle_path_completion()` clears only route-owned bookkeeping and hands route-level arrival/dwell logic to `RouteExecutor._enter_waypoint_arrival(...)`, which enters dwell when the route plan says this completed stop is a station stop. It does not re-check the exact stop position from live train/controller state; dispatcher completion plus the passed completion context are authoritative.
11. Dwell continues through `RUNNING_AT_STATION` and `Waypoint.should_depart()`.
12. When dwell completes, `RouteExecutor` advances the waypoint index and the cycle repeats.

TODO: desired end-to-end manual-arrival note:
- Manual trains use the same dispatcher-confirmed arrival and `RouteExecutor._enter_waypoint_arrival(...)` dwell-entry path as ATO trains. The only difference is departure behavior: `TrainController.depart()` in manual mode transitions directly into movement and does not call `TrainController.ato_go()`.

TODO: desired end-to-end retained-authority restart note:
- If EB or a manual stop leaves the train in `STANDBY` while the same `received_path_plan` still exists, the next fresh departure reuses that retained authority. `RouteExecutor` must not create a duplicate path in that case; it should issue `controller.depart()` only when the resolved departure policy allows it. For EB-originated restarts, `TrainController.post_eb_restart_pending` is the explicit gate that makes `Route.auto_depart_after_eb` apply; once the fresh departure decision is made, clear that flag.

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

TODO: desired review checklist:
- Is the controller receiving a path plan with `path_id`, `resource_sequence`, `authority_end_block_id`, `stop_position_cm`, and `authority_end_mode` when path-end stopping matters?
- Does each `resource_sequence` entry carry the ordered `path_tiles` slice and explicit traversal metadata (`entry_edge` / `exit_edge` or equivalent) needed for controller lookahead and UI geometry after `Train.current_path` is removed?
- Is controller lookahead the only source of target speed and speed-limit reason?
- In manual mode, does ATC enforce the end of the assigned path as a hard stop through `_calculate_authority_boundary_limit()`?
- Can any path-complete event still fire while the train is braking or creeping toward the terminal stop point?
- Does `_ato_calculate_handle()` read only the current tick's canonical lookahead payload, not stale scalar fields?
- Does `RouteExecutor` read controller state rather than mirrored runtime state on `Train`?
- For same-block waypoints, does the route either treat the train as already arrived or create a synthetic same-block path rather than starting movement without dispatcher authority?
- For same-block / same-tile waypoint movement, does the request carry an explicit `requested_destination_direction`, and does dispatcher pathfinding reject ambiguous or wrong-direction candidates rather than guessing?
- Are repeated resources rejected during both `create_path()` and `extend_path()`?
- In `best_effort` mode, does the system keep one live `path_id`, extend `resource_sequence` in place, and avoid treating a truncated-but-live authority as a creation failure?
- Do dispatcher progress/completion/release callbacks all accept `train_id` only, with `PathDispatcher` resolving active progress from its own database rather than from physics or train mirrors, while delayed release of a still-occupied block or switch resolves from the resource itself after authority teardown?
- Does the normal `CAUTION -> FREE` reevaluation require the forward neighbour (in `caution_direction`) to be non-occupied, while still allowing the explicit completion-time `force` exception in `release_path_block_reservations(..., completed=True)`? (`occupied_by` is always `None` in `CAUTION` — guaranteed by `on_block_tail_vacated()` clearing it — so a separate non-occupied check is vacuous. `reserved_by_path` is explicitly not a gating condition; if `_is_caution` has cleared but `reserved_by_path` remains set, the computed block state is `RESERVED`, not `FREE`.)
- Can any completion or teardown path still unlock a block or switch that the train physically occupies at full stop?
- After `release_emergency_cmd()`, does the system return to `STANDBY` and reuse retained authority without creating a duplicate path, while still honoring `departure_mode` and `auto_depart_after_eb`?

TODO: desired-model notes:
- In the desired model, ordinary switch release happens when the train tail vacates the switch tile, not on full path completion. `TrainPhysics` reports the vacated resource to `PathDispatcher` as a typed resource-release event, and dispatcher releases it according to `resource_type`: blocks go through `BlockManager.on_block_tail_vacated(...)`, while switches clear `Switch.occupied_by` and then apply any remaining train-ID-based lock/flank teardown. Both completion and cancellation must respect physical occupancy: if a switch is still physically occupied, its lock stays in place until the later tail-vacate event.
- In the desired model, `assigned_path_id` moves from `Train` to `TrainController` as a read-only derived property of `received_path_plan.path_id`. `PathDispatcher` owns the underlying `received_path_plan`: it sets that plan when the active path is established, mirrors only the single active authority path referenced by `active_path_id_by_train[train_id]`, and clears it on path teardown by calling `controller.receive_path_plan(None)` before calling `RouteExecutor.handle_path_completion()`. In the current implementation, `PathDispatcher` clears `train.assigned_path_id` directly at path completion (path_dispatcher.py `_complete_path_on_head_arrival` and `complete_path`), which is one source of the event-consumption race noted in line 142 above. Moving the runtime read to the controller and making it derived from the pushed path plan eliminates that race.
