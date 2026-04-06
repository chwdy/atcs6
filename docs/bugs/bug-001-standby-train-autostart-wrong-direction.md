# Bug Findings & Known Issues

This document records confirmed bugs, their root causes, and the tests that reproduce them.

---

## BUG-001: Standby Train Auto-Starts in Wrong Direction When New Path Assigned

**Status**: Confirmed (reproduction test passing)
**Test**: `tests/test_bug_standby_path_autostart.py`
**Reported**: 2026-04-05

### Symptom

In the default demo layout:
1. Train is given a path to the east branch terminal and completes it.
2. `train.state` shows `STANDBY`.
3. User assigns a new path going back toward the east terminal (reverse direction).
4. Train immediately starts moving — no explicit depart command issued.
5. Train moves in the **wrong direction** (continues north/away from destination).
6. UI still shows state as `STANDBY` for at least one tick.

### Root Cause

Five compounding failures, all in `train_controller.py`:

#### 1. `ato_running` is never cleared when ATC brakes the train to zero

When ATC brakes the train to `speed=0` at the end of a path (blocks ahead are occupied/red), the `RUNNING_MOVING → RUNNING_WAITING` transition fires. The transition comment reads "ato_running stays as-is" — meaning `ato_running=True` is never cleared.

#### 2. `_is_standalone_path_complete()` is gated on `ato_running=False`

```python
# train_controller.py ~line 1925
def _is_standalone_path_complete(self) -> bool:
    if self.ato_running:      # <-- this guard kills standalone completion
        return False
    ...
```

Because `ato_running=True` was never cleared, standalone path completion **never fires** while the train is in `RUNNING_WAITING`. The train is stuck.

#### 3. Path completion arrives externally via `dispatcher.complete_path()`

When the path actually does complete (destination block clears, `on_train_progress_from_physics` fires), `path_dispatcher.complete_path()` is called. This clears `train.assigned_path_id`, `train.current_path`, and `train.ato_destination_block_id` — but it does **not** clear `controller.ato_running`. The controller's `ato_running` remains `True`.

#### 4. `assign_complete_path_to_train()` fails for reverse paths

`prepare_path_for_train()` checks `train_idx > path_start_idx` (in block positions sorted by current facing direction). The train is at the north end of the block (y=5), but the reverse path starts at the south end (y=1). This is "past" the path start → returns failure: `"Train is past path start in block"`.

As a result, the train's head tile is **not included in the new path's tile list**, which causes `_path_needs_cab_flip()` to return `False` (the connectivity check cannot run).

#### 5. `RUNNING_WAITING → RUNNING_MOVING` auto-transition fires

With the new path assigned, destination blocks are free → `effective_limit > STOPPED_SPEED_THRESHOLD`. The auto-resume check fires:

```python
# train_controller.py ~line 612-624
elif self.state == TrainState.RUNNING_WAITING:
    if standalone_complete:        # False — blocked by ato_running=True
        ...
    elif (effective_limit > STOPPED_SPEED_THRESHOLD
          and self.power_on
          and not self.eb_active):
        self.state = TrainState.RUNNING_MOVING   # <-- FIRES
        # ato_running stays as-is (True)
```

`ato_running=True` → ATO sets `handle_position = P1` → train accelerates immediately, no command needed. Cab is now `B` (set by `_required_cab_for_path()` fallback), so the train faces south, but `ato_running=True` means it will keep trying to reach whatever destination was stored.

### Why State Appeared as STANDBY

`train.state` is synced from controller only at the end of `_update_train()`. Between the path-completion event (which called `transition_to_standby()` externally) and the next `tick()`, `train.state` showed `STANDBY` — a stale snapshot. One tick later, the controller transitioned to `RUNNING_MOVING` and synced the new state back.

### Relevant Code Locations

| Location | Issue |
|---|---|
| `train_controller.py:612-624` | RUNNING_WAITING → RUNNING_MOVING auto-transition; `ato_running` not cleared |
| `train_controller.py:1925` | `_is_standalone_path_complete()` guard: `if self.ato_running: return False` |
| `path_utils.py:154-160` | `prepare_path_for_train()` rejects train past path start in block |
| `train_controller.py:1960-1996` | `_path_needs_cab_flip()` returns False when head tile not in path |
| `path_dispatcher.py:1309-1386` | `complete_path()` does not clear `controller.ato_running` |

### Suggested Fix

The safest minimal fix is in the `RUNNING_WAITING → RUNNING_MOVING` auto-transition: before auto-resuming, verify that `ato_running` is consistent with the current path context. If the train has a new path assigned but `ato_running=True` from a previous path, clear `ato_running` before allowing the transition.

A deeper fix would ensure `complete_path()` (or `transition_to_standby()`) always clears `ato_running` on the controller, not just on the train model.

### Reproduction

```bash
conda activate general
pytest tests/test_bug_standby_path_autostart.py -v
```

All four tests pass, with `test_full_bug_sequence_running_waiting_auto_start_wrong_direction` printing the confirmation:

```
[BUG CONFIRMED] Train auto-transitioned to RUNNING_MOVING!
  ato_running=True → ATO will accelerate
  active_cab=B, facing=Direction.SOUTH

  Root causes:
  1. ato_running=True never cleared when ATC braked train to 0
  2. assign_complete_path_to_train failed → head not in path
  3. _path_needs_cab_flip returns False → no cab flip
  4. effective_limit > 0 (new path blocks are clear)
  5. RUNNING_WAITING → RUNNING_MOVING auto-transition fires
  6. ato_running=True causes immediate acceleration
```
