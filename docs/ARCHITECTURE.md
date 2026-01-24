# ATCS6 Architecture Overview

## System Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         WEB FRONTEND                             │
│  (index.html, CSS, JS: app.js, editor.js, renderer.js, api.js)  │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP REST API
┌────────────────────────────▼────────────────────────────────────┐
│                    FLASK SERVER (server.py)                      │
│  - Network Management: /api/network                              │
│  - Track CRUD: /api/track/{x}/{y}                                │
│  - Bulk Operations: /api/tracks/bulk, /api/tracks/bulk-delete    │
│  - Signals: /api/signal/{id}, /api/signal/validate/{x}/{y}       │
│  - Blocks: /api/blocks, /api/blocks/{id}                         │
│  - Stations: /api/station/{id}, /api/station/validate            │
│  - Trains: /api/trains, /api/trains/{id}                         │
│  - Switches: /api/switches, /api/switches/{id}                   │
│  - Paths: /api/paths, /api/dispatcher/paths                      │
│  - Routes: /api/train-routes, /api/schedules                     │
│  - Speed Profiles: /api/speed-profiles                           │
│  - Simulation: /api/simulation/control                           │
└────────────────────────────┬────────────────────────────────────┘
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
┌──────▼──────┐    ┌────────▼────────┐    ┌──────▼──────┐
│RailwayNetwork│    │SimulationController│   │ demo_main  │
│ (network.py) │    │ (simulation.py)    │   │(init data) │
└──────┬───────┘    └────────┬───────────┘   └────────────┘
       │                     │
       │         ┌───────────┼───────────┐
       │         │           │           │
       │    ┌────▼────┐ ┌────▼────┐ ┌────▼────┐
       │    │TrainMover│ │TrainATC │ │BlockPwr │
       │    │(train_   │ │(train_  │ │Controller│
       │    │movement) │ │ atc.py) │ │(block_  │
       │    └──────────┘ └─────────┘ │power.py)│
       │                             └─────────┘
┌──────┴─────────────────────┴──────┐
│                                   │
▼                                   ▼
┌────────────┐              ┌──────────────┐
│TrackSegment│              │    Train     │
│ (track.py) │              │ (train.py)   │
└────┬───────┘              └──────┬───────┘
     │                             │
     ▼                             ▼
┌────────────┐ ┌──────────────┐ ┌──────────────────┐
│   Signal   │ │    Block     │ │ path_dispatcher  │
│(railway_   │ │  (block.py)  │ │ (PathDispatcher) │
│ signal.py) │ └──────┬───────┘ └────────┬─────────┘
└────────────┘        │                  │
                ┌─────▼──────┐    ┌──────▼───────┐
                │BlockManager│    │pathfinder.py │
                │(block_     │    │(A* routing)  │
                │manager.py) │    └──────────────┘
                └────────────┘
```

## Module Descriptions

### Core Modules

| Module | Purpose |
|--------|---------|
| `server.py` | Flask REST API server, handles all HTTP endpoints |
| `network.py` | Central coordinator - manages tracks, trains, blocks, signals, stations |
| `track.py` | TrackSegment class with 6-bit track geometry |
| `train.py` | Train, TrainCar, TrainPosition entities with consist model |
| `station.py` | Station class with multi-tile platforms and orientation |
| `block.py` | Block section entity for signaling and train separation |
| `block_manager.py` | Centralized block state manager (single source of truth for occupancy) |
| `block_validation.py` | Block continuity validation rules |
| `railway_signal.py` | Signal entity with block monitoring |
| `signal_validation.py` | Signal placement validation rules |
| `switch.py` | Switch entity with length/radius properties |

### Simulation & Train Control

| Module | Purpose |
|--------|---------|
| `simulation.py` | SimulationController - tick loop, train updates, power control |
| `train_movement.py` | TrainMover - track geometry, direction math, tile advancement |
| `train_atc.py` | TrainATC - automatic train control, speed limits, clearance |
| `block_power.py` | BlockPowerController - voltage management for blocks |
| `speed_profile.py` | SpeedProfile - acceleration/deceleration curves |
| `train_config.py` | Configuration constants (scale, margins, defaults) |
| `route_executor.py` | RouteExecutor - executes train routes/schedules |
| `train_route.py` | Route, Waypoint - train travel plans |

### Dispatcher & Pathfinding

| Module | Purpose |
|--------|---------|
| `path_dispatcher.py` | **ACTIVE** PathDispatcher - block reservation and switch locking |
| `path.py` | PathSegment, PathStatus - dispatcher reservation management |
| `pathfinder.py` | RailwayPathfinder - A* pathfinding for train movement |

### Persistence & Utilities

| Module | Purpose |
|--------|---------|
| `persistence.py` | PersistenceManager - save/load network state to JSON |
| `enums.py` | Shared enumerations (Direction, SignalState, BlockState, etc.) |
| `path_utils.py` | Utilities for path/tile conversion |

### Legacy (Not Active)

| Module | Purpose |
|--------|---------|
| `dispatcher.py` | Legacy train dispatcher (replaced by path_dispatcher.py) |
| `interlocking.py` | Alternative route locking (not integrated) |

## Frontend Architecture

| File | Purpose |
|------|---------|
| `static/js/app.js` | Application initialization, event binding |
| `static/js/editor.js` | Track editing tools, construction pathfinder |
| `static/js/editor-*.js` | Specialized editors (blocks, signals, stations, trains, routes, switches) |
| `static/js/renderer.js` | Isometric canvas rendering coordinator |
| `static/js/renderer-*.js` | Specialized renderers (tracks, trains, blocks, stations, signals, routes, debug) |
| `static/js/api.js` | Server communication wrapper |
| `static/js/state-manager.js` | Client-side state management |
| `static/js/pathfinders.js` | Construction and existing-track pathfinders |
| `static/js/helpers.js` | UI helper functions |
| `static/js/constants.js` | Rendering constants (colors, sizes) |
| `static/js/train-monitor.js` | Train status monitoring bar |

## Key Concepts

### 6-Bit Track System

Tracks use a 6-bit encoding for geometry:

| Bit | Value | Path |
|-----|-------|------|
| 0 | 1 | North-South |
| 1 | 2 | East-West |
| 2 | 4 | North-East diagonal |
| 3 | 8 | North-West diagonal |
| 4 | 16 | South-East diagonal |
| 5 | 32 | South-West diagonal |

See `Reference_6Bit_Track_System.md` for complete documentation (1,000+ lines of detailed track geometry docs).

### Two Pathfinders

The system has two separate pathfinding implementations:

1. **Construction Pathfinder** (JavaScript, `editor.js`)
   - Plans track-laying paths for the editor
   - Prefers zigzag patterns for realistic railway construction
   - Avoids creating unnecessary switches
   - Prioritizes using existing tracks

2. **Train Pathfinder** (Python, `pathfinder.py`)
   - Routes trains through existing track network
   - Considers signal states, switch positions, track occupancy
   - A* algorithm with railway-specific constraints

### Coordinate System

- X-axis: West (-) to East (+)
- Y-axis: South (-) to North (+)
- Supports negative coordinates (e.g., `/api/track/-5/10`)

### Dead-End Detection

Dead-ends are automatically computed during propagation:
- A track edge is a dead-end if it has a path but no connecting neighbor
- Changes propagate to adjacent tracks when tracks are added/removed

### Switch System

- Switches are automatically detected when a tile has 2+ track segments
- Switch state: 0 = MAIN (long paths), 1 = DIVERGE (short/diagonal paths)
- All switches on a tile share the same state

### Station System

Stations are multi-tile entities representing passenger platforms:

- **Orientation Detection**: Automatically determines track direction (NS, EW, NE_SW, NW_SE)
- **Platform Sides**: Maps `left`/`right` to absolute compass directions:
  - NS tracks: west/east
  - EW tracks: north/south
  - NE_SW tracks: northwest/southeast
  - NW_SE tracks: southwest/northeast
- **Rendering**: Platforms drawn parallel to track using endpoint-finding algorithm
- **Labels**: Abbreviated names (ATC-style codes) with full name on hover

### Entity Architecture

The system uses standalone entity classes for signals, blocks, and stations, stored in network-level dictionaries.

#### Storage Structure

```python
# In RailwayNetwork (network.py)
network.signals: Dict[str, Signal]      # signal_id -> Signal
network.blocks: Dict[str, Block]        # block_id -> Block
network.stations: Dict[str, Station]    # station_id -> Station
network.block_sections: Dict[str, BlockSection]  # Legacy alias for blocks
```

#### Signal Entity (`railway_signal.py`)

Signals are independent entities that reference tracks and blocks:

```python
Signal(
    signal_id: str,              # Unique ID (auto: "SIG_" + uuid)
    x: int, y: int,              # Track position
    direction: Direction,         # Train travel direction (NORTH, EAST, SOUTH, WEST)
    signal_type: str = "block",  # Signal type
    state: SignalState = RED,    # Current aspect (RED, YELLOW, GREEN)
    name: Optional[str] = None,  # Human-readable name
    monitored_block_id: Optional[str] = None  # Connected block
)
```

Key behaviors:
- **Auto-connect**: On creation, signals automatically find and connect to the first reachable block in their direction
- **Validation**: Signals can only be placed on tracks with valid exit paths in the specified direction
- **State updates**: Signal state reflects monitored block status

#### Block Entity (`block.py`)

Blocks (track sections) manage occupancy and signaling:

```python
Block(
    block_id: str,               # Unique ID (auto: "BLK_" + uuid)
    name: str = "",              # Human-readable name
    state: BlockState = FREE,    # FREE, RESERVED, OCCUPIED, CAUTION
    tracks: List[Tuple[int,int]] = []  # List of (x, y) positions
)
```

Block states:
- **FREE**: Block is clear, signals show GREEN
- **RESERVED**: Train approaching, signals show YELLOW
- **OCCUPIED**: Train in block, signals show RED
- **CAUTION**: Train just left, signals show YELLOW

Tracks reference their block via `track.block_section_id`.

#### Train Consist Model (`train.py`)

Trains are composed of individual cars:

```python
TrainCar(
    car_id: str,
    car_type: str,     # "LOCOMOTIVE", "PASSENGER", "FREIGHT"
    length: float,     # Length in centimeters (cm)
    mass: float,       # Mass in kg
    max_speed: float   # Max speed for this car
)

Train(
    train_id: str,
    consist: List[TrainCar] = [],  # List of cars making up the train
    max_speed: float = 100.0,      # Operational max speed
    current_speed: float = 0.0,    # Current speed in km/h
    # ... position, state, schedule, etc.
)
```

Train properties derived from consist:
- `train.length` → Sum of car lengths (converted to cm for occupancy)
- `train.total_mass` → Sum of car masses
- `train.braking_distance` → Calculated from speed and deceleration (in cm)

#### Entity Relationships

```
Signal ──monitors──▶ Block ◀──belongs_to── Track
   │                   │
   │                   ├── tracks: [(x,y), ...]
   │                   └── state: FREE/RESERVED/OCCUPIED/CAUTION
   │
   └── x, y ──on_track──▶ TrackSegment
                              │
                              └── block_section_id: "BLK_..."

Station ──contains──▶ Track positions
   │
   ├── positions: [(x,y), ...]
   ├── orientation: NS/EW/NE_SW/NW_SE
   ├── platform_sides: [west, east] / [north, south] / etc.
   └── block_assignments: {(x,y): block_id, ...}  # Multi-block support
```

### Station-Block Relationship

Stations and blocks interact with the following rules:

1. **Auto-created Station Block**: When a station is created, an auto-created block (`{station_id}_BLOCK`) is created for convenience
2. **Blocks Can Exist Within Stations**: Users can create custom blocks within stations for multi-block platforms
3. **Station Boundary Enforcement**: Blocks cannot cross station boundaries:
   - Blocks fully inside a station can only contain tiles from that station
   - Blocks fully outside stations cannot extend into any station
   - A single block cannot span both inside and outside a station
4. **Platform Shrink/Extend Behavior**:
   - When a station tile is removed, it's automatically removed from any block (with proper splitting/deletion)
   - When a station extends, existing non-station blocks must give up those tiles
5. **Station Deletion**: Deleting a station automatically deletes all blocks that are fully contained within it

This design allows:
- Simple single-block stations (auto-created `{station_id}_BLOCK`)
- Complex multi-block stations (e.g., different platform sections with separate signal control)
- Clear separation between station infrastructure and regular track blocks


## Train Control Architecture (2026)

### Overview
The train control refactor centralized block occupancy in `block_manager.py` to eliminate race conditions from the previous three-way updates (PathDispatcher reservations, `train.update_occupancy`, and manual block updates). This architecture fixed train teleporting, random slowdowns from block thrashing, and trains getting stuck on switches by enforcing a single state machine for blocks.

### Architecture Summary
```
SimulationController (simulation.py)
    ├─ TrainMover (train_movement.py) - path-queue updates
    ├─ TrainATC (train_atc.py) - speed limits
    ├─ BlockPowerController (block_power.py) - voltage
    ├─ RouteExecutor (route_executor.py) - route/schedule
    └─ Legacy movement logic (_advance_*, distance_on_track)

RailwayNetwork (network.py) owns registries + BlockManager
PathDispatcher (path_dispatcher.py) reserves blocks, locks switches
Block (block.py) stores state only
```

### Component Responsibilities
- **RailwayNetwork** initializes `BlockManager` and holds tracks, blocks, trains, and paths.
- **BlockManager** enforces `FREE → RESERVED → OCCUPIED → CAUTION → FREE` and provides atomic `occupy_block`/`clear_block` operations.
- **PathDispatcher** reserves blocks and locks switches for a path. When a train advances along its path, PathDispatcher calls `BlockManager.occupy_block()` to transition blocks to OCCUPIED.
- **SimulationController** runs the tick loop. It currently uses legacy movement logic in `_advance_train_track`/`_advance_through_block` and calls `TrainMover.update_train_path_position()` for the path-queue renderer migration.
- **TrainMover** handles track geometry, direction math, and path-queue (head/tail) position updates for the renderer.
- **Train** stores dual position systems:
  - **Legacy**: `current_pos`, `current_direction`, `distance_on_track` (cm from entry edge)
  - **New**: `TrainPosition` with `path_tiles`, `head_offset`, `tail_offset` for multi-tile rendering

### Block State Management Workflow

**State Machine:**
```
FREE → RESERVED → OCCUPIED → CAUTION → FREE
     ↑______________|
```

States:
- **FREE**: No train reservation or occupation
- **RESERVED**: Reserved for train's planned path (interlocking locked)
- **OCCUPIED**: Physical train presence detected  
- **CAUTION**: Train recently cleared, safety margin maintained

**Workflow Steps:**
1. `PathDispatcher` reserves path blocks, setting them to `RESERVED`.
2. `SimulationController` advances the train via legacy `_update_train_position` until it crosses a tile boundary.
3. When the head enters a new block, `PathDispatcher.on_train_advances()` calls `BlockManager.occupy_block()` to transition it to `OCCUPIED`.
4. When the train clears a block, `SimulationController` calls `BlockManager.clear_block()` to move it to `CAUTION` or `FREE`.
5. `CAUTION` keeps a safety buffer behind the train for signals and following traffic.

**Sequence (tick-level):**
```
PathDispatcher → reserve blocks (RESERVED)
    ↓
SimulationController → _update_train_position()
    ↓ (crossed boundary)
PathDispatcher.on_train_advances() → BlockManager.occupy_block(new_block, train_id)
SimulationController → BlockManager.clear_block(old_block, train_id)
```

### Deprecated `train.update_occupancy()`
`train.update_occupancy()` remains for backward compatibility only. It can race with dispatcher reservations and manual block updates, causing out-of-order transitions and block thrashing. Production code should route all occupancy changes through `BlockManager`.

**Correct usage:**
```python
# When train enters block
network.block_manager.occupy_block(block_id, train_id)

# When train leaves block  
network.block_manager.clear_block(block_id, train_id)
```

**Incorrect (deprecated):**
```python
train.update_occupancy(network)  # DO NOT USE in new code
```

### Train Movement and Rendering
- `Train.distance_on_track` is the distance in **centimeters (cm)** from the entry edge.
- `TrainMover.calculate_distance_to_advance()` updates this value based on speed and tick duration.
- Scale is applied via `MODEL_SCALE` configuration in `train_config.py`.
- When `distance_on_track >= track.length_cm`, the train advances one tile and resets `distance_on_track`.
- The frontend renderer receives a normalized 0.0–1.0 position for smooth interpolation between tile centers.
- Because position advances only when the distance threshold is met and block updates are atomic, trains no longer teleport or stall on switches.

### Design Principles
1. **Single Authority:** Only `BlockManager` modifies block states.
2. **Atomic Operations:** All transitions use `BlockManager` methods.
3. **State Machine Enforcement:** Illegal transitions are rejected.
4. **Separation of Concerns:** `Block` stores state, `BlockManager` manages transitions, `TrainMover` handles geometry.

### Module References
- `block_manager.py` - Centralized block state manager
- `train_movement.py` - Train movement calculations and path-queue updates
- `simulation.py` - Simulation tick loop with legacy movement logic
- `path_dispatcher.py` - Path reservation, switch locking, and occupancy transitions
- `train_atc.py` - Automatic train control and speed limiting
- `block_power.py` - Block voltage management
- `route_executor.py` - Route and schedule execution
- `network.py` - Infrastructure registry
- `train.py` - Train entity with dual position systems
- `static/js/renderer-trains.js` - Frontend interpolation using path-queue

### Migration Notes
The codebase is in a migration state:
- **Legacy movement** (`simulation.py`): `_advance_train_track`, `_advance_through_block`, `distance_on_track`
- **New path-queue** (`train_movement.py`): `TrainPosition`, `path_tiles`, `head_offset/tail_offset`
- Both systems run in parallel; legacy drives physics, path-queue drives rendering
- `train.update_occupancy()` is deprecated but still called in some API handlers
