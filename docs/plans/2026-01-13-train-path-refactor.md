# Train Path Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Unify train position tracking using a path-based data structure that both layout rendering and train monitor read from, eliminating data discrepancies.

**Architecture:** Train entity owns a `path_tiles` list with `end_a_*`/`end_b_*` positions and `leading_end` for efficient reversing. TrainMover updates positions based on simulation. Both layout renderer and train monitor read from the same train state - no separate lookahead calculation.

**Tech Stack:** Python 3 (backend), Vanilla JavaScript (frontend), Flask REST API

---

## Phase 1: Backend Data Structure

### Task 1.1: Add Path Fields to Train Entity

**Files:**
- Modify: `train.py`
- Test: `tests/test_backend.py`

**Step 1: Write failing tests for new train path fields**

Add to `tests/test_backend.py`:

```python
class TestTrainPathTiles:
    """Tests for new path-based train position system."""

    def test_train_path_tiles_initialization(self):
        """Train should initialize with empty path_tiles."""
        train = Train(train_id="T1")
        assert train.path_tiles == []
        assert train.end_a_index == 0
        assert train.end_b_index == 0
        assert train.end_a_distance_cm == 0.0
        assert train.end_b_distance_cm == 0.0
        assert train.end_a_entry_edge is None
        assert train.end_b_entry_edge is None
        assert train.leading_end == "a"

    def test_train_head_tail_properties(self):
        """Head/tail properties should delegate to correct end based on leading_end."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 2
        train.end_a_distance_cm = 25.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 10.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        
        # With leading_end='a', head is end_a, tail is end_b
        assert train.head_index == 2
        assert train.tail_index == 0
        assert train.head_distance_cm == 25.0
        assert train.tail_distance_cm == 10.0
        
    def test_train_reverse_flips_leading_end(self):
        """Reverse should flip leading_end without changing positions."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0)]
        train.end_a_index = 1
        train.end_b_index = 0
        train.leading_end = "a"
        
        train.reverse_direction()
        
        assert train.leading_end == "b"
        # Indices unchanged
        assert train.end_a_index == 1
        assert train.end_b_index == 0
        # But head/tail swap
        assert train.head_index == 0
        assert train.tail_index == 1

    def test_train_occupied_tiles_property(self):
        """occupied_tiles should return tiles between tail and head."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
        train.end_a_index = 3  # head at (3, 0)
        train.end_b_index = 1  # tail at (1, 0)
        train.leading_end = "a"
        
        occupied = train.occupied_tiles
        assert occupied == [(1, 0), (2, 0), (3, 0)]
```

**Step 2: Run tests to verify they fail**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathTiles -v`
Expected: FAIL with AttributeError for missing path_tiles field

**Step 3: Add path fields to Train dataclass**

In `train.py`, add new fields after existing position fields (around line 145):

```python
    # === NEW PATH-BASED POSITION SYSTEM ===
    # Full path: passed tiles + occupied tiles + ahead tiles
    path_tiles: List[Tuple[int, int]] = field(default_factory=list)
    
    # Two physical ends (don't change on reverse)
    end_a_index: int = 0           # Index into path_tiles
    end_a_distance_cm: float = 0.0 # Distance from entry edge on this tile
    end_a_entry_edge: Optional[str] = None  # 'N', 'E', 'S', 'W'
    
    end_b_index: int = 0
    end_b_distance_cm: float = 0.0
    end_b_entry_edge: Optional[str] = None
    
    # Which end is currently leading (flip on reverse)
    leading_end: str = "a"  # 'a' or 'b'
```

**Step 4: Add derived properties to Train class**

Add after the existing properties (around line 280):

```python
    # === PATH POSITION PROPERTIES ===
    
    @property
    def head_index(self) -> int:
        """Index of head tile in path_tiles."""
        return self.end_a_index if self.leading_end == "a" else self.end_b_index
    
    @property
    def tail_index(self) -> int:
        """Index of tail tile in path_tiles."""
        return self.end_b_index if self.leading_end == "a" else self.end_a_index
    
    @property
    def head_distance_cm(self) -> float:
        """Distance from entry edge on head tile."""
        return self.end_a_distance_cm if self.leading_end == "a" else self.end_b_distance_cm
    
    @head_distance_cm.setter
    def head_distance_cm(self, value: float) -> None:
        if self.leading_end == "a":
            self.end_a_distance_cm = value
        else:
            self.end_b_distance_cm = value
    
    @property
    def tail_distance_cm(self) -> float:
        """Distance from entry edge on tail tile."""
        return self.end_b_distance_cm if self.leading_end == "a" else self.end_a_distance_cm
    
    @tail_distance_cm.setter
    def tail_distance_cm(self, value: float) -> None:
        if self.leading_end == "a":
            self.end_b_distance_cm = value
        else:
            self.end_a_distance_cm = value
    
    @property
    def head_entry_edge(self) -> Optional[str]:
        """Entry edge for head tile."""
        return self.end_a_entry_edge if self.leading_end == "a" else self.end_b_entry_edge
    
    @head_entry_edge.setter
    def head_entry_edge(self, value: Optional[str]) -> None:
        if self.leading_end == "a":
            self.end_a_entry_edge = value
        else:
            self.end_b_entry_edge = value
    
    @property
    def tail_entry_edge(self) -> Optional[str]:
        """Entry edge for tail tile."""
        return self.end_b_entry_edge if self.leading_end == "a" else self.end_a_entry_edge
    
    @tail_entry_edge.setter
    def tail_entry_edge(self, value: Optional[str]) -> None:
        if self.leading_end == "a":
            self.end_b_entry_edge = value
        else:
            self.end_a_entry_edge = value
    
    @property
    def occupied_tiles(self) -> List[Tuple[int, int]]:
        """Tiles currently occupied by train (tail to head)."""
        if not self.path_tiles:
            return []
        start = min(self.tail_index, self.head_index)
        end = max(self.tail_index, self.head_index)
        return self.path_tiles[start:end + 1]
    
    def reverse_direction(self) -> None:
        """Flip which end is the head (no position recalculation needed)."""
        self.leading_end = "b" if self.leading_end == "a" else "a"
```

**Step 5: Run tests to verify they pass**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathTiles -v`
Expected: PASS

**Step 6: Commit**

```bash
git add train.py tests/test_backend.py
git commit -m "feat(train): add path_tiles and end_a/end_b position fields"
```

---

### Task 1.2: Add Queue Methods to Train

**Files:**
- Modify: `train.py`
- Test: `tests/test_backend.py`

**Step 1: Write failing tests for queue methods**

Add to `tests/test_backend.py`:

```python
class TestTrainPathQueue:
    """Tests for train path queue operations."""

    def test_advance_head_increments_index(self):
        """advance_head should move to next tile in path."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 1
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.leading_end = "a"
        
        train.advance_head("W")
        
        assert train.end_a_index == 2
        assert train.end_a_entry_edge == "W"
        assert train.end_a_distance_cm == 0.0

    def test_advance_tail_increments_index(self):
        """advance_tail should move tail to next tile."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 2
        train.end_b_index = 0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        
        train.advance_tail("W")
        
        assert train.end_b_index == 1
        assert train.end_b_entry_edge == "W"

    def test_extend_path_appends_tiles(self):
        """extend_path should append new tiles to path_tiles."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0)]
        
        train.extend_path([(2, 0), (3, 0)])
        
        assert train.path_tiles == [(0, 0), (1, 0), (2, 0), (3, 0)]

    def test_clear_path_resets_all(self):
        """clear_path should reset path and indices."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 2
        train.end_b_index = 0
        
        train.clear_path()
        
        assert train.path_tiles == []
        assert train.end_a_index == 0
        assert train.end_b_index == 0
```

**Step 2: Run tests to verify they fail**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathQueue -v`
Expected: FAIL with AttributeError for missing advance_head method

**Step 3: Add queue methods to Train class**

Add after the properties (around line 350):

```python
    # === PATH QUEUE METHODS ===
    
    def advance_head(self, new_entry_edge: str) -> bool:
        """Move head to next tile in path.
        
        Args:
            new_entry_edge: Entry edge for the new tile ('N', 'E', 'S', 'W')
            
        Returns:
            True if advanced, False if at end of path
        """
        if self.leading_end == "a":
            if self.end_a_index + 1 >= len(self.path_tiles):
                return False
            self.end_a_index += 1
            self.end_a_entry_edge = new_entry_edge
            self.end_a_distance_cm = 0.0
        else:
            if self.end_b_index + 1 >= len(self.path_tiles):
                return False
            self.end_b_index += 1
            self.end_b_entry_edge = new_entry_edge
            self.end_b_distance_cm = 0.0
        return True
    
    def advance_tail(self, new_entry_edge: str) -> bool:
        """Move tail to next tile (called when tail leaves a tile).
        
        Args:
            new_entry_edge: Entry edge for the new tile
            
        Returns:
            True if advanced, False if tail would pass head
        """
        if self.leading_end == "a":
            if self.end_b_index + 1 > self.end_a_index:
                return False
            self.end_b_index += 1
            self.end_b_entry_edge = new_entry_edge
            self.end_b_distance_cm = 0.0
        else:
            if self.end_a_index + 1 > self.end_b_index:
                return False
            self.end_a_index += 1
            self.end_a_entry_edge = new_entry_edge
            self.end_a_distance_cm = 0.0
        return True
    
    def extend_path(self, tiles: List[Tuple[int, int]]) -> None:
        """Append tiles to the path (when path is extended).
        
        Args:
            tiles: List of (x, y) positions to append
        """
        self.path_tiles.extend(tiles)
    
    def clear_path(self) -> None:
        """Clear path and reset all position data (when path is cancelled)."""
        self.path_tiles = []
        self.end_a_index = 0
        self.end_b_index = 0
        self.end_a_distance_cm = 0.0
        self.end_b_distance_cm = 0.0
        self.end_a_entry_edge = None
        self.end_b_entry_edge = None
```

**Step 4: Run tests to verify they pass**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathQueue -v`
Expected: PASS

**Step 5: Commit**

```bash
git add train.py tests/test_backend.py
git commit -m "feat(train): add path queue methods (advance_head, advance_tail, extend_path, clear_path)"
```

---

### Task 1.3: Update Train.to_dict() for Path Data

**Files:**
- Modify: `train.py`
- Test: `tests/test_backend.py`

**Step 1: Write failing test for to_dict path fields**

Add to `tests/test_backend.py`:

```python
class TestTrainPathSerialization:
    """Tests for train path serialization."""

    def test_to_dict_includes_path_fields(self, network):
        """to_dict should include all path-related fields."""
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0)]
        train.end_a_index = 1
        train.end_a_distance_cm = 25.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 10.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        
        data = train.to_dict(network)
        
        assert data["path_tiles"] == [(0, 0), (1, 0)]
        assert data["end_a_index"] == 1
        assert data["end_a_distance_cm"] == 25.0
        assert data["end_a_entry_edge"] == "W"
        assert data["end_b_index"] == 0
        assert data["end_b_distance_cm"] == 10.0
        assert data["end_b_entry_edge"] == "W"
        assert data["leading_end"] == "a"
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathSerialization -v`
Expected: FAIL with KeyError for path_tiles

**Step 3: Update to_dict method**

In `train.py`, update the `to_dict` method (around line 896) to add path fields:

```python
    def to_dict(self, network=None) -> dict:
        """Convert train to dictionary for serialization.

        Args:
            network: Optional RailwayNetwork to calculate tile length for distance ratio
        """
        data = {
            "id": self.train_id,
            "length": self.length,
            "speed": self.current_speed,
            "max_speed": self.max_speed,
            "occupied_tracks": self.occupied_tracks,
            "model": self.model,
            "min_radius_cm": self.min_radius_cm,
            "facing_direction": self.facing_direction.name
            if self.facing_direction
            else None,
            "travel_direction": self.travel_direction,
            "state": self.state.value,
            "speed_profile_id": self.speed_profile_id,
            "assigned_route_id": self.assigned_route_id,
            "target_speed": self.target_speed,
            "manual_target_speed": self.manual_target_speed,
            "wait_reason": self.wait_reason,
            "speed_limit_reason": self.speed_limit_reason,
            # === NEW PATH FIELDS ===
            "path_tiles": self.path_tiles,
            "end_a_index": self.end_a_index,
            "end_a_distance_cm": self.end_a_distance_cm,
            "end_a_entry_edge": self.end_a_entry_edge,
            "end_b_index": self.end_b_index,
            "end_b_distance_cm": self.end_b_distance_cm,
            "end_b_entry_edge": self.end_b_entry_edge,
            "leading_end": self.leading_end,
        }
        
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
                # Add signal info if present
                if track and track.signal_ids:
                    tile_info["signals"] = []
                    for sig_id in track.signal_ids:
                        signal = network.signals.get(sig_id)
                        if signal:
                            tile_info["signals"].append({
                                "id": sig_id,
                                "state": signal.state.value if signal.state else "unknown",
                                "direction": signal.detection_direction,
                            })
                # Add switch info if present
                if track and track.is_switch:
                    tile_info["is_switch"] = True
                    tile_info["switch_state"] = track.switch_state
                path_tile_info.append(tile_info)
            data["path_tile_info"] = path_tile_info

        # ... rest of existing to_dict code for position, direction, schedule ...
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainPathSerialization -v`
Expected: PASS

**Step 5: Run all tests to ensure no regressions**

Run: `conda run -n general pytest tests/test_backend.py -v --tb=short`
Expected: All tests pass

**Step 6: Commit**

```bash
git add train.py tests/test_backend.py
git commit -m "feat(train): add path fields to to_dict serialization"
```

---

## Phase 2: Backend Movement Logic

### Task 2.1: Update TrainMover for Path Queue

**Files:**
- Modify: `train_movement.py`
- Test: `tests/test_backend.py`

**Step 1: Write failing tests for new TrainMover logic**

Add to `tests/test_backend.py`:

```python
class TestTrainMoverPathQueue:
    """Tests for TrainMover using path queue system."""
    
    @pytest.fixture
    def simple_track_network(self):
        """Create a simple 3-tile straight track network."""
        network = RailwayNetwork()
        from track import TrackSegment, TRACK_EW
        for x in range(3):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        # Create a block for length calculation
        network.create_block_with_tracks(
            "B1", [(0, 0), (1, 0), (2, 0)], 
            name="Block 1", length_cm=150.0
        )
        return network
    
    def test_update_head_position(self, simple_track_network):
        """TrainMover should update head distance correctly."""
        from train_movement import TrainMover
        
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0)]
        train.end_a_index = 1
        train.end_a_distance_cm = 20.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 0.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        train.current_speed = 36.0  # km/h
        
        mover = TrainMover(simple_track_network)
        # Simulate 1 second of movement
        mover.update_train_path_position(train, 1.0)
        
        # At 36 km/h with 1:160 scale, speed is ~0.625 cm/s
        # After 1 second, distance should increase by ~0.625
        assert train.end_a_distance_cm > 20.0
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainMoverPathQueue -v`
Expected: FAIL with AttributeError for missing update_train_path_position

**Step 3: Add update_train_path_position method to TrainMover**

In `train_movement.py`, add after existing methods (around line 176):

```python
    def update_train_path_position(
        self, train: Train, delta_seconds: float
    ) -> None:
        """Update train position using the path_tiles queue system.
        
        This method:
        1. Calculates distance to advance based on speed
        2. Updates head distance
        3. Handles tile transitions (advance_head)
        4. Updates tail position based on train length
        
        Args:
            train: Train to update
            delta_seconds: Time elapsed since last update
        """
        from train_config import kmh_to_cm_per_sec
        
        if not train.path_tiles or train.current_speed <= 0:
            return
        
        # Calculate distance to move
        speed_cm_s = kmh_to_cm_per_sec(train.current_speed)
        distance_cm = speed_cm_s * delta_seconds
        
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
                # Calculate new entry edge based on track geometry
                current_track = self.network.get_track(*head_tile)
                next_tile = train.path_tiles[train.head_index + 1]
                
                new_entry_edge = self._calculate_entry_edge(
                    head_tile, next_tile, train.head_entry_edge, current_track
                )
                
                if not train.advance_head(new_entry_edge):
                    # Can't advance, cap at tile end
                    train.head_distance_cm = tile_length
                    break
                
                train.head_distance_cm = overflow
                head_tile = train.path_tiles[train.head_index]
                tile_length = self.network._get_tile_length_cm(*head_tile)
            else:
                # At end of path, cap at tile end
                train.head_distance_cm = tile_length
                break
        
        # Update tail position based on train length
        self._update_tail_for_length(train)
    
    def _calculate_entry_edge(
        self,
        from_tile: Tuple[int, int],
        to_tile: Tuple[int, int],
        current_entry_edge: str,
        track: Optional[TrackSegment]
    ) -> str:
        """Calculate entry edge when moving from one tile to another.
        
        The entry edge is the opposite of the edge we exit through.
        """
        dx = to_tile[0] - from_tile[0]
        dy = to_tile[1] - from_tile[1]
        
        # Determine which edge we exit through
        if dx == 1:
            exit_edge = "E"
        elif dx == -1:
            exit_edge = "W"
        elif dy == 1:
            exit_edge = "N"
        elif dy == -1:
            exit_edge = "S"
        else:
            return current_entry_edge or "W"
        
        # Entry edge is opposite of exit edge
        opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
        return opposite[exit_edge]
    
    def _update_tail_for_length(self, train: Train) -> None:
        """Update tail position based on train length.
        
        Calculates total distance from tail to head and advances tail
        when it exceeds train length.
        """
        if not train.path_tiles:
            return
        
        # Get train length in cm
        train_length_cm = train.length * 100.0 * 100.0  # length is in track units
        if train.consist:
            train_length_cm = sum(car.length for car in train.consist)
        else:
            train_length_cm = 50.0  # Default 50cm
        
        # Calculate total distance from tail to head
        total_distance = 0.0
        
        # Distance from tail entry to end of tail tile
        tail_tile = train.path_tiles[train.tail_index]
        tail_tile_length = self.network._get_tile_length_cm(*tail_tile)
        total_distance += tail_tile_length - train.tail_distance_cm
        
        # Full tiles between tail and head
        for i in range(train.tail_index + 1, train.head_index):
            tile = train.path_tiles[i]
            total_distance += self.network._get_tile_length_cm(*tile)
        
        # Distance from head tile entry to head position
        if train.head_index > train.tail_index:
            total_distance += train.head_distance_cm
        
        # Advance tail while total distance exceeds train length
        while total_distance > train_length_cm and train.tail_index < train.head_index:
            # How much to advance tail
            advance_distance = total_distance - train_length_cm
            
            # Distance remaining in current tail tile
            remaining_in_tail = tail_tile_length - train.tail_distance_cm
            
            if advance_distance >= remaining_in_tail:
                # Advance to next tile
                if train.tail_index + 1 <= train.head_index:
                    next_tile = train.path_tiles[train.tail_index + 1]
                    current_tile = train.path_tiles[train.tail_index]
                    new_entry = self._calculate_entry_edge(
                        current_tile, next_tile, train.tail_entry_edge, None
                    )
                    train.advance_tail(new_entry)
                    train.tail_distance_cm = 0.0
                    total_distance -= remaining_in_tail
                    
                    # Update for next iteration
                    tail_tile = train.path_tiles[train.tail_index]
                    tail_tile_length = self.network._get_tile_length_cm(*tail_tile)
                else:
                    break
            else:
                # Advance within current tile
                train.tail_distance_cm += advance_distance
                break
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_backend.py::TestTrainMoverPathQueue -v`
Expected: PASS

**Step 5: Commit**

```bash
git add train_movement.py tests/test_backend.py
git commit -m "feat(train_movement): add path queue based position update"
```

---

### Task 2.2: Integrate TrainMover with SimulationController

**Files:**
- Modify: `simulation.py`
- Test: `tests/test_backend.py`

**Step 1: Write integration test**

Add to `tests/test_backend.py`:

```python
class TestSimulationPathIntegration:
    """Tests for simulation using path queue system."""
    
    def test_simulation_updates_path_position(self):
        """SimulationController should update train path position."""
        from simulation import SimulationController
        
        network = RailwayNetwork()
        from track import TrackSegment, TRACK_EW
        for x in range(5):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)
        network.create_block_with_tracks(
            "B1", [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)],
            name="Block 1", length_cm=250.0
        )
        
        train = Train(train_id="T1")
        train.path_tiles = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
        train.end_a_index = 1
        train.end_a_distance_cm = 25.0
        train.end_a_entry_edge = "W"
        train.end_b_index = 0
        train.end_b_distance_cm = 0.0
        train.end_b_entry_edge = "W"
        train.leading_end = "a"
        train.current_speed = 36.0
        train.state = TrainState.RUNNING_MOVING
        train.facing_direction = Direction.EAST
        # Set legacy fields for compatibility
        train.current_pos = (1, 0)
        train.current_direction = Direction.EAST
        train.current_path = [(x, 0, Direction.EAST) for x in range(5)]
        train.path_index = 1
        
        network.trains["T1"] = train
        
        controller = SimulationController(network)
        initial_distance = train.end_a_distance_cm
        
        # Run simulation for 1 second
        controller.update(1.0)
        
        # Head should have moved forward
        assert train.end_a_distance_cm > initial_distance or train.end_a_index > 1
```

**Step 2: Run test to verify current behavior**

Run: `conda run -n general pytest tests/test_backend.py::TestSimulationPathIntegration -v`
Expected: May pass or fail depending on integration state

**Step 3: Update SimulationController to use TrainMover path methods**

In `simulation.py`, modify `_update_train_position` (around line 292) to also update path queue:

```python
    def _update_train_position(self, train: Train, delta_seconds: float) -> None:
        if not train.current_pos:
            return

        current_track = self.network.get_track(*train.current_pos)
        if not current_track:
            return

        tile_length_cm = self.network._get_tile_length_cm(*train.current_pos)
        speed_cm_per_sec = kmh_to_cm_per_sec(train.current_speed)
        dist_cm = speed_cm_per_sec * delta_seconds

        train.distance_on_track += dist_cm
        
        # === UPDATE PATH QUEUE POSITION ===
        if train.path_tiles and len(train.path_tiles) > 0:
            from train_movement import TrainMover
            mover = TrainMover(self.network)
            mover.update_train_path_position(train, delta_seconds)

        if train.distance_on_track >= tile_length_cm:
            overflow_cm = train.distance_on_track - tile_length_cm
            self._advance_train_track(train, overflow_cm)
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_backend.py::TestSimulationPathIntegration -v`
Expected: PASS

**Step 5: Run all backend tests**

Run: `conda run -n general pytest tests/test_backend.py -v --tb=short`
Expected: All tests pass

**Step 6: Commit**

```bash
git add simulation.py tests/test_backend.py
git commit -m "feat(simulation): integrate TrainMover path queue updates"
```

---

## Phase 3: Frontend Layout Rendering

### Task 3.1: Update renderer-trains.js for Path-Based Rendering

**Files:**
- Modify: `static/js/renderer-trains.js`
- Test: Manual testing + `tests/test_javascript_syntax.py`

**Step 1: Run JavaScript syntax test before changes**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS (baseline)

**Step 2: Update drawTrain method for path-based rendering**

Replace the `drawTrain` method in `static/js/renderer-trains.js`:

```javascript
class RendererTrainHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }
    get currentNetwork() { return this.renderer.currentNetwork; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    /**
     * Get edge center point in tile-local coordinates.
     * @param {string} edge - 'N', 'E', 'S', 'W'
     * @returns {{x: number, y: number}} Local coordinates
     */
    getEdgeCenterLocal(edge) {
        const h = this.gridSize * this.scale / 2;
        const edgePoints = {
            'N': { x: h / 2, y: -h / 2 },
            'E': { x: h / 2, y: h / 2 },
            'S': { x: -h / 2, y: h / 2 },
            'W': { x: -h / 2, y: -h / 2 }
        };
        return edgePoints[edge] || { x: 0, y: 0 };
    }

    /**
     * Get exit edge from a tile given entry edge.
     * Uses track bits to determine valid exit.
     */
    getExitEdge(trackBits, entryEdge) {
        const BIT_TO_EDGES = [
            ['N', 'S'],  // bit 0: NS
            ['E', 'W'],  // bit 1: EW
            ['N', 'E'],  // bit 2: NE
            ['N', 'W'],  // bit 3: NW
            ['S', 'E'],  // bit 4: SE
            ['S', 'W'],  // bit 5: SW
        ];
        
        for (let bit = 0; bit < 6; bit++) {
            if (trackBits & (1 << bit)) {
                const [a, b] = BIT_TO_EDGES[bit];
                if (a === entryEdge) return b;
                if (b === entryEdge) return a;
            }
        }
        return null;
    }

    /**
     * Calculate screen position for a point along a tile's path.
     */
    getTilePathPosition(tile, entryEdge, distance_cm, tile_length_cm, trackBits) {
        const x = tile[0];
        const y = tile[1];
        
        // Get tile center in screen coords
        const centerX = this.gridToScreenX(x, y);
        const centerY = this.gridToScreenY(x, y);
        
        // Get entry and exit edge points
        const entryLocal = this.getEdgeCenterLocal(entryEdge);
        const exitEdge = this.getExitEdge(trackBits, entryEdge);
        const exitLocal = exitEdge ? this.getEdgeCenterLocal(exitEdge) : entryLocal;
        
        // Calculate progress along path (0 = entry, 1 = exit)
        const progress = tile_length_cm > 0 ? Math.min(1.0, distance_cm / tile_length_cm) : 0;
        
        // Interpolate between entry and exit
        const localX = entryLocal.x + (exitLocal.x - entryLocal.x) * progress;
        const localY = entryLocal.y + (exitLocal.y - entryLocal.y) * progress;
        
        return {
            x: centerX + localX,
            y: centerY + localY,
            rotation: Math.atan2(exitLocal.y - entryLocal.y, exitLocal.x - entryLocal.x)
        };
    }

    drawTrain(train) {
        // Check for new path-based position
        if (train.path_tiles && train.path_tiles.length > 0 && train.path_tile_info) {
            this.drawTrainFromPath(train);
            return;
        }
        
        // Fall back to legacy rendering
        this.drawTrainLegacy(train);
    }

    drawTrainFromPath(train) {
        const pathTiles = train.path_tiles;
        const pathInfo = train.path_tile_info || [];
        const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
        const tailIndex = train.leading_end === 'a' ? train.end_b_index : train.end_a_index;
        const headDistanceCm = train.leading_end === 'a' ? train.end_a_distance_cm : train.end_b_distance_cm;
        const tailDistanceCm = train.leading_end === 'a' ? train.end_b_distance_cm : train.end_a_distance_cm;
        const headEntryEdge = train.leading_end === 'a' ? train.end_a_entry_edge : train.end_b_entry_edge;
        const tailEntryEdge = train.leading_end === 'a' ? train.end_b_entry_edge : train.end_a_entry_edge;
        
        if (headIndex >= pathTiles.length || tailIndex >= pathTiles.length) return;
        
        // Get track bits for head and tail tiles
        const tracks = this.currentNetwork?.tracks || [];
        const getTrackBits = (tile) => {
            const t = tracks.find(tr => tr.x === tile[0] && tr.y === tile[1]);
            return t ? t.track_bits : 0;
        };
        
        const headTile = pathTiles[headIndex];
        const tailTile = pathTiles[tailIndex];
        const headInfo = pathInfo[headIndex] || { length_cm: 50 };
        const tailInfo = pathInfo[tailIndex] || { length_cm: 50 };
        
        // Calculate head position
        const headPos = this.getTilePathPosition(
            headTile, headEntryEdge, headDistanceCm, 
            headInfo.length_cm, getTrackBits(headTile)
        );
        
        // Calculate tail position
        const tailPos = this.getTilePathPosition(
            tailTile, tailEntryEdge, tailDistanceCm,
            tailInfo.length_cm, getTrackBits(tailTile)
        );
        
        // Draw path line (white rectangle) connecting tail to head
        this.drawTrainPathLine(train, pathTiles, pathInfo, tailIndex, headIndex, 
                               tailPos, headPos, tailDistanceCm, headDistanceCm, 
                               tailEntryEdge, headEntryEdge, getTrackBits);
        
        // Draw tail square
        this.drawTailMarker(tailPos.x, tailPos.y);
        
        // Draw head triangle
        this.drawHeadMarker(headPos.x, headPos.y, headPos.rotation);
        
        // Draw label
        this.drawTrainLabel(train, headPos.x, headPos.y, getTrackBits(headTile), train.direction);
    }

    drawTrainPathLine(train, pathTiles, pathInfo, tailIdx, headIdx, tailPos, headPos, 
                      tailDistCm, headDistCm, tailEntry, headEntry, getTrackBits) {
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
        this.ctx.lineWidth = 4 * this.scale;
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        
        this.ctx.beginPath();
        this.ctx.moveTo(tailPos.x, tailPos.y);
        
        // Draw through intermediate tiles
        let lastEntry = tailEntry;
        for (let i = tailIdx; i <= headIdx; i++) {
            const tile = pathTiles[i];
            const info = pathInfo[i] || { length_cm: 50 };
            const trackBits = getTrackBits(tile);
            
            if (i === tailIdx) {
                // Start from tail position to exit
                const exitEdge = this.getExitEdge(trackBits, lastEntry);
                if (exitEdge && i < headIdx) {
                    const exitPos = this.getTilePathPosition(tile, lastEntry, info.length_cm, info.length_cm, trackBits);
                    this.ctx.lineTo(exitPos.x, exitPos.y);
                    lastEntry = this.getOppositeEdge(exitEdge);
                }
            } else if (i === headIdx) {
                // End at head position
                this.ctx.lineTo(headPos.x, headPos.y);
            } else {
                // Full tile - entry to exit
                const entryPos = this.getTilePathPosition(tile, lastEntry, 0, info.length_cm, trackBits);
                const exitEdge = this.getExitEdge(trackBits, lastEntry);
                const exitPos = this.getTilePathPosition(tile, lastEntry, info.length_cm, info.length_cm, trackBits);
                this.ctx.lineTo(entryPos.x, entryPos.y);
                this.ctx.lineTo(exitPos.x, exitPos.y);
                if (exitEdge) lastEntry = this.getOppositeEdge(exitEdge);
            }
        }
        
        this.ctx.stroke();
        this.ctx.restore();
    }

    getOppositeEdge(edge) {
        const opposites = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' };
        return opposites[edge] || edge;
    }

    drawHeadMarker(x, y, rotation) {
        const s = this.scale;
        const triSize = 8 * s;
        
        this.ctx.save();
        this.ctx.translate(x, y);
        this.ctx.rotate(rotation);
        
        this.ctx.fillStyle = '#ffffff';
        this.ctx.beginPath();
        this.ctx.moveTo(triSize, 0);
        this.ctx.lineTo(-triSize * 0.6, -triSize * 0.6);
        this.ctx.lineTo(-triSize * 0.6, triSize * 0.6);
        this.ctx.closePath();
        this.ctx.fill();
        
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.stroke();
        
        this.ctx.restore();
    }

    drawTailMarker(x, y) {
        const s = this.scale;
        const size = 5 * s;
        
        this.ctx.save();
        this.ctx.translate(x, y);
        
        this.ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
        this.ctx.fillRect(-size / 2, -size / 2, size, size);
        
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-size / 2, -size / 2, size, size);
        
        this.ctx.restore();
    }

    drawTrainLabel(train, cx, cy, trackBits, direction) {
        const s = this.scale;
        const tileSize = this.gridSize * s;
        const quadrant = getBestLabelQuadrant(trackBits, direction || 'EAST');
        const offset = QUADRANT_OFFSETS[quadrant];
        
        const labelX = cx + offset.dx * tileSize;
        const labelY = cy + offset.dy * tileSize;
        
        // Draw connector line
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        this.ctx.lineWidth = 1 * s;
        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(labelX, labelY);
        this.ctx.stroke();
        this.ctx.restore();
        
        // Draw label box
        const labelText = train.id;
        this.ctx.save();
        this.ctx.font = `bold ${9 * s}px sans-serif`;
        const textMetrics = this.ctx.measureText(labelText);
        const textWidth = textMetrics.width;
        const textHeight = 9 * s;
        const padding = 4 * s;
        const boxWidth = textWidth + padding * 2;
        const boxHeight = textHeight + padding * 1.5;
        
        this.ctx.fillStyle = 'rgba(130, 87, 229, 0.9)';
        const cornerRadius = 3 * s;
        const boxX = labelX - boxWidth / 2;
        const boxY = labelY - boxHeight / 2;
        
        this.ctx.beginPath();
        this.ctx.moveTo(boxX + cornerRadius, boxY);
        this.ctx.lineTo(boxX + boxWidth - cornerRadius, boxY);
        this.ctx.quadraticCurveTo(boxX + boxWidth, boxY, boxX + boxWidth, boxY + cornerRadius);
        this.ctx.lineTo(boxX + boxWidth, boxY + boxHeight - cornerRadius);
        this.ctx.quadraticCurveTo(boxX + boxWidth, boxY + boxHeight, boxX + boxWidth - cornerRadius, boxY + boxHeight);
        this.ctx.lineTo(boxX + cornerRadius, boxY + boxHeight);
        this.ctx.quadraticCurveTo(boxX, boxY + boxHeight, boxX, boxY + boxHeight - cornerRadius);
        this.ctx.lineTo(boxX, boxY + cornerRadius);
        this.ctx.quadraticCurveTo(boxX, boxY, boxX + cornerRadius, boxY);
        this.ctx.closePath();
        this.ctx.fill();
        
        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(labelText, labelX, labelY);
        this.ctx.restore();
    }

    // Legacy rendering for backward compatibility
    drawTrainLegacy(train) {
        if (!train.position) return;

        const baseX = train.position.x;
        const baseY = train.position.y;
        const distance = train.position.distance || 0.0;
        const direction = train.direction || 'EAST';
        
        const positionOffset = distance - 0.5;
        
        let interpX = baseX;
        let interpY = baseY;
        
        if (direction === 'NORTH') {
            interpY = baseY + positionOffset;
        } else if (direction === 'SOUTH') {
            interpY = baseY - positionOffset;
        } else if (direction === 'EAST') {
            interpX = baseX + positionOffset;
        } else if (direction === 'WEST') {
            interpX = baseX - positionOffset;
        }

        const cx = this.gridToScreenX(interpX, interpY);
        const cy = this.gridToScreenY(interpX, interpY);
        const s = this.scale;

        let rotation = 0;
        if (direction === 'NORTH') rotation = -Math.PI / 4;
        else if (direction === 'EAST') rotation = Math.PI / 4;
        else if (direction === 'SOUTH') rotation = Math.PI * 3 / 4;
        else if (direction === 'WEST') rotation = -Math.PI * 3 / 4;

        this.drawHeadMarker(cx, cy, rotation);
        
        const tracks = this.currentNetwork?.tracks || [];
        const track = tracks.find(t => t.x === baseX && t.y === baseY);
        this.drawTrainLabel(train, cx, cy, track ? track.track_bits : 0, direction);
    }
}
```

**Step 3: Run JavaScript syntax test**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 4: Run app smoke test**

Run: `conda run -n general pytest tests/test_app_smoke.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add static/js/renderer-trains.js
git commit -m "feat(renderer): add path-based train rendering with head/tail/path line"
```

---

## Phase 4: Frontend Train Monitor

### Task 4.1: Update train-monitor.js for Path-Based Rendering

**Files:**
- Modify: `static/js/train-monitor.js`
- Modify: `static/js/api.js` (remove lookahead method)
- Test: `tests/test_javascript_syntax.py`

**Step 1: Run JavaScript syntax test before changes**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 2: Update TrainMonitor to use path_tile_info**

Replace the `update` method and related methods in `static/js/train-monitor.js`:

```javascript
    update(train) {
        // Use path_tile_info from train data instead of separate lookahead API
        if (!train || !train.path_tile_info) {
            this.lookaheadData = null;
            this.render();
            return;
        }
        
        // Build lookahead-compatible data from path_tile_info
        const pathTiles = train.path_tiles || [];
        const pathInfo = train.path_tile_info || [];
        const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
        const headDistanceCm = train.leading_end === 'a' ? train.end_a_distance_cm : train.end_b_distance_cm;
        
        // Build blocks, signals, switches from tiles ahead of head
        const blocks = [];
        const signals = [];
        const switches = [];
        const stations = [];
        
        let cumulativeDistance = -headDistanceCm; // Start from head position
        const seenBlocks = new Set();
        
        for (let i = headIndex; i < pathTiles.length; i++) {
            const tile = pathTiles[i];
            const info = pathInfo[i];
            if (!info) continue;
            
            const tileLength = info.length_cm || 50;
            const tileStart = Math.max(0, cumulativeDistance);
            
            // Add block if not seen
            if (info.block_id && !seenBlocks.has(info.block_id)) {
                seenBlocks.add(info.block_id);
                
                // Find all tiles in this block to calculate total length
                let blockLength = 0;
                let tileCount = 0;
                for (let j = i; j < pathInfo.length; j++) {
                    if (pathInfo[j]?.block_id === info.block_id) {
                        blockLength += pathInfo[j].length_cm || 50;
                        tileCount++;
                    } else {
                        break;
                    }
                }
                
                blocks.push({
                    id: info.block_id,
                    name: info.block_id,
                    state: 'free', // Could be enhanced with block state
                    start_cm: tileStart,
                    end_cm: tileStart + blockLength,
                    length_cm: blockLength,
                    tile_count: tileCount,
                    sequence: blocks.length
                });
            }
            
            // Add signals
            if (info.signals) {
                info.signals.forEach(sig => {
                    signals.push({
                        id: sig.id,
                        state: sig.state,
                        distance_cm: tileStart,
                        sequence: signals.length
                    });
                });
            }
            
            // Add switches
            if (info.is_switch) {
                switches.push({
                    x: tile[0],
                    y: tile[1],
                    position: info.switch_state === 0 ? 'main' : 'diverging',
                    distance_cm: tileStart,
                    sequence: switches.length
                });
            }
            
            cumulativeDistance += tileLength;
        }
        
        // Build lookahead-compatible data structure
        this.lookaheadData = {
            current_speed_kmh: train.speed || 0,
            target_speed_kmh: train.target_speed || 0,
            state: train.state,
            wait_reason: train.wait_reason,
            speed_limit_reason: train.speed_limit_reason,
            blocks: blocks,
            signals: signals,
            switches: switches,
            stations: stations,
            distance_into_path_cm: 0, // Head is always at 0
            clearance_cm: cumulativeDistance
        };
        
        // Update UI elements
        this.currentSpeedEl.textContent = Math.round(this.lookaheadData.current_speed_kmh || 0);
        this.targetSpeedEl.textContent = Math.round(this.lookaheadData.target_speed_kmh || 0);
        
        if (this.speedCmsEl) {
            const speedCms = ((this.lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
            this.speedCmsEl.textContent = speedCms;
        }
        if (this.clearanceEl) {
            const clearanceM = ((this.lookaheadData.clearance_cm || 0) / 100).toFixed(1);
            this.clearanceEl.textContent = clearanceM;
        }
        
        if (this.waitReasonEl) {
            if (this.lookaheadData.state === 'running_waiting' && this.lookaheadData.wait_reason) {
                this.waitReasonEl.textContent = this.lookaheadData.wait_reason;
                this.waitReasonEl.classList.remove('hidden');
            } else {
                this.waitReasonEl.classList.add('hidden');
            }
        }
        
        if (this.speedLimitReasonEl) {
            const speedLimitReason = this.lookaheadData.speed_limit_reason;
            if (speedLimitReason && this.lookaheadData.state !== 'running_waiting') {
                this.speedLimitReasonEl.textContent = speedLimitReason;
                this.speedLimitReasonEl.classList.remove('hidden');
            } else {
                this.speedLimitReasonEl.classList.add('hidden');
            }
        }
        
        this.updateUpcomingElements(this.lookaheadData);
        this.render();
    }
```

**Step 3: Run JavaScript syntax test**

Run: `source ~/.nvm/nvm.sh && nvm use 22 && conda run -n general pytest tests/test_javascript_syntax.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add static/js/train-monitor.js
git commit -m "feat(train-monitor): use path_tile_info instead of lookahead API"
```

---

## Phase 5: Cleanup

### Task 5.1: Remove Deprecated Code

**Files:**
- Delete: `train_lookahead.py`
- Modify: `server.py` (remove lookahead endpoint)
- Modify: `static/js/api.js` (remove getTrainLookahead)
- Test: All test suites

**Step 1: Remove lookahead endpoint from server.py**

Find and remove the `/api/train/<train_id>/lookahead` endpoint from `server.py`.

**Step 2: Remove getTrainLookahead from api.js**

Remove the `getTrainLookahead` method from `static/js/api.js` if it exists.

**Step 3: Delete train_lookahead.py**

```bash
git rm train_lookahead.py
```

**Step 4: Update any imports that reference train_lookahead**

Search for imports of `train_lookahead` in `server.py` and remove them.

**Step 5: Run all tests**

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```
Expected: All tests pass

**Step 6: Commit**

```bash
git add -A
git commit -m "cleanup: remove deprecated train_lookahead module and endpoint"
```

---

### Task 5.2: Final Integration Test

**Step 1: Start server and verify**

```bash
python3 server.py
```
Expected: Server starts without errors, all startup tests pass

**Step 2: Manual testing in browser**

1. Open `http://localhost:5000`
2. Load a layout with trains
3. Verify train rendering shows head triangle + tail square + path line
4. Open train monitor and verify it shows blocks/signals ahead
5. Run simulation and verify positions update smoothly

**Step 3: Run full test suite**

```bash
./run_all_tests.sh
```
Expected: All tests pass

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete train path refactor - unified position tracking"
```

---

## Summary

This plan refactors the train position system from separate calculations for movement, lookahead, and rendering into a unified path-based approach:

1. **Phase 1**: Add `path_tiles`, `end_a_*`/`end_b_*` fields to Train entity
2. **Phase 2**: Update TrainMover to use path queue for position updates
3. **Phase 3**: Update renderer to draw from path data (head, tail, path line)
4. **Phase 4**: Update train monitor to read from train's path_tile_info
5. **Phase 5**: Remove deprecated lookahead code

The key benefit is a **single source of truth** - both rendering and monitor read the same train path data, eliminating data discrepancies that caused jumping, inconsistent updates, and disappearing elements.
