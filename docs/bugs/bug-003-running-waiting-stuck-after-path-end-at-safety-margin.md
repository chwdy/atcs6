# Bug Findings & Known Issues

This document records confirmed bugs, their root causes, and the tests that reproduce them.

---

## BUG-003: Train Stuck in RUNNING_WAITING After Path Ends at Safety Margin

**Status**: Fixed
**Test**: `tests/test_bug_dual_path_and_rw_completion.py::TestBugRunningWaitingAtPathEnd`
**Reported**: 2026-04-05
**Fixed**: 2026-04-05

### Symptom

1. Train is assigned a path ending at block 10,1 (or any terminal block).
2. ATO runs the train toward the destination.
3. ATC brakes the train to 0 at the safety margin before the path end tile.
4. Path finishes (either in the same tick or the next).
5. Train state becomes `RUNNING_WAITING` and **stays there indefinitely**.
6. State machine doc requires `STANDBY` — `RUNNING_WAITING` must have an active path/route context.

### Root Cause

Two independent failures compound to produce the stuck state.

#### Root Cause A — `_complete_path_on_head_arrival` misses RUNNING_WAITING state

The tick execution order matters:

1. `controller.tick()` — step 8 state transitions:
   - `effective_limit = 0` (ATC safety margin) AND `train.current_speed = 0`
   - Fires: `train.state = TrainState.RUNNING_WAITING` (sets both `controller.state` and `train.state` directly)

2. `on_train_progress_from_physics()` — path completion check:
   - `path.status == CLEARING` and `train.is_stopped()` → calls `_complete_path_on_head_arrival()`

3. Inside `_complete_path_on_head_arrival()`:
   ```python
   if train_obj.state == TrainState.RUNNING_MOVING:   # ← WRONG: state is now RUNNING_WAITING
       controller.transition_to_standby()              # ← never called
   ```
   Because step 1 already set `train.state = RUNNING_WAITING` before step 3 runs,
   the check is always False. `transition_to_standby()` is never called.

**Fix**: Changed to `if train_obj.state in (TrainState.RUNNING_MOVING, TrainState.RUNNING_WAITING)`.
See `path_dispatcher.py:_complete_path_on_head_arrival()`.

#### Root Cause B — `_sim_controller` never wired in real Flask app

`_complete_path_on_head_arrival` reaches the `TrainController` through an indirect chain:

```python
sim_controller = getattr(self.network.power_controller, "_sim_controller", None)
if sim_controller:
    controller = sim_controller.get_controller(train_id)
    if controller:
        controller.ato_running = False          # ← only cleared if chain succeeds
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
        if train_obj.state in (...):
            controller.transition_to_standby()
```

`network.power_controller._sim_controller` was **never set** in:
- `bootstrap.py` — `SimulationController` created but not wired back
- `server.py` — same
- `scenario_runner.py` — same

Only the test suite explicitly wired it:
```python
sim.power_controller._sim_controller = sim   # only in tests
```

Without the wiring, `getattr(..., "_sim_controller", None)` returns `None`, the entire
controller-clearing block is silently skipped, and these stale flags remain set:
- `controller.ato_running = True`
- `controller.ato_path_id = <old_path_id>`
- `controller.ato_destination_block_id = <old_block>`

In the next tick, `_is_standalone_path_complete()` checks `_has_path_backed_run_context()`:
```python
def _has_active_path_authority(self, train):
    if self.ato_path_id:   # ← stale flag keeps returning True
        return True
```

`_has_path_backed_run_context()` returns True → `_is_standalone_path_complete()` returns False →
RUNNING_WAITING state never clears. The train is **stuck indefinitely**.

The test suite passed because the tests wired `_sim_controller`. The real app silently failed.

**Fix**: Added `sim_controller.power_controller._sim_controller = sim_controller` in
`bootstrap.py`, `server.py`, and `scenario_runner.py`.

### How Root Causes A and B interact

- Without fix A alone: path completes, controller flags are cleared (if wired), but
  `transition_to_standby()` is not called in that tick. The NEXT tick's
  `_is_standalone_path_complete()` catches it and transitions to STANDBY. One tick late,
  but recovers.
- Without fix B alone: controller flags are never cleared. RUNNING_WAITING persists
  forever. Fix A alone cannot help because `_is_standalone_path_complete()` keeps
  returning False due to stale `ato_path_id`.
- Both fixes together: path completes → controller flags cleared AND `transition_to_standby()`
  called in same tick → STANDBY immediately.

### Relevant Code Locations

| Location | Issue |
|---|---|
| `path_dispatcher.py:_complete_path_on_head_arrival()` | Only handled `RUNNING_MOVING`; missed `RUNNING_WAITING` |
| `bootstrap.py` | `_sim_controller` not wired after `SimulationController` created |
| `server.py` | Same as bootstrap.py |
| `scenario_runner.py` | Same as bootstrap.py |
| `train_controller.py:_has_active_path_authority()` | Trusts `ato_path_id` as live path context |

### Reproduction

```bash
conda activate general
pytest tests/test_bug_dual_path_and_rw_completion.py::TestBugRunningWaitingAtPathEnd -v -s
```

- `test_complete_path_on_head_arrival_skips_standby_for_running_waiting` — demonstrates Root Cause A (pre-fix: fails; post-fix: passes)
- `test_running_waiting_stuck_when_sim_controller_not_wired` — demonstrates Root Cause B behavior (shows stale flags when unwired)
- `test_state_is_standby_after_path_completes_at_safety_margin` — end-to-end: STANDBY after both fixes (post-fix: passes)
