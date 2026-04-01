# Route, Station & Platform System

**Status:** Implemented (Route Editor UI and Schedule System are future work)  
**Created:** February 2026  
**Updated:** April 2026

**Implementation Status:**
- Platform entity (renamed from Station): ✅ Implemented in `platform.py`
- Station grouping entity: ✅ Implemented in `station.py`
- Route data structures: ✅ Implemented in `train_route.py`
- RouteExecutor with multi-train support: ✅ Implemented in `route_executor.py`
- Flexible platform resolution: ✅ Implemented
- Route modes (ONE_OFF, CIRCULAR, ROUND_TRIP): ✅ Implemented
- Route Editor UI: ⏳ Not implemented
- Schedule system: ⏳ Not implemented

---

## Table of Contents

1. [Overview & Core Concepts](#1-overview--core-concepts)
2. [Platform Entity](#2-platform-entity)
3. [Station Entity](#3-station-entity)
4. [Route Entity](#4-route-entity)
5. [Waypoint](#5-waypoint)
6. [RouteExecutionState](#6-routeexecutionstate)
7. [RouteExecutor](#7-routeexecutor)
8. [API Endpoints](#8-api-endpoints)
9. [Route Editor UI](#9-route-editor-ui)
10. [Schedule System](#10-schedule-system)
11. [Implementation Phases](#11-implementation-phases)
12. [Future Considerations](#12-future-considerations)
13. [Appendix A: Relationship Diagram](#appendix-a-relationship-diagram)

---

## 1. Overview & Core Concepts

### 1.1 Purpose

This document specifies the Route, Station, and Platform system:

- **Platform**: Physical multi-tile entity representing a railway platform (renamed from the original `Station` class)
- **Station**: Logical grouping entity that collects one or more Platforms under a single name
- **Block-based Waypoints**: Routes target block IDs instead of station IDs, enabling pass-through waypoints at any block
- **Multi-train Routes**: A single Route can be assigned to multiple trains, each with independent execution state
- **Flexible Platform**: Automatic platform selection within a Station when multiple platforms are available

### 1.2 Design Philosophy

| Principle | Description |
|-----------|-------------|
| **Rename, Don't Rewrite** | Platform is a rename of the original Station — all existing functionality is preserved |
| **Separation of Definition and Execution** | Route stores only the definition (waypoints, mode); execution state is per-train in RouteExecutionState |
| **Block is the Unit of Navigation** | Waypoints target blocks, not platforms. Platforms are an optional property of a block. |
| **Station is a Grouping Concept** | Station has no physical tiles — it groups Platforms for flexible routing |
| **Backward-Compatible Persistence** | Saved layouts auto-migrate on load; old `station_id` fields are mapped to new `block_id` fields |

### 1.3 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ROUTE / STATION SYSTEM                              │
│                                                                             │
│  DEFINITION LAYER (stored in network, persisted)                            │
│  ──────────────────────────────────────────────                             │
│                                                                             │
│  ┌─────────────┐       ┌─────────────┐       ┌─────────────────────┐       │
│  │   Station    │──────►│  Platform   │──────►│   Block             │       │
│  │  (grouping)  │ 1..N  │  (physical) │       │  (track section)    │       │
│  │             │       │             │       │                     │       │
│  │  station_id  │       │  platform_id │       │  block_id           │       │
│  │  name        │       │  name        │       │  state              │       │
│  │  platform_ids│       │  positions[] │       │  occupied_by        │       │
│  └─────────────┘       │  block_assign│       └─────────────────────┘       │
│                         │  orientation │                ▲                    │
│                         └─────────────┘                │                    │
│                                                         │ references        │
│  ┌──────────────────────────────────────────────────────│──────────────┐    │
│  │                        Route                         │              │    │
│  │  route_id, name, mode, is_active, started_at         │              │    │
│  │                                                      │              │    │
│  │  waypoints: [Waypoint, Waypoint, ...]                │              │    │
│  │              │                                       │              │    │
│  │              ▼                                       │              │    │
│  │  ┌───────────────────────────────────────┐           │              │    │
│  │  │           Waypoint                    │───────────┘              │    │
│  │  │  block_id (target block)              │                          │    │
│  │  │  dwell_time_sec                       │                          │    │
│  │  │  flexible_platform (bool)             │                          │    │
│  │  │  timing_mode, target_time             │                          │    │
│  │  └───────────────────────────────────────┘                          │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  EXECUTION LAYER (runtime only, not persisted)                              │
│  ──────────────────────────────────────────────                             │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                       RouteExecutor                                  │   │
│  │  execution_states: Dict[str, RouteExecutionState]                    │   │
│  │                                                                      │   │
│  │  ┌──────────────────────────────────┐  Per train:                    │   │
│  │  │     RouteExecutionState          │  - current_waypoint_index      │   │
│  │  │  (one per assigned train)        │  - direction (+1/-1)           │   │
│  │  │                                  │  - arrived_at_platform_time    │   │
│  │  │  train_id, route_id              │  - waiting_for_path            │   │
│  │  │  current_waypoint_index          │  - route_start_time            │   │
│  │  │  direction                       │  - resolved_platform_id        │   │
│  │  └──────────────────────────────────┘                                │   │
│  │                                                                      │   │
│  │  Uses: PathDispatcher (viability + path creation)                    │   │
│  │  Uses: BlockManager (block state queries)                            │   │
│  │  Uses: TrainController (state transitions)                           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Terminology

| Term | Definition |
|------|------------|
| **Platform** | Physical multi-tile entity on the track (renamed from `Station`). Has positions, block assignments, orientation, platform_side. |
| **Station** | Logical grouping of one or more Platforms under a single name. No physical tiles. |
| **Route** | Train's travel plan — a sequence of Waypoints defining where to go. Shared definition, per-train execution. |
| **Waypoint** | A single stop/pass-through point in a Route. Targets a `block_id`. |
| **Path** | Dispatcher's reserved route through blocks — infrastructure reservation. Not a Route. |
| **Block** | Electrically isolated track section. The fundamental unit of navigation. |
| **Flexible Platform** | When enabled on a Waypoint, the executor may route to any available platform within the same Station. |

### 1.5 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Waypoint targets block_id | Yes | Blocks are the navigation unit; platforms are optional metadata on blocks |
| Route stores no execution state | Yes | Execution state is per-train, enabling multi-train routes |
| Station has no physical tiles | Yes | Station is purely a grouping concept; Platform owns the tiles |
| Flexible platform is opt-in | Yes | Explicit per-waypoint flag prevents unexpected platform changes |
| Viability check is non-blocking | Yes | UI stays responsive; viability result displayed asynchronously |
| Route activation is independent | Yes | Route can be active before any train is assigned |

### 1.6 Route vs Path Distinction

```
Route (high-level travel plan)
  └── Waypoint 1 (Block A — Platform 1)
  └── Waypoint 2 (Block B — pass-through)
  └── Waypoint 3 (Block C — Platform 2)

During execution, RouteExecutor creates Paths:
  Route Waypoint 1→2 triggers: Path from Block_A to Block_B
  Route Waypoint 2→3 triggers: Path from Block_B to Block_C
```

Routes are reusable, persistent templates. Paths are transient dispatcher reservations for immediate movement.

---

## 2. Platform Entity

### 2.1 Purpose

Platform is the **renamed** original `Station` class. It represents a physical multi-tile railway platform on the track. All existing functionality is preserved — this was a name change to free up "Station" for the new logical grouping concept.

**Key rename:** The original `network.stations` dict (holding physical platform entities) became `network.platforms`. A new `network.stations` dict holds the logical Station grouping entities.

### 2.2 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `platform_id` | `str` | — | Unique identifier (e.g., `"PLT_001"`) |
| `name` | `str` | `""` | Human-readable name (e.g., `"Platform 1"`) |
| `positions` | `List[Tuple[int, int]]` | `[]` | Tile coordinates that make up the platform |
| `length_cm` | `float` | `0.0` | Physical track length in cm |
| `platform_side` | `PlatformSide` | `BOTH` | Which side(s) have platforms |
| `top_speed_kmh` | `Optional[float]` | `None` | Speed limit through platform |
| `created_at` | `Optional[str]` | auto | ISO timestamp |
| `initial_orientation` | `Optional[str]` | `None` | `'NS'`, `'EW'`, `'NE_SW'`, or `'NW_SE'` |
| `block_assignments` | `Dict[Tuple[int,int], str]` | `{}` | `(x, y)` → `block_id` mapping |

### 2.3 Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `to_dict` | `() -> Dict[str, Any]` | Serialize to JSON-compatible dict |
| `to_dict_with_orientation` | `(network) -> Dict[str, Any]` | Serialize with computed orientation data |
| `from_dict` | `(data) -> Platform` | Deserialize from dict |
| `add_position` | `(x, y) -> None` | Add tile to platform |
| `remove_position` | `(x, y) -> bool` | Remove tile from platform |
| `contains_position` | `(x, y) -> bool` | Check if tile belongs to platform |
| `get_center_position` | `() -> Optional[Tuple]` | Center point of all tiles |
| `assign_tile_to_block` | `(x, y, block_id) -> bool` | Assign tile to block |
| `unassign_tile_from_block` | `(x, y) -> bool` | Remove block assignment |
| `get_block_for_tile` | `(x, y) -> Optional[str]` | Get block for specific tile |
| `get_tiles_for_block` | `(block_id) -> List` | Get all tiles for a block |
| `get_all_assigned_blocks` | `() -> Set[str]` | Get all block IDs |

### 2.4 Class Definition

```python
# platform.py

@dataclass
class Platform:
    platform_id: str
    name: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    length_cm: float = 0.0
    platform_side: PlatformSide = PlatformSide.BOTH
    top_speed_kmh: Optional[float] = None
    created_at: Optional[str] = None
    initial_orientation: Optional[str] = None
    block_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)
```

---

## 3. Station Entity

### 3.1 Purpose

Station is a **logical grouping** entity. It collects one or more Platforms under a single name, enabling:

- Display of a unified station name label on the map
- Flexible platform routing (train goes to any available platform within the station)
- Logical grouping for schedule/timetable display

Station has **no physical tiles** — it is purely a grouping concept.

### 3.2 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `station_id` | `str` | — | Unique identifier (e.g., `"STA_001"`) |
| `name` | `str` | `""` | Human-readable name (e.g., `"Central Station"`) |
| `platform_ids` | `List[str]` | `[]` | Ordered list of Platform IDs belonging to this station |
| `created_at` | `Optional[str]` | auto | ISO timestamp |

### 3.3 Class Definition

```python
# station.py

@dataclass
class Station:
    station_id: str
    name: str = ""
    platform_ids: List[str] = field(default_factory=list)
    created_at: Optional[str] = None

    def add_platform(self, platform_id: str) -> bool: ...
    def remove_platform(self, platform_id: str) -> bool: ...
    def has_platform(self, platform_id: str) -> bool: ...

    def get_all_block_ids(self, network: "RailwayNetwork") -> Set[str]:
        """Get all block IDs across all Platforms — used for flexible platform routing."""
        ...

    def get_center_position(self, network: "RailwayNetwork") -> Optional[Tuple[float, float]]:
        """Average (x, y) across all Platform tiles — used for label rendering."""
        ...
```

### 3.4 Network Integration

```python
# network.py — RailwayNetwork.__init__()

self.platforms: Dict[str, Platform] = {}   # Physical platform entities
self.stations: Dict[str, Station] = {}     # Logical grouping entities
```

### 3.5 Station ↔ Platform Relationship

```
                  Station "Central Station"
                  ┌───────────────────────────┐
                  │  station_id: "STA_001"     │
                  │  name: "Central Station"   │
                  │  platform_ids:              │
                  │    ["PLT_001", "PLT_002"]  │
                  └─────┬───────────┬──────────┘
                        │           │
            ┌───────────┘           └───────────┐
            ▼                                   ▼
Platform "Platform 1"              Platform "Platform 2"
┌───────────────────┐              ┌───────────────────┐
│ PLT_001            │              │ PLT_002            │
│ positions: [(5,10),│              │ positions: [(5,12),│
│  (6,10),(7,10)]    │              │  (6,12),(7,12)]    │
│ block: BLK_A       │              │ block: BLK_B       │
└───────────────────┘              └───────────────────┘
            │                                   │
            ▼                                   ▼
Block "BLK_A"                      Block "BLK_B"
┌───────────────────┐              ┌───────────────────┐
│ state: FREE        │              │ state: OCCUPIED    │
│ occupied_by: None  │              │ occupied_by: T-002 │
└───────────────────┘              └───────────────────┘
```

A route waypoint targeting `block_id: "BLK_A"` with `flexible_platform: true` checks Station `"STA_001"`, finds both `BLK_A` (FREE) and `BLK_B` (OCCUPIED), and selects `BLK_A`.

---

## 4. Route Entity

### 4.1 Purpose

Route defines a train's travel plan — a sequence of Waypoints. Route is a **shared definition** that multiple trains can follow simultaneously, each with their own independent execution state (`RouteExecutionState`).

### 4.2 Design Rationale

Execution state (`current_waypoint_index`, `direction`) was moved off the Route and into per-train `RouteExecutionState`. This enables multiple trains to share one Route without interfering with each other. `Route.activate()` no longer resets any waypoint index — it only sets `is_active=True`.

### 4.3 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `route_id` | `str` | — | Unique identifier (e.g., `"ROUTE_001"`) |
| `name` | `str` | `""` | Display name (e.g., `"Express Line A"`) |
| `waypoints` | `List[Waypoint]` | `[]` | Ordered list of stops |
| `mode` | `RouteMode` | `ONE_OFF` | Traversal mode (see §4.4) |
| `is_active` | `bool` | `False` | Whether route is being executed |
| `started_at` | `Optional[float]` | `None` | Simulation time when activated |
| `assigned_train_ids` | `List[str]` | `[]` | Train IDs currently assigned to this route |
| `last_error` | `Optional[str]` | `None` | Last error message (runtime only) |
| `error_time` | `Optional[float]` | `None` | When error occurred (runtime only) |

### 4.4 Route Modes

The `RouteMode` enum values in `train_route.py`:

| Enum Value | Behavior | Visual |
|------------|----------|--------|
| `ONE_OFF` | A→B→C, then stops. Route deactivates when last assigned train finishes. | `WP1 ──► WP2 ──► WP3 ──► [STOP]` |
| `CIRCULAR` | A→B→C→A→B→C→... wraps around indefinitely. | `WP1 ──► WP2 ──► WP3 ──┐` / `  ▲                     │` / `  └─────────────────────┘` |
| `ROUND_TRIP` | A→B→C→B→A→B→... reverses at each end. | `WP1 ◄──► WP2 ◄──► WP3` |

**ROUND_TRIP detail:** Forward pass visits 1→2→3; reverse pass visits 3→2→1. Direction is tracked per-train in `RouteExecutionState.direction` (`+1` forward, `-1` reverse).

### 4.5 Route Lifecycle

| State | Condition |
|-------|-----------|
| Inactive | `is_active = False` — not being executed |
| Active | `is_active = True`, trains are following the route |
| Complete (ONE_OFF) | Last assigned train finishes all waypoints → `deactivate()` called |

### 4.6 Class Definition

```python
# train_route.py

@dataclass
class Route:
    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_OFF
    assigned_train_ids: List[str] = field(default_factory=list)
    is_active: bool = False
    started_at: Optional[float] = None
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    def activate(self, simulation_time: float) -> None:
        """Set is_active=True. Does NOT reset per-train execution state."""
        self.is_active = True
        self.started_at = simulation_time

    def assign_train(self, train_id: str) -> bool: ...
    def unassign_train(self, train_id: str) -> bool: ...
    def get_progress(self, waypoint_index: int) -> float: ...
```

---

## 5. Waypoint

### 5.1 Purpose

Waypoint represents a single stop or pass-through point in a Route. It targets a `block_id` rather than a platform ID, which enables:

- Pass-through waypoints at any block (no platform required)
- Platform-aware waypoints when the target block belongs to a Platform
- Flexible platform routing via the Station grouping

### 5.2 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `block_id` | `str` | — | Target block ID for this waypoint |
| `dwell_time_sec` | `float` | `30.0` | Stop duration in seconds (0 for pass-through) |
| `flexible_platform` | `bool` | `False` | If true, executor may route to any available platform within the same Station |
| `timing_mode` | `TimingMode` | `NONE` | How timing is applied |
| `target_time` | `Optional[float]` | `None` | For ABSOLUTE/RELATIVE modes (seconds) |

### 5.3 Waypoint Types

| Type | `dwell_time_sec` | `flexible_platform` | Use Case |
|------|-----------------|---------------------|----------|
| Station Stop | > 0 | false | Specific platform, fixed dwell |
| Flexible Station Stop | > 0 | true | Any available platform in station |
| Pass-through | 0 | — | Via point, no stop |
| Timing Point | depends | — | Schedule alignment |

### 5.4 Waypoint Behavior by Type

| Block Type | `flexible_platform` | Behavior |
|------------|---------------------|----------|
| Non-platform block | — (ignored) | Train navigates to block, pauses for `dwell_time_sec` |
| Platform block | `false` | Train navigates to specific platform block, dwells |
| Platform block | `true` | Executor finds Station, checks all platform blocks, routes to best available |

### 5.5 Class Definition

```python
@dataclass
class Waypoint:
    block_id: str
    dwell_time_sec: float = 30.0
    flexible_platform: bool = False
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None

    def should_depart(
        self, time_at_station: float, route_start_time: float, simulation_time: float
    ) -> bool:
        """Determine if train should depart based on timing_mode and dwell_time_sec."""
        if self.timing_mode == TimingMode.NONE:
            return time_at_station >= self.dwell_time_sec
        elif self.timing_mode == TimingMode.RELATIVE:
            elapsed = simulation_time - route_start_time
            return elapsed >= (self.target_time or self.dwell_time_sec)
        elif self.timing_mode == TimingMode.ABSOLUTE:
            return simulation_time >= (self.target_time or 0)
        return False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Waypoint":
        # Migration: accept both block_id and legacy station_id
        block_id = data.get("block_id") or data.get("station_id", "")
        ...
```

---

## 6. RouteExecutionState

### 6.1 Purpose

RouteExecutionState tracks per-train execution progress through a Route. Each assigned train gets its own instance, enabling multiple trains to independently traverse the same Route definition.

### 6.2 Properties (Actual Implementation)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `train_id` | `str` | — | Train being tracked |
| `route_id` | `str` | — | Route being executed |
| `current_waypoint_index` | `int` | `0` | Current position in waypoints list |
| `direction` | `int` | `1` | `+1` forward, `-1` reverse (for ROUND_TRIP) |
| `route_start_time` | `Optional[float]` | `None` | When route was activated for this train |
| `arrived_at_platform_time` | `Optional[float]` | `None` | When train arrived at current waypoint |
| `waiting_for_path` | `bool` | `False` | Whether waiting for path reservation |
| `last_waypoint_platform_id` | `Optional[str]` | `None` | Last platform visited (for direction inference) |
| `resolved_platform_id` | `Optional[str]` | `None` | Actual platform selected after flexible resolution |

### 6.3 Class Definition

```python
@dataclass
class RouteExecutionState:
    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None
```

### 6.4 Waypoint Advancement Logic

Waypoint advancement lives in `RouteExecutor._advance_waypoint()`, not on Route:

```python
def _advance_waypoint(self, exec_state: RouteExecutionState, route: Route):
    if route.mode == RouteMode.ONE_OFF:
        exec_state.current_waypoint_index += 1
        if exec_state.current_waypoint_index >= len(route.waypoints):
            return None  # Route complete for this train

    elif route.mode == RouteMode.CIRCULAR:
        exec_state.current_waypoint_index = (
            exec_state.current_waypoint_index + 1
        ) % len(route.waypoints)

    elif route.mode == RouteMode.ROUND_TRIP:
        exec_state.current_waypoint_index += exec_state.direction
        # Bounce at ends
        if exec_state.current_waypoint_index >= len(route.waypoints):
            exec_state.direction = -1
            exec_state.current_waypoint_index = len(route.waypoints) - 2
        elif exec_state.current_waypoint_index < 0:
            exec_state.direction = 1
            exec_state.current_waypoint_index = 1

    exec_state.resolved_platform_id = None  # Clear for next waypoint
    return route.get_waypoint_at(exec_state.current_waypoint_index)
```

---

## 7. RouteExecutor

### 7.1 Overview

`RouteExecutor` (`route_executor.py`) manages all active routes each simulation tick. It:

1. Iterates active routes, then their assigned trains (multi-train aware)
2. Resolves target blocks (with flexible platform support)
3. Requests paths from `PathDispatcher`
4. Monitors arrival at waypoints
5. Manages dwell timing and departure
6. Advances waypoints and handles route completion

### 7.2 Update Loop

```python
def update(self, delta_time: float) -> None:
    self.simulation_time += delta_time
    for route in self.network.routes.values():
        if not route.is_active:
            continue
        for train_id in route.assigned_train_ids:
            train = self.network.trains.get(train_id)
            if train:
                self._execute_route_for_train(train, route)
```

### 7.3 Flexible Platform Resolution

When `waypoint.flexible_platform = True`, the executor resolves the actual target at departure time using `TravelDirectionPolicy`:

```python
class TravelDirectionPolicy(Enum):
    EITHER = "either"     # Any available platform in the station
    PRESERVE = "preserve" # Must approach from same direction as previous waypoint
```

**PRESERVE policy** — prevents unwanted reversals at multi-platform termini:
1. `RouteExecutor` tracks `last_waypoint_platform_id` in `RouteExecutionState`
2. On each waypoint, it infers the train's approach direction from the previous platform
3. Only selects a platform reachable in that direction without reversing

This means a ROUND_TRIP route on a multi-platform station automatically uses the correct platform for each direction of travel.

Resolution algorithm:
1. If `flexible_platform=False` → use `waypoint.block_id` directly
2. Find the Platform that owns `waypoint.block_id`
3. Find the Station that contains that Platform
4. Get all block IDs across all Platforms in the Station
5. Filter to blocks that are FREE or RESERVED for this train
6. Apply TravelDirectionPolicy to select the best match
7. Cache result in `exec_state.resolved_platform_id`

### 7.4 Arrival Detection

```python
def _is_train_at_block(self, train: "Train", block_id: str) -> bool:
    if not train.position:
        return False
    head_tile = train.position.head_tile
    track = self.network.get_track(*head_tile)
    return track and track.block_section_id == block_id
```

### 7.5 ONE_OFF Completion

When a train finishes all waypoints in ONE_OFF mode:

```python
def _handle_route_complete_for_train(self, train, route, exec_state):
    route.unassign_train(train.train_id)
    train.assigned_route_id = None
    del self.execution_states[train.train_id]
    controller.depart_from_station(has_more_waypoints=False)
    # Deactivate route only when last train finishes
    if route.mode == RouteMode.ONE_OFF and not route.assigned_train_ids:
        route.deactivate()
```

---

## 8. API Endpoints

> See [`docs/API.md`](API.md) for complete API documentation with request/response examples.

### 8.1 Platform CRUD

All original `/api/station` endpoints are now at `/api/platform` with identical behavior.

| Method | URL | Description |
|--------|-----|-------------|
| `POST` | `/api/platform` | Create platform |
| `GET` | `/api/platform/<id>` | Get platform |
| `PATCH` | `/api/platform/<id>` | Update platform |
| `DELETE` | `/api/platform/<id>` | Delete platform |
| `GET` | `/api/platform/validate/<x>/<y>` | Validate placement |
| `POST` | `/api/platform/<id>/extend` | Add tiles |
| `POST` | `/api/platform/<id>/remove_tile` | Remove a tile |
| `POST` | `/api/platform/<id>/assign_tile` | Assign tile to block |
| `POST` | `/api/platform/<id>/unassign_tile` | Unassign tile from block |
| `GET` | `/api/platform/<id>/block_assignments` | Get tile→block mappings |

### 8.2 Station CRUD

| Method | URL | Description |
|--------|-----|-------------|
| `POST` | `/api/station` | Create station |
| `GET` | `/api/station` | List all stations |
| `GET` | `/api/station/<id>` | Get station (enriched with platform details) |
| `PATCH` | `/api/station/<id>` | Update station |
| `DELETE` | `/api/station/<id>` | Delete station |
| `POST` | `/api/station/<id>/add-platform` | Add platform to station |
| `POST` | `/api/station/<id>/remove-platform` | Remove platform from station |

### 8.3 Route CRUD

| Method | URL | Description |
|--------|-----|-------------|
| `GET` | `/api/train-routes` | List all routes |
| `POST` | `/api/train-routes` | Create route |
| `GET` | `/api/train-routes/<id>` | Get route |
| `PUT` | `/api/train-routes/<id>` | Update route |
| `DELETE` | `/api/train-routes/<id>` | Delete route |
| `POST` | `/api/train-routes/<id>/activate` | Activate route |
| `POST` | `/api/train-routes/<id>/deactivate` | Deactivate route |
| `GET` | `/api/train-routes/<id>/status` | Execution status for all assigned trains |

**Waypoints use `block_id`:**

```json
{
    "name": "Express Line A",
    "mode": "circular",
    "waypoints": [
        {"block_id": "BLK_A", "dwell_time_sec": 30, "flexible_platform": true},
        {"block_id": "BLK_C", "dwell_time_sec": 0},
        {"block_id": "BLK_D", "dwell_time_sec": 45}
    ]
}
```

### 8.4 Train-Route Assignment

```
PUT /api/trains/<train_id>/assign-route
{"route_id": "ROUTE_001"}   // Assign
{"route_id": null}           // Unassign
```

Assignment is bidirectional: `Train.assigned_route_id` and `Route.assigned_train_ids` are both updated. A `RouteExecutionState` is created for the train.

### 8.5 Viability Check

```
POST /api/train-routes/check-viability
{"pairs": [["BLK_A", "BLK_C"], ["BLK_C", "BLK_D"]]}

Response:
{"results": [
    {"from": "BLK_A", "to": "BLK_C", "viable": true, "hop_count": 3},
    {"from": "BLK_C", "to": "BLK_D", "viable": false, "error": "No path found"}
]}
```

---

## 9. Route Editor UI

> **Status: ⏳ Not implemented.** This section is a specification for the future Route Editor.

### 9.1 Overview

The Route Editor is a secondary sidebar panel for creating and managing routes visually. It allows operators to click blocks on the canvas to define waypoints and see real-time viability between consecutive stops.

### 9.2 Panel Layout

```
┌─────────────────────────────────────┐
│  Route Editor                    ✕  │
├─────────────────────────────────────┤
│                                     │
│  Route Name: [Express Line A    ]   │
│  Mode: [ONE_OFF ▾]                  │
│                                     │
│  ─── Waypoints ─────────────────── │
│                                     │
│  1. ■ BLK_A (Platform 1)     [✕]   │
│     🔀 Flexible Platform [✓]        │
│     ⏱ Dwell: [30] sec               │
│        ✅ → viable (3 blocks)        │
│                                     │
│  2. ■ BLK_C (pass-through)   [✕]   │
│     ⏱ Dwell: [0] sec                │
│        ✅ → viable                   │
│                                     │
│  3. ■ BLK_D (Platform 3)     [✕]   │
│     🔀 Flexible Platform [ ]        │
│     ⏱ Dwell: [45] sec               │
│        ❌ → no path found            │
│                                     │
│  [+ Click block on map to add]      │
│                                     │
│  ─── Assigned Trains ───────────── │
│                                     │
│  T-001  WP: 2/3  [Unassign]        │
│  T-002  WP: 1/3  [Unassign]        │
│                                     │
│  Assign: [Select train ▾] [Assign]  │
│                                     │
│  ─── Controls ──────────────────── │
│                                     │
│  [ Activate ]  [ Deactivate ]       │
│  [ Save ]      [ Delete ]           │
│                                     │
└─────────────────────────────────────┘
```

### 9.3 Interaction Flows

**Creating a route:**
1. Click "New Route" → `POST /api/train-routes` → editor opens
2. Enter route name and select mode
3. Click canvas tiles to add waypoints; each tile click resolves to a `block_id`
4. If block belongs to a Platform → show flexible_platform toggle
5. Adjust dwell times; viability checks run between consecutive pairs
6. Assign trains from dropdown
7. Click Activate

**Viability checking:** After each waypoint add/reorder/delete, all consecutive pairs are sent to `POST /api/train-routes/check-viability`. Results display inline as ✅/❌ between waypoints. Non-blocking.

**Naming conflict note:** The current `editor-routes.js` is a **path editor** (dispatcher reservations). The Route Editor UI will be in a new `editor-routes.js` after the existing file is renamed to `editor-paths.js`.

---

## 10. Schedule System

> **Status: ⏳ Not implemented.** This section is a design specification for future work.

### 10.1 Schedule Definition

```python
@dataclass
class TrainSchedule:
    schedule_id: str
    train_id: str
    entries: List[ScheduleEntry]
    start_time: datetime
    end_time: Optional[datetime]       # None = indefinite
    repeat_mode: ScheduleRepeatMode    # ONCE, DAILY, WEEKLY, CUSTOM
    repeat_days: Optional[List[int]]   # For WEEKLY: 0=Mon, 6=Sun
```

### 10.2 Schedule Entry

```python
@dataclass
class ScheduleEntry:
    entry_id: str
    route_id: str
    departure_time: time               # When to start route
    departure_window_sec: float        # Allowed delay before warning
    priority: int                      # For conflict resolution
```

---

## 11. Implementation Phases

### Phase 1: Core Route System ✅ Complete
- Route data structure and storage (`train_route.py`)
- Basic CRUD operations (`/api/train-routes` endpoints)
- Waypoint management
- ONE_OFF mode

### Phase 2: Route Editor UI ⏳ Not Started
- Route list panel
- Waypoint editor with block-click input
- Viability indicators
- Route validation

### Phase 3: Route Execution ✅ Complete
- RouteExecutor integration (`route_executor.py`)
- Automatic path creation between waypoints
- Dwell time management
- Status tracking

### Phase 4: Advanced Modes ✅ Complete
- CIRCULAR mode (wraps around)
- ROUND_TRIP (shuttle) mode with direction reversal
- Flexible platform resolution with PRESERVE/EITHER `TravelDirectionPolicy`
- Multi-train route assignment

### Phase 5: Schedule System ⏳ Not Started
- Schedule data structure
- Time-based triggers
- Schedule editor UI
- Recurrence patterns

### Phase 6: Advanced Features ⏳ Not Started
- Route conflict detection
- Priority-based scheduling
- Delay propagation
- Route optimization suggestions

---

## 12. Future Considerations

1. **Route Templates** — Pre-defined route patterns for common operations
2. **Conditional Waypoints** — Skip waypoints based on time/conditions
3. **Dynamic Re-routing** — Automatically adjust route for delays
4. **Multi-train Coordination** — Ensure trains don't conflict on shared segments
5. **Energy Optimization** — Suggest speed profiles for energy efficiency

---

## Appendix A: Relationship Diagram

```
                ┌──────────────────────────────────────────────────────────┐
                │                    RailwayNetwork                         │
                │                                                          │
                │  platforms: Dict[str, Platform]                           │
                │  stations: Dict[str, Station]                            │
                │  routes: Dict[str, Route]                                │
                │  trains: Dict[str, Train]                                │
                │  blocks: Dict[str, Block]                                │
                └───────┬──────────┬──────────┬──────────┬────────────────┘
                        │          │          │          │
           ┌────────────┘          │          │          └────────────┐
           ▼                       ▼          ▼                      ▼
┌──────────────────┐   ┌────────────┐  ┌──────────┐        ┌──────────────┐
│    Platform      │   │  Station   │  │  Route   │        │    Train     │
│  platform_id     │◄──│ platform_  │  │ route_id │        │  train_id    │
│  name            │   │   ids[]    │  │ name     │◄───────│  assigned_   │
│  positions[]     │   │ station_id │  │ waypoints│        │  route_id    │
│  block_assign    │   │ name       │  │ mode     │────────►              │
│  orientation     │   └────────────┘  │ assigned_│        └──────────────┘
│  platform_side   │                   │ train_ids│
└────────┬─────────┘                   └────┬─────┘
         │                                  │
         │ tiles belong to                  │ waypoints target
         ▼                                  ▼
┌──────────────────┐              ┌──────────────────┐
│     Block        │◄─────────────│    Waypoint      │
│  block_id        │   block_id   │  block_id        │
│  state           │              │  dwell_time_sec  │
│  occupied_by     │              │  flexible_plat.  │
│  length_cm       │              │  timing_mode     │
└──────────────────┘              └──────────────────┘
```
