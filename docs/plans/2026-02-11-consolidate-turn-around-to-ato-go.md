# Consolidate Turn-Around Logic to ato_go() Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove redundant turn-around logic from path assignment and route executor, making `ato_go()` the single place where cab direction is resolved before departure.

**Architecture:** Currently there are 3 places that attempt turn-around: path assignment (`routes/paths.py`), route executor (`route_executor.py`), and `ato_go()`. This creates race conditions (e.g. path assigned while train still moving = turn-around skipped). The fix consolidates to a single mechanism: `ato_go()` recalculates `ato_info` fresh and auto-corrects cab. Route executor's `depart()` already delegates to `ato_go()`, so it gets this for free.

**Tech Stack:** Python backend only, no frontend changes.

---

## Summary of Changes

| Location | Current Behavior | New Behavior |
|---|---|---|
| `routes/paths.py:82-106` | Turn around on path assignment (only if stopped) | Remove — just assign the path |
| `route_executor.py:194-197` | `_should_turn_around()` + `turn_around()` before requesting path | Remove — let `ato_go()` handle it |
| `route_executor.py:712-735` | `_should_turn_around()` helper method | Remove (dead code after above) |
| `train_controller.py:ato_go()` | Uses cached `self.ato_info` for cab check | Recalculate `ato_info` fresh before cab check |

---

### Task 1: Make `ato_go()` recalculate `ato_info` fresh

Currently `ato_go()` reads `self.ato_info` which was set by the last `tick()`. If path was assigned between ticks, `ato_info` may be stale. Fix by recalculating inside `ato_go()`.

**Files:**
- Modify: `train_controller.py:3099-3149` (ato_go method)

**Step 1: Modify `ato_go()` to recalculate ato_info**

In `train_controller.py`, in the `ato_go()` method, add a fresh recalculation of `ato_info` before the cab auto-correction check. Replace:

```python
        # ATO Auto-correction: Check and adjust cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
```

With:

```python
        # Recalculate ATO info fresh to ensure cab direction matches
        # the current path (path may have been assigned since last tick)
        self.ato_info = self._calculate_ato_info()

        # ATO Auto-correction: Check and adjust cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
```

**Step 2: Run existing ato_go tests to verify no regression**

Run: `conda run -n general pytest tests/test_dispatcher.py::TestTurnAroundAndCabDirection::test_ato_go_auto_corrects_cab_for_opposite_path tests/test_dispatcher.py::TestTurnAroundAndCabDirection::test_ato_go_no_correction_when_cab_matches -v`
Expected: PASS

---

### Task 2: Remove turn-around logic from path assignment

**Files:**
- Modify: `routes/paths.py:82-106`

**Step 1: Remove the turn-around block from path assignment**

In `routes/paths.py`, remove lines 82-106 (the entire `# Check if train needs to turn around for this path` block). The code after the removal should go directly from setting `train.ato_destination_block_id` to `return jsonify(...)`.

Before (lines 80-107):
```python
                            train.ato_destination_block_id = result.path.end_block_id

                            # Check if train needs to turn around for this path.
                            # Use the controller's ato_info to determine the
                            # required cab — this handles diagonal and 90-degree
                            # cases that a simple "exact opposite" check misses.
                            if (
                                train.position
                                and train.current_path
                                and len(train.current_path) >= 2
                                and train.facing_direction is not None
                                and train.is_stopped()
                            ):
                                # Recalculate ato_info now that the path is assigned
                                controller.ato_info = controller._calculate_ato_info()
                                if (
                                    controller.ato_info
                                    and controller.ato_info.required_cab
                                    and controller.active_cab
                                    and controller.ato_info.required_cab
                                    != controller.active_cab
                                ):
                                    # Stop ATO before turning
                                    if controller.ato_running:
                                        controller.ato_stop()
                                    # Switch cab to match path direction
                                    controller.set_cab(controller.ato_info.required_cab)
            return jsonify(result.to_dict()), 200 if result.success else 400
```

After:
```python
                            train.ato_destination_block_id = result.path.end_block_id
            return jsonify(result.to_dict()), 200 if result.success else 400
```

**Step 2: Run smoke tests**

Run: `conda run -n general pytest tests/test_app_smoke.py tests/test_javascript_syntax.py -v`
Expected: PASS

---

### Task 3: Remove turn-around logic from route executor

**Files:**
- Modify: `route_executor.py:194-197` (remove turn_around call)
- Modify: `route_executor.py:712-735` (remove `_should_turn_around` method)

**Step 1: Remove the `_should_turn_around` + `turn_around()` call from `_try_depart_to_waypoint`**

In `route_executor.py`, remove lines 194-197:
```python
        if self._should_turn_around(train, platform):
            # Ensure cab switch is allowed in all stop-and-reverse route legs.
            controller.ato_running = False
            controller.turn_around()
```

The method should go directly from the platform-not-found check to the target_block_id lookup.

**Step 2: Remove the `_should_turn_around` helper method**

In `route_executor.py`, remove the entire `_should_turn_around` method (lines 712-735):
```python
    def _should_turn_around(self, train: "Train", target_platform: object) -> bool:
        """Check if train should turn around to approach the target."""
        ...
        return False
```

**Step 3: Run backend tests**

Run: `conda run -n general pytest tests/test_backend.py -v`
Expected: PASS (or identify tests that need updating in Task 4)

---

### Task 4: Update tests to match new behavior

Tests that explicitly simulate the path-assignment turn-around logic need to be updated to instead test that `ato_go()` handles it.

**Files:**
- Modify: `tests/test_dispatcher.py`

**Step 1: Update `test_path_assignment_turn_around_opposite`**

This test (line 3202) currently simulates the path-assignment turn-around code that we just removed. Update it to verify that `ato_go()` handles the turn-around instead.

Replace the test body so that after assigning the path, it calls `ato_go()` and verifies the cab was switched:

```python
    def test_path_assignment_turn_around_opposite(self):
        """ato_go() should turn train around when path is opposite direction.

        Previously path assignment handled turn-around directly, but now
        ato_go() is the single point for cab auto-correction.
        """
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (4, 0), Direction.EAST, "BLK_EAST")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.control_mode = "ato"

        assert controller.active_cab == "A"

        # Assign a return path going WEST (no turn-around happens here anymore)
        train.current_path = [
            (4, 0, Direction.WEST),
            (3, 0, Direction.WEST),
            (2, 0, Direction.WEST),
        ]
        train.assigned_path_id = "test_path"
        controller.ato_destination_block_id = "BLK_WEST"
        train.ato_destination_block_id = "BLK_WEST"

        # Cab should NOT have changed yet — path assignment no longer turns
        assert controller.active_cab == "A"

        # ato_go() should auto-correct the cab
        result = controller.ato_go()
        assert result.success, f"ato_go failed: {result.message}"
        assert controller.active_cab == "B"
        assert train.facing_direction == Direction.WEST
```

**Step 2: Update `test_path_assignment_no_turn_when_same_direction`**

Similarly update this test (line 3249) to go through `ato_go()`:

```python
    def test_path_assignment_no_turn_when_same_direction(self):
        """ato_go() should NOT switch cab when path matches facing direction."""
        network = self._build_straight_ew_network()
        train = self._create_stopped_train(network, (2, 0), Direction.EAST, "BLK_MID")

        sim = SimulationController(network, PathDispatcher(network))
        controller = sim._get_or_create_controller(train)
        controller.power_on_cmd()
        controller.set_direction_and_standby(Direction.EAST)
        controller.control_mode = "ato"

        assert controller.active_cab == "A"

        # Assign forward path going EAST
        train.current_path = [
            (2, 0, Direction.EAST),
            (3, 0, Direction.EAST),
            (4, 0, Direction.EAST),
        ]
        train.assigned_path_id = "test_path"
        controller.ato_destination_block_id = "BLK_EAST"
        train.ato_destination_block_id = "BLK_EAST"

        # ato_go() should NOT switch cab
        result = controller.ato_go()
        assert result.success
        assert controller.active_cab == "A"
        assert train.facing_direction == Direction.EAST
```

**Step 3: Run all tests**

Run: `conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v`
Expected: ALL PASS

**Step 4: Commit**

```
git add routes/paths.py route_executor.py train_controller.py tests/test_dispatcher.py
git commit -m "refactor: consolidate turn-around logic to ato_go()

Remove redundant cab-switching from path assignment (routes/paths.py)
and route executor (_should_turn_around). ato_go() now recalculates
ato_info fresh and is the single point for cab auto-correction.
depart() delegates to ato_go(), so route executor gets this for free."
```
