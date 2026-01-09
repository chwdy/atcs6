# Testing Specification

> **Created:** 2026-01-01
> **Purpose:** Comprehensive testing guide for manual verification and future automated testing

## Table of Contents

1. [Editor Tools](#1-editor-tools)
   - [Track Placement Tool](#11-track-placement-tool)
   - [Block/Signal Placement Tool](#12-blocksignal-placement-tool)
   - [Station Tool](#13-station-tool)
   - [Eraser Tool](#14-eraser-tool)
2. [Simulation Behaviors](#2-simulation-behaviors)
   - [Block State Management](#21-block-state-management)
   - [Signal State Calculation](#22-signal-state-calculation)
   - [Switch Operations](#23-switch-operations)
3. [Train Movement](#3-train-movement)
4. [Route Dispatching](#4-route-dispatching)
5. [Integration Scenarios](#5-integration-scenarios)
6. [API Testing](#6-api-testing)
   - [Block APIs with Tile Lists](#61-block-apis-with-tile-lists)
   - [Station Extension APIs](#62-station-extension-apis)
   - [Bulk Track Operations](#63-bulk-track-operations)
   - [Frontend-Backend Validation Parity](#64-frontend-backend-validation-parity)

---

## How to Use This Document

### Manual Testing
Each test case includes:
- **Test ID**: Unique identifier for tracking
- **Scenario**: What to set up and do
- **Expected Behavior**: What should happen
- **Steps**: Detailed manual testing steps

### Future Automation
Test cases marked "Automatable: Yes" can be converted to pytest (backend) or Jest (frontend) tests.
The **Setup** and **Assertion** sections provide the key logic.

---

## 1. Editor Tools

### 1.1 Track Placement Tool

#### TRK-001: Place Track on Empty Tile

| Field | Value |
|-------|-------|
| **Test ID** | TRK-001 |
| **Scenario** | Place track segment on an empty tile |
| **Expected Behavior** | Single path created matching the direction of drag |
| **Automatable** | Yes |

**Steps:**
1. Select Track Tool from toolbar
2. Click and drag from empty tile (3,3) to empty tile (3,5)
3. Release mouse button

**Assertions:**
- Tiles (3,3), (3,4), (3,5) now have tracks
- Each tile has `track_bits = 1` (NS path)
- No switches created

**API Test:**
```python
def test_place_track_on_empty():
    network = RailwayNetwork()
    # Simulate track placement via API
    result = network.set_track_bits(3, 3, TRACK_NS)
    assert result == True
    track = network.get_track(3, 3)
    assert track.track_bits == 1
    assert track.is_switch == False
```

---

#### TRK-002: Place Track Following Existing Direction

| Field | Value |
|-------|-------|
| **Test ID** | TRK-002 |
| **Scenario** | Drag through tile with existing track in same direction |
| **Expected Behavior** | Track extended, no change to existing tile |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5)
2. Select Track Tool
3. Drag from (5,4) through (5,5) to (5,6)

**Assertions:**
- Tile (5,5) still has `track_bits = 1` (unchanged)
- Tiles (5,4) and (5,6) have `track_bits = 1`
- No switches created

---

#### TRK-003: Create Switch by Adding Different Direction

| Field | Value |
|-------|-------|
| **Test ID** | TRK-003 |
| **Scenario** | Add track in different direction to existing track tile |
| **Expected Behavior** | Creates a switch (Y-junction) if valid configuration |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5) - `track_bits = 1`
2. Select Track Tool
3. Drag from (4,5) to (5,5) - this adds EW component

**Assertions:**
- Tile (5,5) now has `track_bits = 3` (NS + EW = 1 + 2)
- Track type is `DIAMOND` (no switch - paths don't share edge)

**Alternative - Create Y-Switch:**
1. Create NS track at (5,5) - `track_bits = 1`
2. Drag from (6,6) to (5,5) - this adds NE component

**Assertions:**
- Tile (5,5) now has `track_bits = 5` (NS + NE = 1 + 4)
- Track type is `SWITCH`
- `is_switch == True`
- Switch at edge N with exits [E, S]

---

#### TRK-004: Reject Forbidden Configuration

| Field | Value |
|-------|-------|
| **Test ID** | TRK-004 |
| **Scenario** | Attempt to create a 3-way switch (forbidden) |
| **Expected Behavior** | Placement rejected, track unchanged |
| **Automatable** | Yes |

**Steps:**
1. Create track with `track_bits = 5` (NS + NE) at (5,5)
2. Try to add NW component (would make bits = 13)

**Assertions:**
- `track_bits` remains 5 (unchanged)
- Error message displayed: "Cannot create forbidden configuration"
- 13 is in FORBIDDEN_TRACK_BITS

**Backend Test:**
```python
def test_reject_forbidden_config():
    segment = TrackSegment(x=5, y=5, track_bits=5)  # NS + NE
    result = segment.add_track_bit(TRACK_NW)  # Would make 13
    assert result == False
    assert segment.track_bits == 5  # Unchanged
    assert 13 in FORBIDDEN_TRACK_BITS
```

---

#### TRK-005: Diagonal Build Mode (Shift Key)

| Field | Value |
|-------|-------|
| **Test ID** | TRK-005 |
| **Scenario** | Hold Shift while dragging to create diagonal tracks |
| **Expected Behavior** | Creates stair-stepped NW/SE or NE/SW segments |
| **Automatable** | Yes (UI) |

**Steps:**
1. Select Track Tool
2. Hold Shift key
3. Drag from (0,0) to (4,4)

**Assertions:**
- Path uses diagonal segments (NE, SE, SW, or NW bits)
- Creates a zigzag pattern instead of straight lines
- `diagonalBuildMode = true` while Shift held

---

#### TRK-006: Drag Over Station Tile (Protected)

| Field | Value |
|-------|-------|
| **Test ID** | TRK-006 |
| **Scenario** | Drag track path through an existing station |
| **Expected Behavior** | Station tile protected, track path stops at station boundary |
| **Automatable** | Yes |

**Steps:**
1. Create a station at (5,5)
2. Select Track Tool
3. Drag from (3,5) to (7,5) (would pass through station)

**Assertions:**
- Station tile (5,5) is not modified
- Pathfinder routes around or stops at station

---

#### TRK-007: Drag Over Signal Tile (Protected)

| Field | Value |
|-------|-------|
| **Test ID** | TRK-007 |
| **Scenario** | Drag track path through a tile with signal |
| **Expected Behavior** | Signal tile protected, underlying track may be modified but signal preserved |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5) with signal facing NORTH
2. Select Track Tool
3. Drag from (4,5) to (6,5)

**Assertions:**
- Signal remains on tile
- Track may be modified if valid (e.g., crossing created)

---

### 1.2 Block/Signal Placement Tool

#### BLK-001: Signal on Simple NS/EW Track

| Field | Value |
|-------|-------|
| **Test ID** | BLK-001 |
| **Scenario** | Place signal on straight (NS or EW) track |
| **Expected Behavior** | Signal placed at right-hand side of track |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5)
2. Select Signal Tool
3. Click on (5,5)

**Assertions:**
- Signal created with direction from valid set [N, S]
- Signal positioned on east side (for north-facing) or west side (for south-facing)
- Max 2 signals allowed on long paths (one per direction)

**Validation API:**
```python
VALID_SIGNAL_DIRECTIONS = {
    1:  ['N', 'S'],      # NS straight
    2:  ['E', 'W'],      # EW straight
}
```

---

#### BLK-002: Signal on Diagonal Track

| Field | Value |
|-------|-------|
| **Test ID** | BLK-002 |
| **Scenario** | Place signal on diagonal (NE, NW, SE, SW) track |
| **Expected Behavior** | Signal placed at tile center, max 1 signal |
| **Automatable** | Yes |

**Steps:**
1. Create NE track at (5,5) - `track_bits = 4`
2. Select Signal Tool
3. Click on (5,5)

**Assertions:**
- Signal created with direction from valid set [N, E]
- Signal positioned at tile center
- Only 1 signal allowed on short paths

---

#### BLK-003: Signal on Switch Tile (Rejected)

| Field | Value |
|-------|-------|
| **Test ID** | BLK-003 |
| **Scenario** | Attempt to place signal on a switch (junction) tile |
| **Expected Behavior** | Placement rejected, red cursor shown |
| **Automatable** | Yes |

**Steps:**
1. Create switch at (5,5) - `track_bits = 5` (NS + NE)
2. Select Signal Tool
3. Hover over (5,5)

**Assertions:**
- Red cursor/indicator shown
- Click does nothing
- Toast message: "Cannot place signal on switch tile"

**Validation:**
```python
FORBIDDEN_SIGNAL_BITS = {
    5, 9, 17, 33,  # Y-switches (NS + diagonal)
    6, 10, 18, 34, # Y-switches (EW + diagonal)
    12, 20, 40, 48, # Diagonal switches
    # ... all multi-path configurations
}
```

---

#### BLK-004: Signal on Crossing Tile (Rejected)

| Field | Value |
|-------|-------|
| **Test ID** | BLK-004 |
| **Scenario** | Attempt to place signal on a diamond crossing |
| **Expected Behavior** | Placement rejected, red cursor shown |
| **Automatable** | Yes |

**Steps:**
1. Create crossing at (5,5) - `track_bits = 3` (NS + EW)
2. Select Signal Tool
3. Hover over (5,5)

**Assertions:**
- Red cursor/indicator shown
- Click does nothing
- Crossing track_bits (3, 24, 36) are in forbidden set

---

#### BLK-005: Create Block Over Zigzag Line

| Field | Value |
|-------|-------|
| **Test ID** | BLK-005 |
| **Scenario** | Drag block selection over a zigzag track path |
| **Expected Behavior** | Block follows existing track path correctly |
| **Automatable** | Yes |

**Steps:**
1. Create zigzag track: (0,0)→(1,0)→(1,1)→(2,1)→(2,2)
2. Select Block Tool
3. Drag from (0,0) to (2,2)

**Assertions:**
- Block created containing all 5 tiles
- `ExistingTrackPathfinder` follows the connected path
- No tiles outside the path are included

---

#### BLK-006: Block Extension Over Signal

| Field | Value |
|-------|-------|
| **Test ID** | BLK-006 |
| **Scenario** | Extend a block over tiles that contain signals |
| **Expected Behavior** | Confirmation dialog appears, signal marked as needing reconfiguration |
| **Automatable** | Partial (requires UI interaction) |

**Steps:**
1. Create block "BLK_A" at tiles (0,0) to (2,0)
2. Place signal at (3,0) facing WEST, monitoring another block
3. Select Extend Block Tool
4. Drag from (2,0) to (4,0)

**Assertions:**
- Confirmation dialog appears: "Signal will need reconfiguration"
- If confirmed: signal's `monitored_block_id` is cleared (set to null)
- Signal shows orange warning triangle
- Block now includes tiles (0,0) to (4,0)

---

#### BLK-007: Block Cannot Contain Switch

| Field | Value |
|-------|-------|
| **Test ID** | BLK-007 |
| **Scenario** | Attempt to create block that includes a switch tile |
| **Expected Behavior** | Block creation stops at switch boundary |
| **Automatable** | Yes |

**Steps:**
1. Create track: (0,0)→(1,0)→(2,0) with switch at (1,0)
2. Select Block Tool
3. Drag from (0,0) to (2,0)

**Assertions:**
- Block only contains (0,0) OR error message shown
- Switches cannot be inside blocks (they are boundaries)
- Error: "Switches cannot be inside blocks"

---

#### BLK-008: Signal Auto-Connects to Block Ahead

| Field | Value |
|-------|-------|
| **Test ID** | BLK-008 |
| **Scenario** | Place signal with `auto_connect=True` |
| **Expected Behavior** | Signal automatically finds and connects to nearest block |
| **Automatable** | Yes |

**Steps:**
1. Create track from (0,0) to (5,0)
2. Create block "BLK_A" at tiles (3,0) to (5,0)
3. Place signal at (2,0) facing WEST (trains travel east)

**Assertions:**
- Signal `monitored_block_id == "BLK_A"`
- Signal found block by tracing path in travel direction

**API:**
```python
signal = network.add_signal(2, 0, "WEST", auto_connect=True)
assert signal.monitored_block_id == "BLK_A"
```

---

#### BLK-009: Signal With No Block Ahead

| Field | Value |
|-------|-------|
| **Test ID** | BLK-009 |
| **Scenario** | Place signal where no block exists ahead |
| **Expected Behavior** | Signal shows orange warning triangle |
| **Automatable** | Yes |

**Steps:**
1. Create track from (0,0) to (5,0) with NO blocks
2. Place signal at (2,0) facing WEST

**Assertions:**
- Signal `monitored_block_id == null`
- Orange warning triangle displayed on signal
- Signal state defaults to RED (fail-safe)

---

#### BLK-010: Right-Click Cycles Signal Direction

| Field | Value |
|-------|-------|
| **Test ID** | BLK-010 |
| **Scenario** | Right-click with signal tool to cycle direction |
| **Expected Behavior** | Preview cycles through valid directions for track |
| **Automatable** | Partial (UI) |

**Steps:**
1. Create NS track at (5,5)
2. Select Signal Tool
3. Hover over (5,5)
4. Right-click repeatedly

**Assertions:**
- Preview cycles: NORTH → SOUTH → NORTH → ...
- Only valid directions for track type are shown

---

### 1.3 Station Tool

#### STA-001: Place Station on Simple Track

| Field | Value |
|-------|-------|
| **Test ID** | STA-001 |
| **Scenario** | Place station on straight track |
| **Expected Behavior** | Station created with auto-generated block |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5)
2. Select Station Tool
3. Click on (5,5)
4. Fill in station dialog (name, length, etc.)

**Assertions:**
- Station created with unique ID (e.g., "STA_ABC123")
- Auto-generated block: "{station_id}_BLOCK"
- Track tile has `station_id` set
- Track tile has `block_section_id == "{station_id}_BLOCK"`

---

#### STA-002: Station on Switch (Rejected)

| Field | Value |
|-------|-------|
| **Test ID** | STA-002 |
| **Scenario** | Attempt to place station on switch tile |
| **Expected Behavior** | Placement rejected |
| **Automatable** | Yes |

**Steps:**
1. Create switch at (5,5)
2. Select Station Tool
3. Click on (5,5)

**Assertions:**
- Error: "Cannot place station on switch"
- No station created

---

#### STA-003: Extend Existing Station

| Field | Value |
|-------|-------|
| **Test ID** | STA-003 |
| **Scenario** | Extend station by dragging from existing station tile |
| **Expected Behavior** | New tiles added to same station |
| **Automatable** | Yes |

**Steps:**
1. Create station "STA_001" at (5,5)
2. Create adjacent track at (5,6)
3. Select Extend Station Tool
4. Drag from (5,5) to (5,6)

**Assertions:**
- Station now includes both (5,5) and (5,6)
- Both tiles have `station_id == "STA_001"`
- Station block includes both tiles

---

#### STA-004: Station on Signal Tile (Rejected)

| Field | Value |
|-------|-------|
| **Test ID** | STA-004 |
| **Scenario** | Attempt to place station on tile with signal |
| **Expected Behavior** | Placement rejected |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5) with signal
2. Select Station Tool
3. Click on (5,5)

**Assertions:**
- Error: "Cannot place station on tile with signal"
- No station created

---

#### STA-005: Station Splits Existing Block

| Field | Value |
|-------|-------|
| **Test ID** | STA-005 |
| **Scenario** | Create station on tile that belongs to an existing block |
| **Expected Behavior** | Tile is removed from original block (may split block), auto-block created for station |
| **Automatable** | Yes |

**Steps:**
1. Create track from (0,0) to (4,0)
2. Create block "BLK_MAIN" containing all 5 tiles
3. Create station at (2,0)

**Assertions:**
- Station gets auto-created block "STA_{id}_BLOCK"
- Original block "BLK_MAIN" no longer contains (2,0)
- If block connectivity is broken, block is split into separate blocks
- Blocks cannot cross station boundaries (fully inside or fully outside)

---

### 1.4 Eraser Tool

#### ERA-001: First Click Removes Object (Signal)

| Field | Value |
|-------|-------|
| **Test ID** | ERA-001 |
| **Scenario** | Click eraser on tile with signal |
| **Expected Behavior** | Signal removed, underlying track preserved |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5) with signal
2. Select Eraser Tool
3. Click on (5,5)

**Assertions:**
- Signal removed from tile
- Track remains (`track_bits = 1`)
- Confirmation dialog may appear

---

#### ERA-002: Second Click Removes Track

| Field | Value |
|-------|-------|
| **Test ID** | ERA-002 |
| **Scenario** | Click eraser on plain track (no objects) |
| **Expected Behavior** | Track deleted entirely |
| **Automatable** | Yes |

**Steps:**
1. Create NS track at (5,5) (no signal/station)
2. Select Eraser Tool
3. Click on (5,5)

**Assertions:**
- Track removed from network
- Tile is now empty (`track_bits = 0` or tile removed)

---

#### ERA-003: Bulk Erase (Drag)

| Field | Value |
|-------|-------|
| **Test ID** | ERA-003 |
| **Scenario** | Drag eraser along track path |
| **Expected Behavior** | All tiles in path erased |
| **Automatable** | Yes |

**Steps:**
1. Create track from (0,0) to (5,0)
2. Select Eraser Tool
3. Drag from (0,0) to (5,0)

**Assertions:**
- All 6 tiles erased
- Uses `ExistingTrackPathfinder` to follow path

---

## 2. Simulation Behaviors

### 2.1 Block State Management

#### BST-001: Right-Click Cycles Block State

| Field | Value |
|-------|-------|
| **Test ID** | BST-001 |
| **Scenario** | Right-click on track with assigned block |
| **Expected Behavior** | Block state cycles: FREE → RESERVED → OCCUPIED → CAUTION → FREE |
| **Automatable** | Yes |

**Steps:**
1. Create block "BLK_A" with some tiles
2. Right-click on one of the block tiles

**Assertions:**
- State cycles through all 4 values
- Track color changes accordingly:
  - FREE: Blue (#4285f4)
  - RESERVED: Green (#04d361)
  - OCCUPIED: Red (#e83f5b)
  - CAUTION: Yellow (#ffcd1e)

---

#### BST-002: Block State Affects Signal Color

| Field | Value |
|-------|-------|
| **Test ID** | BST-002 |
| **Scenario** | Change block state and verify signal updates |
| **Expected Behavior** | Signal color matches block state per signal matrix |
| **Automatable** | Yes |

**Steps:**
1. Create block "BLK_A"
2. Place signal monitoring "BLK_A"
3. Set block to different states

**Assertions:**
- Block FREE → Signal GREEN
- Block OCCUPIED → Signal RED
- Block RESERVED → Signal RED
- Block CAUTION → Signal YELLOW

---

### 2.2 Signal State Calculation

#### SIG-001: Block A = FREE, No Block B

| Field | Value |
|-------|-------|
| **Test ID** | SIG-001 |
| **Scenario** | Signal monitors free block, no lookahead block exists |
| **Expected Behavior** | Signal shows GREEN |
| **Automatable** | Yes |

---

#### SIG-002: Block A = FREE, Block B = FREE

| Field | Value |
|-------|-------|
| **Test ID** | SIG-002 |
| **Scenario** | Both monitored block and lookahead block are free |
| **Expected Behavior** | Signal shows GREEN |
| **Automatable** | Yes |

---

#### SIG-003: Block A = FREE, Block B = OCCUPIED

| Field | Value |
|-------|-------|
| **Test ID** | SIG-003 |
| **Scenario** | Monitored block free, but lookahead block occupied |
| **Expected Behavior** | Signal shows YELLOW |
| **Automatable** | Yes |

---

#### SIG-004: Block A = OCCUPIED

| Field | Value |
|-------|-------|
| **Test ID** | SIG-004 |
| **Scenario** | Monitored block is occupied by a train |
| **Expected Behavior** | Signal shows RED |
| **Automatable** | Yes |

---

#### SIG-005: Block A = RESERVED

| Field | Value |
|-------|-------|
| **Test ID** | SIG-005 |
| **Scenario** | Monitored block is reserved by another train |
| **Expected Behavior** | Signal shows RED |
| **Automatable** | Yes |

---

#### SIG-006: Block A = CAUTION

| Field | Value |
|-------|-------|
| **Test ID** | SIG-006 |
| **Scenario** | Monitored block in caution state (train just left) |
| **Expected Behavior** | Signal shows YELLOW |
| **Automatable** | Yes |

---

#### SIG-007: No Monitored Block Assigned

| Field | Value |
|-------|-------|
| **Test ID** | SIG-007 |
| **Scenario** | Signal has no `monitored_block_id` |
| **Expected Behavior** | Signal shows RED (fail-safe) |
| **Automatable** | Yes |

---

#### SIG-008: Switch Blocks Path to Block A

| Field | Value |
|-------|-------|
| **Test ID** | SIG-008 |
| **Scenario** | Switch between signal and monitored block is set against path |
| **Expected Behavior** | Signal shows RED (route blocked) |
| **Automatable** | Yes |

---

### 2.3 Switch Operations

#### SWT-001: Right-Click Toggles Switch

| Field | Value |
|-------|-------|
| **Test ID** | SWT-001 |
| **Scenario** | Right-click on switch tile |
| **Expected Behavior** | Switch toggles between MAIN (0) and DIVERGE (1) |
| **Automatable** | Yes |

**Steps:**
1. Create switch at (5,5)
2. Right-click on (5,5)

**Assertions:**
- `switch_state` alternates: 0 → 1 → 0
- Active path rendering changes

---

#### SWT-002: Switch Affects Train Routing

| Field | Value |
|-------|-------|
| **Test ID** | SWT-002 |
| **Scenario** | Train pathfinding respects switch position |
| **Expected Behavior** | Train follows active path determined by switch state |
| **Automatable** | Partial |

---

#### SWT-003: Switch Affects Signal Path Trace

| Field | Value |
|-------|-------|
| **Test ID** | SWT-003 |
| **Scenario** | Signal path tracing follows current switch position |
| **Expected Behavior** | Signal state recalculates when switch changes |
| **Automatable** | Yes |

---

## 3. Train Movement

#### TRN-001: Train Respects RED Signal

| Field | Value |
|-------|-------|
| **Test ID** | TRN-001 |
| **Scenario** | Train approaches signal showing RED |
| **Expected Behavior** | Train stops before signal tile |
| **Automatable** | Partial |

---

#### TRN-002: Train Proceeds on GREEN Signal

| Field | Value |
|-------|-------|
| **Test ID** | TRN-002 |
| **Scenario** | Train approaches signal showing GREEN |
| **Expected Behavior** | Train enters the block |
| **Automatable** | Partial |

---

#### TRN-003: Train Reserves Block Ahead

| Field | Value |
|-------|-------|
| **Test ID** | TRN-003 |
| **Scenario** | Train approaches a block |
| **Expected Behavior** | Block state becomes RESERVED for train |
| **Automatable** | Yes |

---

#### TRN-004: Train Occupies Block

| Field | Value |
|-------|-------|
| **Test ID** | TRN-004 |
| **Scenario** | Train enters a block |
| **Expected Behavior** | Block state becomes OCCUPIED |
| **Automatable** | Yes |

---

#### TRN-005: Train Exits Block

| Field | Value |
|-------|-------|
| **Test ID** | TRN-005 |
| **Scenario** | Train leaves a block |
| **Expected Behavior** | Block state transitions: OCCUPIED → CAUTION → FREE |
| **Automatable** | Yes |

---

## 4. Route Dispatching

#### DSP-001: Set Route Reserves Blocks

| Field | Value |
|-------|-------|
| **Test ID** | DSP-001 |
| **Scenario** | Dispatcher sets route for train |
| **Expected Behavior** | All blocks in route become RESERVED |
| **Automatable** | Yes |

**API:**
```python
success, error = dispatcher.set_route(
    train_id="TRAIN_001",
    signal_ids=["SIG_1", "SIG_2"],
    block_ids=["BLK_A", "BLK_B"],
    switch_positions={(3, 0): 0}
)
assert success == True
assert network.blocks["BLK_A"].state == BlockState.RESERVED
```

---

#### DSP-002: Set Route Locks Switches

| Field | Value |
|-------|-------|
| **Test ID** | DSP-002 |
| **Scenario** | Route includes switch positions |
| **Expected Behavior** | Switches set and locked to specified positions |
| **Automatable** | Yes |

---

#### DSP-003: Set Route Assigns Signals

| Field | Value |
|-------|-------|
| **Test ID** | DSP-003 |
| **Scenario** | Route includes signals |
| **Expected Behavior** | Signal `approaching_train_id` set to train ID |
| **Automatable** | Yes |

---

#### DSP-004: Cancel Route Releases Resources

| Field | Value |
|-------|-------|
| **Test ID** | DSP-004 |
| **Scenario** | Dispatcher cancels active route |
| **Expected Behavior** | All blocks, switches, signals released |
| **Automatable** | Yes |

---

#### DSP-005: Train Advance Updates Blocks

| Field | Value |
|-------|-------|
| **Test ID** | DSP-005 |
| **Scenario** | Train progresses through route |
| **Expected Behavior** | Staged block release: OCCUPIED → CAUTION → FREE |
| **Automatable** | Yes |

**State Transitions:**
```
Train in A:  [A: OCCUPIED] [B: RESERVED] [C: RESERVED]
Train in B:  [A: CAUTION]  [B: OCCUPIED] [C: RESERVED]
Train in C:  [A: FREE]     [B: CAUTION]  [C: OCCUPIED]
```

---

## 5. Integration Scenarios

#### INT-001: Complete Track Building to Train Operation

| Field | Value |
|-------|-------|
| **Test ID** | INT-001 |
| **Scenario** | Build layout, add signals/blocks, run train |
| **Expected Behavior** | End-to-end workflow functions correctly |
| **Automatable** | No (integration) |

**Steps:**
1. Build oval track layout
2. Add blocks along the route
3. Place signals at block boundaries
4. Verify signals auto-connect to blocks
5. Add train to track
6. Observe train movement respecting signals

---

#### INT-002: Block Extension Across Multiple Signals

| Field | Value |
|-------|-------|
| **Test ID** | INT-002 |
| **Scenario** | Extend block that crosses multiple existing signals |
| **Expected Behavior** | Each signal gets warning and needs reconfiguration |
| **Automatable** | Partial |

---

## 6. API Testing

This section covers comprehensive API testing for methods that accept tile lists (positions arrays). Tests are organized into two tiers:

### Test Tiers

| Tier | Purpose | When to Run |
|------|---------|-------------|
| **Core** | Common operations that must always work | Every build, CI/CD |
| **Extensive** | Edge cases, error handling, rare scenarios | Before releases, after major changes |

### Test Files

| File | Language | Purpose |
|------|----------|---------|
| `tests/test_backend.py` | Python | Direct class method testing via `RailwayNetwork` |
| `tests/test_api.js` | JavaScript | REST API endpoint testing via fetch |

### Orientation Groups (Key Concept)

Understanding orientation groups is critical for zigzag path testing:

```python
BIT_TO_GROUP = {
    0: 'NS',     # TRACK_NS (bit 0) - vertical
    1: 'EW',     # TRACK_EW (bit 1) - horizontal
    2: 'NE_SW',  # TRACK_NE (bit 2) - diagonal group 1
    3: 'NW_SE',  # TRACK_NW (bit 3) - diagonal group 2
    4: 'NW_SE',  # TRACK_SE (bit 4) - diagonal group 2 (same as NW!)
    5: 'NE_SW',  # TRACK_SW (bit 5) - diagonal group 1 (same as NE!)
}
```

**Why this matters:** A zigzag pattern like `NW → SE → NW → SE` is considered a **straight line** because all tiles share the same orientation group (`NW_SE`). Stations can be extended along these zigzag diagonals.

### 6.1 Block APIs with Tile Lists

#### Core Tests

##### BLK-API-001: Create Block with Straight Line Positions

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-001 |
| **Tier** | Core |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Create block with continuous EW track positions |
| **Expected** | Block created with all positions assigned |

**Python Test:**
```python
def test_create_block_straight_line():
    network = RailwayNetwork()
    # Create EW tracks
    for x in range(5):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    block = network.create_block_with_tracks(
        block_id="BLK_STRAIGHT",
        positions=[(0,0), (1,0), (2,0), (3,0), (4,0)],
        name="Straight Block"
    )
    
    assert block.block_id == "BLK_STRAIGHT"
    for x in range(5):
        track = network.get_track(x, 0)
        assert track.block_section_id == "BLK_STRAIGHT"
```

**REST API Test:**
```javascript
async function test_create_block_straight_line() {
    // Setup: create tracks first
    await api.addTracksBulk([
        {x: 0, y: 0, track_bits: 2},  // TRACK_EW
        {x: 1, y: 0, track_bits: 2},
        {x: 2, y: 0, track_bits: 2},
        {x: 3, y: 0, track_bits: 2},
        {x: 4, y: 0, track_bits: 2},
    ]);
    
    const result = await fetch('/api/blocks/create-with-tracks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            block_id: "BLK_STRAIGHT",
            positions: [[0,0], [1,0], [2,0], [3,0], [4,0]],
            name: "Straight Block"
        })
    });
    
    assert(result.ok);
    const block = await result.json();
    assert(block.block_id === "BLK_STRAIGHT");
}
```

---

##### BLK-API-002: Create Block with Zigzag NS Path

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-002 |
| **Tier** | Core |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Create block following zigzag path: (0,0)→(1,0)→(1,1)→(2,1) |
| **Expected** | Block follows connected path, all positions assigned |

**Python Test:**
```python
def test_create_block_zigzag_path():
    network = RailwayNetwork()
    # Create zigzag: EW at (0,0), EW+NS switch at (1,0), NS at (1,1), EW at (2,1)
    # Note: This uses actual connected path, not a switch
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
    network.add_track(TrackSegment(1, 0, track_bits=TRACK_NE))  # Curve connecting E to N
    network.add_track(TrackSegment(1, 1, track_bits=TRACK_SW))  # Curve connecting S to W
    network.add_track(TrackSegment(2, 1, track_bits=TRACK_EW))
    
    # This should work - path is continuous via curves
    block = network.create_block_with_tracks(
        block_id="BLK_ZIGZAG",
        positions=[(0,0), (1,0), (1,1), (2,1)],
        name="Zigzag Block"
    )
    
    assert block is not None
    assert len(network.get_block_positions("BLK_ZIGZAG")) == 4
```

---

##### BLK-API-003: Extend Block with Adjacent Positions

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-003 |
| **Tier** | Core |
| **API** | `extend_block(block_id, new_positions=[...])` |
| **Scenario** | Extend existing block with adjacent positions |
| **Expected** | Block extended, new positions added |

**Python Test:**
```python
def test_extend_block_adjacent():
    network = RailwayNetwork()
    # Create initial tracks
    for x in range(6):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    # Create block with first 3 tiles
    network.create_block_with_tracks(
        block_id="BLK_EXTEND",
        positions=[(0,0), (1,0), (2,0)]
    )
    
    # Extend with next 3 tiles
    block = network.extend_block("BLK_EXTEND", [(3,0), (4,0), (5,0)])
    
    assert len(network.get_block_positions("BLK_EXTEND")) == 6
```

---

#### Extensive Tests (Error Handling)

##### BLK-API-E01: Reject Non-Existent Track Positions

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E01 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Position list includes coordinates with no track |
| **Expected** | ValueError: "Track does not exist" |

**Python Test:**
```python
def test_reject_nonexistent_tracks():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
    # (1, 0) has no track
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (1,0)]  # (1,0) doesn't exist
        )
    assert "does not exist" in str(exc.value).lower()
```

---

##### BLK-API-E02: Reject Switch in Block

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E02 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Position list includes a switch tile |
| **Expected** | ValueError: "Cannot add switch to block" |

**Python Test:**
```python
def test_reject_switch_in_block():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
    network.add_track(TrackSegment(1, 0, track_bits=TRACK_EW | TRACK_NE))  # Switch
    network.add_track(TrackSegment(2, 0, track_bits=TRACK_EW))
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (1,0), (2,0)]  # (1,0) is switch
        )
    assert "switch" in str(exc.value).lower()
```

---

##### BLK-API-E03: Reject Crossing in Block

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E03 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Position list includes a diamond crossing (track_bits=3, 24, or 36) |
| **Expected** | ValueError: "Cannot add crossing to block" |

**Python Test:**
```python
def test_reject_crossing_in_block():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
    network.add_track(TrackSegment(1, 0, track_bits=3))  # NS+EW = crossing
    network.add_track(TrackSegment(2, 0, track_bits=TRACK_EW))
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (1,0), (2,0)]
        )
    assert "crossing" in str(exc.value).lower()
```

---

##### BLK-API-E04: Reject Discontinuous Positions

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E04 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Positions exist but don't form continuous path |
| **Expected** | ValueError: "not continuous" |

**Python Test:**
```python
def test_reject_discontinuous_positions():
    network = RailwayNetwork()
    for x in range(5):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (4,0)]  # Gap between!
        )
    assert "not continuous" in str(exc.value).lower()
```

---

##### BLK-API-E05: Reject Track Already in Another Block

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E05 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Position already belongs to another block |
| **Expected** | ValueError: "already in block" |

**Python Test:**
```python
def test_reject_track_in_other_block():
    network = RailwayNetwork()
    for x in range(4):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    network.create_block_with_tracks(
        block_id="BLK_FIRST",
        positions=[(0,0), (1,0)]
    )
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_SECOND",
            positions=[(1,0), (2,0)]  # (1,0) already in BLK_FIRST
        )
    assert "already in block" in str(exc.value).lower()
```

---

##### BLK-API-E06: Reject Track with Signal

| Field | Value |
|-------|-------|
| **Test ID** | BLK-API-E06 |
| **Tier** | Extensive |
| **API** | `create_block_with_tracks(positions=[...])` |
| **Scenario** | Position has a signal on it |
| **Expected** | ValueError: "signal" in error message |

**Python Test:**
```python
def test_reject_track_with_signal():
    network = RailwayNetwork()
    for x in range(3):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    network.add_signal(1, 0, "EAST", signal_type="block")
    
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (1,0), (2,0)]  # (1,0) has signal
        )
    assert "signal" in str(exc.value).lower()
```

---

### 6.2 Station Extension APIs

#### Core Tests

##### STA-API-001: Extend Station Along Same Orientation (NS)

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-001 |
| **Tier** | Core |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Extend NS station with adjacent NS track |
| **Expected** | Station extended, both tiles in station |

**Python Test:**
```python
def test_extend_station_same_orientation_ns():
    network = RailwayNetwork()
    for y in range(3):
        network.add_track(TrackSegment(0, y, track_bits=TRACK_NS))
    
    station = network.add_station(x=0, y=1, name="Test Station")
    network.extend_station(station.station_id, [(0, 2)])
    
    assert (0, 2) in station.positions
    assert len(station.positions) == 2
```

---

##### STA-API-002: Extend Station Along Zigzag Diagonal (NW_SE Group)

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-002 |
| **Tier** | Core |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Extend station on NW track to adjacent SE track (same orientation group) |
| **Expected** | Station extended - NW and SE are both in `NW_SE` group |

**Python Test:**
```python
def test_extend_station_zigzag_diagonal():
    network = RailwayNetwork()
    # Create zigzag diagonal pattern: NW → SE → NW
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NW))  # NW_SE group
    network.add_track(TrackSegment(1, 1, track_bits=TRACK_SE))  # NW_SE group (same!)
    network.add_track(TrackSegment(2, 2, track_bits=TRACK_NW))  # NW_SE group
    
    # Create station on first tile
    station = network.add_station(x=0, y=0, name="Diagonal Station")
    assert station.initial_orientation == "NW_SE"
    
    # Extend to SE tile - should work (same group)
    network.extend_station(station.station_id, [(1, 1)])
    assert (1, 1) in station.positions
    
    # Extend further - should work
    network.extend_station(station.station_id, [(2, 2)])
    assert len(station.positions) == 3
```

---

##### STA-API-003: Extend Station Along Zigzag Diagonal (NE_SW Group)

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-003 |
| **Tier** | Core |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Extend station on NE track to adjacent SW track |
| **Expected** | Station extended - NE and SW are both in `NE_SW` group |

**Python Test:**
```python
def test_extend_station_zigzag_ne_sw():
    network = RailwayNetwork()
    # Create zigzag: NE → SW → NE pattern
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NE))  # NE_SW group
    network.add_track(TrackSegment(1, 1, track_bits=TRACK_SW))  # NE_SW group (same!)
    
    station = network.add_station(x=0, y=0, name="NE-SW Station")
    assert station.initial_orientation == "NE_SW"
    
    network.extend_station(station.station_id, [(1, 1)])
    assert (1, 1) in station.positions
```

---

#### Extensive Tests (Error Handling)

##### STA-API-E01: Reject Extension to Different Orientation (NS → EW)

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E01 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend NS station with EW track |
| **Expected** | ValueError: "orientation match" |

**Python Test:**
```python
def test_reject_extension_wrong_orientation():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(0, 1, track_bits=TRACK_EW))  # Different orientation!
    
    station = network.add_station(x=0, y=0, name="NS Station")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station.station_id, [(0, 1)])
    assert "orientation" in str(exc.value).lower()
```

---

##### STA-API-E02: Reject Extension NW → NE (Different Diagonal Groups)

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E02 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend NW station (NW_SE group) with NE track (NE_SW group) |
| **Expected** | ValueError: "orientation match" |

**Python Test:**
```python
def test_reject_extension_different_diagonal_groups():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NW))  # NW_SE group
    network.add_track(TrackSegment(1, 0, track_bits=TRACK_NE))  # NE_SW group - different!
    
    station = network.add_station(x=0, y=0, name="NW Station")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station.station_id, [(1, 0)])
    assert "orientation" in str(exc.value).lower()
```

---

##### STA-API-E03: Reject Extension to Switch Tile

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E03 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend station to a switch |
| **Expected** | ValueError: "switch" |

**Python Test:**
```python
def test_reject_extension_to_switch():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(0, 1, track_bits=TRACK_NS | TRACK_NE))  # Switch
    
    station = network.add_station(x=0, y=0, name="Test")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station.station_id, [(0, 1)])
    assert "switch" in str(exc.value).lower()
```

---

##### STA-API-E04: Reject Extension to Tile with Signal

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E04 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend station to tile that has a signal |
| **Expected** | ValueError: "signal" |

**Python Test:**
```python
def test_reject_extension_to_signal_tile():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(0, 1, track_bits=TRACK_NS))
    
    station = network.add_station(x=0, y=0, name="Test")
    network.add_signal(0, 1, "NORTH", signal_type="block")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station.station_id, [(0, 1)])
    assert "signal" in str(exc.value).lower()
```

---

##### STA-API-E05: Reject Non-Adjacent Extension

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E05 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend station with non-adjacent tile |
| **Expected** | ValueError: "adjacent" |

**Python Test:**
```python
def test_reject_nonadjacent_extension():
    network = RailwayNetwork()
    for y in range(5):
        network.add_track(TrackSegment(0, y, track_bits=TRACK_NS))
    
    station = network.add_station(x=0, y=0, name="Test")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station.station_id, [(0, 3)])  # Gap!
    assert "adjacent" in str(exc.value).lower()
```

---

##### STA-API-E06: Reject Extension to Other Station's Tile

| Field | Value |
|-------|-------|
| **Test ID** | STA-API-E06 |
| **Tier** | Extensive |
| **API** | `extend_station(station_id, new_positions=[...])` |
| **Scenario** | Try to extend station to tile belonging to another station |
| **Expected** | ValueError: "already belongs to station" |

**Python Test:**
```python
def test_reject_extension_to_other_station():
    network = RailwayNetwork()
    for y in range(3):
        network.add_track(TrackSegment(0, y, track_bits=TRACK_NS))
    
    station1 = network.add_station(x=0, y=0, name="Station 1")
    station2 = network.add_station(x=0, y=2, name="Station 2")
    
    with pytest.raises(ValueError) as exc:
        network.extend_station(station1.station_id, [(0, 2)])
    assert "already belongs to station" in str(exc.value).lower()
```

---

### 6.3 Bulk Track Operations

#### Core Tests

##### BULK-001: Add Tracks Bulk - Straight Line

| Field | Value |
|-------|-------|
| **Test ID** | BULK-001 |
| **Tier** | Core |
| **API** | `POST /api/tracks/bulk` |
| **Scenario** | Add multiple EW tracks in a row |
| **Expected** | All tracks created successfully |

**REST API Test:**
```javascript
async function test_add_tracks_bulk_straight() {
    const tracks = [];
    for (let x = 0; x < 5; x++) {
        tracks.push({x, y: 0, track_bits: 2});  // TRACK_EW
    }
    
    const result = await fetch('/api/tracks/bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tracks})
    });
    
    assert(result.ok);
    const data = await result.json();
    assert(data.created.length === 5);
}
```

---

##### BULK-002: Add Tracks Bulk - Zigzag Pattern

| Field | Value |
|-------|-------|
| **Test ID** | BULK-002 |
| **Tier** | Core |
| **API** | `POST /api/tracks/bulk` |
| **Scenario** | Add tracks forming a zigzag curve |
| **Expected** | All tracks created with correct bits |

**REST API Test:**
```javascript
async function test_add_tracks_bulk_zigzag() {
    const tracks = [
        {x: 0, y: 0, track_bits: 4},   // TRACK_NE
        {x: 1, y: 1, track_bits: 32},  // TRACK_SW
        {x: 1, y: 2, track_bits: 4},   // TRACK_NE
        {x: 2, y: 3, track_bits: 32},  // TRACK_SW
    ];
    
    const result = await fetch('/api/tracks/bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tracks})
    });
    
    assert(result.ok);
}
```

---

##### BULK-003: Delete Tracks Bulk

| Field | Value |
|-------|-------|
| **Test ID** | BULK-003 |
| **Tier** | Core |
| **API** | `DELETE /api/tracks/bulk` |
| **Scenario** | Delete multiple tracks by positions |
| **Expected** | All specified tracks removed |

**REST API Test:**
```javascript
async function test_delete_tracks_bulk() {
    // Setup: create tracks first
    await api.addTracksBulk([
        {x: 0, y: 0, track_bits: 2},
        {x: 1, y: 0, track_bits: 2},
        {x: 2, y: 0, track_bits: 2},
    ]);
    
    const result = await fetch('/api/tracks/bulk', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({positions: [[0,0], [1,0], [2,0]]})
    });
    
    assert(result.ok);
    const data = await result.json();
    assert(data.deleted === 3);
}
```

---

#### Extensive Tests (Error Handling)

##### BULK-E01: Reject Forbidden Track Configuration

| Field | Value |
|-------|-------|
| **Test ID** | BULK-E01 |
| **Tier** | Extensive |
| **API** | `POST /api/tracks/bulk` |
| **Scenario** | Attempt to create track with forbidden bits (e.g., 13, 22, 42) |
| **Expected** | Track skipped or error returned |

**REST API Test:**
```javascript
async function test_reject_forbidden_track_bits() {
    // 13 = NS(1) + NE(4) + NW(8) = 3-way switch (forbidden)
    const result = await fetch('/api/tracks/bulk', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tracks: [{x: 0, y: 0, track_bits: 13}]})
    });
    
    // Should either skip or error
    const data = await result.json();
    assert(data.created.length === 0 || !result.ok);
}
```

---

##### BULK-E02: Merge Creates Forbidden Configuration

| Field | Value |
|-------|-------|
| **Test ID** | BULK-E02 |
| **Tier** | Extensive |
| **API** | `POST /api/tracks/bulk` |
| **Scenario** | Adding track bits to existing track would create forbidden config |
| **Expected** | Merge rejected, original track unchanged |

**Python Test:**
```python
def test_merge_rejects_forbidden():
    network = RailwayNetwork()
    # Create NS + NE track (bits = 5)
    network.add_track(TrackSegment(0, 0, track_bits=5))
    
    # Try to add NW (8) which would make 13 (forbidden)
    track = network.get_track(0, 0)
    original_bits = track.track_bits
    
    result = track.can_add_bits(8)  # NW
    assert result == False
    assert track.track_bits == original_bits  # Unchanged
```

---

##### BULK-E03: Protected Station Tiles Not Deleted

| Field | Value |
|-------|-------|
| **Test ID** | BULK-E03 |
| **Tier** | Extensive |
| **API** | `DELETE /api/tracks/bulk` |
| **Scenario** | Bulk delete includes tiles with stations |
| **Expected** | Station tiles protected, others deleted |

**REST API Test:**
```javascript
async function test_protected_station_tiles() {
    // Setup: create tracks and station
    await api.addTracksBulk([
        {x: 0, y: 0, track_bits: 2},
        {x: 1, y: 0, track_bits: 2},
        {x: 2, y: 0, track_bits: 2},
    ]);
    await api.createStation(1, 0, "Test Station", 50, "both", 40);
    
    // Try to delete all
    const result = await fetch('/api/tracks/bulk', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({positions: [[0,0], [1,0], [2,0]]})
    });
    
    // Station tile should be protected
    const track = await api.getTrack(1, 0);
    assert(track !== null);  // Still exists
}
```

---

### 6.4 Frontend-Backend Validation Parity

> **Purpose:** Ensure backend rejects all operations that frontend blocks.
> This prevents crashes when frontend validation is bypassed or degraded.

#### Forbidden Track Configurations

##### PARITY-001: Backend Rejects All FORBIDDEN_TRACK_BITS

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-001 |
| **Tier** | Core |
| **Scenario** | Backend must reject all 27 forbidden track bit combinations |
| **Expected** | All forbidden bits rejected consistently |

**Python Test:**
```python
FORBIDDEN_TRACK_BITS = frozenset({
    13, 22, 42, 49,           # 3-way switches
    15, 23, 43, 51,           # Double slips with 3-way
    29, 30, 45, 46, 53, 54, 57, 58,  # Complex 4-path
    31, 47, 55, 59, 61, 62,   # 5-path
    63,                       # 6-path (full)
    28, 44, 52, 56, 60,       # Multi-diagonal without through
})

def test_backend_rejects_all_forbidden_bits():
    network = RailwayNetwork()
    
    for bits in FORBIDDEN_TRACK_BITS:
        # Direct creation should fail
        track = TrackSegment(0, 0, track_bits=bits)
        assert track.track_bits != bits or bits not in FORBIDDEN_TRACK_BITS
        
        # API should also reject
        result = network.add_track(TrackSegment(0, 0, track_bits=bits))
        # Verify track was not added with forbidden bits
```

---

#### Signal Placement Restrictions

##### PARITY-002: Backend Rejects Signal on Switch

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-002 |
| **Tier** | Core |
| **Scenario** | Frontend uses `FORBIDDEN_SIGNAL_BITS`, backend must match |
| **Expected** | Signal placement rejected on all switch/crossing types |

**Python Test:**
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
    # Complex junctions
    7, 11, 19, 35, 14, 38, 26, 50, 21, 37, 25, 41
}

def test_backend_rejects_signal_on_forbidden_bits():
    network = RailwayNetwork()
    
    for bits in FORBIDDEN_SIGNAL_BITS:
        if bits in FORBIDDEN_TRACK_BITS:
            continue  # Can't create track with these bits
        
        network.add_track(TrackSegment(0, 0, track_bits=bits))
        
        with pytest.raises((ValueError, Exception)):
            network.add_signal(0, 0, "NORTH", signal_type="block")
        
        network.remove_track(0, 0)
```

---

##### PARITY-003: Valid Signal Directions Match Frontend

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-003 |
| **Tier** | Core |
| **Scenario** | Backend only allows directions defined in VALID_SIGNAL_DIRECTIONS |
| **Expected** | Invalid directions rejected with descriptive error |

**Python Test:**
```python
VALID_SIGNAL_DIRECTIONS = {
    1:  ['N', 'S'],      # NS straight
    2:  ['E', 'W'],      # EW straight
    4:  ['N', 'E'],      # NE curve
    8:  ['N', 'W'],      # NW curve
    16: ['S', 'E'],      # SE curve
    32: ['S', 'W'],      # SW curve
}

def test_valid_signal_directions():
    network = RailwayNetwork()
    
    for bits, valid_dirs in VALID_SIGNAL_DIRECTIONS.items():
        network.add_track(TrackSegment(0, 0, track_bits=bits))
        
        # Valid directions should work
        for direction in valid_dirs:
            signal = network.add_signal(0, 0, direction, signal_type="block")
            assert signal is not None
            network.delete_signal(signal.signal_id)
        
        # Invalid directions should fail
        all_dirs = {'N', 'E', 'S', 'W'}
        invalid_dirs = all_dirs - set(valid_dirs)
        for direction in invalid_dirs:
            with pytest.raises(ValueError):
                network.add_signal(0, 0, direction, signal_type="block")
        
        network.remove_track(0, 0)
```

---

#### Block Placement Restrictions

##### PARITY-004: Backend Rejects Crossing in Block

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-004 |
| **Tier** | Core |
| **Scenario** | Frontend `isCrossing()` checks bits 3, 24, 36; backend must match |
| **Expected** | Block creation fails if positions include crossing |

**Python Test:**
```python
CROSSING_BITS = {3, 24, 36}  # NS+EW, NW+SE, NE+SW

def test_backend_rejects_crossing_in_block():
    network = RailwayNetwork()
    
    for bits in CROSSING_BITS:
        network.add_track(TrackSegment(0, 0, track_bits=bits))
        
        with pytest.raises(ValueError) as exc:
            network.create_block_with_tracks(
                block_id="BLK_FAIL",
                positions=[(0, 0)]
            )
        assert "crossing" in str(exc.value).lower()
        
        network.remove_track(0, 0)
```

---

##### PARITY-005: Backend Rejects Station on Block Tile

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-005 |
| **Tier** | Extensive |
| **Scenario** | Blocks respect station boundaries |
| **Expected** | Blocks can be fully inside OR fully outside stations, but cannot cross boundaries |

**Python Test:**
```python
def test_backend_validates_station_block_boundaries():
    network = RailwayNetwork()
    for x in range(5):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    
    # Create station at tiles (1,0) and (2,0)
    station = network.add_station(x=1, y=0, station_id="STA_001", name="Test Station")
    network.extend_station("STA_001", [(2, 0)])
    
    # Test 1: Block fully inside station should PASS
    block_inside = network.create_block_with_tracks(
        block_id="BLK_INSIDE",
        positions=[(1,0), (2,0)]
    )
    assert block_inside is not None
    
    # Test 2: Block fully outside station should PASS
    block_outside = network.create_block_with_tracks(
        block_id="BLK_OUTSIDE",
        positions=[(3,0), (4,0)]
    )
    assert block_outside is not None
    
    # Test 3: Block crossing station boundary should FAIL
    with pytest.raises(ValueError) as exc:
        network.create_block_with_tracks(
            block_id="BLK_FAIL",
            positions=[(0,0), (1,0), (2,0)]  # crosses boundary
        )
    assert "station boundary" in str(exc.value).lower()
```

---

#### Station Placement Restrictions

##### PARITY-006: Backend Rejects Station on Switch

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-006 |
| **Tier** | Core |
| **Scenario** | Frontend checks `is_switch` before station placement |
| **Expected** | Backend rejects station on switch with clear error |

**Python Test:**
```python
def test_backend_rejects_station_on_switch():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW | TRACK_NE))  # Switch
    
    with pytest.raises(ValueError) as exc:
        network.add_station(x=0, y=0, name="Fail Station")
    assert "switch" in str(exc.value).lower()
```

---

##### PARITY-007: Backend Rejects Station on Signal Tile

| Field | Value |
|-------|-------|
| **Test ID** | PARITY-007 |
| **Tier** | Core |
| **Scenario** | Frontend checks `track.signal_ids.length > 0` |
| **Expected** | Backend rejects station on tile with signal |

**Python Test:**
```python
def test_backend_rejects_station_on_signal_tile():
    network = RailwayNetwork()
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
    network.add_signal(0, 0, "EAST", signal_type="block")
    
    with pytest.raises(ValueError) as exc:
        network.add_station(x=0, y=0, name="Fail Station")
    assert "signal" in str(exc.value).lower()
```

---

## Appendix A: Quick Reference Tables

### Signal State Matrix

| Block A | Block B | Switch | Signal |
|---------|---------|--------|--------|
| (none) | - | - | RED |
| - | - | Blocking | RED |
| OCCUPIED | (any) | Aligned | RED |
| RESERVED | (any) | Aligned | RED |
| CAUTION | (any) | Aligned | YELLOW |
| FREE | (none) | Aligned | GREEN |
| FREE | FREE | Aligned | GREEN |
| FREE | CAUTION | Aligned | YELLOW |
| FREE | RESERVED | Aligned | YELLOW |
| FREE | OCCUPIED | Aligned | YELLOW |

### Valid Signal Directions

| track_bits | Segment | Valid Directions |
|------------|---------|------------------|
| 1 | NS | N, S |
| 2 | EW | E, W |
| 4 | NE | N, E |
| 8 | NW | N, W |
| 16 | SE | S, E |
| 32 | SW | S, W |

### Block State Colors

| State | Color | Hex |
|-------|-------|-----|
| FREE | Blue | #4285f4 |
| RESERVED | Green | #04d361 |
| OCCUPIED | Red | #e83f5b |
| CAUTION | Yellow | #ffcd1e |

---

## Appendix B: Test File Structure

### Python Backend Tests (`tests/test_backend.py`)

```python
"""
Backend API Tests - Direct class method testing

Run with: pytest tests/test_backend.py -v
"""
import pytest
from network import RailwayNetwork
from track import (
    TrackSegment, TRACK_NS, TRACK_EW, TRACK_NE, TRACK_NW, 
    TRACK_SE, TRACK_SW, FORBIDDEN_TRACK_BITS
)

# ============= Test Fixtures =============

@pytest.fixture
def network():
    """Fresh network for each test"""
    return RailwayNetwork()

@pytest.fixture
def straight_ew_track(network):
    """5-tile EW straight track"""
    for x in range(5):
        network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
    return network

@pytest.fixture
def zigzag_diagonal_track(network):
    """NW→SE→NW zigzag diagonal (NW_SE group)"""
    network.add_track(TrackSegment(0, 0, track_bits=TRACK_NW))
    network.add_track(TrackSegment(1, 1, track_bits=TRACK_SE))
    network.add_track(TrackSegment(2, 2, track_bits=TRACK_NW))
    return network

# ============= Core Block API Tests =============

class TestBlockAPICore:
    """Core tests for block APIs - must pass every build"""
    
    def test_create_block_straight_line(self, straight_ew_track):
        """BLK-API-001"""
        network = straight_ew_track
        block = network.create_block_with_tracks(
            block_id="BLK_STRAIGHT",
            positions=[(x, 0) for x in range(5)]
        )
        assert block.block_id == "BLK_STRAIGHT"
        for x in range(5):
            assert network.get_track(x, 0).block_section_id == "BLK_STRAIGHT"
    
    def test_extend_block_adjacent(self, network):
        """BLK-API-003"""
        for x in range(6):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        
        network.create_block_with_tracks("BLK_A", [(0,0), (1,0), (2,0)])
        network.extend_block("BLK_A", [(3,0), (4,0), (5,0)])
        
        assert len(network.get_block_positions("BLK_A")) == 6

# ============= Extensive Block API Tests =============

class TestBlockAPIExtensive:
    """Extensive error handling tests"""
    
    def test_reject_nonexistent_tracks(self, network):
        """BLK-API-E01"""
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
        with pytest.raises(ValueError, match="does not exist"):
            network.create_block_with_tracks("BLK_FAIL", [(0,0), (1,0)])
    
    def test_reject_switch_in_block(self, network):
        """BLK-API-E02"""
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW))
        network.add_track(TrackSegment(1, 0, track_bits=TRACK_EW | TRACK_NE))
        with pytest.raises(ValueError, match="switch"):
            network.create_block_with_tracks("BLK_FAIL", [(0,0), (1,0)])
    
    def test_reject_discontinuous(self, network):
        """BLK-API-E04"""
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))
        with pytest.raises(ValueError, match="not continuous"):
            network.create_block_with_tracks("BLK_FAIL", [(0,0), (4,0)])

# ============= Core Station API Tests =============

class TestStationAPICore:
    """Core tests for station extension APIs"""
    
    def test_extend_station_zigzag_diagonal(self, zigzag_diagonal_track):
        """STA-API-002"""
        network = zigzag_diagonal_track
        station = network.add_station(x=0, y=0, name="Diagonal")
        
        assert station.initial_orientation == "NW_SE"
        network.extend_station(station.station_id, [(1, 1)])
        assert (1, 1) in station.positions

# ============= Extensive Station API Tests =============

class TestStationAPIExtensive:
    """Extensive error handling for station APIs"""
    
    def test_reject_wrong_orientation(self, network):
        """STA-API-E01"""
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_NS))
        network.add_track(TrackSegment(0, 1, track_bits=TRACK_EW))
        
        station = network.add_station(x=0, y=0, name="NS")
        with pytest.raises(ValueError, match="orientation"):
            network.extend_station(station.station_id, [(0, 1)])

# ============= Frontend-Backend Parity Tests =============

class TestFrontendBackendParity:
    """Ensure backend rejects what frontend blocks"""
    
    def test_all_forbidden_track_bits(self, network):
        """PARITY-001"""
        for bits in FORBIDDEN_TRACK_BITS:
            track = TrackSegment(0, 0, track_bits=bits)
            # Forbidden bits should be rejected or converted
            assert track.track_bits not in FORBIDDEN_TRACK_BITS
    
    def test_station_on_switch_rejected(self, network):
        """PARITY-006"""
        network.add_track(TrackSegment(0, 0, track_bits=TRACK_EW | TRACK_NE))
        with pytest.raises(ValueError, match="switch"):
            network.add_station(x=0, y=0, name="Fail")
```

---

### JavaScript REST API Tests (`tests/test_api.js`)

```javascript
/**
 * REST API Tests - HTTP endpoint testing
 * 
 * Run with: node tests/test_api.js
 * Requires: Server running at localhost:5000
 */

const BASE_URL = 'http://localhost:5000/api';

// ============= Test Utilities =============

async function fetchJSON(endpoint, options = {}) {
    const response = await fetch(`${BASE_URL}${endpoint}`, {
        headers: {'Content-Type': 'application/json'},
        ...options
    });
    return {ok: response.ok, status: response.status, data: await response.json()};
}

async function resetNetwork() {
    await fetchJSON('/network/reset', {method: 'POST'});
}

function assert(condition, message) {
    if (!condition) throw new Error(`Assertion failed: ${message}`);
}

// ============= Core Block API Tests =============

async function test_BLK_API_001_create_block_straight() {
    await resetNetwork();
    
    // Create tracks
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({tracks: [
            {x: 0, y: 0, track_bits: 2},
            {x: 1, y: 0, track_bits: 2},
            {x: 2, y: 0, track_bits: 2},
        ]})
    });
    
    // Create block
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: "BLK_STRAIGHT",
            positions: [[0,0], [1,0], [2,0]]
        })
    });
    
    assert(result.ok, 'Block creation should succeed');
    assert(result.data.block_id === 'BLK_STRAIGHT', 'Block ID should match');
    console.log('✓ BLK-API-001 passed');
}

// ============= Extensive Block API Tests =============

async function test_BLK_API_E01_reject_nonexistent() {
    await resetNetwork();
    
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({tracks: [{x: 0, y: 0, track_bits: 2}]})
    });
    
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: "BLK_FAIL",
            positions: [[0,0], [1,0]]  // (1,0) doesn't exist
        })
    });
    
    assert(!result.ok, 'Should fail for non-existent track');
    assert(result.data.error.toLowerCase().includes('does not exist'), 
           'Error should mention non-existent track');
    console.log('✓ BLK-API-E01 passed');
}

// ============= Core Station API Tests =============

async function test_STA_API_002_zigzag_diagonal() {
    await resetNetwork();
    
    // Create NW→SE zigzag
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({tracks: [
            {x: 0, y: 0, track_bits: 8},   // TRACK_NW
            {x: 1, y: 1, track_bits: 16},  // TRACK_SE
        ]})
    });
    
    // Create station
    const stationResult = await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x: 0, y: 0, name: "Diagonal", length_cm: 50,
            platform_side: "both", top_speed_kmh: 30
        })
    });
    assert(stationResult.ok, 'Station creation should succeed');
    
    // Extend to SE tile (same orientation group)
    const extendResult = await fetchJSON(`/station/${stationResult.data.station_id}/extend`, {
        method: 'POST',
        body: JSON.stringify({positions: [[1, 1]]})
    });
    assert(extendResult.ok, 'Extension should succeed - same NW_SE group');
    console.log('✓ STA-API-002 passed');
}

// ============= Frontend-Backend Parity Tests =============

async function test_PARITY_002_signal_on_switch() {
    await resetNetwork();
    
    // Create switch
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({tracks: [{x: 0, y: 0, track_bits: 6}]})  // EW+NE switch
    });
    
    // Try to place signal
    const result = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({x: 0, y: 0, direction: 'EAST'})
    });
    
    assert(!result.ok, 'Signal on switch should be rejected');
    console.log('✓ PARITY-002 passed');
}

// ============= Test Runner =============

async function runAllTests() {
    console.log('Starting REST API Tests...\n');
    
    const tests = [
        // Core
        test_BLK_API_001_create_block_straight,
        test_STA_API_002_zigzag_diagonal,
        // Extensive
        test_BLK_API_E01_reject_nonexistent,
        // Parity
        test_PARITY_002_signal_on_switch,
    ];
    
    let passed = 0, failed = 0;
    
    for (const test of tests) {
        try {
            await test();
            passed++;
        } catch (e) {
            console.error(`✗ ${test.name} failed:`, e.message);
            failed++;
        }
    }
    
    console.log(`\n========================================`);
    console.log(`Results: ${passed} passed, ${failed} failed`);
}

runAllTests();
```

---

## Appendix C: Test Fixtures

### Track Layouts for Testing

#### Fixture 1: Straight EW Line (5 tiles)

```
Layout:  ─ ─ ─ ─ ─
Coords:  (0,0) (1,0) (2,0) (3,0) (4,0)
Bits:    2     2     2     2     2
```

```python
def create_straight_ew(network, length=5, y=0):
    for x in range(length):
        network.add_track(TrackSegment(x, y, track_bits=TRACK_EW))
```

---

#### Fixture 2: Zigzag Diagonal NW_SE Group

```
Layout:   ╲
            ╱
              ╲
Coords:  (0,0) → (1,1) → (2,2)
Bits:    8 (NW)  16 (SE)  8 (NW)
Group:   NW_SE   NW_SE    NW_SE  (all same!)
```

```python
def create_zigzag_nw_se(network, length=3):
    for i in range(length):
        bits = TRACK_NW if i % 2 == 0 else TRACK_SE
        network.add_track(TrackSegment(i, i, track_bits=bits))
```

---

#### Fixture 3: Zigzag Diagonal NE_SW Group

```
Layout:   ╱
        ╲
          ╱
Coords:  (0,0) → (1,1) → (2,2)
Bits:    4 (NE)  32 (SW)  4 (NE)
Group:   NE_SW   NE_SW    NE_SW  (all same!)
```

```python
def create_zigzag_ne_sw(network, length=3):
    for i in range(length):
        bits = TRACK_NE if i % 2 == 0 else TRACK_SW
        network.add_track(TrackSegment(i, i, track_bits=bits))
```

---

#### Fixture 4: Y-Switch Configuration

```
Layout:      │
           ──┼──
             │
Coords:    (1,0)
              ↓
           (1,1) ← switch (NS + NE)
              │
           (1,2)
              
Bits at switch: 1 | 4 = 5 (NS + NE)
```

```python
def create_y_switch(network):
    network.add_track(TrackSegment(1, 0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(1, 1, track_bits=TRACK_NS | TRACK_NE))  # Switch
    network.add_track(TrackSegment(1, 2, track_bits=TRACK_NS))
    network.add_track(TrackSegment(2, 0, track_bits=TRACK_SW))  # Diverging route
```

---

#### Fixture 5: Diamond Crossing

```
Layout:    │
         ──┼──
           │
Coords:  (1,1)
Bits:    3 (NS + EW)
```

```python
def create_diamond_crossing(network):
    # Approaching tracks
    network.add_track(TrackSegment(0, 1, track_bits=TRACK_EW))
    network.add_track(TrackSegment(2, 1, track_bits=TRACK_EW))
    network.add_track(TrackSegment(1, 0, track_bits=TRACK_NS))
    network.add_track(TrackSegment(1, 2, track_bits=TRACK_NS))
    # Crossing
    network.add_track(TrackSegment(1, 1, track_bits=3))  # NS + EW
```

---

#### Fixture 6: All Valid Track Bits

Reference from `demo_main.py` - useful for comprehensive testing:

```python
VALID_SIMPLE_BITS = {
    1: 'NS',    # TRACK_NS
    2: 'EW',    # TRACK_EW
    4: 'NE',    # TRACK_NE
    8: 'NW',    # TRACK_NW
    16: 'SE',   # TRACK_SE
    32: 'SW',   # TRACK_SW
}

VALID_CROSSING_BITS = {
    3: 'NS+EW crossing',
    24: 'NW+SE crossing',
    36: 'NE+SW crossing',
}

VALID_SWITCH_BITS = {
    5: 'NS+NE Y-switch',
    6: 'EW+NE Y-switch',
    9: 'NS+NW Y-switch',
    10: 'EW+NW Y-switch',
    17: 'NS+SE Y-switch',
    18: 'EW+SE Y-switch',
    33: 'NS+SW Y-switch',
    34: 'EW+SW Y-switch',
}
```

---

*End of Testing Specification*
