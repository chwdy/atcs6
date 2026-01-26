# Train Path Rendering Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the broken train rendering and lookahead display by ensuring the path-based position system has a single source of truth that flows correctly from simulation to renderer and train monitor.

**Architecture:** Train entity owns `path_tiles`, `end_a_*`, `end_b_*`, `leading_end` fields. TrainMover updates these during simulation. Train.to_dict(network) outputs path_tile_info. Both renderer and train monitor read from the same train data - no separate calculations.

**Tech Stack:** Python 3 (backend), Vanilla JavaScript (frontend), Flask REST API

---

## Issue Analysis

### Root Causes Identified

1. **Dual position systems running in parallel**: Legacy system (`distance_on_track`, `current_pos`) and new path system (`path_tiles`, `end_a_distance_cm`) update independently, causing desync
2. **TrainMover doesn't sync with legacy system**: When path_tiles indices advance, the legacy `current_pos` and `path_index` don't update, causing position discrepancies
3. **path_tile_info may not match current position**: The `path_tile_info` is generated from `path_tiles`, but if indices are wrong, the renderer gets stale data

### Solution Overview

1. **Phase 1**: Ensure TrainMover syncs both systems during movement
2. **Phase 2**: Validate path_tile_info generation includes correct tile data
3. **Phase 3**: Fix renderer to use correct indices from train data
4. **Phase 4**: Ensure train monitor correctly builds lookahead from path_tile_info

---

## Phase 1: Sync TrainMover with Legacy System

### Task 1.1: Verify TrainMover Updates Both Position Systems

**Files:**
- Modify: `train_movement.py:216-272`
- Test: `tests/test_backend.py`

**Step 1: Write test verifying both systems stay in sync**

Add to `tests/test_backend.py`:

```python
class TestTrainMoverPositionSync:
    """Tests for TrainMover keeping legacy and path systems in sync."""

    @pytest.fixture
    def three_tile_network(self):
        """Create a simple 3-tile EW track network."""
        network = RailwayNetwork()
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        network.create_block_with_tracks(
            "B1", [(0, 0), (1, 0), (2, 0)],
            name="Block 1", length_cm=150.0
        )
        return network

    def test_path_system_updates_on_movement(self, three_tile_network):
        """Verify end_a_distance_cm updates during simulation."""
        from train_movement import TrainMover

        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 0
        train.end_a_distance_cm = 25.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 0.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        train.current_speed = 360.0  # Fast for testing

        mover = TrainMover(three_tile_network)
        initial_distance = train.end_a_distance_cm

        mover.update_train_path_position(train, 0.1)

        # Distance should increase
        assert train.end_a_distance_cm > initial_distance

    def test_path_index_advances_on_tile_crossing(self, three_tile_network):
        """Verify end_a_index advances when crossing tile boundary."""
        from train_movement import TrainMover

        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 0
        train.end_a_distance_cm = 49.0  # Near end of first tile (50cm)
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 0.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        train.current_speed = 360.0  # Fast enough to cross

        mover = TrainMover(three_tile_network)

        # Move enough to cross tile boundary
        mover.update_train_path_position(train, 0.1)

        # Should have advanced to next tile
        assert train.end_a_index == 1
        assert train.end_a_entry_edge == "W"  # Entering from west
```

**Step 2: Run tests to verify current behavior**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainMoverPositionSync -v`
Expected: Should pass if existing code works, or reveal specific failures

**Step 3: If tests fail, fix TrainMover update logic**

In `train_movement.py`, the `update_train_path_position` method should correctly:
1. Update head distance based on speed
2. Advance head index when crossing tile boundary
3. Update tail position based on train length

The current implementation looks correct. If tests fail, we need to debug tile length calculation.

**Step 4: Commit**

```bash
git add tests/test_backend.py train_movement.py
git commit -m "test: add TrainMover position sync tests"
```

---

### Task 1.2: Add Debug Logging to TrainMover

**Files:**
- Modify: `train_movement.py:216-272`

**Step 1: Add logging to understand position updates**

In `train_movement.py`, add debug output:

```python
def update_train_path_position(self, train: Train, delta_seconds: float) -> None:
    """Update train position using the path_tiles queue system."""
    if not train.path_tiles or train.current_speed <= 0:
        return

    # Calculate distance to move
    speed_cm_s = kmh_to_cm_per_sec(train.current_speed)
    distance_cm = speed_cm_s * delta_seconds

    # Store initial values for debugging
    initial_head_index = train.head_index
    initial_head_distance = train.head_distance_cm

    # Update head position
    if train.leading_end == "a":
        train.end_a_distance_cm += distance_cm
    else:
        train.end_b_distance_cm += distance_cm

    # Check if head crossed into next tile
    head_tile = train.path_tiles[train.head_index]
    tile_length = self.network._get_tile_length_cm(*head_tile)

    while train.head_distance_cm >= tile_length:
        overflow = train.head_distance_cm - tile_length

        # Try to advance to next tile
        if train.head_index + 1 < len(train.path_tiles):
            next_tile = train.path_tiles[train.head_index + 1]
            new_entry_edge = self._calculate_entry_edge(
                head_tile, next_tile, train.head_entry_edge
            )

            if not train.advance_head(new_entry_edge):
                train.head_distance_cm = tile_length
                break

            train.head_distance_cm = overflow
            head_tile = train.path_tiles[train.head_index]
            tile_length = self.network._get_tile_length_cm(*head_tile)
        else:
            train.head_distance_cm = tile_length
            break

    # Update tail position based on train length
    self._update_tail_for_length(train)
```

**Step 2: Verify the implementation handles tile crossing correctly**

The key issue might be in `_update_tail_for_length`. Let's verify it exists and works.

---

## Phase 2: Ensure path_tile_info Includes Block State

### Task 2.1: Add Block State to path_tile_info

**Files:**
- Modify: `train.py:1393-1423`
- Test: `tests/test_backend.py`

**Step 1: Write test for block state in path_tile_info**

Add to `tests/test_backend.py`:

```python
class TestPathTileInfo:
    """Tests for path_tile_info generation in Train.to_dict()."""

    def test_path_tile_info_includes_block_state(self, network):
        """path_tile_info should include block state for lookahead display."""
        # Setup: Create track with block
        from track import TrackSegment, TRACK_EW
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        network.create_block_with_tracks(
            "B1", [(0, 0), (1, 0), (2, 0)],
            name="Block 1", length_cm=150.0
        )

        # Create train with path
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 0
        train.end_a_distance_cm = 25.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 0.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        train.current_pos = (0, 0)

        data = train.to_dict(network)

        assert "path_tile_info" in data
        assert len(data["path_tile_info"]) == 3

        # First tile should have block_id
        assert data["path_tile_info"][0]["block_id"] == "B1"
```

**Step 2: Run test**

Run: `conda run -n general pytest tests/test_backend.py::TestPathTileInfo -v`
Expected: PASS if current implementation is correct

**Step 3: Add block_state to tile_info if missing**

In `train.py`, update the path_tile_info generation (around line 1401):

```python
# Add path_tile_info if network available
if network and self.path_tiles:
    path_tile_info = []
    for tile in self.path_tiles:
        x, y = tile
        track = network.get_track(x, y)
        tile_info = {
            "tile": [x, y],
            "length_cm": network._get_tile_length_cm(x, y) if track else 50.0,
            "block_id": track.block_section_id if track else None,
        }
        # Add block state if block exists
        if track and track.block_section_id:
            block = network.blocks.get(track.block_section_id)
            if block:
                tile_info["block_state"] = block.state.value
        # ... rest of signal/switch info ...
```

**Step 4: Commit**

```bash
git add train.py tests/test_backend.py
git commit -m "feat: add block_state to path_tile_info for lookahead display"
```

---

## Phase 3: Fix Renderer Position Calculation

### Task 3.1: Verify Renderer Uses Correct Indices

**Files:**
- Review: `static/js/renderer-trains.js:400-480`

**Step 1: Check renderer logic matches backend data**

The renderer's `drawTrainFromPath` should use:
- `train.end_a_index` / `train.end_b_index` for positions in path_tiles
- `train.end_a_distance_cm` / `train.end_b_distance_cm` for position on tile
- `train.path_tile_info[index].length_cm` for tile length

Current code (line 405-411):
```javascript
const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
const tailIndex = train.leading_end === 'a' ? train.end_b_index : train.end_a_index;
const headDistanceCm = train.leading_end === 'a' ? train.end_a_distance_cm : train.end_b_distance_cm;
const tailDistanceCm = train.leading_end === 'a' ? train.end_b_distance_cm : train.end_a_distance_cm;
```

This looks correct. The issue may be that `path_tile_info` is missing or indices are out of bounds.

**Step 2: Add defensive bounds checking**

In `static/js/renderer-trains.js`, enhance `drawTrainFromPath`:

```javascript
drawTrainFromPath(train) {
    const pathTiles = train.path_tiles;
    const pathInfo = train.path_tile_info || [];
    
    // Validate indices
    const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
    const tailIndex = train.leading_end === 'a' ? train.end_b_index : train.end_a_index;
    
    if (headIndex === undefined || headIndex === null || 
        tailIndex === undefined || tailIndex === null) {
        console.warn('[RendererTrainHandler] Missing head/tail indices', {
            headIndex, tailIndex, trainId: train.id
        });
        return;
    }
    
    if (headIndex >= pathTiles.length || tailIndex >= pathTiles.length) {
        console.warn('[RendererTrainHandler] Index out of bounds', {
            headIndex, tailIndex, pathLength: pathTiles.length, trainId: train.id
        });
        return;
    }
    
    // ... rest of rendering
}
```

**Step 3: Run JS syntax test**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add static/js/renderer-trains.js
git commit -m "fix: add bounds checking to renderer drawTrainFromPath"
```

---

## Phase 4: Fix Train Monitor Lookahead

### Task 4.1: Verify Train Monitor Uses path_tile_info Correctly

**Files:**
- Review: `static/js/train-monitor.js:149-240`

**Step 1: Check train monitor receives path_tile_info**

The train monitor's `update(train)` method should:
1. Check for `train.path_tile_info`
2. Build blocks/signals/switches from tiles ahead of head
3. Display in the lookahead bar

Current code checks `!train.path_tile_info` and returns early if missing. This is correct.

**Step 2: Add debug logging to identify missing data**

In `static/js/train-monitor.js`, add logging:

```javascript
update(train) {
    // Check for path-based data
    if (!train) {
        console.warn('[TrainMonitor] No train data');
        this.lookaheadData = null;
        this.render();
        return;
    }
    
    if (!train.path_tile_info) {
        console.warn('[TrainMonitor] Missing path_tile_info', {
            trainId: train.id,
            hasPathTiles: !!train.path_tiles,
            pathTilesLength: train.path_tiles?.length
        });
        this.lookaheadData = null;
        this.render();
        return;
    }
    
    console.log('[TrainMonitor] Updating with path_tile_info', {
        trainId: train.id,
        tileCount: train.path_tile_info.length,
        headIndex: train.leading_end === 'a' ? train.end_a_index : train.end_b_index
    });
    
    // ... rest of update logic
}
```

**Step 3: Run JS syntax test**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add static/js/train-monitor.js
git commit -m "debug: add logging to train monitor for path_tile_info"
```

---

## Phase 5: Integration Testing

### Task 5.1: Run Full Test Suite

**Step 1: Run all mandatory tests**

```bash
# JavaScript syntax validation
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v

# App smoke tests
conda run -n general pytest tests/test_app_smoke.py -v

# Backend unit tests
conda run -n general pytest tests/test_backend.py -v
```

**Step 2: Manual testing**

1. Start server: `python3 server.py`
2. Open browser to `http://localhost:5000`
3. Load a layout with a train
4. Click play to start simulation
5. Verify:
   - Train renders at correct position (head triangle + tail square)
   - Train moves smoothly along track
   - Train monitor shows lookahead data (blocks, signals ahead)
   - Debug panel shows correct end_a/end_b info

**Step 3: Document any remaining issues**

If issues persist after these fixes, the problem may be in:
1. How path_tiles is initialized when train is placed
2. How path_tiles is extended when path is assigned
3. Timing between legacy and new position systems

---

## Summary

This plan addresses the train rendering issues by:

1. **Verifying TrainMover correctly updates path positions** during simulation
2. **Ensuring path_tile_info includes block state** for lookahead display
3. **Adding bounds checking** to renderer to prevent errors
4. **Adding debug logging** to identify data flow issues
5. **Running full test suite** to verify no regressions

The key insight is that the current uncommitted changes are partially implementing the design from `2026-01-13-train-path-refactor-design.md`, but the integration between the new path system and the legacy position system needs verification.

---

## Files Modified

| File | Type | Changes |
|------|------|---------|
| `train_movement.py` | Backend | Verify/fix position update logic |
| `train.py` | Backend | Add block_state to path_tile_info |
| `static/js/renderer-trains.js` | Frontend | Add bounds checking |
| `static/js/train-monitor.js` | Frontend | Add debug logging |
| `tests/test_backend.py` | Tests | Add position sync tests |
