# Technical Specification: Signals and Blocks

> **Document Type:** Technical Implementation Specification
> **Version:** 2.0
> **Last Updated:** December 2025
> **Last Verified:** 2026-01-01

## Table of Contents

1. [Overview](#1-overview)
2. [Signal Class Specification](#2-signal-class-specification)
3. [Block Class Specification](#3-block-class-specification)
4. [Signal Placement Workflow](#4-signal-placement-workflow)
5. [Block Creation Workflow](#5-block-creation-workflow)
6. [Signal-Block Interaction](#6-signal-block-interaction)
7. [API Reference](#7-api-reference)
8. [UI Specifications](#8-ui-specifications)
9. [Validation Rules Reference](#9-validation-rules-reference)
10. [Signal State Calculation (Detailed)](#10-signal-state-calculation-detailed)
- [Appendix A: Track Bits Reference](#appendix-a-track-bits-reference)
- [Appendix B: Direction Semantics](#appendix-b-direction-semantics)
- [Appendix C: Color Reference](#appendix-c-color-reference)

---

## 1. Overview

### 1.1 Purpose

This document provides the complete technical specification for implementing signals and blocks in the railway control system. It serves as the authoritative reference for:

- Class definitions with all properties and methods
- Workflow sequences with allowed/forbidden actions
- UI behavior specifications
- API contracts
- Validation rules

### 1.2 Core Concepts

| Concept | Definition |
|---------|------------|
| **Signal** | A directional marker that monitors block occupancy and displays state (RED/YELLOW/GREEN) to approaching trains |
| **Block** | An electrically isolated track section where all tracks share voltage/current; the minimum control unit for train movement |
| **Vehicle-Driven Reservation** | Trains actively check signal states and reserve paths ahead; signals are passive indicators |

### 1.3 Data Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Network                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐ │
│  │ signals: Dict   │  │ blocks: Dict    │  │ tracks: Dict │ │
│  │ {id: Signal}    │  │ {id: Block}     │  │ {pos: Track} │ │
│  └────────┬────────┘  └────────┬────────┘  └──────┬───────┘ │
│           │                    │                   │         │
│           │    monitored_      │    block_         │         │
│           │    block_id        │    section_id     │         │
│           └────────────────────┼───────────────────┘         │
│                                │                             │
│              Signal ──────────►│◄─────────── Track           │
│                                Block                         │
└─────────────────────────────────────────────────────────────┘
```

**Source of Truth:**
- `network.signals` dict is authoritative for signal data
- `network.blocks` dict is authoritative for block data
- Track properties (`signal_*`, `block_section_id`) are synchronized for renderer compatibility

### 1.4 Important Implementation Note

> ⚠️ **All modifications to signals and blocks MUST go through the API/class methods.**  
> Direct manipulation of track properties is DEPRECATED and will be rejected.

---

## 2. Signal Class Specification

### 2.1 Signal Properties

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `signal_id` | `string` | Yes | Auto-generated | Unique identifier. Format: `SIG_{x}_{y}_{hex6}` |
| `position` | `(int, int)` | Yes | - | Grid coordinates (x, y) where signal is placed |
| `direction` | `string` | Yes | - | Direction signal faces: `NORTH`, `SOUTH`, `EAST`, `WEST` |
| `signal_type` | `SignalType` | No | `BLOCK` | Type of signal (see 2.2) |
| `state` | `SignalState` | No | `RED` | Current display state (see 2.3) |
| `name` | `string` | No | `null` | Human-readable name for display |
| `monitored_block_id` | `string` | No | `null` | Block ID this signal monitors |
| `monitored_switch_id` | `string` | No | `null` | Switch ID (for SWITCH type, future) |
| `monitored_signal_id` | `string` | No | `null` | Signal ID (for PRE_SIGNAL type, future) |
| `approaching_train_id` | `string` | No | `null` | Train ID assigned to this signal for route-based reservations (see 2.5) |

### 2.2 Signal Types (Enum: `SignalType`)

| Value | Description | Status |
|-------|-------------|--------|
| `BLOCK` | Monitors block occupancy | ✅ Implemented |
| `SWITCH` | Controls turnout position | 🔮 Future |
| `PRE_SIGNAL` | Shows next signal state | 🔮 Future |
| `ROUTE` | Combined route/speed indicator | 🔮 Future |

### 2.3 Signal States (Enum: `SignalState`)

| Value | Color | Hex Code | Meaning |
|-------|-------|----------|---------|
| `RED` | Red | `#e83f5b` | Stop - block occupied or not configured |
| `YELLOW` | Amber | `#ffd93d` | Caution - proceed with reduced speed |
| `GREEN` | Green | `#04d361` | Clear - safe to proceed |

### 2.4 Signal Methods

#### `Signal.to_dict() -> Dict`

Serialize signal to JSON-compatible dictionary.

**Returns:**
```python
{
    'signal_id': str,
    'x': int,
    'y': int,
    'direction': str,
    'signal_type': str,
    'state': str,
    'name': str | None,
    'monitored_block_id': str | None,
    'monitored_switch_id': str | None,
    'monitored_signal_id': str | None
}
```

#### `Signal.from_dict(data: Dict) -> Signal` (classmethod)

Create Signal instance from dictionary.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `data` | `Dict` | Yes | Dictionary with signal properties |

**Returns:** `Signal` instance

#### `Signal.update_from_block(block_occupied: bool) -> None`

Update signal state based on block occupancy (for BLOCK type signals only).

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `block_occupied` | `bool` | Whether the monitored block is occupied |

**Behavior:**
- If `block_occupied=True` → `state = RED`
- If `block_occupied=False` → `state = GREEN`

### 2.5 Train-Specific Signal Logic (approaching_train_id)

The `approaching_train_id` field enables route-based reservations where a signal can show GREEN to a specific train even when the monitored block is RESERVED (not FREE).

**Purpose:**
- When a dispatcher sets a route for a train, blocks ahead are RESERVED
- Normally, RESERVED blocks trigger RED signals (block not FREE)
- With `approaching_train_id`, the signal shows GREEN to the specific train the block is reserved for

**Signal State Calculation with approaching_train_id:**

```python
def _calculate_signal_state(signal):
    block = get_block(signal.monitored_block_id)
    
    if block.state == BlockState.FREE:
        # Normal case - block is clear
        return SignalState.GREEN
    
    elif block.state == BlockState.RESERVED:
        # Check if reserved for the approaching train
        if (signal.approaching_train_id and 
            block.occupied_by == signal.approaching_train_id):
            # Block is reserved for this signal's train - show GREEN
            return SignalState.GREEN  # (continues to Block B evaluation)
        else:
            # Reserved for another train - show RED
            return SignalState.RED
    
    elif block.state == BlockState.OCCUPIED:
        return SignalState.RED
    
    elif block.state == BlockState.CAUTION:
        return SignalState.YELLOW
```

**Integration with PathDispatcher:**

The `approaching_train_id` is set automatically by the `PathDispatcher.create_path()` method:

```python
# When setting a path
dispatcher.create_path(
    train_id="TRAIN_001",
    start_block_id="BLK_A",
    end_block_id="BLK_C"
)

# Internally: signal.approaching_train_id = train_id for each signal in the path
```

**Lifecycle:**
1. **Path Set:** `approaching_train_id` assigned to signals in path
2. **Train Progresses:** Signal continues showing GREEN as train approaches
3. **Path Complete/Cancelled:** `approaching_train_id` cleared to `null`

---

## 3. Block Class Specification

**IMPORTANT:** Use `network.block_manager` for all block state changes in production code. This ensures atomic transitions and avoids race conditions between the dispatcher and simulation loop.

### 3.1 Block Definition

A **Block** is an electrically isolated track section representing the minimum control unit for train movement. All tracks within a block share the same voltage/current.

**Key Concepts:**
- Physical length may differ from tile count (real-world scale vs grid)
- Signals mark block boundaries
- Switches are block boundaries (connect blocks but are NOT inside blocks)
- Stations are block boundaries

### 3.2 Block Properties

| Property | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `block_id` | `string` | Yes | - | Unique identifier (e.g., `BLK_001`) |
| `name` | `string` | No | `""` | Human-readable name |
| `length_cm` | `float` | No | `0.0` | Total track length in centimeters |
| `min_radius_cm` | `float` | No | `null` | Tightest curve radius in cm |
| `max_speed_kmh` | `float` | No | `null` | Speed limit for this block (km/h) |
| `entry_signals` | `List[str]` | No | `[]` | Signal IDs at block entry points *(Not implemented in block.py - future feature)* |
| `exit_signals` | `List[str]` | No | `[]` | Signal IDs at block exit points *(Not implemented in block.py - future feature)* |
| `state` | `BlockState` | No | `FREE` | Current occupancy state (see 3.3) |
| `occupied_by` | `string` | No | `null` | Train ID if occupied/reserved |

### 3.3 Block States (Enum: `BlockState`)

| Value | Track Color | Hex Code | Meaning |
|-------|-------------|----------|---------|
| `FREE` | Blue | `#4285f4` | Block is clear, available for entry |
| `RESERVED` | Green | `#04d361` | Block is reserved for an approaching train |
| `OCCUPIED` | Red | `#e83f5b` | Train is physically present in the block |
| `CAUTION` | Yellow | `#ffcd1e` | Train just left, next block still occupied |

**State Transitions:**
```
FREE ──reserve()──► RESERVED ──occupy()──► OCCUPIED ──release()──► CAUTION ──clear()──► FREE
```

### 3.4 Block Methods

#### `Block.to_dict() -> Dict`

Serialize block to JSON-compatible dictionary.

**Returns:**
```python
{
    'block_id': str,
    'name': str,
    'length_cm': float,
    'min_radius_cm': float | None,
    'max_speed_kmh': float | None,
    'entry_signals': List[str],
    'exit_signals': List[str],
    'state': str,
    'occupied_by': str | None
}
```

#### `Block.from_dict(data: Dict) -> Block` (classmethod)

Create Block instance from dictionary.

#### `Block.can_enter(train_id: str) -> bool`

Check if a train can enter this block.

**Returns:** `True` if block is `FREE` or `RESERVED` by the same train

#### `Block.reserve(train_id: str) -> bool`

Reserve block for specified train.

**Precondition:** Block state must be `FREE`  
**Postcondition:** Block state becomes `RESERVED`, `occupied_by` set to `train_id`  
**Returns:** `True` if successful, `False` if block not free

#### `Block.occupy(train_id: str) -> bool`

Mark block as occupied by specified train. **For testing/backward compatibility only.** Use `BlockManager.occupy_block()` in production.

**Precondition:** `can_enter(train_id)` returns `True`  
**Postcondition:** Block state becomes `OCCUPIED`, `occupied_by` set to `train_id`  
**Returns:** `True` if successful, `False` otherwise

#### `Block.release() -> None`

Release block, making it available.

**Postcondition:** Block state becomes `FREE`, `occupied_by` set to `null`

#### `Block.is_free() -> bool`

**Returns:** `True` if block state is `FREE`

#### `Block.is_occupied() -> bool`

**Returns:** `True` if block state is `OCCUPIED`

#### `Block.is_reserved() -> bool`

**Returns:** `True` if block state is `RESERVED`

---

## 4. Signal Placement Workflow

This section defines the complete workflow for placing signals, including validation, UI feedback, and automatic block connection.

### 4.1 Signal Position Availability

Signals have specific position slots on each tile based on track type.

#### 4.1.1 Long Paths (NS=1, EW=2) - Two Positions

On straight tracks, **two signal positions** are available (one per side):

| Track | Position 1 | Position 2 | Layout |
|-------|-----------|-----------|---------|
| NS (track_bits=1) | NORTH-facing (east side) | SOUTH-facing (west side) | Bidirectional |
| EW (track_bits=2) | EAST-facing (south side) | WEST-facing (north side) | Bidirectional |

**Position Rule:** Signals are placed on the RIGHT-HAND side relative to the train's travel direction.

#### 4.1.2 Short Paths (Diagonals) - One Position

On diagonal/curved tracks, **one signal position** at tile center:

| Track | Valid Directions | Position |
|-------|-----------------|----------|
| NE (track_bits=4) | NORTH, EAST | Center |
| NW (track_bits=8) | NORTH, WEST | Center |
| SE (track_bits=16) | SOUTH, EAST | Center |
| SW (track_bits=32) | SOUTH, WEST | Center |

#### 4.1.3 Forbidden Configurations - No Signals Allowed

| Configuration | track_bits | Reason |
|---------------|------------|--------|
| Crossings | 3, 24, 36 | Ambiguous signal semantics |
| Y-Switches | 5, 9, 17, 33, 6, 10, 18, 34 | Switch tiles cannot have signals |
| Diagonal Switches | 12, 20, 40, 48 | Switch tiles cannot have signals |
| Complex Junctions | 7, 11, 19, 35, 14, 38, 26, 50, 21, 37, 25, 41 | Too complex |
| Two Horizontal Short Paths | NE+SW (36), NW+SE (24) | Confusion risk |

### 4.2 Placement Workflow Sequence

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SIGNAL PLACEMENT WORKFLOW                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  User selects Signal Tool                                                   │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────┐                                                        │
│  │ Hover over tile │                                                        │
│  └────────┬────────┘                                                        │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────────────────────────────┐                               │
│  │ STEP 1: Check tile validity             │                               │
│  │   • Has track? (track_bits > 0)         │                               │
│  │   • Is switch/platform? → FORBIDDEN     │                               │
│  │   • track_bits in FORBIDDEN_SET? → NO   │                               │
│  └────────┬────────────────────────────────┘                               │
│           │                                                                 │
│      ┌────┴────┐                                                           │
│      │ Valid?  │                                                           │
│      └────┬────┘                                                           │
│     NO    │    YES                                                         │
│     │     │                                                                │
│     ▼     ▼                                                                │
│  ┌──────┐ ┌─────────────────────────────────┐                              │
│  │ RED  │ │ STEP 2: Check available slots   │                              │
│  │ TOOL │ │   • Get valid directions        │                              │
│  └──────┘ │   • Check existing signals      │                              │
│           │   • Calculate free positions    │                              │
│           └────────┬────────────────────────┘                              │
│                    │                                                        │
│               ┌────┴────┐                                                  │
│               │ Slots?  │                                                  │
│               └────┬────┘                                                  │
│          NONE      │     ≥1                                                │
│           │        │                                                       │
│           ▼        ▼                                                       │
│        ┌──────┐  ┌─────────────────────────────────┐                       │
│        │ RED  │  │ STEP 3: Show preview signal     │                       │
│        │ TOOL │  │   • Render at first free slot   │                       │
│        └──────┘  │   • Semi-transparent appearance │                       │
│                  └────────┬────────────────────────┘                       │
│                           │                                                │
│                           ▼                                                │
│                  ┌─────────────────────────────────┐                       │
│                  │ STEP 4: User interaction        │                       │
│                  │   • RIGHT-CLICK: Cycle to next  │                       │
│                  │     available position/direction│                       │
│                  │   • LEFT-CLICK: Place signal    │                       │
│                  └────────┬────────────────────────┘                       │
│                           │                                                │
│                      LEFT-CLICK                                            │
│                           │                                                │
│                           ▼                                                │
│                  ┌─────────────────────────────────┐                       │
│                  │ STEP 5: Create signal via API   │                       │
│                  │   POST /api/signal              │                       │
│                  └────────┬────────────────────────┘                       │
│                           │                                                │
│                           ▼                                                │
│                  ┌─────────────────────────────────┐                       │
│                  │ STEP 6: Auto-connect to block   │                       │
│                  │   (see Section 4.4)             │                       │
│                  └────────┬────────────────────────┘                       │
│                           │                                                │
│                           ▼                                                │
│                  ┌─────────────────────────────────┐                       │
│                  │ STEP 7: Render final signal     │                       │
│                  │   • Orange △ if no block        │                       │
│                  └─────────────────────────────────┘                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Signal Tool Visual Indicators

| Indicator | Appearance | Meaning |
|-----------|------------|---------|
| **Normal cursor** | Default | Signal can be placed |
| **Red tool/cursor** | Red tint | Placement forbidden (switch, platform, or no slots) |
| **Preview signal** | Semi-transparent | Shows where signal will be placed |
| **Orange triangle** | ⚠️ on placed signal | Signal has no monitored block configured |

### 4.4 Automatic Block Connection

When a signal is placed, the system automatically attempts to connect it to a block.

#### 4.4.1 Auto-Connect Algorithm: `find_nearest_block()`

```
function find_nearest_block(signal_position, signal_direction):
    """
    Scan path AHEAD of signal to find the first block.

    Signal direction semantics:
    - Signal "faces" a direction = light visible from that direction
    - Trains approach FROM the facing direction
    - Trains travel in OPPOSITE direction (that's where we search)
    """

    travel_direction = opposite(signal_direction)
    current_position = signal_position
    signal_own_block = get_block_at(signal_position)  # Exclude this

    WHILE NOT reached_termination_condition:
        next_position = move(current_position, travel_direction)
        next_track = get_track(next_position)

        IF next_track is NULL:
            RETURN None  # Dead end

        IF next_track.is_switch:
            RETURN None  # Stop at switch

        IF next_track.is_platform:
            RETURN None  # Stop at platform

        IF next_track.block_section_id is NOT NULL:
            IF next_track.block_section_id != signal_own_block:
                RETURN next_track.block_section_id  # Found block!

        # Continue along track
        current_position = next_position
        travel_direction = get_exit_direction(next_track, travel_direction)

    RETURN None  # No block found
```

#### 4.4.2 Connection Behavior

| Scenario | Action |
|----------|--------|
| No block on signal's tile | Search ahead, connect if found |
| Block found ahead | Set `monitored_block_id` to that block |
| No block found (dead end/switch/platform) | Leave `monitored_block_id` as `null`, show orange warning |
| Signal placed on existing block | Auto-connect excludes own block, searches beyond |

### 4.5 Block Splitting When Signal Placed

When a signal is placed on a tile that belongs to an existing block, the block is automatically split.

#### 4.5.1 Split Algorithm

```
function split_block_at_signal(signal, block):
    """
    Split a block into two parts when a signal is placed.

    The signal tile and all tiles BEHIND it (opposite to travel direction)
    remain in the original block. Tiles AHEAD become a new block or
    are removed from the original.
    """

    signal_tile = signal.position
    travel_direction = opposite(signal.direction)

    # Partition tiles
    tiles_behind = [signal_tile]  # Signal tile stays with "behind" block
    tiles_ahead = []

    FOR each tile IN block.tiles:
        IF tile == signal_tile:
            CONTINUE
        IF is_ahead_of_signal(tile, signal_tile, travel_direction):
            tiles_ahead.append(tile)
        ELSE:
            tiles_behind.append(tile)

    # Update original block
    block.tiles = tiles_behind
    signal.monitored_block_id = None  # Will be set by auto-connect

    # Handle tiles ahead
    IF tiles_ahead is NOT EMPTY:
        # Create new block or leave unassigned
        FOR tile IN tiles_ahead:
            tile.block_section_id = None  # Remove from original block

    # Check if original block is now empty (excluding signal tile)
    IF len(tiles_behind) == 1:  # Only signal tile
        delete_block(block)
        signal_tile.block_section_id = None

    # Auto-connect signal to block ahead
    auto_connect_signal(signal)
```

#### 4.5.2 Split Examples

**Example 1: Signal in middle of block**

```
BEFORE:
Block "BLK_A" = [T1, T2, T3, T4, T5]
                      ↑
              Place signal here (facing WEST, trains travel EAST)

AFTER:
Block "BLK_A" = [T1, T2, T3]  (signal tile + tiles behind)
Tiles [T4, T5] = unassigned (or new block)
Signal monitors: searches ahead, finds nothing or new block
```

**Example 2: Signal at block boundary**

```
BEFORE:
Block "BLK_A" = [T1, T2, T3]
Block "BLK_B" = [T4, T5, T6]
                 ↑
         Place signal at T3 (facing WEST)

AFTER:
Block "BLK_A" = [T1, T2, T3]  (unchanged, signal tile stays)
Block "BLK_B" = [T4, T5, T6]  (unchanged)
Signal monitors: "BLK_B" (found ahead)
```

### 4.6 Validation Rules Summary

#### 4.6.1 ALLOWED

| Condition | Result |
|-----------|--------|
| Simple track (NS, EW, diagonals) with free position | ✅ Can place |
| Long path with one existing signal | ✅ Can place on opposite side |
| Track with existing block | ✅ Can place, block will split |
| Track without any block | ✅ Can place, will try to find the nearest block to monitor, if failed, will show orange warning |

#### 4.6.2 FORBIDDEN

| Condition | Result | UI Feedback |
|-----------|--------|-------------|
| Switch tile (any switch track_bits) | ❌ Cannot place | Red tool |
| Platform/station tile | ❌ Cannot place | Red tool |
| Crossing (track_bits 3, 24, 36) | ❌ Cannot place | Red tool |
| All positions occupied | ❌ Cannot place | Red tool |
| No track at position | ❌ Cannot place | Red tool |

---

## 5. Block Creation Workflow

This section defines the complete workflow for creating and managing blocks.

### 5.1 Block Physical Concept

A **Block** represents an electrically isolated track section in model railway layouts:

- All tracks within a block share the same **voltage** and **current**
- Blocks are the **minimum control unit** for train movement
- Physical length (in cm) may differ from visual tile count

### 5.2 Block Boundary Rules

| Rule | Description | Reason |
|------|-------------|--------|
| **No switches inside blocks** | Switches (turnouts) MUST be at block boundaries, not inside | Prevents short circuits when switch points change |
| **Block cannot cross station boundaries** | Blocks must be fully inside OR fully outside stations | Maintains clear station/track separation |
| **Signals create boundaries** | A signal always divides tracks into separate blocks | Signals mark block entry/exit points |
| **Multiple consecutive blocks OK** | Blocks without signals between them are allowed | For electrical isolation without signaling |

### 5.3 Block Creation Workflow Sequence

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BLOCK CREATION WORKFLOW                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  User selects Block Tool                                                    │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────┐                                                        │
│  │ Click start tile│                                                        │
│  └────────┬────────┘                                                        │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────────────────────────────┐                               │
│  │ STEP 1: Validate start tile             │                               │
│  │   • Has track?                          │                               │
│  │   • Is switch? → WARN                   │                               │
│  │   • In station? → OK (blocks allowed)   │                               │
│  │   • Already has block? → Mode: EXTEND   │                               │
│  │   • No block? → Mode: CREATE            │                               │
│  └────────┬────────────────────────────────┘                               │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────────────────────────────┐                               │
│  │ STEP 2: Drag to select path             │                               │
│  │   • Pathfinder traces connected tracks  │                               │
│  │   • Real-time validation feedback       │                               │
│  │   • Highlight valid/invalid sections    │                               │
│  └────────┬────────────────────────────────┘                               │
│           │                                                                 │
│           ▼                                                                 │
│  ┌─────────────────────────────────────────┐                               │
│  │ STEP 3: Validate entire path            │                               │
│  │   • Check for switches in path          │                               │
│  │   • Check station boundaries            │                               │
│  │   • Check for signals (boundary!)       │                               │
│  │   • Check continuity                    │                               │
│  └────────┬────────────────────────────────┘                               │
│           │                                                                 │
│      ┌────┴────┐                                                           │
│      │ Valid?  │                                                           │
│      └────┬────┘                                                           │
│     NO    │    YES                                                         │
│     │     │                                                                │
│     ▼     ▼                                                                │
│  ┌──────────┐  ┌─────────────────────────────────┐                         │
│  │ Show     │  │ STEP 4: Show block dialog       │                         │
│  │ Error    │  │   • Block ID (auto or manual)   │                         │
│  │ Message  │  │   • Block name                  │                         │
│  └──────────┘  │   • Physical parameters         │                         │
│                └────────┬────────────────────────┘                         │
│                         │                                                  │
│                         ▼                                                  │
│                ┌─────────────────────────────────┐                         │
│                │ STEP 5: Create via API          │                         │
│                │   POST /api/blocks/create-with- │                         │
│                │        tracks                   │                         │
│                └────────┬────────────────────────┘                         │
│                         │                                                  │
│                         ▼                                                  │
│                ┌─────────────────────────────────┐                         │
│                │ STEP 6: Update track references │                         │
│                │   • Set block_section_id on     │                         │
│                │     each track                  │                         │
│                └────────┬────────────────────────┘                         │
│                         │                                                  │
│                         ▼                                                  │
│                ┌─────────────────────────────────┐                         │
│                │ STEP 7: Render block            │                         │
│                │   • Apply block color           │                         │
│                │   • Show block boundary         │                         │
│                └─────────────────────────────────┘                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 5.4 Block Validation Function

```python
def validate_block_path(path: List[Tile], mode: str) -> Tuple[bool, str]:
    """
    Validate a path for block creation/extension.

    Args:
        path: List of tiles to form the block
        mode: 'create' or 'extend'

    Returns:
        (is_valid, error_message)
    """

    FOR tile IN path:
        # Rule 1: No switches inside block
        IF tile.is_switch:
            RETURN (False, f"Switches cannot be inside blocks: {tile.position}")

        # Rule 2: Check for signals (creates boundary)
        IF tile.has_signal:
            RETURN (False, f"Signal at {tile.position} creates block boundary")

        # Rule 3: For CREATE mode, tiles must not have existing block
        IF mode == 'create' AND tile.block_section_id is NOT NULL:
            RETURN (False, f"Tile {tile.position} already assigned to block")

    # Rule 4: Validate station boundaries
    IF NOT validate_station_boundary(path):
        RETURN (False, "Block cannot cross station boundary")

    # Rule 5: Path must be continuous
    IF NOT is_continuous_path(path):
        RETURN (False, "Path is not continuous")

    RETURN (True, None)
```

### 5.5 Network Methods for Block Management

#### `Network.create_block(block_id, name) -> Block`

Create a new empty block.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `block_id` | `str` | Yes | Unique block identifier |
| `name` | `str` | No | Human-readable name |

**Returns:** New `Block` instance  
**Raises:** `ValueError` if block_id already exists

#### `Network.create_block_with_tracks(block_id, name, track_positions, ...) -> Block`

Create block and assign tracks in one operation.

**Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `block_id` | `str` | Yes | Unique block identifier |
| `name` | `str` | No | Human-readable name |
| `track_positions` | `List[(x,y)]` | Yes | Positions of tracks to include |
| `length_cm` | `float` | No | Physical length |
| `min_radius_cm` | `float` | No | Minimum curve radius |
| `max_speed_kmh` | `float` | No | Speed limit |

**Returns:** New `Block` instance with tracks assigned

#### `Network.extend_block(block_id, new_positions) -> Block`

Add tracks to an existing block.

**Precondition:** New positions must be adjacent to existing block tiles  
**Returns:** Updated `Block` instance

#### `Network.delete_block(block_id) -> bool`

Delete a block and clear all track references.

**Postcondition:** All tracks with this block_id have `block_section_id` set to `null`  
**Returns:** `True` if deleted, `False` if not found

#### `Network.assign_track_to_block(x, y, block_id) -> bool`

Assign a single track to a block.

**Returns:** `True` if successful

#### `Network.remove_track_from_block(x, y, block_id) -> bool`

Remove a single track from a block.

**Returns:** `True` if successful

#### `Network.get_block_tracks(block_id) -> List[Track]`

Get all tracks assigned to a block.

**Returns:** List of `Track` objects

#### `Network.validate_block_continuity(block_id) -> Tuple[bool, List[str]]`

Validate that all tracks in a block form a continuous path.

**Returns:** `(is_valid, list_of_issues)`

### 5.6 Block State Management

#### `Network.set_block_state(block_id, state) -> bool`

Set the occupancy state of a block.

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `block_id` | `str` | Block identifier |
| `state` | `BlockState` | New state (FREE, RESERVED, OCCUPIED, CAUTION) |

**Side Effects:**
- Updates block.state
- Triggers signal recalculation for all signals monitoring this block
- Updates track display colors

#### Right-Click State Cycling (UI)

Right-clicking on a track with an assigned block cycles the block state:

```
FREE → RESERVED → OCCUPIED → CAUTION → FREE → ...
(Blue)   (Green)    (Red)    (Yellow)  (Blue)
```

This calls `Network.set_block_state()` internally.

### 5.7 Station-Block Interaction Rules

Blocks and stations interact with clear boundary rules:

#### Allowed Configurations

1. **Block Fully Inside Station**
   * All block tiles belong to the same station
   * Example: Multi-platform station with separate block per platform
   * Use case: Independent control of different platform sections

2. **Block Fully Outside Stations**
   * No block tiles belong to any station
   * Standard track blocks on open track
   * Most common configuration for mainline tracks

#### Prohibited Configurations

1. **Block Crossing Station Boundary**
   * Block has some tiles in station, some outside
   * **Validation Error:** "Block cannot cross station boundary"
   * Reason: Maintains clear separation between station and track infrastructure

2. **Block Spanning Multiple Stations**
   * Block contains tiles from 2+ different stations
   * **Validation Error:** "Block cannot span multiple stations"
   * Reason: Each station must have independent block management

#### Station Extension Behavior

When a station extends over existing blocks/signals:

* **Blocks:** If station extends over block tiles, those tiles are removed from the block (may split block into separate blocks)
* **Signals:** If station extends over signal tiles, signals are automatically removed
* **Auto-created Block:** When a station is created, an auto-block `{station_id}_BLOCK` is created for convenience

This maintains the rule that blocks cannot cross station boundaries.

#### Best Practice

To avoid unexpected block splitting or signal removal:

1. Plan station locations before creating blocks
2. Create stations first, then add blocks within or outside station boundaries
3. If extending stations over existing infrastructure, check which blocks/signals will be affected

---

## 6. Signal-Block Interaction

This section defines how signals and blocks interact, including automatic signal state calculation.

### 6.1 Vehicle-Driven Reservation System

This system uses a **Vehicle-Driven Reservation System**:

- **Signals are passive** - they display state but don't actively control trains
- **Vehicles are active** - trains calculate block availability and update signal displays
- **Reservation model** - trains reserve blocks ahead, not just occupy them

#### Interaction Sequence

```
1. Train wants to move forward
2. Train checks can_enter_tile() for next tile
3. Train sees Signal on next tile
4. Train calls is_signal_clear():
   a. Looks at signal's monitored_block_id
   b. Checks block state (FREE/RESERVED/OCCUPIED/CAUTION)
   c. Optionally checks Block B (lookahead)
5. Train updates Signal display state based on result
6. Train moves or stops based on signal state
```

**Key Insight:** The Signal does not stop the train. The Train stops itself because the Signal "told" it the path ahead is blocked.

### 6.2 Signal State Calculation Algorithm

#### Priority: Block A Takes Precedence

When calculating signal state:
1. First check Block A (the signal's `monitored_block_id`)
2. If Block A is FREE, then check Block B (lookahead)
3. Block A state always overrides Block B considerations

#### Decision Tree

```
                    ┌─────────────────────────────┐
                    │  signal_block_id assigned?  │
                    └─────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                   NO                          YES
                    │                            │
                    ▼                            ▼
               ┌────────┐          ┌─────────────────────────┐
               │  RED   │          │  Block A = OCCUPIED?    │
               └────────┘          └─────────────────────────┘
                                                │
                                   ┌────────────┴────────────┐
                                  YES                        NO
                                   │                          │
                                   ▼                          ▼
                              ┌────────┐      ┌────────────────────────┐
                              │  RED   │      │  Block A = RESERVED?   │
                              └────────┘      └────────────────────────┘
                                                          │
                                             ┌────────────┴────────────┐
                                            YES                        NO
                                             │                          │
                                             ▼                          ▼
                                        ┌────────┐      ┌────────────────────────┐
                                        │  RED   │      │  Block A = CAUTION?    │
                                        └────────┘      └────────────────────────┘
                                                                    │
                                                       ┌────────────┴────────────┐
                                                      YES                        NO
                                                       │                          │
                                                       ▼                          ▼
                                                  ┌────────┐      ┌──────────────────────┐
                                                  │ YELLOW │      │  Block B exists?     │
                                                  └────────┘      └──────────────────────┘
                                                                              │
                                                                 ┌────────────┴────────────┐
                                                                NO                        YES
                                                                 │                          │
                                                                 ▼                          ▼
                                                            ┌────────┐      ┌──────────────────────┐
                                                            │ GREEN  │      │  Block B = FREE?     │
                                                            └────────┘      └──────────────────────┘
                                                                                        │
                                                                           ┌────────────┴────────────┐
                                                                          YES                        NO
                                                                           │                          │
                                                                           ▼                          ▼
                                                                      ┌────────┐                ┌────────┐
                                                                      │ GREEN  │                │ YELLOW │
                                                                      └────────┘                └────────┘
```

### 6.3 Complete State Matrix

| Block A | Block B | Switch Status | Signal Color |
|---------|---------|---------------|--------------|
| (not assigned) | - | - | **RED** |
| - | - | Path blocked | **RED** |
| OCCUPIED | (any) | Aligned | **RED** |
| RESERVED | (any) | Aligned | **RED** |
| CAUTION | (any) | Aligned | **YELLOW** |
| FREE | (none) | Aligned | **GREEN** |
| FREE | FREE | Aligned | **GREEN** |
| FREE | CAUTION | Aligned | **YELLOW** |
| FREE | RESERVED | Aligned | **YELLOW** |
| FREE | OCCUPIED | Aligned | **YELLOW** |

### 6.4 Switch Impact on Signals

When a switch lies between a signal and its monitored block:

| Switch Position | Route Status | Signal Result |
|-----------------|--------------|---------------|
| Points toward Block A | Route aligned | Normal calculation |
| Points away from Block A | Route blocked | **RED** |
| Unknown/Error state | Fail-safe | **RED** |

### 6.5 Signal State Update Triggers

Signal states are recalculated when:

1. **Block state changes** - Any monitored block's state changes
2. **Switch position changes** - Any switch in the signal's path changes
3. **Signal configuration changes** - `monitored_block_id` is modified
4. **Track layout changes** - Tracks affecting the signal's path are added/removed

---

## 7. API Reference

### 7.1 Signal API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/signals` | List all signals |
| `POST` | `/api/signal` | Create new signal |
| `GET` | `/api/signal/<signal_id>` | Get signal by ID |
| `PATCH` | `/api/signal/<signal_id>` | Update signal properties |
| `DELETE` | `/api/signal/<signal_id>` | Delete signal by ID |
| `GET` | `/api/signal/<x>/<y>` | Get signal at position |
| `DELETE` | `/api/signal/<x>/<y>` | Delete signal at position |
| `GET` | `/api/signal/validate/<x>/<y>` | Validate placement |
| `POST` | `/api/signal/<signal_id>/connect` | Connect signal to block |
| `GET` | `/api/signal/<signal_id>/reachable-blocks` | Get reachable blocks |

#### POST /api/signal - Create Signal

**Request Body:**
```json
{
    "x": 5,
    "y": 10,
    "direction": "NORTH",
    "signal_type": "block",
    "name": "Signal A",
    "auto_connect": true,
    "signal_id": "SIG_CUSTOM_001"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `x` | int | Yes | - | X coordinate |
| `y` | int | Yes | - | Y coordinate |
| `direction` | string | Yes | - | NORTH, SOUTH, EAST, WEST |
| `signal_type` | string | No | "block" | Signal type |
| `name` | string | No | null | Human-readable name |
| `auto_connect` | bool | No | true | Auto-connect to block ahead |
| `signal_id` | string | No | auto | Custom signal ID |

**Response:** Signal object (see Section 2.4)

**Error Responses:**
- `400`: Invalid position, direction, or signal already exists
- `400`: Track is switch/crossing (cannot place signal)

#### PATCH /api/signal/<signal_id> - Update Signal

**Allowed Updates:**
- `state`: SignalState value
- `name`: string
- `monitored_block_id`: string

**Immutable Properties (cannot change):**
- `signal_id`
- `position`
- `direction`

#### POST /api/signal/<signal_id>/connect - Connect to Block

**Request Body:**
```json
{
    "block_id": "BLK_001"
}
```

**Validation:**
- Block must exist
- Signal cannot monitor its own block (the block it sits on)
- Path must exist from signal to target block

### 7.2 Block API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/blocks` | List all blocks |
| `POST` | `/api/blocks` | Create empty block |
| `GET` | `/api/blocks/<block_id>` | Get block details |
| `PATCH` | `/api/blocks/<block_id>` | Update block properties |
| `DELETE` | `/api/blocks/<block_id>` | Delete block |
| `GET` | `/api/blocks/<block_id>/validate` | Validate continuity |
| `POST` | `/api/blocks/create-with-tracks` | Create with tracks |
| `POST` | `/api/blocks/<block_id>/extend` | Extend block |
| `POST` | `/api/blocks/<block_id>/tracks` | Add tracks |
| `DELETE` | `/api/blocks/<block_id>/tracks` | Remove tracks |
| `POST` | `/api/tracks/assign-block` | Bulk assign tracks |

#### POST /api/blocks/create-with-tracks - Create Block with Tracks

**Request Body:**
```json
{
    "block_id": "BLK_001",
    "name": "Main Line Block 1",
    "track_positions": [[0, 0], [1, 0], [2, 0]],
    "length_cm": 150.0,
    "min_radius_cm": 30.0,
    "max_speed_kmh": 60.0
}
```

**Response:** Block object (see Section 3.4)

#### PATCH /api/blocks/<block_id> - Update Block

**Allowed Updates:**
- `name`: string
- `length_cm`: float
- `min_radius_cm`: float
- `max_speed_kmh`: float
- `state`: BlockState value

### 7.3 Deprecated: Direct Track Manipulation

> ⚠️ **DEPRECATED:** Modifying signal properties via `/api/track` is no longer supported.

The following track properties are **read-only** and synchronized from the authoritative signal/block objects:

| Track Property | Source | Sync Direction |
|----------------|--------|----------------|
| `signal_state` | `Signal.state` | Signal → Track |
| `signal_direction` | `Signal.direction` | Signal → Track |
| `signal_id` | `Signal.signal_id` | Signal → Track |
| `signal_name` | `Signal.name` | Signal → Track |
| `signal_block_id` | `Signal.monitored_block_id` | Signal → Track |
| `block_section_id` | Assignment via Block API | Block → Track |

**Attempting to update these via `/api/track` will return:**
```json
{
    "error": "Signal management via /api/track is deprecated",
    "use_instead": "POST /api/signal to create, PATCH /api/signal/<id> to update"
}
```

### 7.3 RouteDispatcher Interface

The `RouteDispatcher` class provides high-level route management for train dispatching. It manages block reservations, switch locks, and signal assignments as a coordinated unit.

> **Note:** Dispatcher state is stored in-memory and not persisted. If the system restarts, all routes are cleared and must be re-established. This fail-safe behavior is consistent with real-world interlocking systems.

#### RouteDispatcher Methods

| Method | Description |
|--------|-------------|
| `set_route(train_id, signal_ids, block_ids, switch_positions)` | Set up a complete route for a train |
| `cancel_route(train_id)` | Cancel route and release all resources |
| `advance_train(train_id, entered_block_id)` | Called when train enters a new block |
| `train_exited_block(train_id, exited_block_id)` | Called when train completely exits a block |
| `complete_route(train_id)` | Complete route after train clears all blocks |
| `lock_switch(position, desired_state, dispatcher_id, train_id)` | Lock a switch |
| `unlock_switch(position, dispatcher_id)` | Unlock a switch |
| `get_route(train_id)` | Get route for a train |
| `get_locked_switches()` | Get all locked switches |
| `is_switch_locked(position)` | Check if switch is locked |

#### set_route() - Set Up Route

```python
success, failed_at = dispatcher.set_route(
    train_id="TRAIN_001",
    signal_ids=["SIG_1", "SIG_2"],
    block_ids=["BLK_A", "BLK_B", "BLK_C"],
    switch_positions={(3, 0): 0, (7, 2): 1}  # position -> state
)
```

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `train_id` | str | ID of the train |
| `signal_ids` | List[str] | Signal IDs to assign to this train |
| `block_ids` | List[str] | Block IDs to reserve (in order of travel) |
| `switch_positions` | Dict[(x,y), int] | Switch positions (0=MAIN, 1=DIVERGE) |
| `dispatcher_id` | str | ID of dispatcher (default: "default") |

**Returns:** `(success: bool, failed_at: Optional[str])`

**Resource Acquisition:**
1. Validates all blocks are FREE or already reserved for this train
2. Validates all switches can be locked (not locked by another)
3. Validates all signals can be assigned
4. Reserves all blocks with train_id
5. Sets and locks all switches
6. Sets `approaching_train_id` on all signals

**Failure Conditions:**
- Train already has active route
- Block is OCCUPIED
- Block is RESERVED by another train
- Switch is locked by another dispatcher
- Signal already assigned to another train

#### advance_train() - Train Block Progression

```python
dispatcher.advance_train("TRAIN_001", "BLK_B")
```

**Event-Based Staged Release:**
- When train enters Block B, Block A transitions: `OCCUPIED → CAUTION`
- When train enters Block C, Block A transitions: `CAUTION → FREE`

This provides a two-block buffer behind the train for realistic signaling.

**State Transitions:**
```
Train in A:  [A: OCCUPIED] [B: RESERVED] [C: RESERVED]
Train in B:  [A: CAUTION]  [B: OCCUPIED] [C: RESERVED]
Train in C:  [A: FREE]     [B: CAUTION]  [C: OCCUPIED]
```

#### lock_switch() / unlock_switch() - Switch Control

```python
# Lock switch at (3,0) to MAIN (0) for TRAIN_001
success, error = dispatcher.lock_switch((3, 0), 0, "dispatcher_1", "TRAIN_001")

# Unlock (only dispatcher who locked it can unlock)
success, error = dispatcher.unlock_switch((3, 0), "dispatcher_1")
```

**First-Come-First-Served:** Switches can only be locked by one dispatcher at a time.

#### Route Lifecycle

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  set_route() │────►│advance_train()│────►│complete_route()│
│              │     │  (multiple)   │     │              │
│  ACTIVE      │     │  ACTIVE       │     │  COMPLETED   │
└──────────────┘     └──────────────┘     └──────────────┘
       │                                         
       │                                         
       ▼                                         
┌──────────────┐                                
│cancel_route()│                                
│              │                                
│  CANCELLED   │                                
└──────────────┘                                
```

---

## 8. UI Specifications

### 8.1 Signal Visual Rendering

#### Signal Components

A signal is rendered with:

1. **Colored circle** - Shows current state (GREEN/YELLOW/RED)
2. **Grey outer ring** - Border around the circle
3. **Stick** - Points in the train's TRAVEL direction
4. **Orthogonal bar** - Cross-bar at end of stick
5. **Name label** - Displayed below signal if `signal_name` is set
6. **Warning indicator** - Orange triangle if no block configured

#### Signal Positioning

| Track Type | Position | Description |
|------------|----------|-------------|
| Long paths (NS=1, EW=2) | Right-hand side | 0.25 tile offset from center |
| Short paths (diagonals) | Tile center | No offset |

**Right-Hand Rule for Long Paths:**

| Train Travel | Signal Position |
|--------------|-----------------|
| NORTH | East side (right) |
| SOUTH | West side (right) |
| EAST | South side (right) |
| WEST | North side (right) |

#### Signal State Colors

| State | Color | Hex Code |
|-------|-------|----------|
| GREEN | Bright green | `#04d361` |
| YELLOW | Amber | `#ffd93d` |
| RED | Red | `#e83f5b` |

#### Warning Indicator

| Condition | Indicator | Appearance |
|-----------|-----------|------------|
| No `monitored_block_id` | Orange triangle | ⚠️ below signal |

### 8.2 Block Visual Rendering

#### Block State Colors (Track Fill)

| State | Color | Hex Code | Opacity |
|-------|-------|----------|---------|
| FREE | Blue | `#4285f4` | 30% |
| RESERVED | Green | `#04d361` | 30% |
| OCCUPIED | Red | `#e83f5b` | 30% |
| CAUTION | Yellow | `#ffcd1e` | 30% |
| (no block) | Grey | `#666666` | 100% |

#### Debug Visualization

When "Show Block Colors" is enabled:

1. **Block boundary colors** - Each block gets unique HSL color based on ID hash
2. **Signal-block connections** - Magenta dotted lines from signal to monitored block center

### 8.3 Properties Panel - Signal

When a signal is selected, display a **table layout** with:

| Field | Type | Editable | Description |
|-------|------|----------|-------------|
| Signal ID | Text | ❌ No | Auto-generated identifier |
| Location | Text | ❌ No | `(x, y)` coordinates |
| Signal Type | Dropdown | ✅ Yes | BLOCK, SWITCH, etc. |
| Facing Direction | Dropdown | ❌ No | Valid directions for track |
| Current State | Badge | ❌ No | Color indicator (R/Y/G) |
| Name | Input | ✅ Yes | Human-readable label |
| Monitored Block | Dropdown | ✅ Yes | Populated by `get_reachable_blocks()` |

**Monitored Block Dropdown:**
- Calls `/api/signal/<id>/reachable-blocks` to populate
- Shows only blocks reachable via track connectivity
- Excludes the block the signal sits on

### 8.4 Properties Panel - Block

When a block is selected, display:

| Field | Type | Editable | Description |
|-------|------|----------|-------------|
| Block ID | Text | ❌ No | Unique identifier |
| Name | Input | ✅ Yes | Human-readable name |
| Track Count | Text | ❌ No | Number of tiles in block |
| Length (cm) | Input | ✅ Yes | Physical track length |
| Min Radius (cm) | Input | ✅ Yes | Tightest curve radius |
| Max Speed (km/h) | Input | ✅ Yes | Speed limit |
| Current State | Badge | ❌ No | FREE/RESERVED/OCCUPIED/CAUTION |
| Occupied By | Text | ❌ No | Train ID if occupied |

### 8.5 Right-Click Behavior

#### On Track with Block

Cycles the block state:

```
FREE → RESERVED → OCCUPIED → CAUTION → FREE → ...
```

Calls `Network.set_block_state(block_id, next_state)`.

#### On Switch Tile

Toggles switch position:

```
MAIN ↔ DIVERGE
```

#### On Signal

No action (signals are now fully automatic).

### 8.6 Tool Indicators

| Tool | Normal | Invalid Hover |
|------|--------|---------------|
| Signal | Default cursor | Red tint cursor |
| Block | Blue highlight | Red highlight |
| Eraser | Default cursor | N/A |

**Invalid conditions for Signal tool:**
- Switch tile
- Platform tile
- Crossing tile
- All positions occupied

---

## 9. Validation Rules Reference

### 9.1 Signal Placement Validation

**File:** `signal_validation.py`

#### Forbidden Track Bits (No Signals Allowed)

```python
FORBIDDEN_SIGNAL_BITS = {
    # Crossings
    3,   # NS + EW
    24,  # NW + SE
    36,  # NE + SW

    # Y-switches (NS + diagonal)
    5, 9, 17, 33,

    # Y-switches (EW + diagonal)
    6, 10, 18, 34,

    # Diagonal switches
    12, 20, 40, 48,

    # Complex junctions (3+ segments)
    7, 11, 19, 35, 14, 38, 26, 50, 21, 37, 25, 41
}
```

#### Valid Directions per Track Type

```python
VALID_SIGNAL_DIRECTIONS = {
    1:  ['NORTH', 'SOUTH'],   # NS straight
    2:  ['EAST', 'WEST'],     # EW straight
    4:  ['NORTH', 'EAST'],    # NE diagonal
    8:  ['NORTH', 'WEST'],    # NW diagonal
    16: ['SOUTH', 'EAST'],    # SE diagonal
    32: ['SOUTH', 'WEST'],    # SW diagonal
}
```

#### Validation Functions

| Function | Parameters | Returns | Description |
|----------|------------|---------|-------------|
| `can_place_signal(track_bits)` | int | bool | Check if track allows signals |
| `get_valid_directions(track_bits)` | int | List[str] | Get valid directions |
| `is_valid_direction(track_bits, direction)` | int, str | bool | Check specific direction |
| `validate_signal_placement(track_bits, direction)` | int, str | (bool, str?) | Full validation with error |

### 9.2 Block Validation Rules

| Rule | Check | Error Message |
|------|-------|---------------|
| No switches | `tile.is_switch == False` | "Switches cannot be inside blocks" |
| Station boundaries | `validate_station_boundary(tiles)` | "Block cannot cross station boundary" |
| No signals crossing | `tile.has_signal == False` | "Signal creates block boundary" |
| Continuous path | `is_continuous_path(tiles)` | "Path is not continuous" |
| No existing block (create) | `tile.block_section_id == None` | "Tile already assigned" |

### 9.3 Signal-Block Connection Validation

| Rule | Check | Error Message |
|------|-------|---------------|
| Block exists | `block_id in blocks` | "Block not found" |
| Not own block | `block_id != signal_own_block` | "Cannot monitor own block" |
| Path exists | `_can_reach_block(signal, block_id)` | "No valid path exists" |

---

## 10. Signal State Calculation (Detailed)

This section provides the complete specification for signal state calculation.

### 10.1 Design Decisions

| Decision | Choice | Reasoning |
|----------|--------|-----------|
| RESERVED block in Block A | **RED** (full stop) | RESERVED = another train claimed it |
| Switch handling | **Auto-trace** | Follow current switch positions |
| No block assigned | **RED** (fail-safe) | Unassigned signals = restrictive |
| CAUTION block in Block A | **YELLOW** | Train just left, slow down |

### 10.2 Core Concepts

| Concept | Definition |
|---------|------------|
| **Block A** | Primary block - the `monitored_block_id` |
| **Block B** | Lookahead block - next block after Block A |
| **Path Tracing** | Follow track in signal direction, respecting switch positions |

### 10.3 Block State Priority

| State | Priority | Danger Level |
|-------|----------|--------------|
| OCCUPIED | 1 (highest) | DANGER |
| RESERVED | 2 | DANGER |
| CAUTION | 3 | CAUTION |
| FREE | 4 (lowest) | CLEAR |

#### Step 4: Get Block A State

```
block_a = get_block(signal.signal_block_id)
IF block_a IS NULL:
    RETURN RED  // Block doesn't exist, fail-safe

block_a_state = block_a.state
```

#### Step 5: Evaluate Block A State

```
SWITCH block_a_state:
    CASE OCCUPIED:
        RETURN RED
    CASE RESERVED:
        RETURN RED
    CASE CAUTION:
        RETURN YELLOW
    CASE FREE:
        // Continue to check Block B
        BREAK
```

#### Step 6: Find Block B (Lookahead)

```
// Continue path trace past Block A to find next block
block_b = NULL
FOR EACH tile IN path AFTER Block A ends:
    IF tile.block_id IS NOT NULL AND tile.block_id != block_a.id:
        block_b = get_block(tile.block_id)
        BREAK
```

#### Step 7: Evaluate Block B State

```
IF block_b IS NULL:
    RETURN GREEN  // No second block exists, first is clear

SWITCH block_b.state:
    CASE FREE:
        RETURN GREEN
    CASE CAUTION:
        RETURN YELLOW
    CASE RESERVED:
        RETURN YELLOW
    CASE OCCUPIED:
        RETURN YELLOW
```

### 10.5 Complete State Matrix

#### When Signal Has No Monitored Block

| Condition | Signal Color | Reason |
|-----------|--------------|--------|
| `signal_block_id` is null/empty | **RED** | Fail-safe default (must configure block) |

#### When Signal Has Block A Only (No Block B Exists)

| Block A State | Signal Color | Reason |
|---------------|--------------|--------|
| FREE | **GREEN** | Block is clear, safe to proceed |
| CAUTION | **YELLOW** | Train just left, proceed with caution |
| RESERVED | **RED** | Block claimed by another train, stop |
| OCCUPIED | **RED** | Train present in block, stop |

#### When Signal Has Block A and Block B (Two-Block Lookahead)

| Block A State | Block B State | Signal Color | Reason |
|---------------|---------------|--------------|--------|
| FREE | FREE | **GREEN** | Both blocks clear |
| FREE | CAUTION | **YELLOW** | Lookahead block in caution |
| FREE | RESERVED | **YELLOW** | Lookahead block is claimed |
| FREE | OCCUPIED | **YELLOW** | Lookahead block has train |
| CAUTION | (any) | **YELLOW** | First block in caution |
| RESERVED | (any) | **RED** | First block is claimed |
| OCCUPIED | (any) | **RED** | First block has train |

**Priority Rule:** Block A state takes precedence. When Block A is not FREE, Block B state is irrelevant.

### 10.6 Switch Impact on Signal Display

#### Case 1: Switch Between Signal and Block A

```
Layout:
Signal → [Switch] → [Block A]
```

| Switch Points To | Can Reach Block A? | Signal Color |
|------------------|-------------------|--------------|
| Route toward Block A | Yes | Normal calculation based on block states |
| Route AWAY from Block A | No | **RED** (route physically blocked) |

**Implementation:**

```
function is_route_blocked_by_switch(signal, block_a):
    path = trace_path(signal.position, signal.direction)

    for tile in path:
        if tile.is_switch:
            # Check if switch allows passage toward block_a
            if not switch_allows_route(tile, block_a):
                return TRUE  # Blocked

    return FALSE  # Not blocked
```

#### Case 2: Switch Between Block A and Block B

```
Layout:
Signal → [Block A] → [Switch] → [Block B-Main]
                            ↘ [Block B-Diverge]
```

The signal checks whichever Block B the switch currently points to:

| Switch Position | Block B to Check |
|-----------------|------------------|
| MAIN | Block B on main route |
| DIVERGE | Block B on diverging route |

**Implementation:**

```
function find_block_b(path, block_a):
    in_block_a = FALSE

    for tile in path:
        if tile.block_id == block_a.id:
            in_block_a = TRUE
        elif in_block_a and tile.block_id is not NULL:
            # Found a different block after Block A
            return get_block(tile.block_id)

        # If we hit a switch, path already follows current switch position

    return NULL  # No Block B found
```

#### Case 3: Switch Set Against Train (Entry Edge Not Active)

**CRITICAL:** A switch has multiple track segments, but only ONE is active at a time. If a train's entry edge is not part of the currently active segment, the switch is "set against" them - a derailment scenario.

```
Example: Switch with TRACK_EW | TRACK_NW (edges: E, W, N)

Active Segments by State:
- MAIN (0): EW is active → only E↔W connections work
- DIVERGE (1): NW is active → only N↔W connections work
```

| Switch State | Entry Edge | Active Segment | Result |
|--------------|------------|----------------|--------|
| MAIN (0) | E | EW (E↔W) ✓ | Exit W allowed |
| MAIN (0) | W | EW (E↔W) ✓ | Exit E allowed |
| MAIN (0) | **N** | EW (E↔W) ✗ | **RED** - switch set against train |
| DIVERGE (1) | N | NW (N↔W) ✓ | Exit W allowed |
| DIVERGE (1) | W | NW (N↔W) ✓ | Exit N allowed |
| DIVERGE (1) | **E** | NW (N↔W) ✗ | **RED** - switch set against train |

**Implementation:**

```python
def get_exits(entry_edge, switch_state):
    # Find which segment connects to entry edge
    main_exits = []    # via NS/EW segments
    diverge_exits = [] # via diagonal segments

    for segment in track_segments:
        if entry_edge in segment.edges:
            if segment.is_main:  # NS or EW
                main_exits.append(segment.other_edge)
            else:  # diagonal
                diverge_exits.append(segment.other_edge)

    if switch_state == MAIN:
        return main_exits if main_exits else []  # Empty = blocked
    else:
        return diverge_exits if diverge_exits else []  # Empty = blocked
```

#### Case 4: Switch Set to Unknown/Error State

| Condition | Signal Color | Reason |
|-----------|--------------|--------|
| Switch state is undefined | **RED** | Fail-safe |
| Switch state is error | **RED** | Fail-safe |

### 10.7 Path Tracing Implementation

#### Direction Mapping

For each signal direction, trace in this direction:

| Signal Direction | Train Travel | Trace Direction | Coordinate Change |
|------------------|--------------|-----------------|-------------------|
| NORTH | Northward (Y+) | North | y + 1 |
| SOUTH | Southward (Y-) | South | y - 1 |
| EAST | Eastward (X+) | East | x + 1 |
| WEST | Westward (X-) | West | x - 1 |

#### Track Connectivity

Use `ribi` (direction bits) to determine valid exits from each tile:

```
function get_next_tile(current_tile, direction):
    track = get_track_at(current_tile)

    # Check if track allows exit in this direction
    if not ribi_allows_exit(track.ribi, direction):
        return NULL  # Dead end

    # Get the tile in that direction
    next_pos = current_tile + direction_to_offset(direction)
    next_track = get_track_at(next_pos)

    if next_track is NULL:
        return NULL  # End of line

    # Check if next tile allows entry from opposite direction
    opposite = get_opposite_direction(direction)
    if not ribi_allows_entry(next_track.ribi, opposite):
        return NULL  # Cannot enter

    return next_pos
```

#### Switch Traversal

When path tracing encounters a switch:

```
function traverse_switch(switch_tile, entry_direction, switch_state):
    # Get the two possible exit directions for this switch
    exits = get_switch_exits(switch_tile.track_bits, entry_direction)

    if switch_state == MAIN:
        return exits.main
    else:  # DIVERGE
        return exits.diverge
```

### 10.8 Edge Cases

#### Edge Case 1: Signal at End of Line

* No Block B exists (dead end or buffer stop)
* Signal only evaluates Block A
* Use single-block logic from Section 10.5

#### Edge Case 2: Circular Track (Loop)

* Path tracing may return to starting point
* Stop tracing if we reach the signal's tile again
* Treat as "no Block B found"

#### Edge Case 3: Multiple Signals in Sequence

```
Layout:
Signal1 → Signal2 → [Block A]
```

* Each signal operates independently
* Signal1 should see Signal2 as its Block A boundary (if no block between them)
* This is typically a configuration error - warn user

#### Edge Case 4: Block Contains Switch Tile

Per Section 5 rules, blocks should NOT contain switches. If violated:
* Log warning: "Block X contains switch at tile (x,y)"
* Signal behavior is undefined - may produce incorrect results
* Recommend user fix block assignment

#### Edge Case 5: Signal Block ID References Non-Existent Block

```
if get_block(signal.signal_block_id) is NULL:
    log_error("Signal references non-existent block")
    return RED  # Fail-safe
```

### 10.9 Complete Implementation (Pseudocode)

```python
def calculate_signal_state(signal):
    """
    Calculate signal color based on block states and switch positions.

    Args:
        signal: Signal object with properties:
            - signal_block_id: string or null
            - signal_direction: 'N', 'E', 'S', or 'W'
            - position: {x, y}

    Returns:
        'RED', 'YELLOW', or 'GREEN'
    """

    # ========== STEP 1: Check block assignment ==========
    if signal.signal_block_id is None or signal.signal_block_id == '':
        return 'RED'  # Fail-safe: unconfigured signal

    # ========== STEP 2: Get Block A ==========
    block_a = get_block_by_id(signal.signal_block_id)
    if block_a is None:
        return 'RED'  # Block doesn't exist, fail-safe

    # ========== STEP 3: Trace path from signal ==========
    direction = signal.signal_direction
    path = trace_path_from_signal(signal.position, direction)

    # ========== STEP 4: Check for blocking switches ==========
    if is_route_blocked_by_switch(path, block_a):
        return 'RED'  # Switch blocks route to Block A

    # ========== STEP 5: Evaluate Block A state ==========
    block_a_state = block_a.state

    if block_a_state == 'OCCUPIED':
        return 'RED'

    if block_a_state == 'RESERVED':
        return 'RED'

    if block_a_state == 'CAUTION':
        return 'YELLOW'

    # Block A is FREE, continue to check Block B

    # ========== STEP 6: Find Block B ==========
    block_b = find_next_block_along_path(path, block_a)

    if block_b is None:
        return 'GREEN'  # No second block, first is clear

    # ========== STEP 7: Evaluate Block B state ==========
    block_b_state = block_b.state

    if block_b_state == 'FREE':
        return 'GREEN'
    else:
        return 'YELLOW'  # CAUTION, RESERVED, or OCCUPIED


def trace_path_from_signal(start_position, direction):
    """
    Trace track path from signal position, following switch states.

    Returns:
        List of {position, tile_data} along the path
    """
    path = []
    current_pos = start_position
    current_dir = direction
    visited = set()

    while True:
        # Prevent infinite loops
        pos_key = (current_pos.x, current_pos.y)
        if pos_key in visited:
            break
        visited.add(pos_key)

        # Get next tile in current direction
        next_pos = get_adjacent_position(current_pos, current_dir)
        tile = get_tile_at(next_pos)

        if tile is None or tile.track is None:
            break  # End of line

        # Check if we hit a signal (boundary)
        if tile.signal is not None and tile != start_position:
            break  # Stop at next signal

        path.append({
            'position': next_pos,
            'tile': tile,
            'block_id': tile.block_id
        })

        # Handle switches
        if tile.is_switch:
            # Get exit direction based on switch state
            current_dir = get_switch_exit_direction(
                tile.track_bits,
                current_dir,
                tile.switch_state  # 'MAIN' or 'DIVERGE'
            )
        else:
            # Continue in same direction (or follow curve)
            current_dir = get_track_exit_direction(tile.track_bits, current_dir)

        current_pos = next_pos

    return path


def is_route_blocked_by_switch(path, target_block):
    """
    Check if any switch along the path blocks the route to target_block.
    """
    for step in path:
        tile = step['tile']

        if tile.is_switch:
            # Check if this switch's current position leads toward target_block
            # We need to verify the switch connects to tiles that are part of target_block

            switch_exit = get_current_switch_exit(tile)
            reachable_blocks = get_blocks_reachable_from(switch_exit)

            if target_block.id not in reachable_blocks:
                return True  # Route blocked

        # If we've reached the target block, stop checking
        if step['block_id'] == target_block.id:
            break

    return False  # Route not blocked


def find_next_block_along_path(path, block_a):
    """
    Find Block B by continuing path trace past Block A.
    """
    passed_block_a = False

    for step in path:
        block_id = step['block_id']

        if block_id == block_a.id:
            passed_block_a = True
        elif passed_block_a and block_id is not None and block_id != block_a.id:
            # Found a different block after Block A
            return get_block_by_id(block_id)

    return None  # No Block B found
```

### 10.10 Signal State Update Triggers

The signal state should be recalculated when:

1. **Block state changes** - Any block's state changes (FREE ↔ RESERVED ↔ OCCUPIED ↔ CAUTION)
2. **Switch position changes** - Any switch along the signal's path changes position
3. **Signal configuration changes** - `signal_block_id` is modified
4. **Track layout changes** - Tracks are added/removed that affect the signal's path

**Implementation Recommendation:**

```javascript
// Option 1: Recalculate all signals when any change occurs
function on_block_state_change(block_id) {
    for (signal of all_signals) {
        signal.state = calculate_signal_state(signal);
    }
}

// Option 2: Only recalculate affected signals (more efficient)
function on_block_state_change(block_id) {
    affected_signals = get_signals_monitoring_block(block_id);
    for (signal of affected_signals) {
        signal.state = calculate_signal_state(signal);
    }
}
```

### 10.11 Quick Reference: Signal Color Decision Tree

```
                    ┌─────────────────────────────┐
                    │  signal_block_id assigned?  │
                    └─────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                   NO                          YES
                    │                            │
                    ▼                            ▼
               ┌────────┐          ┌─────────────────────────┐
               │  RED   │          │  Switch blocking route? │
               └────────┘          └─────────────────────────┘
                                                │
                                   ┌────────────┴────────────┐
                                  YES                        NO
                                   │                          │
                                   ▼                          ▼
                              ┌────────┐        ┌────────────────────────┐
                              │  RED   │        │  Block A = OCCUPIED?   │
                              └────────┘        └────────────────────────┘
                                                            │
                                               ┌────────────┴────────────┐
                                              YES                        NO
                                               │                          │
                                               ▼                          ▼
                                          ┌────────┐      ┌────────────────────────┐
                                          │  RED   │      │  Block A = RESERVED?   │
                                          └────────┘      └────────────────────────┘
                                                                      │
                                                         ┌────────────┴────────────┐
                                                        YES                        NO
                                                         │                          │
                                                         ▼                          ▼
                                                    ┌────────┐      ┌────────────────────────┐
                                                    │  RED   │      │  Block A = CAUTION?    │
                                                    └────────┘      └────────────────────────┘
                                                                                │
                                                                   ┌────────────┴────────────┐
                                                                  YES                        NO
                                                                   │                          │
                                                                   ▼                          ▼
                                                              ┌────────┐      ┌──────────────────────┐
                                                              │ YELLOW │      │  Block A = FREE      │
                                                              └────────┘      │  Block B exists?     │
                                                                              └──────────────────────┘
                                                                                          │
                                                                             ┌────────────┴────────────┐
                                                                            NO                        YES
                                                                             │                          │
                                                                             ▼                          ▼
                                                                        ┌────────┐      ┌──────────────────────┐
                                                                        │ GREEN  │      │  Block B = FREE?     │
                                                                        └────────┘      └──────────────────────┘
                                                                                                    │
                                                                                       ┌────────────┴────────────┐
                                                                                      YES                        NO
                                                                                       │                          │
                                                                                       ▼                          ▼
                                                                                  ┌────────┐                ┌────────┐
                                                                                  │ GREEN  │                │ YELLOW │
                                                                                  └────────┘                └────────┘
```

### 10.12 Summary Table: All Signal States

| Block A | Block B | Switch Status | Signal Color |
|---------|---------|---------------|--------------|
| (none/unconfigured) | - | - | **RED** |
| - | - | Blocking | **RED** |
| OCCUPIED | (any) | Aligned | **RED** |
| RESERVED | (any) | Aligned | **RED** |
| CAUTION | (any) | Aligned | **YELLOW** |
| FREE | (none) | Aligned | **GREEN** |
| FREE | FREE | Aligned | **GREEN** |
| FREE | CAUTION | Aligned | **YELLOW** |
| FREE | RESERVED | Aligned | **YELLOW** |
| FREE | OCCUPIED | Aligned | **YELLOW** |

---

## Appendix A: Track Bits Reference

### Valid Signal Configurations

| track_bits | Binary | Segment | Valid Directions | Max Signals |
|------------|--------|---------|------------------|-------------|
| 1 | 0b000001 | NS | NORTH, SOUTH | 2 |
| 2 | 0b000010 | EW | EAST, WEST | 2 |
| 4 | 0b000100 | NE | NORTH, EAST | 1 |
| 8 | 0b001000 | NW | NORTH, WEST | 1 |
| 16 | 0b010000 | SE | SOUTH, EAST | 1 |
| 32 | 0b100000 | SW | SOUTH, WEST | 1 |

### Forbidden Configurations (Complete List)

| track_bits | Type | Reason |
|------------|------|--------|
| 3 | NS+EW Crossing | Ambiguous semantics |
| 24 | NW+SE Crossing | Ambiguous semantics |
| 36 | NE+SW Crossing | Ambiguous semantics |
| 5, 9, 17, 33 | Y-switch (NS) | Switch tile |
| 6, 10, 18, 34 | Y-switch (EW) | Switch tile |
| 12, 20, 40, 48 | Diagonal switch | Switch tile |
| 7, 11, 19, 35 | Complex (NS+EW+diag) | Complex junction |
| 14, 38, 26, 50 | Complex (EW+2diag) | Complex junction |
| 21, 37, 25, 41 | Complex (NS+2diag) | Complex junction |

---

## Appendix B: Direction Semantics

### Signal Direction Meaning

| Direction | Signal Faces | Trains Approach From | Trains Travel |
|-----------|--------------|---------------------|---------------|
| NORTH | North | North side | Southward |
| SOUTH | South | South side | Northward |
| EAST | East | East side | Westward |
| WEST | West | West side | Eastward |

### Direction Vectors

| Direction | (dx, dy) | Grid Movement |
|-----------|----------|---------------|
| NORTH | (0, +1) | Y increases |
| SOUTH | (0, -1) | Y decreases |
| EAST | (+1, 0) | X increases |
| WEST | (-1, 0) | X decreases |

---

## Appendix C: Color Reference

### Signal Colors

| State | Hex | RGB |
|-------|-----|-----|
| GREEN | `#04d361` | rgb(4, 211, 97) |
| YELLOW | `#ffd93d` | rgb(255, 217, 61) |
| RED | `#e83f5b` | rgb(232, 63, 91) |

### Block Colors

| State | Hex | RGB | Opacity |
|-------|-----|-----|---------|
| FREE | `#4285f4` | rgb(66, 133, 244) | 30% |
| RESERVED | `#04d361` | rgb(4, 211, 97) | 30% |
| OCCUPIED | `#e83f5b` | rgb(232, 63, 91) | 30% |
| CAUTION | `#ffcd1e` | rgb(255, 205, 30) | 30% |
| (no block) | `#666666` | rgb(102, 102, 102) | 100% |

---

*End of Technical Specification*
