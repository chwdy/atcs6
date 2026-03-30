# Data Structures Documentation

This document describes the key data structures in the railway simulation system, their relationships, and how data flows through the system.

---

## Entity Overview

### Core Entities

| Entity | Class | Storage | Description |
|--------|-------|---------|-------------|
| **Track** | `TrackSegment` | `network.tracks[(x,y)]` | Individual track tiles on the grid |
| **Block** | `Block` | `network.blocks[block_id]` | Electrically isolated track sections |
| **Signal** | `Signal` | `network.signals[signal_id]` | Signal entities controlling train movement |
| **Switch** | `Switch` | `network.switches[switch_id]` | Turnouts with main/diverge paths |
| **Train** | `Train` | `network.trains[train_id]` | Train entities with position, state |
| **Platform** | `Platform` | `network.platforms[platform_id]` | Physical passenger platforms |
| **Station** | `Station` | `network.station_groups[station_id]` | Logical grouping of platforms |
| **Path** | `Path` | `path_dispatcher.paths[path_id]` | Reserved route for a train |

### Manager Classes

| Manager | Manages | Purpose |
|---------|---------|---------|
| **BlockManager** | `Block` | Single source of truth for block state transitions |
| **PathDispatcher** | `Path` | Path creation, validation, resource acquisition |
| **SimulationController** | Simulation | Orchestrates train movement |
| **TrainController** | Train logic | ATC + ATO control decisions (per train) |
| **TrainPhysics** | Train movement | Physical movement calculations |

---

## Train Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           RailwayNetwork                                 │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────┐  │
│  │ Tracks  │  │ Blocks  │  │ Signals │  │ Switches│  │ Trains      │  │
│  │ (grid)  │  │ (state) │  │ (state) │  │ (state) │  │ (position)  │  │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └─────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
         │                  │                   │                   │
         ▼                  ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         SimulationController                             │
│  ┌─────────────────────────┐  ┌─────────────────────────────────────┐  │
│  │     PathDispatcher      │  │      TrainPhysics                   │  │
│  │  - paths[path_id]       │  │  - Per-train physics calculations   │  │
│  │  - create_path()        │  │  - Movement, voltage output         │  │
│  │  - reserve_blocks()     │  └─────────────────────────────────────┘  │
│  └─────────────────────────┘                                           │
│                    │                                                    │
│                    ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              TrainController (per train)                         │   │
│  │  - _calculate_lookahead() - Get blocks ahead                    │   │
│  │  - _get_blocks_ahead() - Iterate path tiles                     │   │
│  │  - get_path_tiles() - Get canonical path from dispatcher        │   │
│  │  - atc_speed_limit - Computed speed limit                       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Path Sources (Important!)

**The train lookahead and path following system has TWO sources of path data:**

### 1. Canonical Source: PathDispatcher (SINGLE SOURCE OF TRUTH)

```python
# In TrainController.get_path_tiles():
if self.path_dispatcher:
    path = self.path_dispatcher.get_path(train.assigned_path_id)
    return convert_tiles_to_train_path(path.path_tiles)
```

- Stored in `Path.path_tiles` (canonical)
- Managed by `PathDispatcher.paths[path_id]`
- Updated when path is extended/cancelled
- **Use this for all control decisions**

### 2. Cache: Train.current_path (SERIALIZATION ONLY)

```python
# Fallback in TrainController.get_path_tiles():
return train.current_path
```

- Stored in `Train.current_path`
- Used for JSON persistence only
- **Can become stale** - do NOT use for control decisions

### Debugging Path Issues

Use these endpoints to compare sources:
- `/api/debug/trains/{id}/path` - Shows both dispatcher path and cache
- `/api/debug/dispatcher/paths/{id}` - Shows canonical path details

---

## Lookahead Calculation

The lookahead system determines safe speed by looking at blocks ahead:

```
TrainController._calculate_lookahead()
    │
    ├── 1. _get_current_block_id()
    │       └── Uses train.position.head_tile → network.tracks → block_section_id
    │
    ├── 2. _get_remaining_distance_in_current_block()
    │       └── Uses get_path_tiles() or train.current_path + path_index
    │
    └── 3. _get_blocks_ahead()
            └── Iterates path tiles in travel direction
                └── For each tile: check block state via BlockManager
```

### Key Data Structures

**LookaheadPoint** (in `train_controller.py`):
```python
@dataclass
class LookaheadPoint:
    tile: Tuple[int, int]          # Grid position
    distance_cm: float              # Distance from train head
    block_id: str                   # Block containing this tile
    obstructed: bool                # Whether block is blocked
    signal_state: Optional[str]     # Signal aspect (for display)
```

**Note:** `signal_state` is included for display but is NOT used in ATC calculation (CBTC-style).

### Debugging Lookahead

Use these endpoints:
- `/api/debug/trains/{id}/lookahead` - Full lookahead points
- `/api/debug/trains/{id}/clearance` - Blocks ahead with obstruction status
- `/api/debug/trains/{id}/atc` - ATC speed limit breakdown
- `/api/debug/trains/{id}/scan-step` - Direction detection debug

---

## Block State Machine

```
┌──────────┐     reserve      ┌───────────┐     occupy      ┌──────────┐
│   FREE   │ ───────────────▶ │  RESERVED │ ──────────────▶ │ OCCUPIED │
└──────────┘                  └───────────┘                 └──────────┘
     ▲                                                                    │
     │                                                                    │
     │                       clear                    set_caution        │
     └───────────────────────────────  ◀  ───────────────────────────────┘
                  ┌───────────┐
                  │  CAUTION  │
                  └───────────┘
```

### State Descriptions

| State | Description |
|-------|-------------|
| **FREE** | No train reservation or occupation |
| **RESERVED** | Reserved for a train's planned path (interlocking locked) |
| **OCCUPIED** | Physical train presence detected |
| **CAUTION** | Train recently cleared but safety margin maintained |

### BlockManager Methods

| Method | Transition | Description |
|--------|------------|-------------|
| `occupy_block(id, train)` | FREE/RESERVED → OCCUPIED | Train enters block |
| `set_block_to_caution(id)` | OCCUPIED → CAUTION | Train exits, safety margin |
| `clear_block(id)` | CAUTION/OCCUPIED/RESERVED → FREE | Complete release |
| `release_block(id, train)` | OCCUPIED → CAUTION → FREE | Full release cycle |

### Debugging Blocks

- `/api/debug/blocks` - All blocks with state
- `/api/debug/blocks/{id}` - Single block details
- `/api/debug/block-manager` - State distribution summary
- `/api/debug/block-manager/history` - Recent state changes (last 50)

---

## Block Manager (Single Source of Truth)

The `BlockManager` class is the **single source of truth** for block state:

```python
class BlockManager:
    def __init__(self, blocks: Dict[str, Block]):
        self.blocks = blocks
        self.history: deque = deque(maxlen=100)  # Recent state changes
    
    def occupy_block(self, block_id: str, train_id: str) -> bool:
        # Atomic state change with history recording
        ...
    
    def clear_block(self, block_id: str) -> bool:
        # Atomic state change with history recording
        ...
```

### Why BlockManager Exists

Previous implementation had competing block update mechanisms causing:
- Race conditions
- Train teleporting
- Stuck trains
- Random slowdowns

**Solution:** All block state changes go through BlockManager methods.

---

## Train Controller (ATC + ATO)

### ATC (Automatic Train Control)

- **Speed enforcement**: Enforce speed limits based on clearance
- **Lookahead**: Calculate safe speed based on blocks ahead
- **Obstruction detection**: Detect blocked blocks in path

### ATO (Automatic Train Operation)

- **Station stopping**: Stop at designated platforms
- **Speed profiling**: Accelerate/decelerate according to profiles
- **Door control**: Manage door opening/closing

### Key Attributes

```python
class TrainController:
    def __init__(self, train, network, sim_controller):
        self.train = train
        self.network = network
        self.path_dispatcher = sim_controller.path_dispatcher
        
        # ATC outputs
        self.atc_speed_limit: float = 0.0       # Current speed limit
        self.clearance_distance_cm: float = 0.0  # Distance to obstruction
        self.handle_position: float = 0.0       # Throttle/brake (0-1)
        self.power: float = 0.0                 # Power output (negative = brake)
        
        # ATO state
        self.ato_destination_block_id: Optional[str] = None
```

### Debugging Train Controller

- `/api/debug/trains/{id}` - Full train state
- `/api/debug/trains/{id}/lookahead` - Lookahead points
- `/api/debug/trains/{id}/atc` - Speed limit breakdown
- `/api/debug/trains/{id}/physics` - Position, speed, voltage

---

## API Debug Endpoints Reference

### Entity Summaries

| Endpoint | Description |
|----------|-------------|
| `GET /api/debug/summary` | Counts of all entity types |
| `GET /api/debug/tracks` | All tracks |
| `GET /api/debug/blocks` | All blocks with state |
| `GET /api/debug/signals` | All signals |
| `GET /api/debug/switches` | All switches |
| `GET /api/debug/trains` | All trains (summary) |

### Single Entity

| Endpoint | Description |
|----------|-------------|
| `GET /api/debug/tracks/{x}/{y}` | Single track |
| `GET /api/debug/blocks/{id}` | Single block |
| `GET /api/debug/signals/{id}` | Single signal |
| `GET /api/debug/switches/{id}` | Single switch |
| `GET /api/debug/trains/{id}` | Full train state |
| `GET /api/debug/platforms/{id}` | Single platform |
| `GET /api/debug/stations/{id}` | Single station |

### Train Debug

| Endpoint | Description |
|----------|-------------|
| `GET /api/debug/trains/{id}/lookahead` | Lookahead points |
| `GET /api/debug/trains/{id}/path` | Path source comparison |
| `GET /api/debug/trains/{id}/clearance` | Clearance info |
| `GET /api/debug/trains/{id}/atc` | ATC speed limit |
| `GET /api/debug/trains/{id}/physics` | Physics state |
| `GET /api/debug/trains/{id}/scan-step` | Direction detection |
| `GET /api/debug/trains/{id}/state` | Train state machine |
| `GET /api/debug/trains/{id}/route` | Route execution |

### Dispatcher & Block Manager

| Endpoint | Description |
|----------|-------------|
| `GET /api/debug/dispatcher` | Dispatcher summary |
| `GET /api/debug/dispatcher/paths` | All paths |
| `GET /api/debug/dispatcher/paths/{id}` | Single path |
| `GET /api/debug/block-manager` | Block manager summary |
| `GET /api/debug/block-manager/history` | State change history |

### API Logging

| Endpoint | Description |
|----------|-------------|
| `GET /api/debug/api-log` | Recent API calls |
| `GET /api/debug/api-log?replay=true` | Replayable format |
| `POST /api/debug/api-log/clear` | Clear logs |

---

## Common Debugging Scenarios

### Scenario 1: Train not moving

Check:
1. `/api/debug/trains/{id}/state` - Is train in RUNNING state?
2. `/api/debug/trains/{id}/atc` - Is ATC speed limit > 0?
3. `/api/debug/trains/{id}/clearance` - Any obstructed blocks?
4. `/api/debug/blocks/{block_id}` - Is current block OCCUPIED?

### Scenario 2: Train moving too slow

Check:
1. `/api/debug/trains/{id}/lookahead` - What's blocking the path?
2. `/api/debug/trains/{id}/atc` - What's the limiting factor?
3. `/api/debug/block-manager/history` - Any CAUTION blocks?

### Scenario 3: Path issues

Check:
1. `/api/debug/trains/{id}/path` - Does dispatcher path match cache?
2. `/api/debug/dispatcher/paths/{id}` - Is path still active?
3. `/api/debug/trains/{id}/scan-step` - Correct direction detected?

### Scenario 4: Block state issues

Check:
1. `/api/debug/block-manager/history` - Recent state changes
2. `/api/debug/blocks/{id}` - Current state
3. `/api/debug/trains/{id}` - Which train claims to occupy it