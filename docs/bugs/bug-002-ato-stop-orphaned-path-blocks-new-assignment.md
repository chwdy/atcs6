# Bug Findings & Known Issues

This document records confirmed bugs, their root causes, and the tests that reproduce them.

---

## BUG-002: Orphaned Path After ato_stop → STANDBY Blocks New Path Creation

**Status**: Fixed at API/frontend conflict boundary; dispatcher/controller behavior intentionally unchanged

**Tests**:
- Reproduction: `tests/test_bug_dual_path_and_rw_completion.py::TestBugDualPathAfterAtoStop`
- Regression: `tests/test_api_routes.py::TestPathRoutes::test_create_path_conflicts_when_train_already_has_active_path`

**Reported**: 2026-04-05

### Symptom

1. Train is assigned a path P1 (e.g., BLOCK_A → BLOCK_B) and ATO is started.
2. User calls ATO stop (`ato_stop()`) — train brakes and transitions to `STANDBY`.
3. User tries to create a new path P2 in a different direction for the same train.
4. Path creation fails: **"All paths blocked"** — even though the train is stopped.
5. If the new path uses blocks not overlapping with P1, both P1 and P2 can coexist
   in the dispatcher simultaneously, with two paths referencing the same `train_id`.

### Root Cause

`ato_stop()` only sets `ato_running = False`. Path cancellation is not triggered:

```python
# train_controller.py ato_stop()
def ato_stop(self) -> CommandResult:
    ...
    self.ato_running = False
    # Path P1 still in path_dispatcher.active_paths
    # train.assigned_path_id still points to P1
    # P1 still holds block reservations
    return CommandResult(True, "ATO stopping")
```

When the train reaches speed 0, `transition_to_standby()` is called (tick step 6):

```python
def transition_to_standby(self) -> None:
    self.state = TrainState.STANDBY
    self.ato_running = False
    self.path_backed_run_active = False
    # Does NOT cancel path in dispatcher
    # Does NOT clear train.assigned_path_id
    # Does NOT clear ato_path_id / ato_destination_block_id
```

Result: train is in `STANDBY` but P1 is an orphan — still alive in the dispatcher,
still holding block reservations, still tagged with `train_id`.

Before the fix, when the user subsequently created P2 via `POST /api/paths`:
- If P2 routes through any block reserved by P1 → **"All paths blocked"** (user-visible error)
- If P2 uses different blocks → both P1 and P2 exist with the same `train_id` → undefined behavior when ATO starts

### Relevant Code Locations

| Location | Issue |
|---|---|
| `train_controller.py:ato_stop()` | Does not cancel or detach the active path |
| `train_controller.py:transition_to_standby()` | Does not clean up dispatcher-side path state |
| `routes/paths.py:create_path()` | Now checks for an existing active assigned path and returns HTTP 409 |

### Fix Approach

**Do not auto-cancel the path in `ato_stop()`** — the path may still be needed
(e.g., the operator might resume by calling `ato_go()` again after a brief service stop).

Instead, the fix is at the **API/frontend boundary**:

1. **Backend** (`routes/paths.py` `POST /api/paths`): when a `train_id` is supplied
   and `train.assigned_path_id` already points to an active path, return a conflict
   response (HTTP 409) with `"conflict": "train_has_active_path"` and the existing
   path ID. Do not auto-cancel.

2. **Frontend**: on receiving a 409 conflict, show a confirmation dialog:
   > "Train T1 already has an active path. Cancel the existing path and assign the new one?"
   
   If the user confirms, the frontend calls `DELETE /api/paths/<old_path_id>` first,
   then retries `POST /api/paths`.

This keeps the decision with the operator, matching the dispatcher's role as a tool
that surfaces conflicts rather than resolving them silently.

### Reproduction

```bash
conda activate general
pytest tests/test_bug_dual_path_and_rw_completion.py::TestBugDualPathAfterAtoStop -v -s
```

`test_ato_stop_cancels_path_and_transitions_to_standby` documents the current behavior:
path P1 remains in the dispatcher after `ato_stop()` → STANDBY.

`test_orphaned_path_blocks_new_path_creation` shows the user-visible symptom:
new path creation fails with "All paths blocked" because P1's reservations are not released.

The reproduction tests still assert the low-level dispatcher/controller condition:
`ato_stop()` transitions the stopped train to `STANDBY` but leaves P1 active. This is
intentional because an operator may resume ATO on that same path.

The fix is verified by the API regression test: assigning another path to the same
train now returns HTTP 409 before the dispatcher attempts pathfinding or creates a
second train path. The frontend preserves that 409 payload, prompts the operator,
cancels the old path only after confirmation, and then retries creation.
