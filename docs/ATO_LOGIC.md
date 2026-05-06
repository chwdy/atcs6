# ATO Logic

**Status:** Current reference  
**Last updated:** 2026-04-07

This document describes how automatic train operation (ATO), automatic train control (ATC), path dispatching, and train physics interact.

## Components

- `Train` (`train.py`): stores physical train state such as speed, position, active path IDs, route IDs, cached path data for serialization, and destination metadata.
- `PathDispatcher` (`path_dispatcher.py`): owns live path authority. Active `Path.path_tiles` are the source of truth for movement authority; `train.current_path` is a cache for display and fallback only.
- `TrainController` (`train_controller.py`): owns ATC and ATO decisions. It calculates clearance, speed limits, monitor lookahead, speed curves, cab requirements, target speed, and the throttle/brake handle.
- `TrainPhysics` (`train_physics.py`): executes the selected handle. It converts handle notches to acceleration/deceleration, updates speed and position, and updates block occupancy.
- `SimulationController` (`simulation.py`): calls controller tick, physics tick, dispatcher progress updates, and serialization synchronization.
- `RouteExecutor` (`route_executor.py`): requests paths for route waypoints and uses `TrainController.depart()` / arrival transitions for route flow.

## Tick Order

1. `SimulationController.update(delta)` gets or creates a controller and physics instance for each train.
2. `TrainController.tick(delta)` calculates lookahead, ATC limits, ATO info, handle position, target speed, and running/waiting/standby transitions.
3. `TrainPhysics.tick(delta)` applies `controller.handle_position` to update speed and move the train.
4. `PathDispatcher.on_train_progress_from_physics(...)` advances or completes the live path after physics movement.
5. Controller-derived display fields are synced back to `Train` for the frontend.

## Path Authority

Controller and physics logic should prefer dispatcher-backed live path data:

- `TrainController.get_path_tiles()` reads `PathDispatcher.get_path(train.assigned_path_id)`.
- If no dispatcher/live path exists, controller logic may fall back to `train.current_path`.
- `train.current_path` can be stale after path extension, cancellation, or completion, so it should not be treated as movement authority when an assigned path is absent.

## ATC

ATC calculates a safety limit from:

- Remaining clearance in the current block.
- Blocks ahead along the active path and travel direction.
- Block occupancy/reservation obstruction.
- Red/yellow signals where applicable.
- Current block speed limits.
- Destination safety margin: `25 cm` normally, `5 cm` for platform destinations.

If current speed exceeds the ATC limit by more than `5 km/h`, ATC raises a warning. If it exceeds by more than `10 km/h`, ATC activates emergency braking.

## ATO Speed Curve

The train monitor speed curve comes from `TrainController.calculate_speed_curves(...)`.

- ATC curve uses full service braking.
- ATO operational curve uses the speed profile’s `operational_decel_ratio` and stays `ATO_SPEED_MARGIN_KMH` below ATC.
- Stop restrictions are generated from clearance/path end and destination safety margin.
- Block speed limits are added as speed restrictions so ATO slows before entering slower blocks.

The current-point curve value at `distance_cm = 0` is the limit at the train head and must be included in ATO handle scoring. Otherwise the handle can continue powering even when the monitor already shows the train above the ATO curve.

## ATO Handle Selection

`TrainController._ato_calculate_handle()`:

- Gets monitor lookahead and speed curves.
- If the curve at the train head says stop, applies direct braking.
- If the train is already above the operational curve at the current head position, selects braking toward that current curve value.
- Otherwise evaluates candidate handles by simulating future speed and scoring against the operational curve, including the current point. The evaluator horizon expands when a lower nonzero restriction is within the restriction lookahead window, so ATO prepares for fast curve drops without clamping to the lowest future point.
- Applies an ATC cap override if the selected handle would exceed the live ATC limit.
- Rate-limits handle movement to avoid jitter, except for immediate stop conditions.

`TrainPhysics` then applies the selected handle. It does not inspect the speed curve or choose a speed target.

An active path end is also a movement boundary for physics. The train must not fall back to the track's default exit and continue beyond the assigned path.

## Completion

When a path completes, `PathDispatcher` clears live path/destination state. A stopped train in `RUNNING_MOVING` or `RUNNING_WAITING` with no active path, route, or destination context should transition to `STANDBY`. This completion logic must not depend solely on historical `path_backed_run_active`, because that flag can be false for legacy/manual/test flows even after a path-like run is complete.
