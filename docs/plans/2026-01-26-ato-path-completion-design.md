# ATO Control Redesign + Path Completion Design

**Date:** 2026-01-26

## Goals

- Auto-start ATO in the demo without manual button press.
- Redesign ATO speed control to be distance-aware and smooth near targets.
- Stop ATO quickly using aggressive service braking (never EB).
- Complete paths when the train head reaches the end tile and release safe resources.
- Ensure RouteExecutor stays consistent with path lifecycle without blocking one-off paths.

## Non-Goals

- Introduce full PID control.
- Change ATC overspeed safety behavior.
- Rework pathfinding or dispatcher architecture.

## Current Issues (Observed)

- Demo ATO does not auto-start because a temporary controller is used before the shared controller is created.
- ATO speed control uses tight tolerance around target speed, causing oscillation and re-acceleration.
- End-of-path behavior flips braking/neutral and can trigger EB due to reactive control.
- Path completion does not currently notify route logic, and paths are not completed on arrival.

## Design Overview

### 1) ATO Auto-Start in Demo

**Root cause:** demo creates a temporary controller; SimulationController later creates a new controller with `ato_running=False`.

**Design:** use the shared controller instance owned by SimulationController in `demo_main.py` and call ATO start on that controller (after setup/depart) to ensure ATO starts once and stays running.

### 2) Path Completion Rule (Head-Arrival, Immediate)

**Rule:** A path is completed when the train head reaches the **final tile** of the path. Completion is immediate regardless of speed.

**Resource release:** on completion, release only blocks **not currently occupied** by the train (using `train.position.occupied_blocks`). Blocks still occupied remain occupied.

### 3) Route-Path-Train Coordination

- Path lifecycle remains authoritative in `PathDispatcher`.
- `PathDispatcher` records completion events `(train_id, path_id, end_tile, time)`.
- `RouteExecutor` polls for completion events and updates route state if the train has an active route.
- One-off paths (no route) still complete and release resources without RouteExecutor involvement.

### 4) ATO Speed Control (Braking Curve + Aggressive Service Stop)

**Approach:** distance-aware target speed based on braking distance, then choose handle notch to track smoothly.

- Use `SpeedProfile.get_braking_distance_cm()` with **aggressive service decel** (below EB).
- Compute a desired speed from remaining distance to the stop point.
- Select handle notch based on speed error with low-speed hysteresis to avoid N/B chatter.
- **ATO Stop button:** immediately set aggressive service brake notch (no EB).
- EB only allowed by ATC overspeed safety logic.

## Data Flow Summary

1. `TrainPhysics` updates positions and occupied blocks.
2. `PathDispatcher` checks head-at-end, completes path, releases safe resources.
3. `RouteExecutor` consumes path completion events to finalize routes and set STANDBY.
4. `TrainController` uses distance-aware ATO control to select handles smoothly.

## Testing Plan

- Path completion when head reaches end tile, releasing only unoccupied blocks.
- RouteExecutor handles path completion for route-based trains (STANDBY, route cleared).
- ATO Stop uses aggressive service brake (never EB).
- End-of-path deceleration is smooth and avoids EB unless ATC overspeed.

## Files Expected to Change

- `demo_main.py`
- `simulation.py` (if completion events are passed to RouteExecutor here)
- `path_dispatcher.py`
- `route_executor.py`
- `train_controller.py`
- `tests/test_dispatcher.py` and `tests/test_trains.py`
