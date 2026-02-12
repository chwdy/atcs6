# Path Single Source of Truth Refactor

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the dual-copy path data problem by making `Path.path_tiles` (in PathDispatcher) the single source of truth, so TrainController reads path data directly from the dispatcher instead of a stale copy on `train.current_path`.

**Architecture:** Give TrainController a reference to PathDispatcher. Add a helper method `get_path_tiles()` on TrainController that reads from `dispatcher.active_paths[train.assigned_path_id].path_tiles` and converts to `(x, y, Direction)` format on the fly. Replace all `train.current_path` reads in TrainController and TrainPhysics with this helper. Keep `train.current_path` as a write target for `Train.to_dict()` serialization only (populated lazily from the path). Clean up `ato_destination_block_id` on path completion.

**Tech Stack:** Python 3, pytest (conda run -n general)

**Key insight:** `train.current_path` is `List[(x, y, Direction)]` while `Path.path_tiles` is `List[(x, y)]`. The conversion is trivial — `convert_tiles_to_train_path()` in `path_utils.py` infers Direction from consecutive tile deltas. The helper caches this conversion so we don't recompute every tick.

---

## Background: The Dual-Copy Problem

Currently there are two independent copies of path data:

| | `Path.path_tiles` | `train.current_path` |
|---|---|---|
| Format | `List[(x, y)]` | `List[(x, y, Direction)]` |
| Lives in | `PathDispatcher.active_paths[id]` | `Train` dataclass |
| Updated by | `extend_path()`, `cancel_path()` | Copied once at assignment, then stale |
| Read by | PathDispatcher only | TrainController (63 refs), TrainPhysics (5 refs), Train.to_dict() |

When `extend_path()` updates the Path object, `train.current_path` stays stale. The ATO reads the stale copy and stops at the wrong destination.

Additionally, `ato_destination_block_id` is never cleared on normal path completion — it's only cleared on explicit cancel. This is a latent bug.

## File Impact Summary

### Production files to modify:
- `train_controller.py` — Add `path_dispatcher` field, add `get_path_tiles()` helper, replace 63 `train.current_path` refs
- `train_physics.py` — Replace 5 `train.current_path` refs with helper
- `simulation.py` — Pass `path_dispatcher` to controller factory (line 147)
- `path_dispatcher.py` — Remove inline train sync from `extend_path()` (lines 1526-1561), add `ato_destination_block_id` cleanup to `_complete_path_on_head_arrival()`
- `route_executor.py` — Clean up `ato_destination_block_id` in `handle_path_completion()` (line 118)
- `path_utils.py` — `assign_complete_path_to_train()` still sets `train.current_path` for `to_dict()` — keep but document as cache
- `train.py` — Document `current_path` as a serialization cache, not source of truth
- `demo_main.py` — Pass `path_dispatcher` to `create_controller_for_train()` (line 617)
- `routes/paths.py` — Remove manual `ato_destination_block_id` sync in create_path (lines 73-80)

### Test files to update:
- `tests/test_extend_path_bug.py` — Update assertions (path reads come from dispatcher now)
- `tests/test_trains.py` — 42 refs to `current_path`
- `tests/test_dispatcher.py` — 38 refs to `current_path`
- `tests/test_speed_curve_debug.py` — 2 refs
- `tests/test_signals.py` — 2 refs
- `tests/test_train_position_update.py` — 1 ref

---

## Task 1: Add `path_dispatcher` field to TrainController

**Files:**
- Modify: `train_controller.py:247` (dataclass field)
- Modify: `train_controller.py:4189-4202` (`create_controller_for_train`)
- Modify: `simulation.py:147-150` (pass dispatcher to factory)
- Modify: `demo_main.py:617-619` (pass dispatcher to factory)
- Test: `tests/test_backend.py` (run existing tests — no new tests yet)

**Step 1: Add field to TrainController dataclass**

In `train_controller.py`, after line 247 (`network: Optional["RailwayNetwork"] = None`), add:

```python
    path_dispatcher: Optional["PathDispatcher"] = None
```

Add to the `TYPE_CHECKING` imports at top of file:

```python
if TYPE_CHECKING:
    from path_dispatcher import PathDispatcher
```

**Step 2: Update factory function**

In `train_controller.py`, update `create_controller_for_train` (line 4189):

```python
def create_controller_for_train(
    train_id: str,
    network: "RailwayNetwork",
    path_dispatcher: Optional["PathDispatcher"] = None,
) -> TrainController:
    return TrainController(
        train_id=train_id,
        network=network,
        path_dispatcher=path_dispatcher,
    )
```

**Step 3: Pass dispatcher in SimulationController**

In `simulation.py:147`, change:

```python
controller = create_controller_for_train(
    train_id=train.train_id,
    network=self.network,
)
```
to:
```python
controller = create_controller_for_train(
    train_id=train.train_id,
    network=self.network,
    path_dispatcher=self.path_dispatcher,
)
```

**Step 4: Pass dispatcher in demo_main**

In `demo_main.py:617`, change:

```python
controller = create_controller_for_train(
    train_id=demo_train.train_id,
    network=network,
)
```
to:
```python
controller = create_controller_for_train(
    train_id=demo_train.train_id,
    network=network,
    path_dispatcher=path_dispatcher,
)
```

**Step 5: Run tests**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```

Expected: All pass (no behavior change yet).

**Step 6: Commit**

```bash
git add train_controller.py simulation.py demo_main.py
git commit -m "refactor: add path_dispatcher field to TrainController"
```

---

## Task 2: Add `get_path_tiles()` helper to TrainController

This is the bridge method that reads from PathDispatcher and returns data in the `(x, y, Direction)` format that all existing code expects.

**Files:**
- Modify: `train_controller.py` (add helper method + cache)
- Test: New test in `tests/test_extend_path_bug.py`

**Step 1: Write the failing test**

Add to `tests/test_extend_path_bug.py`:

```python
class TestGetPathTiles:
    """Tests for TrainController.get_path_tiles() reading from PathDispatcher."""

    def test_get_path_tiles_returns_dispatcher_path(self, simple_network):
        """get_path_tiles reads from dispatcher, not train.current_path."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        # Create a train and place it
        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Create a path
        start_block = network.get_track(0, 0).block_section_id
        end_block = network.get_track(0, 2).block_section_id
        result = dispatcher.create_path(
            start_block_id=start_block,
            end_block_id=end_block,
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        # Create controller with dispatcher
        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        # get_path_tiles should return tiles from dispatcher
        path_tiles = controller.get_path_tiles()
        assert path_tiles is not None
        assert len(path_tiles) > 0

        # Each entry should be (x, y, Direction)
        for entry in path_tiles:
            assert len(entry) == 3
            assert isinstance(entry[2], Direction)

    def test_get_path_tiles_returns_none_when_no_path(self, simple_network):
        """get_path_tiles returns None when train has no assigned path."""
        network = simple_network
        dispatcher = PathDispatcher(network)
        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        assert controller.get_path_tiles() is None

    def test_get_path_tiles_reflects_extension(self, simple_network):
        """After extend_path, get_path_tiles returns the extended path immediately."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        # Create initial path
        start_block = network.get_track(0, 0).block_section_id
        mid_block = network.get_track(0, 2).block_section_id
        result = dispatcher.create_path(
            start_block_id=start_block,
            end_block_id=mid_block,
            train_id=train.train_id,
        )
        assert result.success
        train.assigned_path_id = result.path.path_id

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )

        tiles_before = controller.get_path_tiles()
        count_before = len(tiles_before)

        # Extend the path (if there's a further block)
        end_block = network.get_track(0, 4).block_section_id
        if end_block and end_block != mid_block:
            ext_result = dispatcher.extend_path(result.path.path_id, end_block)
            if ext_result.success:
                tiles_after = controller.get_path_tiles()
                assert len(tiles_after) > count_before, (
                    "get_path_tiles should reflect extension immediately"
                )
```

**Step 2: Run test to verify it fails**

```bash
conda run -n general pytest tests/test_extend_path_bug.py::TestGetPathTiles -v
```

Expected: FAIL — `get_path_tiles` doesn't exist yet.

**Step 3: Implement `get_path_tiles()`**

Add to `TrainController` class, near the path-related methods (after `_get_head_path_index` around line 1026):

```python
    def get_path_tiles(self) -> Optional[List[Tuple[int, int, Direction]]]:
        """Get path tiles from PathDispatcher (single source of truth).

        Reads Path.path_tiles from the dispatcher and converts to
        (x, y, Direction) format. Returns None if no active path exists.

        This replaces reading from train.current_path, which was a stale
        copy that didn't reflect path extensions or cancellations.
        """
        train = self._get_train()
        if not train or not train.assigned_path_id:
            return None

        if not self.path_dispatcher:
            # Fallback: no dispatcher reference, use train's copy
            return train.current_path

        path = self.path_dispatcher.get_path(train.assigned_path_id)
        if not path or not path.path_tiles:
            return None

        from path_utils import convert_tiles_to_train_path
        return convert_tiles_to_train_path(path.path_tiles)
```

Note: `convert_tiles_to_train_path` is cheap (single pass over tiles). If profiling shows it's hot, we can cache with a generation counter on Path. But YAGNI — don't cache until measured.

**Step 4: Run test to verify it passes**

```bash
conda run -n general pytest tests/test_extend_path_bug.py::TestGetPathTiles -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add train_controller.py tests/test_extend_path_bug.py
git commit -m "feat: add get_path_tiles() to read path from dispatcher"
```

---

## Task 3: Replace `train.current_path` reads in TrainController

This is the largest task. Replace all 63 `train.current_path` reads with `self.get_path_tiles()`. The key pattern:

**Before:**
```python
if train.current_path:
    for i, (x, y, _) in enumerate(train.current_path):
```

**After:**
```python
path_tiles = self.get_path_tiles()
if path_tiles:
    for i, (x, y, _) in enumerate(path_tiles):
```

**Files:**
- Modify: `train_controller.py` — all methods that read `train.current_path`
- Test: Run full test suite

**Key methods to update (in order of importance):**

1. `_get_head_path_index()` (line 1007) — reads `train.current_path` 5 times
2. `_get_path_scan_step()` (line 1139) — reads it 8 times
3. `_get_blocks_ahead()` (line 1345) — reads it 5 times
4. `_calculate_clearance_to_destination()` (line 631) — reads it 3 times
5. `_calculate_lookahead()` (line 3800+) — reads it 15+ times (biggest method)
6. `_get_path_direction_at_head()` (line 1773) — reads it 8 times
7. `_get_ato_info()` (line 1677) — reads it 4 times
8. `_get_remaining_in_block_from_path()` (line 1259) — reads it 3 times
9. `_get_track_direction_from_path()` (line 830) — reads it 4 times
10. Other scattered reads

**Strategy:** For each method, add `path_tiles = self.get_path_tiles()` at the top and replace `train.current_path` with `path_tiles` throughout. Replace `train.path_index` usage as-is (path_index stays on train — it tracks head position regardless of source).

**Step 1: Update methods one by one**

Work through each method listed above. The transformation is mechanical:
- Replace `train.current_path` → `path_tiles` (local var from `self.get_path_tiles()`)
- Keep `train.path_index` as-is
- Where a method previously returned early on `not train.current_path`, now return early on `not path_tiles`

**Step 2: Run tests after each 2-3 methods**

```bash
conda run -n general pytest tests/test_backend.py -v
```

**Step 3: Commit after all methods updated**

```bash
git add train_controller.py
git commit -m "refactor: TrainController reads path from dispatcher instead of train.current_path"
```

---

## Task 4: Replace `train.current_path` reads in TrainPhysics

**Files:**
- Modify: `train_physics.py:822-851` (2 methods, 5 refs)

**Step 1: Add `get_path_tiles()` helper to TrainPhysics**

TrainPhysics already has a `self.controller` reference (TrainController). Use it:

```python
def _get_path_tiles(self) -> Optional[List[Tuple[int, int, Direction]]]:
    """Get path tiles from controller (reads from dispatcher)."""
    if self.controller:
        return self.controller.get_path_tiles()
    train = self._get_train()
    return train.current_path if train else None
```

**Step 2: Update `_choose_exit_edge` and `_get_next_tile_in_path`**

Replace `train.current_path` with `self._get_path_tiles()`.

**Step 3: Run tests**

```bash
conda run -n general pytest tests/test_backend.py -v
```

**Step 4: Commit**

```bash
git add train_physics.py
git commit -m "refactor: TrainPhysics reads path from controller helper"
```

---

## Task 5: Replace `train.current_path` reads in `Train.to_dict()`

`Train.to_dict()` sends path data to the frontend. It needs path tiles for the lookahead display. Two options:

- **Option A:** Have `to_dict()` read from `train.current_path` which is still populated as a cache by `assign_complete_path_to_train`
- **Option B:** Pass path_tiles into `to_dict()` as a parameter

**Use Option A** — `to_dict()` is called from many places and changing its signature is high-risk for low gain. `train.current_path` stays as a serialization cache that's populated at path assignment. The key change is that TrainController no longer trusts it for control decisions.

**Files:**
- Modify: `train.py:407-412` — update docstring only

**Step 1: Update docstring**

Change the `current_path` field comment in `train.py`:

```python
    # current_path: Cached copy of path tiles for serialization (Train.to_dict).
    # NOT the source of truth for control decisions.
    # TrainController reads path data directly from PathDispatcher.
    # This cache is set by assign_complete_path_to_train() and may be stale
    # after path extension/cancellation — that's OK since it's only used for
    # frontend display, not control logic.
    current_path: Optional[List[Tuple[int, int, Direction]]] = None
```

**Step 2: Commit**

```bash
git add train.py
git commit -m "docs: document current_path as serialization cache, not source of truth"
```

---

## Task 6: Clean up `ato_destination_block_id` on path completion

**Files:**
- Modify: `route_executor.py:118-141` (`handle_path_completion`)
- Modify: `path_dispatcher.py:1202-1253` (`_complete_path_on_head_arrival`)
- Test: New test

**Step 1: Write the failing test**

Add to `tests/test_extend_path_bug.py`:

```python
class TestAtoDestinationCleanup:
    """Tests that ato_destination_block_id is cleared on path completion."""

    def test_destination_cleared_on_path_completion(self, simple_network):
        """ato_destination_block_id should be None after path completes."""
        network = simple_network
        dispatcher = PathDispatcher(network)

        train = Train(train_id="TEST", consist=[TrainCar("C1", "LOCOMOTIVE", 40.0)])
        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        start_block = network.get_track(0, 0).block_section_id
        end_block = network.get_track(0, 2).block_section_id
        result = dispatcher.create_path(
            start_block_id=start_block,
            end_block_id=end_block,
            train_id=train.train_id,
        )
        assert result.success

        train.assigned_path_id = result.path.path_id
        train.ato_destination_block_id = end_block

        controller = create_controller_for_train(
            train_id=train.train_id,
            network=network,
            path_dispatcher=dispatcher,
        )
        controller.ato_destination_block_id = end_block

        # Complete the path
        dispatcher.complete_path(result.path.path_id)

        # ato_destination_block_id should be cleared on the train
        assert train.ato_destination_block_id is None, (
            "train.ato_destination_block_id should be cleared on path completion"
        )
```

**Step 2: Run test to verify it fails**

```bash
conda run -n general pytest tests/test_extend_path_bug.py::TestAtoDestinationCleanup -v
```

Expected: FAIL — destination not cleared.

**Step 3: Implement cleanup in `_complete_path_on_head_arrival`**

In `path_dispatcher.py`, in `_complete_path_on_head_arrival()`, before `del self.active_paths[path.path_id]` (line 1253), add:

```python
        # Clear train's stale destination — path is done
        if path.train_id:
            train_obj = self.network.trains.get(path.train_id)
            if train_obj:
                train_obj.ato_destination_block_id = None
                train_obj.assigned_path_id = None
                train_obj.current_path = None
                train_obj.path_index = 0
                train_obj.reserved_blocks = []
```

Also update `route_executor.py:handle_path_completion` to clear the controller's copy (line 134, before the early return):

```python
        controller = self._get_controller(train)
        controller.ato_destination_block_id = None
        controller.ato_path_id = None
```

Move these lines before the `if controller.control_mode == "ato"` check (line 135) so they always run.

**Step 4: Run test to verify it passes**

```bash
conda run -n general pytest tests/test_extend_path_bug.py::TestAtoDestinationCleanup -v
```

**Step 5: Run full suite**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```

**Step 6: Commit**

```bash
git add path_dispatcher.py route_executor.py tests/test_extend_path_bug.py
git commit -m "fix: clear ato_destination_block_id on path completion"
```

---

## Task 7: Remove stale sync code from `extend_path()`

Now that TrainController reads from the dispatcher directly, the inline sync code we added earlier in `extend_path()` is unnecessary.

**Files:**
- Modify: `path_dispatcher.py:1526-1561` (remove train sync block)

**Step 1: Remove the sync block**

In `path_dispatcher.py`, in `extend_path()`, remove the entire block from "FIX: Update train's path..." to the end of the `if path.train_id:` block (lines 1526-1561). Replace with a simple comment:

```python
                    # Train path data is read directly from this Path object
                    # by TrainController.get_path_tiles() — no sync needed.
```

**Step 2: Run tests**

```bash
conda run -n general pytest tests/test_backend.py -v
```

**Step 3: Commit**

```bash
git add path_dispatcher.py
git commit -m "refactor: remove stale path sync from extend_path (dispatcher is source of truth)"
```

---

## Task 8: Update tests

Several test files directly set or assert `train.current_path`. These tests need updating to work with the new architecture.

**Files:**
- Modify: `tests/test_extend_path_bug.py` — Update to test dispatcher reads
- Modify: `tests/test_dispatcher.py` — Tests that set `train.current_path` for controller
- Modify: `tests/test_trains.py` — Tests that assert `train.current_path` state

**Strategy:** Most tests that set `train.current_path` are testing path assignment or train placement — these still work because `assign_complete_path_to_train` still sets `train.current_path` as a cache. Tests that assert controller behavior based on `current_path` may need to ensure a dispatcher+path is set up instead.

**Step 1: Run full suite, identify failures**

```bash
conda run -n general pytest tests/test_backend.py -v 2>&1 | grep FAILED
```

**Step 2: Fix each failing test**

For tests where a controller reads path data:
- Create a PathDispatcher and create a proper path
- Pass dispatcher to the controller
- Remove direct `train.current_path = [...]` assignments where they were being used as controller input

For tests where `train.current_path` is set for non-controller purposes (train placement, to_dict serialization):
- Keep as-is — these still work since `current_path` is a valid cache

**Step 3: Run full suite**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```

**Step 4: Commit**

```bash
git add tests/
git commit -m "test: update tests for path single-source-of-truth refactor"
```

---

## Task 9: Clean up routes/paths.py manual sync

The create_path route manually sets `ato_destination_block_id` on both controller and train. With the dispatcher as source of truth, the controller can read the destination from `Path.end_block_id` directly.

**Files:**
- Modify: `routes/paths.py:73-80`

**Step 1: Simplify**

The destination sync in create_path (lines 73-80) can stay for now — it's setting the controller's `ato_destination_block_id` which is needed immediately for the first tick. But add a comment noting the architectural direction:

```python
                        # Set destination on controller for immediate ATO use.
                        # In future, controller could read Path.end_block_id directly.
                        controller.ato_destination_block_id = (
                            result.path.end_block_id
                        )
                        train.ato_destination_block_id = result.path.end_block_id
```

**Step 2: Commit**

```bash
git add routes/paths.py
git commit -m "docs: annotate destination sync in create_path route"
```

---

## Task 10: Final validation

**Step 1: Run all test suites**

```bash
# JavaScript syntax
conda run -n general pytest tests/test_javascript_syntax.py -v

# App smoke tests
conda run -n general pytest tests/test_app_smoke.py -v

# Backend tests
conda run -n general pytest tests/test_backend.py -v
```

**Step 2: Start server and verify manually**

```bash
python3 server.py
```

- Create a path from one station to another
- Let train run to destination
- Verify train stops correctly
- Extend the path mid-journey
- Verify train follows extended path and stops at new destination
- Verify path completion clears ato_destination_block_id

**Step 3: Final commit if any cleanup needed**

---

## Ordering Notes

Tasks 1-2 are foundational — everything depends on them.
Task 3 is the bulk of the work (63 replacements in train_controller.py).
Task 4 is independent of 3 (physics has its own helper).
Task 5 is documentation only.
Task 6 is the ato_destination_block_id cleanup (independent bug fix).
Task 7 depends on Task 3 (can only remove sync after controller reads from dispatcher).
Task 8 depends on Tasks 3-4 (test fixes for new behavior).
Tasks 9-10 are cleanup.

Critical path: **1 → 2 → 3 → 7 → 8 → 10**
