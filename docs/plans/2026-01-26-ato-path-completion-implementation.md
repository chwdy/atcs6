# ATO Control + Path Completion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement distance-aware ATO control with aggressive service stop, auto-start ATO in demo, and complete paths on head-arrival with route-aware cleanup.

**Architecture:** Path completion is authoritative in `PathDispatcher` and emits completion events; `RouteExecutor` consumes those events to update route state. ATO uses a braking-curve target speed and selects handle notches with hysteresis; ATO Stop applies aggressive service braking (no EB).

**Tech Stack:** Python 3, Flask backend, pytest, vanilla JS UI (no JS changes planned).

> **Note:** User requested NOT to use worktrees during execution. Execute in the current workspace.

## Task 1: Path completion on head-arrival

**Files:**
- Modify: `path_dispatcher.py` (completion event collection and release logic)
- Modify: `simulation.py` (poll/forward completion events)
- Modify: `route_executor.py` (consume completion events)
- Test: `tests/test_dispatcher.py`

**Step 1: Write the failing test**

Add to `tests/test_dispatcher.py`:

```python
def test_complete_path_on_head_arrival_releases_unoccupied_blocks(
    simple_linear_network, dispatcher, train
):
    # Setup: create a path with multiple blocks
    # Simulate head at last tile while train still occupies last block
    # Expect: path completed, only unoccupied blocks released
    ...
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_dispatcher.py::test_complete_path_on_head_arrival_releases_unoccupied_blocks -v`

Expected: FAIL (no head-arrival completion logic).

**Step 3: Write minimal implementation**

- Add a completion check in `path_dispatcher.py` that:
  - Detects `train.position.head_tile == path.path_tiles[-1]`.
  - Marks path `COMPLETED` and releases resources for blocks not in `train.position.occupied_blocks`.
  - Records a completion event (`train_id`, `path_id`, `end_tile`, `time`).
- Add an accessor to retrieve and clear completion events.

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_dispatcher.py::test_complete_path_on_head_arrival_releases_unoccupied_blocks -v`

Expected: PASS.

**Step 5: Commit**

Only if user requests a commit:
`git add path_dispatcher.py tests/test_dispatcher.py && git commit -m "feat: complete paths on head arrival"`

## Task 2: RouteExecutor consumes completion events

**Files:**
- Modify: `simulation.py` (poll events each tick)
- Modify: `route_executor.py` (handle completion event to update route state)
- Test: `tests/test_dispatcher.py` or `tests/test_trains.py`

**Step 1: Write the failing test**

Add to `tests/test_dispatcher.py` (or a new test in `tests/test_trains.py`):

```python
def test_route_executor_handles_path_completion_event(network):
    # Setup: route active, path completed event emitted
    # Expect: train transitions to STANDBY and route cleared
    ...
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_dispatcher.py::test_route_executor_handles_path_completion_event -v`

Expected: FAIL (no event handling).

**Step 3: Write minimal implementation**

- Add `RouteExecutor.handle_path_completion(train_id, path_id, end_tile, time)`.
- In `simulation.py`, poll `PathDispatcher` for completion events each tick and forward to `RouteExecutor`.
- In `RouteExecutor`, if train has assigned route and this is the active path:
  - transition to STANDBY
  - clear or advance route as appropriate (final waypoint: clear route)

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_dispatcher.py::test_route_executor_handles_path_completion_event -v`

Expected: PASS.

**Step 5: Commit**

Only if user requests a commit:
`git add simulation.py route_executor.py tests/test_dispatcher.py && git commit -m "feat: route updates on path completion"`

## Task 3: ATO distance-aware control + aggressive service stop

**Files:**
- Modify: `train_controller.py`
- Test: `tests/test_trains.py`

**Step 1: Write the failing test**

Add to `tests/test_trains.py`:

```python
def test_ato_stop_uses_aggressive_service_brake(train_controller):
    # ATO running -> stop command
    # Expect: handle is B-notch (not EB), higher than normal service
    ...

def test_ato_braking_curve_reduces_end_of_path_oscillation(train_controller):
    # Simulate near-end braking with small distance remaining
    # Expect: handle remains in brake or neutral without rapid flip
    ...
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_trains.py::test_ato_stop_uses_aggressive_service_brake -v`

Expected: FAIL (current logic uses basic tolerance).

**Step 3: Write minimal implementation**

- Add a braking-curve target speed calculation using `SpeedProfile.get_braking_distance_cm()` and a service-decel multiplier.
- Update `_ato_calculate_handle()` to choose notch based on distance-aware target and hysteresis.
- Update `ato_stop()` or ATO tick path to set aggressive service brake notch (never EB).

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_trains.py::test_ato_stop_uses_aggressive_service_brake -v`

Expected: PASS.

**Step 5: Commit**

Only if user requests a commit:
`git add train_controller.py tests/test_trains.py && git commit -m "feat: distance-aware ato control"`

## Task 4: Demo auto-start ATO using shared controller

**Files:**
- Modify: `demo_main.py`
- Test: `tests/test_app_smoke.py` (if needed)

**Step 1: Write the failing test**

If feasible, add a small test in `tests/test_app_smoke.py` or `tests/test_trains.py` to ensure demo uses shared controller:

```python
def test_demo_uses_shared_controller_for_ato_start():
    ...
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_app_smoke.py::test_demo_uses_shared_controller_for_ato_start -v`

Expected: FAIL (uses temp controller).

**Step 3: Write minimal implementation**

- Update `demo_main.py` to retrieve the SimulationController's shared controller for the demo train and call `ato_go()` there.

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_app_smoke.py::test_demo_uses_shared_controller_for_ato_start -v`

Expected: PASS.

**Step 5: Commit**

Only if user requests a commit:
`git add demo_main.py tests/test_app_smoke.py && git commit -m "fix: auto-start demo ato"`

## Final Verification

Run mandatory suites (per `AGENTS.md`):

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```

Expected: all tests pass.
