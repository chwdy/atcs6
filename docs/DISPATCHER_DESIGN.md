# Path Dispatcher System Technical Design

**Version:** 2.0  
**Status:** Implemented  
**Created:** 2026-01-05  
**Updated:** 2026-01-10
**Author:** Design Collaboration

---

## Table of Contents

1. [Overview](#1-overview)
2. [Core Concepts](#2-core-concepts)
3. [Path Creation Workflow](#3-path-creation-workflow)
4. [Resource Management](#4-resource-management)
5. [Path Lifecycle](#5-path-lifecycle)
6. [Path Extension](#6-path-extension)
7. [Approach Locking](#7-approach-locking)
8. [Path Cancellation](#8-path-cancellation)
9. [Overlap Management](#9-overlap-management)
10. [Priority System](#10-priority-system)
11. [UI/UX Specification](#11-uiux-specification)
12. [Data Structures](#12-data-structures)
13. [API Endpoints](#13-api-endpoints)
14. [Configuration](#14-configuration)
15. [Implementation Phases](#15-implementation-phases)
16. [Future Considerations](#16-future-considerations)

---

## 1. Overview

The Dispatcher System enables path-based train control, allowing operators to:

- Select start/end points (blocks or stations)
- Calculate and validate paths through the network
- Reserve blocks, lock switches, and manage path lifecycle
- Handle multiple concurrent paths with conflict resolution
- Manage train priorities for resource allocation

### 1.1 Design Principles

1. **Safety First:** Approach locking, flank protection, and overlap ensure safe operations
2. **All-or-Nothing:** Path creation either fully succeeds or completely fails
3. **Non-Destructive Extension:** Failed extensions leave original path intact
4. **First-Come-First-Served:** With priority-based conflict resolution prompts
5. **Directional Paths:** Trains move forward only; reversing requires a new path

---

## 2. Core Concepts

### 2.1 Path Definition

A **Path** is a reserved path through the railway network with exclusive access to its resources.

```python
@dataclass
class Path:
    path_id: str                           # Human-readable: "PATH_CENTRAL_TO_NORTH_001"
    status: PathStatus                     # PENDING, SETTING, SET, ACTIVE, CLEARING, COMPLETED, CANCELLED
    priority: int                           # 1-10, higher = higher priority (default: 5)
    
    # Endpoints
    start_block_id: str                     # Starting block
    end_block_id: str                       # Destination block
    start_station_id: Optional[str]         # If started from station
    end_station_id: Optional[str]           # If ending at station
    
    # Path data
    path_tiles: List[Tuple[int, int]]       # Ordered list of track positions
    block_sequence: List[str]               # Ordered blocks along path
    switch_positions: Dict[Tuple[int, int], int]  # Required switch states
    
    # Ownership
    train_id: Optional[str]                 # Assigned train (None until train enters)
    dispatcher_id: str                      # Who created this path
    
    # State tracking
    current_block_index: int                # Train's position in block_sequence
    blocks_cleared: Set[str]                # Blocks train has fully exited
    rear_hold_distance_cm: float            # Safety distance behind train
    
    # Protection zones
    flank_protection_switches: Dict[Tuple[int, int], int]  # Switches locked for flank protection
    overlap_blocks: List[str]               # Blocks beyond destination (dynamically acquired)
    overlap_distance_cm: float              # Required overlap distance
    
    # Approach locking
    approach_locked_blocks: Set[str]        # Blocks that cannot be cancelled
    
    # Timestamps
    created_at: datetime
    activated_at: Optional[datetime]
    completed_at: Optional[datetime]
```

### 2.2 Path States

```
┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌──────────┐     ┌───────────┐
│ PENDING │ ──► │ SETTING │ ──► │   SET   │ ──► │ ACTIVE  │ ──► │ CLEARING │ ──► │ COMPLETED │
└─────────┘     └─────────┘     └─────────┘     └─────────┘     └──────────┘     └───────────┘
     │               │               │               │               │
     └───────────────┴───────────────┴───────────────┴───────────────┘
                                     │
                                     ▼
                              ┌───────────┐
                              │ CANCELLED │
                              └───────────┘
```

| State | Description |
|-------|-------------|
| `PENDING` | Path calculated, validation passed, waiting for resource acquisition |
| `SETTING` | Switches moving to correct positions |
| `SET` | All switches locked, blocks reserved, ready for train |
| `ACTIVE` | Train is physically on the path |
| `CLEARING` | Train in final block, releasing resources progressively |
| `COMPLETED` | Train arrived, all resources freed, path disposed |
| `CANCELLED` | Emergency cancelled, train stopping, releasing resources |

### 2.3 Path ID Format

Human-readable format for easy identification:

```
PATH_{START}_{END}_{SEQUENCE}

Examples:
- PATH_BLK_001_TO_BLK_005_001
- PATH_CENTRAL_TO_NORTH_002
- PATH_PLATFORM_A_TO_DEPOT_001
```

### 2.4 Valid Endpoints

**Valid Start Points:**
- Any block (user clicks on block)
- Any station (selects first tile opposite to destination direction)
- Block currently occupied by a train

**Valid End Points:**
- Any block reachable via pathfinding (any switch configuration)
- Any station reachable (covers whole platform length)

**Invalid Endpoints:**
- Switches (not selectable as start/end)
- Paths requiring turn-around at switch (enter exit, reverse, leave different exit)

---

## 3. Path Creation Workflow

### 3.1 Endpoint Selection

When the dispatcher selects a start point:
1. Highlight the start block/station
2. Calculate all reachable endpoints (any switch configuration, no turn-around)
3. Show reachable endpoints in orange
4. Wait for endpoint selection or cancellation (right-click)

### 3.2 Pathfinding Phase

```python
def find_path_paths(network, start_block_id, end_block_id) -> List[PathPath]:
    """
    Find all possible paths between start and end blocks.
    
    Returns:
        List of PathPath objects, sorted by distance (shortest first)
    """
    paths = []
    
    # Use existing RailwayPathfinder with all switch configurations
    for switch_config in all_possible_switch_configurations():
        path = pathfinder.find_path(start_pos, end_pos, direction)
        if path:
            paths.append(PathPath(
                tiles=path,
                distance=calculate_total_distance(path),
                required_switches=switch_config,
                blocks=extract_blocks_from_path(path)
            ))
    
    # Sort by distance, remove duplicates
    return sorted(deduplicate(paths), key=lambda p: p.distance)
```

### 3.3 Validation Phase

For each path (starting with shortest), validate:

1. **Block Coverage:** All tiles must be in blocks (platforms auto-create blocks)
2. **Block Availability:** Not OCCUPIED by other train, not disabled
3. **Switch Availability:** Not locked by other path
4. **Flank Protection:** Required flank switches must be available

```python
def validate_path(network, path: PathPath) -> ValidationResult:
    """
    Validate a path can be used for a path.
    """
    errors = []
    
    # Check 1: Block coverage
    for tile in path.tiles:
        if not is_tile_in_block(tile):
            errors.append(ValidationError(
                type='UNBLOCKED_TILE',
                tile=tile,
                message=f"Tile {tile} is not in any block"
            ))
    
    # Check 2: Block availability
    for block_id in path.blocks:
        block = network.blocks[block_id]
        if block.state == BlockState.OCCUPIED and block.occupied_by != start_train_id:
            errors.append(ValidationError(
                type='BLOCK_OCCUPIED',
                block_id=block_id,
                occupied_by=block.occupied_by
            ))
        if block.state == BlockState.RESERVED:
            errors.append(ValidationError(
                type='BLOCK_RESERVED',
                block_id=block_id,
                reserved_for=block.occupied_by
            ))
    
    # Check 3: Switch availability
    for pos, required_state in path.required_switches.items():
        if is_switch_locked(pos):
            lock = get_switch_lock(pos)
            errors.append(ValidationError(
                type='SWITCH_LOCKED',
                position=pos,
                locked_by_path=lock.path_id
            ))
    
    # Check 4: Flank protection
    flank_switches = calculate_flank_protection_switches(path)
    for pos, safe_state in flank_switches.items():
        if is_switch_locked(pos) and get_locked_state(pos) != safe_state:
            errors.append(ValidationError(
                type='FLANK_SWITCH_CONFLICT',
                position=pos
            ))
    
    return ValidationResult(valid=len(errors) == 0, errors=errors, path=path)
```

### 3.4 Path Selection Algorithm

```python
def create_path(network, start_block_id, end_block_id, dispatcher_id, 
                 priority: int = 5) -> PathResult:
    """
    Create a path using the shortest available path.
    
    Algorithm:
    1. Find all paths, sorted by distance
    2. Try to lock resources for shortest path
    3. If locking fails, try next shortest
    4. If all fail, return error with conflict info
    """
    paths = find_path_paths(network, start_block_id, end_block_id)
    
    if not paths:
        return PathResult(
            success=False,
            error="No path exists between start and end points"
        )
    
    attempted_paths = []
    
    for path in paths:
        validation = validate_path(network, path)
        attempted_paths.append((path, validation))
        
        if validation.valid:
            # Try to acquire resources
            acquisition = acquire_path_resources(network, path, priority)
            
            if acquisition.success:
                path = create_path_from_path(path, dispatcher_id, priority)
                return PathResult(success=True, path=path)
            else:
                # Resource acquisition failed (race condition or priority conflict)
                continue
    
    # All paths failed
    return PathResult(
        success=False,
        error="All paths blocked",
        attempted_paths=attempted_paths,
        conflict_tiles=extract_conflict_tiles(attempted_paths)  # For UI flash
    )
```

---

## 4. Resource Management

### 4.1 Block Reservation

```python
def reserve_blocks_for_path(path: Path) -> bool:
    """Reserve all blocks in the path's path."""
    
    for block_id in path.block_sequence:
        block = network.blocks[block_id]
        
        # If starting block is occupied by a train, that train owns the path
        if block.state == BlockState.OCCUPIED:
            if path.train_id is None:
                path.train_id = block.occupied_by
            elif block.occupied_by != path.train_id:
                return False  # Conflict
        
        # Reserve the block for this path
        if not block.reserve_for_path(path.path_id):
            return False
    
    return True
```

### 4.2 Switch Locking

```python
def lock_switches_for_path(path: Path) -> bool:
    """Lock all required switches and flank protection switches."""
    
    # Main path switches
    for pos, required_state in path.switch_positions.items():
        track = network.tracks[pos]
        
        # Set switch to required state
        track.set_switch_state(required_state)
        
        # Lock it for this path
        if not lock_switch(pos, path.path_id, required_state):
            rollback_switch_locks(path)
            return False
    
    # Flank protection switches
    for pos, safe_state in path.flank_protection_switches.items():
        track = network.tracks[pos]
        track.set_switch_state(safe_state)
        
        if not lock_switch(pos, path.path_id, safe_state):
            rollback_switch_locks(path)
            return False
    
    return True
```

### 4.3 Flank Protection Calculation

Flank protection prevents collisions from merging tracks by locking nearby switches to safe positions.

```python
FLANK_PROTECTION_DISTANCE_CM = 500.0  # Configurable

def calculate_flank_protection_switches(path: PathPath) -> Dict[Tuple[int, int], int]:
    """
    Find switches that could path trains INTO the path and lock them safely.
    
    A switch needs flank protection if:
    1. It's within FLANK_PROTECTION_DISTANCE_CM of any path tile
    2. One of its exits leads to the path
    3. It's not already part of the path's required switches
    """
    flank_switches = {}
    path_tile_set = set(path.tiles)
    
    for tile_pos in path.tiles:
        # Find nearby switches within protection distance
        nearby_switches = find_switches_within_distance(
            tile_pos, 
            FLANK_PROTECTION_DISTANCE_CM
        )
        
        for switch_pos in nearby_switches:
            if switch_pos in path.required_switches:
                continue  # Already handled by main path
            
            if switch_pos in path_tile_set:
                continue  # On the path, already handled
            
            # Check if any exit leads to path
            track = network.tracks[switch_pos]
            for entry, exits in track.switch_points.items():
                for exit_dir in exits:
                    neighbor = get_neighbor(switch_pos, exit_dir)
                    if neighbor in path_tile_set:
                        # This switch could path trains into our path
                        # Lock it to the OTHER state (away from path)
                        safe_state = 1 - get_state_for_exit(entry, exit_dir)
                        flank_switches[switch_pos] = safe_state
    
    return flank_switches
```

---

## 5. Path Lifecycle

### 5.1 Train Enters Path

```python
def on_train_enters_path(path: Path, train_id: str, block_id: str):
    """Called when a train physically enters a path's first block."""
    
    if path.train_id is None:
        # First train to enter claims the path (first-come-first-served)
        path.train_id = train_id
        path.status = PathStatus.ACTIVE
        path.activated_at = datetime.now()
        
        # Inherit train's priority if path has default priority
        train = network.trains[train_id]
        if path.priority == DEFAULT_ROUTE_PRIORITY:
            path.priority = train.priority
    
    elif path.train_id != train_id:
        # Wrong train - should not happen with proper block control
        raise PathViolationError(f"Train {train_id} entered path owned by {path.train_id}")
    
    # Update block state
    block = network.blocks[block_id]
    block.occupy(train_id)
    
    path.current_block_index = path.block_sequence.index(block_id)
```

### 5.2 Train Advances

```python
REAR_SAFETY_DISTANCE_CM = 300.0  # Configurable

def on_train_advances(path: Path, entered_block_id: str):
    """Called when train enters a new block along the path."""
    
    # Update current position
    new_index = path.block_sequence.index(entered_block_id)
    path.current_block_index = new_index
    
    # Occupy new block
    network.blocks[entered_block_id].occupy(path.train_id)
    
    # Release blocks behind train (respecting safety distance)
    release_blocks_behind_train(path)
    
    # Update approach locking based on current speed
    update_approach_locking(path)
    
    # Check if entering final block
    if entered_block_id == path.block_sequence[-1]:
        path.status = PathStatus.CLEARING
        # Start acquiring overlap
        acquire_overlap_blocks(path)
```

### 5.3 Block Release with Safety Distance

```python
def release_blocks_behind_train(path: Path):
    """
    Release blocks behind the train, maintaining safety distance.
    
    Uses CAUTION state as intermediate (train just left).
    Always holds at least one block behind.
    """
    train = network.trains[path.train_id]
    train_rear_pos = calculate_train_rear_position(train)
    
    for i, block_id in enumerate(path.block_sequence):
        if i >= path.current_block_index:
            break  # Don't release current or future blocks
        
        if block_id in path.blocks_cleared:
            continue  # Already released
        
        block = network.blocks[block_id]
        
        # Calculate distance from train rear to block end
        distance_to_train = calculate_distance(block, train_rear_pos)
        
        # Must maintain at least one block AND safety distance
        blocks_behind = path.current_block_index - i
        if blocks_behind >= 1 and distance_to_train >= REAR_SAFETY_DISTANCE_CM:
            
            if block.state == BlockState.OCCUPIED:
                # First step: OCCUPIED -> CAUTION
                block.set_to_caution()
            
            elif block.state == BlockState.CAUTION:
                # Second step: CAUTION -> FREE
                block.clear()
                path.blocks_cleared.add(block_id)
                
                # Release associated switches if safe
                release_switches_for_block(path, block_id)
```

### 5.4 Path Completion

```python
def on_train_enters_final_block(path: Path):
    """Called when train enters the destination block."""
    
    path.status = PathStatus.CLEARING
    
    # Acquire overlap blocks dynamically
    acquire_overlap_blocks(path)

def complete_path(path: Path):
    """Finalize path completion and release all resources."""
    
    path.status = PathStatus.COMPLETED
    path.completed_at = datetime.now()
    
    # Release all remaining blocks
    for block_id in path.block_sequence:
        if block_id not in path.blocks_cleared:
            network.blocks[block_id].release()
    
    # Release overlap blocks
    for block_id in path.overlap_blocks:
        network.blocks[block_id].release()
    
    # Unlock all switches
    for pos in path.switch_positions:
        unlock_switch(pos, path.path_id)
    
    for pos in path.flank_protection_switches:
        unlock_switch(pos, path.path_id)
    
    # Remove path from active paths
    del active_paths[path.path_id]
```

---

## 6. Path Extension

```python
def extend_path(path: Path, new_end_block_id: str) -> ExtensionResult:
    """
    Extend an existing path to a new destination.
    
    - Starts from current path's end block
    - Original path remains intact if extension fails
    - Can be called while train is moving
    """
    if path.status not in [PathStatus.SET, PathStatus.ACTIVE]:
        return ExtensionResult(
            success=False,
            error="Path must be SET or ACTIVE to extend"
        )
    
    current_end = path.block_sequence[-1]
    
    # Find paths from current end to new end
    extension_paths = find_path_paths(network, current_end, new_end_block_id)
    
    if not extension_paths:
        return ExtensionResult(
            success=False,
            error="No path exists to new destination"
        )
    
    # Try to acquire resources for extension (inherits path priority)
    for path in extension_paths:
        validation = validate_path(network, path)
        
        if validation.valid:
            acquisition = acquire_path_resources(network, path, path.priority)
            
            if acquisition.success:
                # Merge extension into existing path
                merge_extension_into_path(path, path)
                
                return ExtensionResult(
                    success=True,
                    extended_blocks=path.blocks
                )
    
    # Extension failed, original path unchanged
    return ExtensionResult(
        success=False,
        error="All extension paths blocked",
        conflict_tiles=extract_conflict_tiles(extension_paths)
    )

def merge_extension_into_path(path: Path, extension: PathPath):
    """Merge extension path into existing path."""
    
    # Append new tiles (skip first tile, it's the connection point)
    path.path_tiles.extend(extension.tiles[1:])
    
    # Append new blocks (skip first, it's already in path)
    path.block_sequence.extend(extension.blocks[1:])
    
    # Merge switch positions
    path.switch_positions.update(extension.required_switches)
    
    # Recalculate flank protection for new section
    new_flank = calculate_flank_protection_switches(extension)
    path.flank_protection_switches.update(new_flank)
    
    # Update end station if applicable
    path.end_block_id = extension.blocks[-1]
```

---

## 7. Approach Locking

Approach locking prevents path cancellation when a train cannot safely stop before reaching the locked blocks.

```python
def update_approach_locking(path: Path):
    """
    Update which blocks are approach-locked based on train position and speed.
    
    A block becomes approach-locked when the train cannot stop before reaching it.
    Based on train's current braking distance calculation.
    """
    if path.train_id is None:
        return
    
    train = network.trains[path.train_id]
    braking_distance = calculate_braking_distance(train)
    
    # Clear previous approach locks (recalculate fresh)
    path.approach_locked_blocks.clear()
    
    # Find blocks within braking distance ahead of train
    distance_ahead = 0.0
    
    for i in range(path.current_block_index, len(path.block_sequence)):
        block_id = path.block_sequence[i]
        block = network.blocks[block_id]
        
        if distance_ahead <= braking_distance:
            path.approach_locked_blocks.add(block_id)
        
        distance_ahead += block.length_cm

def calculate_braking_distance(train: Train) -> float:
    """
    Calculate distance needed to stop from current speed.
    
    Physics: d = v^2 / (2 * deceleration)
    """
    speed_cm_per_sec = train.current_speed * 100000 / 3600  # km/h to cm/s
    deceleration = train.max_braking_rate  # cm/s^2
    
    if deceleration <= 0:
        return float('inf')  # Cannot brake
    
    return (speed_cm_per_sec ** 2) / (2 * deceleration)
```

---

## 8. Path Cancellation

```python
def cancel_path(path: Path, force: bool = False) -> CancellationResult:
    """
    Cancel a path and stop the train.
    
    - Only non-approach-locked blocks can be released immediately
    - Train decelerates at max braking rate
    - After stop, train becomes IDLE and needs new path to move
    """
    if not force and path.approach_locked_blocks:
        # Partial cancellation - only unlocked blocks released
        pass
    
    path.status = PathStatus.CANCELLED
    
    # Emergency brake the train
    if path.train_id:
        train = network.trains[path.train_id]
        train.initiate_emergency_stop()  # Decelerate at max rate
        # Train state becomes IDLE after stopping
    
    # Release non-approach-locked resources immediately
    released_blocks = []
    for block_id in path.block_sequence:
        if block_id not in path.approach_locked_blocks:
            network.blocks[block_id].release()
            released_blocks.append(block_id)
    
    # Release switches not protecting approach-locked blocks
    released_switches = []
    for pos in path.switch_positions:
        if can_release_switch(pos, path.approach_locked_blocks):
            unlock_switch(pos, path.path_id)
            released_switches.append(pos)
    
    # Approach-locked blocks will be released when train stops
    return CancellationResult(
        success=True,
        blocks_released=released_blocks,
        switches_released=released_switches,
        approach_locked=list(path.approach_locked_blocks),
        message="Approach-locked blocks will release when train stops" if path.approach_locked_blocks else None
    )

def on_train_stopped_after_cancellation(path: Path):
    """Called when train has fully stopped after cancellation."""
    
    train = network.trains[path.train_id]
    train.state = TrainState.IDLE
    
    # Release remaining approach-locked blocks
    for block_id in path.approach_locked_blocks:
        network.blocks[block_id].release()
    
    # Release remaining switches
    for pos in path.switch_positions:
        unlock_switch(pos, path.path_id)
    
    for pos in path.flank_protection_switches:
        unlock_switch(pos, path.path_id)
    
    # Remove path
    del active_paths[path.path_id]
```

---

## 9. Overlap Management

Overlap provides a safety margin beyond the destination block.

```python
OVERLAP_DISTANCE_CM = 200.0  # Configurable safety margin

def acquire_overlap_blocks(path: Path):
    """
    Dynamically acquire blocks beyond the destination for safety margin.
    
    Called when train enters final block.
    """
    final_block = network.blocks[path.block_sequence[-1]]
    direction = get_exit_direction(path, final_block)
    
    distance_acquired = 0.0
    current_pos = get_block_exit_position(final_block, direction)
    
    while distance_acquired < path.overlap_distance_cm:
        next_block_id = find_next_block(current_pos, direction)
        
        if next_block_id is None:
            # End of track - acceptable, just less overlap
            break
        
        next_block = network.blocks[next_block_id]
        
        # Try to reserve for overlap
        if next_block.can_enter(path.train_id):
            next_block.reserve_for_path(path.path_id)
            path.overlap_blocks.append(next_block_id)
            distance_acquired += next_block.length_cm
            
            # Update position for next iteration
            current_pos = get_block_exit_position(next_block, direction)
        else:
            # Cannot acquire more overlap - warn dispatcher
            warn_dispatcher(
                path, 
                f"Overlap limited: block {next_block_id} unavailable"
            )
            break
```

---

## 10. Priority System

### 10.1 Priority Levels

Paths and trains have a priority level from 1-10:

| Priority | Description | Example |
|----------|-------------|---------|
| 1-2 | Low | Maintenance vehicles, empty stock |
| 3-4 | Below Normal | Freight trains |
| 5 | Normal (Default) | Standard passenger service |
| 6-7 | Above Normal | Express services |
| 8-9 | High | Limited express, important freight |
| 10 | Critical | Emergency services, rescue trains |

### 10.2 Priority in Path Creation

```python
DEFAULT_ROUTE_PRIORITY = 5

@dataclass
class Train:
    # ... existing fields ...
    priority: int = DEFAULT_ROUTE_PRIORITY  # Default priority 5

@dataclass  
class Path:
    # ... existing fields ...
    priority: int = DEFAULT_ROUTE_PRIORITY  # Inherited from train or set explicitly
```

### 10.3 Conflict Resolution with Priority

When a path creation conflicts with existing paths:

```python
def handle_path_conflict(new_path: Path, conflicting_paths: List[Path]) -> ConflictResolution:
    """
    Handle conflicts between new path and existing paths.
    
    Policy:
    1. First-come-first-served is default
    2. Higher priority paths prompt user for pre-emption
    3. User decides whether to cancel lower priority paths
    """
    # Check if new path has higher priority than ALL conflicts
    max_conflict_priority = max(r.priority for r in conflicting_paths)
    
    if new_path.priority > max_conflict_priority:
        # New path is higher priority - prompt user
        return ConflictResolution(
            type='PRIORITY_PROMPT',
            message=f"Higher priority path (P{new_path.priority}) conflicts with "
                    f"existing paths (max P{max_conflict_priority}). Cancel existing paths?",
            conflicting_paths=conflicting_paths,
            default_action='NO'  # Default is to not pre-empt
        )
    else:
        # Same or lower priority - just report conflict
        return ConflictResolution(
            type='BLOCKED',
            message=f"Path blocked by existing paths",
            conflicting_paths=conflicting_paths
        )

def preempt_paths(paths_to_cancel: List[Path], new_path: Path):
    """Cancel lower priority paths to make way for higher priority path."""
    
    for path in paths_to_cancel:
        # Only cancel if new path has strictly higher priority
        if new_path.priority > path.priority:
            cancel_path(path, force=False)
```

### 10.4 Priority Inheritance

When a train claims a path:

```python
def on_train_claims_path(path: Path, train: Train):
    """Train physically enters and claims ownership of path."""
    
    path.train_id = train.train_id
    
    # Path inherits train's priority if path was created with default
    if path.priority == DEFAULT_ROUTE_PRIORITY:
        path.priority = train.priority
```

---

## 11. UI/UX Specification

### 11.1 Simulation Tab Integration

The Dispatcher Tool lives in the **Simulation** tab, with tools in the editor panel toolbar:

```
┌──────────────────────────────────────────────────────────────┐
│  [Editor]  [Simulation]  [Schedule]  [Settings]              │
├──────────────────────────────────────────────────────────────┤
│  SIMULATION MODE                                             │
│  ┌──────────────────────────────────────────────┐            │
│  │ Tools:                                       │            │
│  │  [Run] [Pause] [Stop]                        │            │
│  │  [Dispatcher] [Train Control]                │            │
│  │  [Path List] [Cancel Path] [Extend Path]  │            │
│  └──────────────────────────────────────────────┘            │
│                                                              │
│  Active Paths:                                              │
│  ┌──────────────────────────────────────────────┐            │
│  │ ROUTE_CENTRAL_TO_NORTH_001  [ACTIVE] T01 P5  │            │
│  │ ROUTE_DEPOT_TO_PLATFORM_002 [SET]    --- P7  │            │
│  └──────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────┘
```

### 11.2 Mode Exclusivity

Dispatcher mode is **exclusive** with editor mode:
- Cannot edit tracks while in dispatcher mode
- Cannot dispatch while in editor mode
- Clear mode indicator in UI

### 11.3 Dispatcher Tool Workflow

```
1. SELECT DISPATCHER TOOL
   └─> Cursor changes to dispatcher icon
   └─> Editor mode disabled

2. LEFT-CLICK ON START POINT (block or station)
   └─> Start point highlighted (bright cyan)
   └─> All reachable endpoints shown in ORANGE
       - Any switch configuration considered
       - No turn-around paths
   └─> Invalid endpoints remain normal color

3. RIGHT-CLICK TO CANCEL
   └─> Clear selection, return to step 2

4. LEFT-CLICK ON ENDPOINT (orange block/station)
   └─> Path calculation begins
   └─> If SUCCESS:
       └─> Path shown in RESERVED color
       └─> Switches show LOCKED indicator (brown ring)
       └─> Path added to Active Paths list
       └─> Return to step 2 (ready for next path)
   └─> If FAILURE (all paths blocked):
       └─> Error message displayed
       └─> Conflict tiles FLASH RED for 2 seconds
       └─> Return to step 2
```

### 11.4 Visual Indicators

| Element | State | Visual |
|---------|-------|--------|
| Block | Selected as start | Bright cyan highlight |
| Block | Reachable endpoint | Orange fill |
| Block | Reserved for path | Path-specific color |
| Block | Occupied by train | Train color overlay |
| Switch | Locked for path | Brown ring around indicator |
| Track | Part of active path | Path color on track lines |
| Track | Approach locked | Gradient indicating braking zone |
| Conflict | Blocked path | Red flash (2 seconds) |

### 11.5 Path List Context Actions

From the Path List panel, selecting a path shows actions:

| Action | Description |
|--------|-------------|
| View Details | Show blocks, train, ETA, priority |
| Extend Path | Open extension mode (select new endpoint) |
| Cancel Path | Cancel with confirmation if approach-locked |
| Change Priority | Adjust path priority (1-10) |

---

## 12. Data Structures

### 12.1 New Enums

```python
class PathStatus(Enum):
    PENDING = "pending"
    SETTING = "setting"
    SET = "set"
    ACTIVE = "active"
    CLEARING = "clearing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
```


### 12.2 New Dataclasses

```python
@dataclass
class PathPath:
    """Represents a potential path for a path."""
    tiles: List[Tuple[int, int]]
    distance: float
    required_switches: Dict[Tuple[int, int], int]
    blocks: List[str]

@dataclass
class ValidationError:
    """Represents a validation error for a path."""
    type: ValidationErrorType
    message: str
    tile: Optional[Tuple[int, int]] = None
    block_id: Optional[str] = None
    position: Optional[Tuple[int, int]] = None
    locked_by_path: Optional[str] = None

@dataclass
class ValidationResult:
    """Result of path validation."""
    valid: bool
    errors: List[ValidationError]
    path: PathPath

@dataclass
class PathResult:
    """Result of path creation attempt."""
    success: bool
    path: Optional[Path] = None
    error: Optional[str] = None
    attempted_paths: Optional[List[Tuple[PathPath, ValidationResult]]] = None
    conflict_tiles: Optional[List[Tuple[int, int]]] = None

@dataclass
class ExtensionResult:
    """Result of path extension attempt."""
    success: bool
    extended_blocks: Optional[List[str]] = None
    error: Optional[str] = None
    conflict_tiles: Optional[List[Tuple[int, int]]] = None

@dataclass
class CancellationResult:
    """Result of path cancellation."""
    success: bool
    blocks_released: List[str]
    switches_released: List[Tuple[int, int]]
    approach_locked: List[str]
    message: Optional[str] = None

@dataclass
class ConflictResolution:
    """How to handle path conflicts."""
    type: str  # 'PRIORITY_PROMPT', 'BLOCKED'
    message: str
    conflicting_paths: List[Path]
    default_action: str = 'NO'
```

### 12.3 Additions to Existing Classes

```python
# block.py - Block class additions
class Block:
    reserved_by_path: Optional[str] = None  # Path ID that reserved this block
    
    def reserve_for_path(self, path_id: str) -> bool:
        """Reserve block for a specific path."""
        if self.state == BlockState.FREE:
            self.state = BlockState.RESERVED
            self.reserved_by_path = path_id
            return True
        return False
    
    def is_reserved_for_path(self, path_id: str) -> bool:
        """Check if block is reserved for specific path."""
        return self.reserved_by_path == path_id

# track.py - TrackSegment class additions
class TrackSegment:
    locked_by_path: Optional[str] = None
    locked_state: Optional[int] = None
    
    def lock_for_path(self, path_id: str, state: int) -> bool:
        """Lock switch in specific state for path."""
        if self.locked_by_path is None:
            self.locked_by_path = path_id
            self.locked_state = state
            self.set_switch_state(state)
            return True
        return False
    
    def unlock_for_path(self, path_id: str) -> bool:
        """Unlock switch if locked by this path."""
        if self.locked_by_path == path_id:
            self.locked_by_path = None
            self.locked_state = None
            return True
        return False
    
    def is_locked(self) -> bool:
        """Check if switch is locked."""
        return self.locked_by_path is not None

# train.py - Train class additions
class Train:
    priority: int = 5  # Default priority
    max_braking_rate: float = 100.0  # cm/s^2
    
    def initiate_emergency_stop(self) -> None:
        """Begin emergency braking."""
        self.target_speed = 0
        self.is_emergency_braking = True
    
    def get_braking_distance(self) -> float:
        """Calculate current braking distance."""
        speed_cm_s = self.current_speed * 100000 / 3600
        return (speed_cm_s ** 2) / (2 * self.max_braking_rate)
```

### 12.4 Network Additions

```python
# network.py additions
class RailwayNetwork:
    active_paths: Dict[str, Path] = {}
    path_sequence_counter: int = 0
    
    def create_path(self, start_block_id: str, end_block_id: str, 
                     dispatcher_id: str, priority: int = 5) -> PathResult:
        """Create a new path between blocks."""
        ...
    
    def extend_path(self, path_id: str, new_end_block_id: str) -> ExtensionResult:
        """Extend an existing path to new destination."""
        ...
    
    def cancel_path(self, path_id: str, force: bool = False) -> CancellationResult:
        """Cancel a path and stop associated train."""
        ...
    
    def get_active_paths(self) -> List[Path]:
        """Get all active paths."""
        return list(self.active_paths.values())
    
    def get_reachable_endpoints(self, start_block_id: str) -> List[str]:
        """Get all blocks/stations reachable from start (for UI)."""
        ...
    
    def update_all_paths(self) -> None:
        """Called each simulation tick to update approach locking, etc."""
        for path in self.active_paths.values():
            if path.status == PathStatus.ACTIVE:
                update_approach_locking(path)
    
    def generate_path_id(self, start_block_id: str, end_block_id: str) -> str:
        """Generate human-readable path ID."""
        self.path_sequence_counter += 1
        start_name = self._get_block_display_name(start_block_id)
        end_name = self._get_block_display_name(end_block_id)
        return f"ROUTE_{start_name}_TO_{end_name}_{self.path_sequence_counter:03d}"
```

---

## 13. API Endpoints

### 13.1 Path Management

```
POST   /api/paths
GET    /api/paths
GET    /api/paths/<path_id>
DELETE /api/paths/<path_id>
PUT    /api/paths/<path_id>/extend
PUT    /api/paths/<path_id>/priority

       Body: { priority }
       Response: Updated path
```

### 13.2 Dispatcher Helpers

```
GET    /api/dispatcher/reachable/<block_id>
       Response: List of reachable block/station IDs

POST   /api/dispatcher/validate
       Body: { start_block_id, end_block_id }
       Response: Validation result without committing
```

---

## 14. Configuration

```python
# dispatcher_config.py

# Default priority
DEFAULT_ROUTE_PRIORITY = 5
MIN_PRIORITY = 1
MAX_PRIORITY = 10

# Safety distances (in centimeters)
REAR_SAFETY_DISTANCE_CM = 300.0       # Min distance behind train before releasing block
OVERLAP_DISTANCE_CM = 200.0           # Safety margin beyond destination
FLANK_PROTECTION_DISTANCE_CM = 500.0  # Distance to check for flank protection switches

# Braking
DEFAULT_MAX_BRAKING_RATE = 100.0      # cm/s^2
EMERGENCY_BRAKING_MULTIPLIER = 1.5    # Multiplier for emergency stops

# UI
CONFLICT_FLASH_DURATION_SEC = 2.0     # How long to flash conflict indicators
```

---

## 15. Implementation Phases

### Phase 1: Core Path System
1. `Path` dataclass and `PathStatus` enum
2. Path ID generation (human-readable)
3. Path creation with pathfinding
4. Block reservation for paths
5. Switch locking for paths
6. Basic path lifecycle (SET -> ACTIVE -> COMPLETED)

### Phase 2: Safety Features
1. Rear safety distance and progressive block release
2. Flank protection calculation and locking
3. Approach locking based on braking distance
4. Path overlap acquisition

### Phase 3: Path Operations
1. Path extension (non-destructive)
2. Path cancellation with approach lock handling
3. Train emergency stop integration
4. Priority system implementation

### Phase 4: UI Integration
1. Simulation tab structure
2. Dispatcher tool in toolbar
3. Endpoint highlighting (orange reachable)
4. Path visualization (reserved color, locked switches)
5. Conflict flash indicators (red, 2 seconds)
6. Path list panel with context actions

### Phase 5: Polish & API
1. REST API endpoints
2. Real-time path event updates
3. Priority conflict prompts
4. Configuration options
5. Comprehensive testing

---

## 16. Future Considerations

1. **Multiple trains per block:** Current design assumes one train per block. Shunting/yard operations may need multiple trains per block with different rules.

2. **Bi-directional paths:** Current design is unidirectional. True bi-directional operation (single-track lines with passing loops) may need additional complexity.

3. **Timetable integration:** Paths could be auto-created from schedule entries at appropriate times.

4. **Diamond crossings:** Need explicit design for locking perpendicular paths through at-grade crossings where trains cross paths without switching.

5. **Path chaining:** Automatically extend paths based on schedule to reduce dispatcher workload.

6. **Deadlock detection:** Detect when multiple paths would create a deadlock situation before confirming.

7. **Path templates:** Save common paths for quick re-dispatch.

---

## Appendix A: State Transition Diagrams

### A.1 Block State Transitions (Path Context)

```
                    reserve_for_path()
         ┌─────────────────────────────────────┐
         │                                     ▼
      ┌──────┐                           ┌──────────┐
      │ FREE │                           │ RESERVED │
      └──────┘                           └──────────┘
         ▲                                     │
         │ clear()                             │ occupy()
         │                                     ▼
    ┌─────────┐      set_to_caution()    ┌──────────┐
    │ CAUTION │ ◄─────────────────────── │ OCCUPIED │
    └─────────┘                          └──────────┘
```

### A.2 Path State Transitions

```
     create_path()
           │
           ▼
      ┌─────────┐    switches moving    ┌─────────┐
      │ PENDING │ ──────────────────►   │ SETTING │
      └─────────┘                       └─────────┘
           │                                 │
           │ (instant if no switch moves)    │ all switches set
           │                                 ▼
           └────────────────────────►   ┌─────────┐
                                        │   SET   │
                                        └─────────┘
                                             │
                                             │ train enters first block
                                             ▼
                                        ┌─────────┐
                                        │ ACTIVE  │
                                        └─────────┘
                                             │
                                             │ train enters final block
                                             ▼
                                        ┌──────────┐
                                        │ CLEARING │
                                        └──────────┘
                                             │
                                             │ all resources released
                                             ▼
                                        ┌───────────┐
                                        │ COMPLETED │
                                        └───────────┘

  (cancel_path() from any state except COMPLETED)
           │
           ▼
      ┌───────────┐
      │ CANCELLED │
      └───────────┘
```

---

## Appendix B: Example Scenarios

### B.1 Simple Path Creation

1. Dispatcher selects block BLK_001 (start)
2. System shows BLK_005, BLK_008, STATION_CENTRAL as reachable (orange)
3. Dispatcher clicks BLK_005
4. System finds shortest path: BLK_001 -> BLK_002 -> BLK_003 -> BLK_005
5. System reserves all blocks, locks switch at (5,3) to state 0
6. Path ROUTE_BLK_001_TO_BLK_005_001 created with status SET
7. Train T01 enters BLK_001, path becomes ACTIVE
8. Train progresses, blocks release behind with safety distance
9. Train enters BLK_005, path status CLEARING
10. Train stops, all resources released, path COMPLETED

### B.2 Path Extension While Moving

1. Path ROUTE_A_TO_B_001 is ACTIVE, train at block B (3 of 5)
2. Dispatcher clicks "Extend Path", selects new destination block C
3. System calculates extension from block 5 to C
4. Extension validated, blocks reserved, switches locked
5. Original path updated: now A -> ... -> B -> ... -> C
6. Train continues seamlessly to new destination

### B.3 Priority Conflict

1. Path ROUTE_X_001 (priority 5) holds blocks 3, 4, 5
2. Dispatcher creates ROUTE_Y_002 (priority 7) needing block 4
3. System detects conflict, prompts: "Higher priority path conflicts. Cancel ROUTE_X_001?"
4. Dispatcher selects "Yes" (or "No" - default)
5. If Yes: ROUTE_X_001 cancelled, train emergency brakes, ROUTE_Y_002 created
6. If No: ROUTE_Y_002 creation fails, conflict tiles flash red
