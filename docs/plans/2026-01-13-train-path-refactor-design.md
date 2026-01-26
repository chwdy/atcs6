# Train Path Refactor Design

**Date:** 2026-01-13  
**Status:** Approved  

## Problem Statement

The current train rendering and train monitor have data discrepancies:
- Lookahead calculates positions one way
- Train movement calculates positions another way
- Renderer interprets data a third way

This causes the monitor to jump around, not move consistently, and elements disappearing unexpectedly.

## Solution Overview

Unify train position tracking using a single path-based data structure that both layout rendering and train monitor read from.

### Core Concept

```
[...tiles passed...] [occupied tiles with end_a/end_b positions] [...tiles ahead...]
         ↑                              ↑                              ↑
    already passed               train is here                  from assigned path
```

## Data Structure

### Train Entity - New Path Fields

```python
class Train:
    # The unified path tile list
    path_tiles: List[Tuple[int, int]]  # Full path: passed + occupied + ahead
    
    # Two physical ends (don't change on reverse)
    end_a_index: int           # Index into path_tiles
    end_a_distance_cm: float   # Distance from entry edge
    end_a_entry_edge: str      # 'N', 'E', 'S', 'W'
    
    end_b_index: int
    end_b_distance_cm: float
    end_b_entry_edge: str
    
    # Which end is currently leading (flip on reverse)
    leading_end: str  # 'a' or 'b'
```

### Derived Properties

```python
@property
def head_index(self) -> int:
    return self.end_a_index if self.leading_end == 'a' else self.end_b_index

@property
def tail_index(self) -> int:
    return self.end_b_index if self.leading_end == 'a' else self.end_a_index

@property
def occupied_tiles(self) -> List[Tuple[int, int]]:
    return self.path_tiles[self.tail_index : self.head_index + 1]
```

### Queue Operations

- `advance_head(tile, entry_edge)` - When head enters new tile, update head index
- `advance_tail()` - When tail leaves tile, update tail index
- `extend_path(tiles)` - Append tiles when path is extended
- `clear_path()` - Reset when path is cancelled
- `reverse()` - Flip `leading_end` (no position recalculation needed)

## Movement Logic

TrainMover updates train position:

```python
def update_train_position(self, train: Train, delta_seconds: float, network: RailwayNetwork):
    # 1. Calculate distance to move
    distance_cm = self._calculate_distance(train.current_speed, delta_seconds)
    
    # 2. Update head position
    train.head_distance_cm += distance_cm
    
    # 3. Check if head crossed into next tile
    tile_length = network.get_tile_length(train.path_tiles[train.head_index])
    if train.head_distance_cm >= tile_length:
        overflow = train.head_distance_cm - tile_length
        train.advance_head()
        train.head_distance_cm = overflow
    
    # 4. Update tail position based on train length
    self._update_tail_position(train, network)
```

## Layout Rendering

The layout renderer draws:
1. Head triangle at head position
2. Tail square at tail position
3. White path rectangle along occupied tiles connecting head and tail

```javascript
drawTrain(train) {
    const occupiedTiles = train.path_tiles.slice(train.tail_index, train.head_index + 1);
    
    // Calculate head/tail screen positions
    const headPos = this.getTilePathPosition(headTile, train.head_entry_edge, train.head_distance_cm);
    const tailPos = this.getTilePathPosition(tailTile, train.tail_entry_edge, train.tail_distance_cm);
    
    // Draw path rectangle (straight lines between edge centers, same as path drawing)
    this.drawTrainPath(occupiedTiles, tailPos, headPos, train);
    
    // Draw head triangle and tail square
}
```

## Train Monitor

Monitor shows tiles ahead of train head with blocks, signals, speed limits:

```javascript
update(train, network) {
    // Get tiles ahead from head position
    const tilesAhead = train.path_tiles.slice(train.head_index);
    
    // Build segments with tile info (length, block, signals)
    // Train head is always at position 0 (left edge)
    // Group by block, render as bars with tile dividers
}
```

No separate lookahead API call - reads directly from train state.

## API Changes

Train `to_dict()` includes all path data:

```python
def to_dict(self, network) -> Dict:
    return {
        # ... existing fields
        'path_tiles': self.path_tiles,
        'end_a_index': self.end_a_index,
        'end_a_distance_cm': self.end_a_distance_cm,
        'end_a_entry_edge': self.end_a_entry_edge,
        'end_b_index': self.end_b_index,
        'end_b_distance_cm': self.end_b_distance_cm,
        'end_b_entry_edge': self.end_b_entry_edge,
        'leading_end': self.leading_end,
        'path_tile_info': [...]  # Per-tile info for frontend
    }
```

## Files to Modify

### Backend (Python)
| File | Action | Changes |
|------|--------|---------|
| `train.py` | Modify | Replace TrainPosition with new path fields, add queue methods |
| `train_movement.py` | Modify | Update to use new train path structure |
| `simulation.py` | Modify | Use updated TrainMover |
| `network.py` | Modify | Remove _calculate_occupied_tiles, simplify position recalculation |
| `train_lookahead.py` | Delete | No longer needed |
| `server.py` | Modify | Remove /api/train/<id>/lookahead endpoint |

### Frontend (JavaScript)
| File | Action | Changes |
|------|--------|---------|
| `static/js/renderer-trains.js` | Modify | New drawTrain() using path_tiles |
| `static/js/train-monitor.js` | Modify | Read from train.path_tile_info |
| `static/js/api.js` | Modify | Remove getTrainLookahead() |

## Implementation Phases

### Phase 1: Backend Data Structure
1. Refactor Train class with new path fields
2. Add queue methods
3. Add derived properties
4. Update to_dict()

### Phase 2: Backend Movement Logic
1. Update TrainMover for new structure
2. Modify simulation loop
3. Simplify network.py position logic
4. Ensure train placement initializes correctly

### Phase 3: Frontend Layout Rendering
1. Update renderer-trains.js for new structure
2. Implement drawTrainPath() for path rectangle
3. Update head/tail position calculation

### Phase 4: Frontend Train Monitor
1. Update train-monitor.js to read from path_tile_info
2. Remove lookahead API call
3. Render from tile info

### Phase 5: Cleanup
1. Delete train_lookahead.py
2. Remove /api/train/<id>/lookahead endpoint
3. Remove deprecated fields
4. Update tests

## Key Design Decisions

1. **end_a/end_b with leading_end** - Preserves efficient reversing (just flip leading_end)
2. **Keep all passed tiles** - Memory bounded by layout size, cleared at destination
3. **Path line style** - Straight lines between edge centers (matches existing path rendering)
4. **Blocks as primary bars** - Monitor shows blocks with tile dividers (current style)
5. **No separate lookahead API** - Single source of truth eliminates data discrepancy
