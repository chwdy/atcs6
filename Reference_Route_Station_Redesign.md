# Technical Specification: Route, Station & Platform System Redesign

> **Document Type:** Technical Implementation Specification  
> **Version:** 1.0  
> **Created:** February 2026  
> **Status:** Design Complete — Not Yet Implemented

## Table of Contents

1. [Overview & Core Concepts](#1-overview--core-concepts)
2. [Platform Entity Specification](#2-platform-entity-specification)
3. [Station Entity Specification](#3-station-entity-specification)
4. [Route Entity Specification](#4-route-entity-specification)
5. [Waypoint Specification](#5-waypoint-specification)
6. [RouteExecutionState Specification](#6-routeexecutionstate-specification)
7. [RouteExecutor Changes](#7-routeexecutor-changes)
8. [API Endpoints](#8-api-endpoints)
9. [Route Editor UI Specification](#9-route-editor-ui-specification)
10. [Migration Plan](#10-migration-plan)
11. [File Changes Summary](#11-file-changes-summary)

---

## 1. Overview & Core Concepts

### 1.1 Purpose

This document specifies a redesign of the Route, Station, and Waypoint system to enable:

- **Platform** (renamed from Station): Physical multi-tile entity representing a railway platform
- **Station** (new): Logical grouping entity that collects one or more Platforms under a single name
- **Block-based Waypoints**: Routes target block IDs instead of station IDs, enabling pass-through waypoints at any block
- **Multi-train Routes**: A single Route can be assigned to multiple trains, each with independent execution state
- **Route Editor UI**: A new secondary sidebar panel for creating and managing routes visually
- **Viability Checking**: Real-time path viability validation between consecutive waypoints
- **Flexible Platform**: Automatic platform selection within a Station when multiple platforms are available

### 1.2 Design Philosophy

| Principle | Description |
|-----------|-------------|
| **Rename, Don't Rewrite** | Platform is a rename of Station — all existing functionality is preserved |
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
│  ┌─────────────────────────────────────────────────────┐│                    │
│  │                        Route                         ││                    │
│  │  ─────────────────────────────────────────────────  ││                    │
│  │  route_id, name, mode, is_active, started_at        ││                    │
│  │                                                     ││                    │
│  │  waypoints: [Waypoint, Waypoint, ...]               ││                    │
│  │              │                                      ││                    │
│  │              ▼                                      ││                    │
│  │  ┌───────────────────────────────────────┐          ││                    │
│  │  │           Waypoint                    │──────────┘│                    │
│  │  │  block_id (target block)              │           │                    │
│  │  │  dwell_time_sec                       │           │                    │
│  │  │  flexible_platform (bool)             │           │                    │
│  │  │  timing_mode, target_time             │           │                    │
│  │  └───────────────────────────────────────┘           │                    │
│  └──────────────────────────────────────────────────────┘                    │
│                                                                             │
│  EXECUTION LAYER (runtime only, not persisted)                              │
│  ──────────────────────────────────────────────                             │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                       RouteExecutor                                    │   │
│  │  ─────────────────────────────────────────────────────────────────   │   │
│  │                                                                       │   │
│  │  execution_states: Dict[str, RouteExecutionState]                     │   │
│  │                                                                       │   │
│  │  ┌──────────────────────────────────┐  Per train:                     │   │
│  │  │     RouteExecutionState          │  - current_waypoint_index       │   │
│  │  │  (one per assigned train)        │  - direction (+1/-1)            │   │
│  │  │                                  │  - arrived_at_station_time      │   │
│  │  │  train_id, route_id              │  - waiting_for_path             │   │
│  │  │  current_waypoint_index          │  - route_start_time             │   │
│  │  │  direction                       │  - resolved_block_id            │   │
│  │  └──────────────────────────────────┘                                 │   │
│  │                                                                       │   │
│  │  Uses: PathDispatcher (viability + path creation)                     │   │
│  │  Uses: BlockManager (block state queries)                              │   │
│  │  Uses: TrainController (state transitions)                             │   │
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

---

## 2. Platform Entity Specification

### 2.1 Purpose

Platform is the **renamed** `Station` class. It represents a physical multi-tile railway platform on the track. All existing functionality is preserved — this is a name change only.

### 2.2 What Changes

| Aspect | Before | After |
|--------|--------|-------|
| Class name | `Station` | `Platform` |
| File name | `station.py` | `platform.py` |
| Network dict | `network.stations` | `network.platforms` |
| API base URL | `/api/station` | `/api/platform` |
| Track reference | `track.station_id` | `track.platform_id` |
| Route file | `routes/stations.py` | `routes/platforms.py` |
| JS references | `station`, `stationId` | `platform`, `platformId` |

### 2.3 Properties (Unchanged)

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

### 2.4 Methods (Unchanged)

All methods from `Station` carry over with no behavioral changes:

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

### 2.5 Serialization Key Changes

The `to_dict()` output key changes:

```python
# Before
{"station_id": "STA_001", "name": "Central", ...}

# After
{"platform_id": "PLT_001", "name": "Central", ...}
```

### 2.6 Class Definition

```python
# platform.py (renamed from station.py)

@dataclass
class Platform:
    """
    Represents a railway platform in the network.

    Platforms can span multiple tiles and have physical properties independent
    of the number of tiles they occupy. Formerly named 'Station'.

    Attributes:
        platform_id: Unique identifier (e.g., "PLT_001")
        name: Human-readable platform name
        positions: List of (x, y) tile positions
        length_cm: Physical track length in cm
        platform_side: Which side(s) have platforms
        top_speed_kmh: Optional speed limit
        created_at: ISO timestamp
        initial_orientation: Track orientation group
        block_assignments: (x, y) -> block_id mapping
    """

    platform_id: str
    name: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    length_cm: float = 0.0
    platform_side: PlatformSide = PlatformSide.BOTH
    top_speed_kmh: Optional[float] = None
    created_at: Optional[str] = None
    initial_orientation: Optional[str] = None
    block_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)

    # ... all methods identical to current Station, just with platform_id
```

---

## 3. Station Entity Specification

### 3.1 Purpose

Station is a **new** logical grouping entity. It collects one or more Platforms under a single name, enabling:

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
# station.py (NEW file — the old station.py becomes platform.py)

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set, Tuple
from datetime import datetime


@dataclass
class Station:
    """
    Logical grouping of railway Platforms under a single station name.

    A Station has no physical tiles — it groups Platforms for unified naming,
    flexible routing, and schedule display. A single Platform can belong to
    at most one Station.

    Attributes:
        station_id: Unique identifier (e.g., "STA_001")
        name: Human-readable station name (e.g., "Central Station")
        platform_ids: Ordered list of Platform IDs belonging to this station
        created_at: ISO timestamp of creation
    """

    station_id: str
    name: str = ""
    platform_ids: List[str] = field(default_factory=list)
    created_at: Optional[str] = None

    def __post_init__(self):
        """Set creation timestamp if not provided."""
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()

    def add_platform(self, platform_id: str) -> bool:
        """
        Add a Platform to this Station.

        Args:
            platform_id: Platform ID to add

        Returns:
            True if added, False if already present
        """
        if platform_id in self.platform_ids:
            return False
        self.platform_ids.append(platform_id)
        return True

    def remove_platform(self, platform_id: str) -> bool:
        """
        Remove a Platform from this Station.

        Args:
            platform_id: Platform ID to remove

        Returns:
            True if removed, False if not found
        """
        if platform_id not in self.platform_ids:
            return False
        self.platform_ids.remove(platform_id)
        return True

    def has_platform(self, platform_id: str) -> bool:
        """Check if Platform belongs to this Station."""
        return platform_id in self.platform_ids

    def get_all_block_ids(self, network: "RailwayNetwork") -> Set[str]:
        """
        Get all block IDs across all Platforms in this Station.

        This is used for flexible platform routing — the executor checks
        availability across all blocks in a Station.

        Args:
            network: Railway network for Platform lookup

        Returns:
            Set of block IDs from all Platforms
        """
        block_ids = set()
        for platform_id in self.platform_ids:
            platform = network.platforms.get(platform_id)
            if platform:
                block_ids.update(platform.get_all_assigned_blocks())
                # Also include blocks from tile track references
                for x, y in platform.positions:
                    track = network.get_track(x, y)
                    if track and track.block_section_id:
                        block_ids.add(track.block_section_id)
        return block_ids

    def get_center_position(self, network: "RailwayNetwork") -> Optional[Tuple[float, float]]:
        """
        Calculate center position across all Platforms for label rendering.

        Args:
            network: Railway network for Platform lookup

        Returns:
            Average (x, y) across all Platform tiles, or None
        """
        all_positions = []
        for platform_id in self.platform_ids:
            platform = network.platforms.get(platform_id)
            if platform:
                all_positions.extend(platform.positions)

        if not all_positions:
            return None

        avg_x = sum(x for x, y in all_positions) / len(all_positions)
        avg_y = sum(y for x, y in all_positions) / len(all_positions)
        return (avg_x, avg_y)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "station_id": self.station_id,
            "name": self.name,
            "platform_ids": list(self.platform_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Station":
        """Reconstruct from dictionary after deserialization."""
        return cls(
            station_id=data["station_id"],
            name=data.get("name", ""),
            platform_ids=list(data.get("platform_ids", [])),
            created_at=data.get("created_at"),
        )
```

### 3.4 Network Integration

```python
# In network.py — RailwayNetwork.__init__()

self.platforms: Dict[str, Platform] = {}   # Renamed from self.stations
self.stations: Dict[str, Station] = {}     # NEW — grouping entities
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

In this example, a route waypoint targeting `block_id: "BLK_A"` with `flexible_platform: true` would check Station `"STA_001"`, discover both `BLK_A` (FREE) and `BLK_B` (OCCUPIED), and select `BLK_A` as the target.

---

## 4. Route Entity Specification

### 4.1 Purpose

Route defines a train's travel plan — a sequence of Waypoints. In this redesign, Route becomes a **shared definition** that multiple trains can follow simultaneously, each with their own independent execution state.

### 4.2 What Changes

| Aspect | Before | After |
|--------|--------|-------|
| Execution state on Route | `current_waypoint_index`, `direction` | Removed — moved to `RouteExecutionState` |
| Train assignment | Single `Train.assigned_route_id` | Route has `assigned_train_ids: List[str]`; Train keeps `assigned_route_id` (bidirectional) |
| `activate()` | Resets `current_waypoint_index` to 0 | Only sets `is_active=True` and `started_at` |
| `advance_to_next_waypoint()` | Mutates Route state | Removed from Route — logic moves to RouteExecutor |
| `get_current_waypoint()` | Uses Route's `current_waypoint_index` | Removed from Route — use RouteExecutionState's index |
| `get_progress()` | Uses Route's index | Takes `waypoint_index` parameter |
| Waypoints | `station_id` based | `block_id` based (see §5) |

### 4.3 Properties

#### 4.3.1 Definition Properties (Persisted)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `route_id` | `str` | — | Unique identifier (e.g., `"ROUTE_001"`) |
| `name` | `str` | `""` | Display name (e.g., `"Express Line A"`) |
| `waypoints` | `List[Waypoint]` | `[]` | Ordered list of stops |
| `mode` | `RouteMode` | `ONE_WAY` | Traversal mode: `ONE_WAY`, `LOOP`, `ROUND_TRIP` |
| `is_active` | `bool` | `False` | Whether route is being executed |
| `started_at` | `Optional[float]` | `None` | Simulation time when activated |

#### 4.3.2 Assignment Properties (Persisted)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `assigned_train_ids` | `List[str]` | `[]` | Train IDs currently assigned to this route |

#### 4.3.3 Error Tracking (Runtime)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `last_error` | `Optional[str]` | `None` | Last error message |
| `error_time` | `Optional[float]` | `None` | When error occurred |

#### 4.3.4 Removed Properties

| Property | Where It Went |
|----------|---------------|
| `current_waypoint_index` | `RouteExecutionState.current_waypoint_index` |
| `direction` | `RouteExecutionState.direction` |

### 4.4 Class Definition

```python
# train_route.py (modified)

@dataclass
class Route:
    """
    Train's travel plan - sequence of waypoints to visit.

    A Route defines WHERE trains should go and HOW they traverse the waypoints.
    Execution state (current waypoint, direction) is stored per-train in
    RouteExecutionState, enabling multiple trains to share one Route.

    Attributes:
        route_id: Unique identifier
        name: Display name
        waypoints: Ordered list of stops
        mode: How to traverse waypoints (ONE_WAY, LOOP, ROUND_TRIP)
        assigned_train_ids: Trains currently assigned to this route
        is_active: Whether route is being executed
        started_at: Simulation time when activated
        last_error: Last error message
        error_time: When error occurred
    """

    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_WAY

    # Assignment
    assigned_train_ids: List[str] = field(default_factory=list)

    # Activation state
    is_active: bool = False
    started_at: Optional[float] = None

    # Error tracking
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    # ── Definition Methods (kept) ──────────────────────────────────

    def add_waypoint(self, waypoint: Waypoint) -> None:
        """Add a waypoint to the end of the route."""
        self.waypoints.append(waypoint)

    def insert_waypoint(self, index: int, waypoint: Waypoint) -> None:
        """Insert a waypoint at a specific position."""
        self.waypoints.insert(index, waypoint)

    def remove_waypoint(self, index: int) -> Optional[Waypoint]:
        """Remove a waypoint at a specific position."""
        if 0 <= index < len(self.waypoints):
            return self.waypoints.pop(index)
        return None

    def get_waypoint_count(self) -> int:
        """Get total number of waypoints."""
        return len(self.waypoints)

    def get_waypoint_at(self, index: int) -> Optional[Waypoint]:
        """Get waypoint at index without any execution state dependency."""
        if 0 <= index < len(self.waypoints):
            return self.waypoints[index]
        return None

    # ── Activation Methods ─────────────────────────────────────────

    def activate(self, simulation_time: float) -> None:
        """
        Activate the route for execution.

        Note: Does NOT reset per-train execution state — that is managed
        by RouteExecutor when it detects a newly active route.

        Args:
            simulation_time: Current simulation time
        """
        self.is_active = True
        self.started_at = simulation_time
        self.last_error = None
        self.error_time = None

    def deactivate(self) -> None:
        """Deactivate the route (pause execution)."""
        self.is_active = False

    def reset(self) -> None:
        """Reset route to initial state."""
        self.is_active = False
        self.started_at = None
        self.last_error = None
        self.error_time = None

    # ── Assignment Methods ─────────────────────────────────────────

    def assign_train(self, train_id: str) -> bool:
        """
        Assign a train to this route.

        Args:
            train_id: Train to assign

        Returns:
            True if added, False if already assigned
        """
        if train_id in self.assigned_train_ids:
            return False
        self.assigned_train_ids.append(train_id)
        return True

    def unassign_train(self, train_id: str) -> bool:
        """
        Remove a train from this route.

        Args:
            train_id: Train to remove

        Returns:
            True if removed, False if not found
        """
        if train_id not in self.assigned_train_ids:
            return False
        self.assigned_train_ids.remove(train_id)
        return True

    def is_train_assigned(self, train_id: str) -> bool:
        """Check if train is assigned to this route."""
        return train_id in self.assigned_train_ids

    # ── Progress (requires external waypoint_index) ────────────────

    def get_progress(self, waypoint_index: int) -> float:
        """
        Get route progress as fraction (0.0 to 1.0) for ONE_WAY mode.

        Args:
            waypoint_index: Current waypoint index (from RouteExecutionState)

        Returns:
            Progress fraction (0.0 = start, 1.0 = complete)
        """
        if self.mode != RouteMode.ONE_WAY or not self.waypoints:
            return 0.0
        return min(1.0, waypoint_index / len(self.waypoints))

    # ── Error Methods ──────────────────────────────────────────────

    def set_error(self, message: str, simulation_time: float) -> None:
        """Record an execution error."""
        self.last_error = message
        self.error_time = simulation_time

    def clear_error(self) -> None:
        """Clear any recorded error."""
        self.last_error = None
        self.error_time = None

    # ── Serialization ──────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "route_id": self.route_id,
            "name": self.name,
            "waypoints": [wp.to_dict() for wp in self.waypoints],
            "mode": self.mode.value,
            "assigned_train_ids": list(self.assigned_train_ids),
            "is_active": self.is_active,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "error_time": self.error_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Route":
        """Reconstruct from dictionary after deserialization."""
        mode_str = data.get("mode", "one_way")
        try:
            mode = RouteMode(mode_str)
        except ValueError:
            mode = RouteMode.ONE_WAY

        waypoints = [
            Waypoint.from_dict(wp_data) for wp_data in data.get("waypoints", [])
        ]

        return cls(
            route_id=data.get("route_id", ""),
            name=data.get("name", ""),
            waypoints=waypoints,
            mode=mode,
            assigned_train_ids=list(data.get("assigned_train_ids", [])),
            is_active=bool(data.get("is_active", False)),
            started_at=data.get("started_at"),
            last_error=data.get("last_error"),
            error_time=data.get("error_time"),
        )
```

### 4.5 Route Modes (Unchanged)

| Mode | Behavior | Completion |
|------|----------|------------|
| `ONE_WAY` | A → B → C → stop | Route deactivates when last train finishes all waypoints |
| `LOOP` | A → B → C → A → B → C → ... | Never completes; runs until deactivated |
| `ROUND_TRIP` | A → B → C → B → A → B → ... | Never completes; bounces at ends |

---

## 5. Waypoint Specification

### 5.1 Purpose

Waypoint represents a single stop or pass-through point in a Route. The key change is targeting `block_id` instead of `station_id`, which enables:

- Pass-through waypoints at any block (no platform required)
- Platform-aware waypoints when the target block belongs to a Platform
- Flexible platform routing via the Station grouping

### 5.2 What Changes

| Aspect | Before | After |
|--------|--------|-------|
| Target field | `station_id: str` | `block_id: str` |
| Platform awareness | Implicit (every waypoint was a station) | Explicit: `is_platform_block` computed at runtime |
| Flexible platform | Not supported | `flexible_platform: bool` field |
| Dwell time | Always required | Only meaningful for platform blocks (or explicit brief stops) |

### 5.3 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `block_id` | `str` | — | Target block ID for this waypoint |
| `dwell_time_sec` | `float` | `30.0` | Stop duration in seconds (0 for pass-through) |
| `flexible_platform` | `bool` | `False` | If true, executor may route to any available platform within the same Station |
| `timing_mode` | `TimingMode` | `NONE` | How timing is applied |
| `target_time` | `Optional[float]` | `None` | For ABSOLUTE/RELATIVE modes |

### 5.4 Platform Detection Logic

At runtime, the RouteExecutor determines whether a waypoint's block is a platform block:

```python
def _is_platform_block(self, block_id: str) -> bool:
    """Check if a block belongs to any Platform."""
    for platform in self.network.platforms.values():
        if block_id in platform.get_all_assigned_blocks():
            return True
        # Also check tiles
        for x, y in platform.positions:
            track = self.network.get_track(x, y)
            if track and track.block_section_id == block_id:
                return True
    return False

def _get_platform_for_block(self, block_id: str) -> Optional[str]:
    """Get Platform ID that owns this block, if any."""
    for platform in self.network.platforms.values():
        if block_id in platform.get_all_assigned_blocks():
            return platform.platform_id
        for x, y in platform.positions:
            track = self.network.get_track(x, y)
            if track and track.block_section_id == block_id:
                return platform.platform_id
    return None

def _get_station_for_platform(self, platform_id: str) -> Optional[str]:
    """Get Station ID that contains this Platform, if any."""
    for station in self.network.stations.values():
        if platform_id in station.platform_ids:
            return station.station_id
    return None
```

### 5.5 Waypoint Behavior by Type

| Block Type | `flexible_platform` | Behavior |
|------------|---------------------|----------|
| Non-platform block | — (ignored) | Train navigates to block, pauses for `dwell_time_sec` (0 = pass-through) |
| Platform block | `false` | Train navigates to specific platform block, dwells for `dwell_time_sec` |
| Platform block | `true` | Executor finds Station, checks all platform blocks for availability, routes to best available |

### 5.6 Class Definition

```python
@dataclass
class Waypoint:
    """
    Single stop or pass-through point in a train's Route.

    Targets a block_id rather than a station_id. If the block belongs to a
    Platform, additional options become available (flexible_platform, dwell).

    Attributes:
        block_id: Target block ID
        dwell_time_sec: Stop duration in seconds (0 for pass-through)
        flexible_platform: Allow routing to any platform in the same Station
        timing_mode: How timing is applied (NONE, RELATIVE, ABSOLUTE)
        target_time: For ABSOLUTE/RELATIVE modes (seconds)
    """

    block_id: str
    dwell_time_sec: float = 30.0
    flexible_platform: bool = False
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None

    def should_depart(
        self, time_at_station: float, route_start_time: float, simulation_time: float
    ) -> bool:
        """
        Determine if train should depart based on timing mode.

        Args:
            time_at_station: Seconds since train arrived at this block
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            True if train should depart, False if it should wait
        """
        if self.timing_mode == TimingMode.NONE:
            return time_at_station >= self.dwell_time_sec

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return time_at_station >= self.dwell_time_sec
            elapsed = simulation_time - route_start_time
            return elapsed >= self.target_time

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return time_at_station >= self.dwell_time_sec
            return simulation_time >= self.target_time

        return False

    def get_remaining_dwell_time(
        self, time_at_station: float, route_start_time: float, simulation_time: float
    ) -> float:
        """Get remaining time before departure."""
        if self.timing_mode == TimingMode.NONE:
            return max(0.0, self.dwell_time_sec - time_at_station)

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_station)
            elapsed = simulation_time - route_start_time
            return max(0.0, self.target_time - elapsed)

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_station)
            return max(0.0, self.target_time - simulation_time)

        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "block_id": self.block_id,
            "dwell_time_sec": self.dwell_time_sec,
            "flexible_platform": self.flexible_platform,
            "timing_mode": self.timing_mode.value,
            "target_time": self.target_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Waypoint":
        """Reconstruct from dictionary after deserialization."""
        timing_mode_str = data.get("timing_mode", "none")
        try:
            timing_mode = TimingMode(timing_mode_str)
        except ValueError:
            timing_mode = TimingMode.NONE

        # Migration: accept both block_id and legacy station_id
        block_id = data.get("block_id", "")
        if not block_id:
            block_id = data.get("station_id", "")  # Legacy fallback

        return cls(
            block_id=block_id,
            dwell_time_sec=float(data.get("dwell_time_sec", 30.0)),
            flexible_platform=bool(data.get("flexible_platform", False)),
            timing_mode=timing_mode,
            target_time=data.get("target_time"),
        )
```

---

## 6. RouteExecutionState Specification

### 6.1 Purpose

RouteExecutionState tracks per-train execution progress through a Route. In the current system, `current_waypoint_index` and `direction` live on the Route itself — making it impossible for multiple trains to share one Route. This redesign moves all execution state into RouteExecutionState.

### 6.2 What Changes

| Aspect | Before | After |
|--------|--------|-------|
| `current_waypoint_index` | On `Route` | On `RouteExecutionState` |
| `direction` (+1/-1) | On `Route` | On `RouteExecutionState` |
| `route_start_time` | On `Route` (`started_at`) | Copied to each `RouteExecutionState` at assignment |
| New: `resolved_block_id` | — | Actual target block after flexible platform resolution |

### 6.3 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `train_id` | `str` | — | Train being tracked |
| `route_id` | `str` | — | Route being executed |
| `current_waypoint_index` | `int` | `0` | Current position in waypoints list |
| `direction` | `int` | `1` | `+1` forward, `-1` reverse (for ROUND_TRIP) |
| `route_start_time` | `Optional[float]` | `None` | When route was activated for this train |
| `arrived_at_station_time` | `Optional[float]` | `None` | When train arrived at current waypoint |
| `waiting_for_path` | `bool` | `False` | Whether waiting for path reservation |
| `last_waypoint_block_id` | `Optional[str]` | `None` | Last block we visited |
| `resolved_block_id` | `Optional[str]` | `None` | Actual target block after flexible platform resolution |

### 6.4 Class Definition

```python
@dataclass
class RouteExecutionState:
    """
    Tracks execution state for a single train's route.

    Each assigned train gets its own RouteExecutionState, enabling multiple
    trains to independently traverse the same Route definition.

    Attributes:
        train_id: Train being tracked
        route_id: Route being executed
        current_waypoint_index: Current position in waypoints list
        direction: +1 forward, -1 reverse (for ROUND_TRIP)
        route_start_time: When route was activated for this train
        arrived_at_station_time: When train arrived at current block
        waiting_for_path: Whether waiting for path reservation
        last_waypoint_block_id: Last block visited
        resolved_block_id: Actual target block after flexible platform resolution
    """

    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1  # +1 forward, -1 reverse (for ROUND_TRIP)
    route_start_time: Optional[float] = None
    arrived_at_station_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_block_id: Optional[str] = None
    resolved_block_id: Optional[str] = None
```

### 6.5 Waypoint Advancement Logic

The waypoint advancement logic that currently lives on `Route.advance_to_next_waypoint()` moves to RouteExecutor, operating on RouteExecutionState:

```python
def _advance_waypoint(
    self, exec_state: RouteExecutionState, route: Route
) -> Optional[Waypoint]:
    """
    Advance to next waypoint based on route mode.

    Updates exec_state.current_waypoint_index and exec_state.direction.

    Args:
        exec_state: Per-train execution state
        route: Route definition

    Returns:
        Next waypoint, or None if route is complete (ONE_WAY only)
    """
    if not route.waypoints:
        return None

    if route.mode == RouteMode.ONE_WAY:
        exec_state.current_waypoint_index += 1
        if exec_state.current_waypoint_index >= len(route.waypoints):
            return None  # Route complete

    elif route.mode == RouteMode.LOOP:
        exec_state.current_waypoint_index += 1
        if exec_state.current_waypoint_index >= len(route.waypoints):
            exec_state.current_waypoint_index = 0  # Wrap around

    elif route.mode == RouteMode.ROUND_TRIP:
        exec_state.current_waypoint_index += exec_state.direction

        # Bounce at ends
        if exec_state.current_waypoint_index >= len(route.waypoints):
            exec_state.direction = -1
            exec_state.current_waypoint_index = len(route.waypoints) - 2
            if exec_state.current_waypoint_index < 0:
                exec_state.current_waypoint_index = 0

        elif exec_state.current_waypoint_index < 0:
            exec_state.direction = 1
            exec_state.current_waypoint_index = 1
            if exec_state.current_waypoint_index >= len(route.waypoints):
                exec_state.current_waypoint_index = 0

    # Clear resolved block for next waypoint
    exec_state.resolved_block_id = None

    return route.get_waypoint_at(exec_state.current_waypoint_index)
```

---

## 7. RouteExecutor Changes

### 7.1 Overview

RouteExecutor undergoes significant changes to support:

1. **Multi-train routes**: Iterate `Route.assigned_train_ids` instead of scanning all trains
2. **Block-based waypoints**: Use `waypoint.block_id` instead of `waypoint.station_id`
3. **Flexible platform**: Resolve best available platform block at departure time
4. **Per-train execution state**: All state reads/writes go through `RouteExecutionState`

### 7.2 Update Loop Changes

```python
# BEFORE (current)
def update(self, delta_time: float) -> None:
    for train in self.network.trains.values():
        if train.assigned_route_id:
            route = routes.get(train.assigned_route_id)
            if route and route.is_active:
                self._execute_route_for_train(train, route)

# AFTER (new)
def update(self, delta_time: float) -> None:
    self.simulation_time += delta_time
    routes = getattr(self.network, "routes", {})

    for route in routes.values():
        if not route.is_active:
            continue
        for train_id in route.assigned_train_ids:
            train = self.network.trains.get(train_id)
            if train:
                self._execute_route_for_train(train, route)
```

### 7.3 Flexible Platform Resolution

When a waypoint has `flexible_platform=True`, the executor resolves the actual target block at departure time:

```python
def _resolve_target_block(
    self, waypoint: Waypoint, exec_state: RouteExecutionState
) -> Optional[str]:
    """
    Resolve the actual target block for a waypoint.

    For non-flexible waypoints, returns waypoint.block_id directly.
    For flexible platform waypoints, finds the best available platform
    block within the same Station.

    Args:
        waypoint: Current waypoint
        exec_state: Per-train execution state

    Returns:
        Target block_id, or None if no block available
    """
    if not waypoint.flexible_platform:
        return waypoint.block_id

    # Already resolved for this waypoint
    if exec_state.resolved_block_id:
        return exec_state.resolved_block_id

    # Find the Platform that owns waypoint.block_id
    platform_id = self._get_platform_for_block(waypoint.block_id)
    if not platform_id:
        # Not a platform block — just use it directly
        return waypoint.block_id

    # Find the Station that contains this Platform
    station_id = self._get_station_for_platform(platform_id)
    if not station_id:
        # Platform not in any station — use original block
        return waypoint.block_id

    station = self.network.stations.get(station_id)
    if not station:
        return waypoint.block_id

    # Get all block IDs across all Platforms in this Station
    all_block_ids = station.get_all_block_ids(self.network)

    # Find available blocks (FREE or RESERVED for this train)
    train_id = exec_state.train_id
    available_blocks = []
    for block_id in all_block_ids:
        block = self.network.blocks.get(block_id)
        if block and (block.is_free() or
                      (block.is_reserved() and block.occupied_by == train_id)):
            available_blocks.append(block_id)

    if not available_blocks:
        # No available blocks — fall back to original
        return waypoint.block_id

    # Prefer the originally specified block if available
    if waypoint.block_id in available_blocks:
        exec_state.resolved_block_id = waypoint.block_id
    else:
        # Use first available
        exec_state.resolved_block_id = available_blocks[0]

    return exec_state.resolved_block_id
```

### 7.4 Arrival Detection Changes

```python
# BEFORE
def _is_train_at_station(self, train, station_id):
    station = self.network.stations.get(station_id)
    target_tile = self._get_target_station_tile(train, station)
    return train.position.head_tile == target_tile

# AFTER
def _is_train_at_block(self, train: "Train", block_id: str) -> bool:
    """
    Check if train's head is within the target block.

    Args:
        train: Train to check
        block_id: Target block ID

    Returns:
        True if train's head tile belongs to the target block
    """
    if not train.position:
        return False

    head_tile = train.position.head_tile
    track = self.network.get_track(*head_tile)
    if not track:
        return False

    return track.block_section_id == block_id
```

### 7.5 ONE_WAY Completion Logic

For `ONE_WAY` mode, the route deactivates when the **last train** assigned to it finishes all waypoints — not just the first:

```python
def _handle_route_complete_for_train(
    self, train: "Train", route: "Route", exec_state: RouteExecutionState
) -> None:
    """
    Handle route completion for a single train.

    For ONE_WAY: removes train from route. If last train, deactivates route.
    """
    controller = self._get_controller(train)

    # Remove train from route
    route.unassign_train(train.train_id)
    train.assigned_route_id = None

    # Clean up execution state
    if train.train_id in self.execution_states:
        del self.execution_states[train.train_id]

    # Transition train state
    controller.depart_from_station(has_more_waypoints=False)

    # Deactivate route if no more trains
    if route.mode == RouteMode.ONE_WAY and not route.assigned_train_ids:
        route.deactivate()
```

---

## 8. API Endpoints

### 8.1 Platform CRUD (Renamed from Station)

All existing station endpoints move to `/api/platform` with identical behavior.

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `POST` | `/api/platform` | Create platform | `{x, y, name?, platform_id?, length_cm?, platform_side?, top_speed_kmh?}` | Platform dict |
| `GET` | `/api/platform/<id>` | Get platform | — | Platform dict with orientation |
| `PATCH` | `/api/platform/<id>` | Update platform | `{name?, length_cm?, platform_side?, top_speed_kmh?}` | Platform dict |
| `DELETE` | `/api/platform/<id>` | Delete platform | — | `{success: true}` |
| `GET` | `/api/platform/validate/<x>/<y>` | Validate placement | — | `{can_place, error?}` |
| `POST` | `/api/platform/<id>/extend` | Extend platform | `{positions: [[x,y],...]}` | Platform dict |
| `POST` | `/api/platform/<id>/remove_tile` | Remove tile | `{x, y}` | Platform dict |
| `POST` | `/api/platform/<id>/can_extend` | Check extendability | `{x, y}` | `{can_extend, reason?}` |
| `POST` | `/api/platform/<id>/assign_tile` | Assign tile to block | `{x, y, block_id}` | `{status, station}` |
| `POST` | `/api/platform/<id>/unassign_tile` | Unassign tile | `{x, y}` | `{status, station}` |
| `GET` | `/api/platform/<id>/block_assignments` | Get block assignments | — | `{assignments, blocks_summary}` |
| `GET` | `/api/platform/<id>/end_tiles` | Get end tiles | — | `{start_tile, end_tile}` |

### 8.2 Station CRUD (New)

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `POST` | `/api/station` | Create station | `{name, station_id?, platform_ids?}` | Station dict |
| `GET` | `/api/station` | List all stations | — | `{stations: [...]}` |
| `GET` | `/api/station/<id>` | Get station | — | Station dict with platform details |
| `PATCH` | `/api/station/<id>` | Update station | `{name?, platform_ids?}` | Station dict |
| `DELETE` | `/api/station/<id>` | Delete station | — | `{success: true}` |
| `POST` | `/api/station/<id>/add-platform` | Add platform | `{platform_id}` | Station dict |
| `POST` | `/api/station/<id>/remove-platform` | Remove platform | `{platform_id}` | Station dict |

#### 8.2.1 Create Station

```
POST /api/station
Content-Type: application/json

{
    "name": "Central Station",
    "station_id": "STA_001",       // optional, auto-generated if omitted
    "platform_ids": ["PLT_001"]    // optional, starts empty if omitted
}

Response 200:
{
    "station_id": "STA_001",
    "name": "Central Station",
    "platform_ids": ["PLT_001"],
    "created_at": "2026-02-06T10:00:00"
}

Error 400:
{
    "error": "Platform PLT_999 not found"
}
```

#### 8.2.2 Get Station (Enriched)

```
GET /api/station/STA_001

Response 200:
{
    "station_id": "STA_001",
    "name": "Central Station",
    "platform_ids": ["PLT_001", "PLT_002"],
    "created_at": "2026-02-06T10:00:00",
    "platforms": [
        {
            "platform_id": "PLT_001",
            "name": "Platform 1",
            "positions": [[5, 10], [6, 10], [7, 10]],
            "block_ids": ["BLK_A"]
        },
        {
            "platform_id": "PLT_002",
            "name": "Platform 2",
            "positions": [[5, 12], [6, 12], [7, 12]],
            "block_ids": ["BLK_B"]
        }
    ],
    "center_position": [6.0, 11.0],
    "all_block_ids": ["BLK_A", "BLK_B"]
}
```

### 8.3 Route CRUD (Changed)

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `GET` | `/api/train-routes` | List all routes | — | `{routes: [...]}` |
| `POST` | `/api/train-routes` | Create route | `{name, mode?, waypoints?}` | Route dict |
| `GET` | `/api/train-routes/<id>` | Get route | — | Route dict |
| `PUT` | `/api/train-routes/<id>` | Update route | `{name?, mode?, waypoints?}` | Route dict |
| `DELETE` | `/api/train-routes/<id>` | Delete route | — | `{success}` |
| `POST` | `/api/train-routes/<id>/activate` | Activate route | — | Route dict |
| `POST` | `/api/train-routes/<id>/deactivate` | Deactivate route | — | Route dict |

#### 8.3.1 Create Route (Changed)

Waypoints now use `block_id` instead of `station_id`:

```
POST /api/train-routes
Content-Type: application/json

{
    "name": "Express Line A",
    "mode": "loop",
    "waypoints": [
        {
            "block_id": "BLK_A",
            "dwell_time_sec": 30,
            "flexible_platform": true
        },
        {
            "block_id": "BLK_C",
            "dwell_time_sec": 0
        },
        {
            "block_id": "BLK_D",
            "dwell_time_sec": 45,
            "flexible_platform": false,
            "timing_mode": "relative",
            "target_time": 120
        }
    ]
}

Response 200:
{
    "status": "success",
    "route": {
        "route_id": "ROUTE-A1B2C3",
        "name": "Express Line A",
        "waypoints": [...],
        "mode": "loop",
        "assigned_train_ids": [],
        "is_active": false,
        "started_at": null,
        "last_error": null,
        "error_time": null
    }
}
```

### 8.4 Train-Route Assignment (Changed)

Route assignment is now bidirectional — both the Route and the Train track the relationship.

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `PUT` | `/api/trains/<train_id>/assign-route` | Assign/unassign route | `{route_id}` or `{route_id: null}` | Train dict |

#### 8.4.1 Assign Route (Changed Behavior)

```
PUT /api/trains/T-001/assign-route
Content-Type: application/json

{
    "route_id": "ROUTE_001"
}

Backend behavior:
1. Validate route exists
2. If train already assigned to another route:
   a. Remove train from old route's assigned_train_ids
3. Set train.assigned_route_id = "ROUTE_001"
4. Add "T-001" to route.assigned_train_ids
5. Create RouteExecutionState for train

Response 200:
{
    "status": "success",
    "train": { ... }
}
```

#### 8.4.2 Force-Assign (Remove from Other Route)

When `force: true` is included, the train is removed from its current route before being assigned to the new one. Without `force`, the endpoint still works but logs a warning if the train was already assigned.

```
PUT /api/trains/T-001/assign-route
Content-Type: application/json

{
    "route_id": "ROUTE_002",
    "force": true
}
```

#### 8.4.3 Unassign Route

```
PUT /api/trains/T-001/assign-route
Content-Type: application/json

{
    "route_id": null
}

Backend behavior:
1. Remove train from current route's assigned_train_ids
2. Set train.assigned_route_id = null
3. Clean up RouteExecutionState
```

### 8.5 Route Execution Status

| Method | URL | Description | Response |
|--------|-----|-------------|----------|
| `GET` | `/api/train-routes/<id>/status` | Get execution status for all assigned trains | See below |

```
GET /api/train-routes/ROUTE_001/status

Response 200:
{
    "route_id": "ROUTE_001",
    "is_active": true,
    "mode": "loop",
    "trains": [
        {
            "train_id": "T-001",
            "current_waypoint_index": 2,
            "direction": 1,
            "current_waypoint_block_id": "BLK_D",
            "resolved_block_id": "BLK_D",
            "at_station": false,
            "waiting_for_path": false,
            "dwell_remaining_sec": null
        },
        {
            "train_id": "T-002",
            "current_waypoint_index": 0,
            "direction": 1,
            "current_waypoint_block_id": "BLK_A",
            "resolved_block_id": "BLK_B",
            "at_station": true,
            "waiting_for_path": false,
            "dwell_remaining_sec": 15.3
        }
    ]
}
```

### 8.6 Viability Check

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `POST` | `/api/train-routes/check-viability` | Check path viability between blocks | `{pairs: [[from, to], ...]}` | Viability results |

```
POST /api/train-routes/check-viability
Content-Type: application/json

{
    "pairs": [
        ["BLK_A", "BLK_C"],
        ["BLK_C", "BLK_D"],
        ["BLK_D", "BLK_A"]
    ]
}

Response 200:
{
    "results": [
        {"from": "BLK_A", "to": "BLK_C", "viable": true, "hop_count": 3},
        {"from": "BLK_C", "to": "BLK_D", "viable": true, "hop_count": 2},
        {"from": "BLK_D", "to": "BLK_A", "viable": false, "error": "No path found"}
    ]
}
```

### 8.7 Train Waypoint Override

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| `POST` | `/api/trains/<id>/set-waypoint` | Set train's current waypoint index | `{waypoint_index}` | Execution status |

```
POST /api/trains/T-001/set-waypoint
Content-Type: application/json

{
    "waypoint_index": 3
}

Response 200:
{
    "status": "success",
    "train_id": "T-001",
    "current_waypoint_index": 3,
    "waypoint_block_id": "BLK_D"
}
```

---

## 9. Route Editor UI Specification

### 9.1 Overview

The Route Editor is a **new secondary sidebar panel** for creating and managing routes visually. There is currently NO route editing UI — this is entirely new.

### 9.2 Panel Layout

```
┌─────────────────────────────────────┐
│  Route Editor                    ✕  │
├─────────────────────────────────────┤
│                                     │
│  Route Name: [Express Line A    ]   │
│                                     │
│  Mode: [ONE_WAY ▾]                  │
│                                     │
│  ─── Waypoints ─────────────────── │
│                                     │
│  1. ■ BLK_A (Platform 1)     [✕]   │
│     🔀 Flexible Platform [✓]        │
│     ⏱ Dwell: [30] sec               │
│        ✅ → viable                   │
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

### 9.3 Component Description

#### 9.3.1 Route Name Field

- Editable text input
- Changes saved on blur or Enter
- Sends `PUT /api/train-routes/<id>` with `{name: ...}`

#### 9.3.2 Mode Selector

- Dropdown: `ONE_WAY`, `LOOP`, `ROUND_TRIP`
- Changes saved immediately
- Sends `PUT /api/train-routes/<id>` with `{mode: ...}`

#### 9.3.3 Waypoint List

Each waypoint row shows:

| Element | Description |
|---------|-------------|
| **Index** | 1-based position number |
| **Block indicator** | Colored square matching block color |
| **Block ID + label** | Block ID and platform name if applicable |
| **Delete button** | `[✕]` removes waypoint |
| **Reorder** | Drag handles or ▲/▼ buttons to reorder |
| **Flexible Platform toggle** | Only shown for platform blocks; checkbox |
| **Dwell time input** | Number input in seconds |
| **Viability indicator** | ✅ (green) or ❌ (red) between consecutive waypoints |

#### 9.3.4 Add Waypoint

- When Route Editor is open, clicking a block on the canvas adds a waypoint
- The editor enters "add waypoint" mode (cursor changes to crosshair)
- Clicking a tile that belongs to a block adds `{block_id: <block>}` to the waypoint list
- After adding, viability is immediately checked between the new waypoint and its neighbors

#### 9.3.5 Viability Indicators

Between each pair of consecutive waypoints, a viability indicator shows whether a path exists:

```
Waypoint A (BLK_001)
   ✅ → path exists (3 blocks)
Waypoint B (BLK_005)
   ❌ → no path found
Waypoint C (BLK_009)
```

**Implementation:**
1. On waypoint add/reorder/delete, collect all consecutive pairs
2. Send `POST /api/train-routes/check-viability` with pairs
3. Display results inline
4. Non-blocking: UI remains responsive during check

#### 9.3.6 Train Assignment Panel

| Element | Description |
|---------|-------------|
| **Assigned train list** | Shows each assigned train with current waypoint progress |
| **Unassign button** | Per-train button to remove from route |
| **Assign dropdown** | Dropdown of unassigned trains |
| **Assign button** | Assigns selected train to route |
| **Force checkbox** | If checked, removes train from current route before assigning |
| **Waypoint override** | Click on a train's "WP: X/Y" to change its current waypoint |

#### 9.3.7 Controls

| Button | Action |
|--------|--------|
| **Activate** | `POST /api/train-routes/<id>/activate` |
| **Deactivate** | `POST /api/train-routes/<id>/deactivate` |
| **Save** | `PUT /api/train-routes/<id>` with full waypoint list |
| **Delete** | `DELETE /api/train-routes/<id>` (with confirmation dialog) |

### 9.4 State Management

The Route Editor maintains local state for the currently edited route:

```javascript
class RouteEditor {
    constructor() {
        this.currentRouteId = null;
        this.routeData = null;        // Cached route definition
        this.viabilityResults = {};    // {pair_key: {viable, error}}
        this.isAddingWaypoint = false; // True when waiting for block click
        this.pendingViabilityCheck = null; // Debounced check timeout
    }
}
```

### 9.5 Interaction Flow: Creating a Route

```
1. User clicks "New Route" button in sidebar
2. POST /api/train-routes with empty waypoints → get route_id
3. Route Editor panel opens with empty waypoint list
4. User enters route name
5. User clicks on canvas tiles to add waypoints:
   a. Tile click → find block_id for that tile
   b. Add waypoint to list
   c. If block is a platform block → show flexible_platform toggle
   d. Trigger viability check for affected pairs
6. User adjusts dwell times, flexible_platform flags
7. User selects mode (ONE_WAY, LOOP, ROUND_TRIP)
8. User assigns trains from dropdown
9. User clicks Activate
```

### 9.6 Interaction Flow: Editing an Existing Route

```
1. User clicks on existing route in route list
2. GET /api/train-routes/<id> → populate editor
3. GET /api/train-routes/<id>/status → show train progress
4. User modifies waypoints, name, mode
5. Changes are saved immediately (or on Save button)
6. Viability is rechecked on waypoint changes
```

---

## 10. Migration Plan

### 10.1 Rename: Station → Platform

This is the most pervasive change. Every file that references `Station`, `station`, `station_id`, etc. must be updated.

#### 10.1.1 Backend Rename Steps

1. **Rename `station.py` → `platform.py`**
   - Class `Station` → `Platform`
   - All `station_id` → `platform_id`
   - All `station.*` → `platform.*`

2. **Update `network.py`**
   - `self.stations` → `self.platforms` (the dict of Platform entities)
   - `self.stations` = new Station dict (grouping entities)
   - All methods: `add_station()` → `add_platform()`, etc.
   - Import changes

3. **Update `track.py`**
   - `TrackSegment.station_id` → `TrackSegment.platform_id`

4. **Update `routes/stations.py` → `routes/platforms.py`**
   - All endpoints `/api/station` → `/api/platform`
   - All `station` references → `platform`

5. **Create new `routes/stations.py`**
   - New Station CRUD endpoints at `/api/station`

6. **Update `route_executor.py`**
   - `self.network.stations` → `self.network.platforms` (for Platform lookups)
   - Add `self.network.stations` usage for Station lookups
   - `waypoint.station_id` → `waypoint.block_id`

7. **Update `train_route.py`**
   - `Waypoint.station_id` → `Waypoint.block_id`
   - Add `flexible_platform` field
   - Remove execution state from `Route`
   - Add `assigned_train_ids` to `Route`

8. **Update `routes/misc.py`**
   - Route CRUD: `station_id` → `block_id` in waypoint handling

9. **Update all other backend files** that reference `station`:
   - `persistence.py` — save/load migration
   - `demo_main.py` — demo data
   - `simulation.py` — if any station references

#### 10.1.2 Frontend Rename Steps

1. **Update `static/js/editor-stations.js`**
   - Rename to `editor-platforms.js`
   - All API calls: `/api/station` → `/api/platform`
   - All variable names: `station` → `platform`

2. **Create new `static/js/editor-stations.js`**
   - New Station grouping editor

3. **Update `static/js/renderer-stations.js`**
   - Rename to `renderer-platforms.js`
   - Update rendering code

4. **Update `static/js/api.js`**
   - `createStation()` → `createPlatform()`
   - Add new `createStation()` for grouping
   - All station API methods renamed

5. **Update `static/js/editor.js`, `app.js`, etc.**
   - All references updated

6. **Update `index.html`**
   - Script tags updated for renamed files

### 10.2 Data Migration (Persistence)

Saved JSON layouts need backward-compatible loading:

```python
# In persistence.py

def load_network(filepath: str) -> RailwayNetwork:
    data = json.load(open(filepath))

    # Migration: old "stations" → new "platforms"
    if "stations" in data and "platforms" not in data:
        data["platforms"] = data.pop("stations")
        # Rename station_id → platform_id in each entry
        for entry in data["platforms"]:
            if "station_id" in entry and "platform_id" not in entry:
                entry["platform_id"] = entry.pop("station_id")

    # Migration: old waypoint station_id → block_id
    for route_data in data.get("routes", []):
        for wp in route_data.get("waypoints", []):
            if "station_id" in wp and "block_id" not in wp:
                # Need to resolve station_id to block_id
                # This requires looking up the platform to find its block
                wp["block_id"] = wp.pop("station_id")
                # Note: station_id was actually a platform reference
                # The block_id will need resolution at load time

    # Migration: old Route execution state
    for route_data in data.get("routes", []):
        # Remove execution state fields (now per-train)
        route_data.pop("current_waypoint_index", None)
        route_data.pop("direction", None)

    # Migration: track station_id → platform_id
    for track_data in data.get("tracks", {}).values():
        if "station_id" in track_data and "platform_id" not in track_data:
            track_data["platform_id"] = track_data.pop("station_id")
```

### 10.3 Waypoint station_id → block_id Resolution

During migration, old waypoints that reference `station_id` (actually a Platform ID) need to be resolved to `block_id`:

```python
def _resolve_platform_to_block(
    self, platform_id: str, network: RailwayNetwork
) -> Optional[str]:
    """
    Resolve a platform_id to its primary block_id for waypoint migration.

    Strategy: Use the block that covers the first tile of the platform.
    """
    platform = network.platforms.get(platform_id)
    if not platform or not platform.positions:
        return None

    # Use first tile's block
    first_pos = platform.positions[0]
    track = network.get_track(*first_pos)
    if track and track.block_section_id:
        return track.block_section_id

    return None
```

---

## 11. File Changes Summary

### 11.1 New Files

| File | Description |
|------|-------------|
| `platform.py` | Platform class (renamed from `station.py`) |
| `station.py` | New Station class (grouping entity) |
| `routes/platforms.py` | Platform CRUD endpoints (renamed from `routes/stations.py`) |
| `routes/stations.py` | New Station CRUD endpoints |
| `static/js/editor-platforms.js` | Platform editor (renamed from `editor-stations.js`) |
| `static/js/editor-stations.js` | New Station grouping editor |
| `static/js/editor-routes.js` | Route editor UI (currently this file is the path editor — rename that first) |
| `static/js/renderer-platforms.js` | Platform renderer (renamed from `renderer-stations.js`) |

### 11.2 Modified Files

| File | Changes |
|------|---------|
| `network.py` | `self.stations` → `self.platforms`, new `self.stations` for grouping. All station methods renamed to platform. New station grouping methods. |
| `train_route.py` | `Waypoint.station_id` → `block_id`, add `flexible_platform`. `Route` loses execution state, gains `assigned_train_ids`. |
| `route_executor.py` | `RouteExecutionState` gains `current_waypoint_index`, `direction`, `route_start_time`, `resolved_block_id`. All station lookups → block/platform lookups. Flexible platform resolution. Multi-train iteration. |
| `track.py` | `TrackSegment.station_id` → `TrackSegment.platform_id` |
| `routes/misc.py` | Route CRUD uses `block_id` in waypoints, supports `assigned_train_ids`. |
| `routes/trains.py` | `assign-route` endpoint updates both `Train.assigned_route_id` and `Route.assigned_train_ids`. Add `set-waypoint` endpoint. |
| `persistence.py` | Migration logic for old saves. Platform/station rename handling. |
| `enums.py` | No changes expected unless new enums needed. |
| `simulation.py` | Update any station references to platform. |
| `demo_main.py` | Update demo data to use new Platform/Station/Waypoint format. |
| `static/js/api.js` | Rename station API → platform API. Add station grouping API. Add route viability API. |
| `static/js/app.js` | Update station references. Add route editor initialization. |
| `static/js/editor.js` | Update station references to platform. Add route editor panel trigger. |
| `static/js/renderer-stations.js` | Rename to `renderer-platforms.js`. Add station name label rendering. |
| `static/js/constants.js` | May need new color constants for route/station rendering. |
| `static/js/train-monitor.js` | Update station references in monitor display. |
| `index.html` | Update script tags for renamed/new JS files. |
| `static/css/style.css` | Add styles for route editor panel components. |

### 11.3 Test File Changes

| File | Changes |
|------|---------|
| `tests/test_backend.py` | Update all station tests to platform. Add Station grouping tests. |
| `tests/test_stations.py` | Rename to `test_platforms.py`, update all references. Add new `test_stations.py` for grouping. |
| `tests/test_trains.py` | Update route assignment tests for multi-train routes. |
| `tests/test_route_execution.py` | Major updates: test per-train execution state, multi-train routes, block-based waypoints, flexible platform. |
| `tests/test_dispatcher.py` | Add viability check tests. |
| `tests/test_javascript_syntax.py` | Update expected class names for renamed JS files. |
| `tests/test_app_smoke.py` | Update expected file list. |
| `tests/test_api_routes.py` | Update station → platform endpoints. Add station grouping endpoint tests. |

### 11.4 Unchanged Files

These files should not require changes:

| File | Reason |
|------|--------|
| `block.py` | Block entity is unchanged |
| `block_manager.py` | Block management is unchanged |
| `block_validation.py` | Block validation is unchanged |
| `block_power.py` | Block power is unchanged |
| `railway_signal.py` | Signal system is unchanged |
| `signal_validation.py` | Signal validation is unchanged |
| `switch.py` | Switch entity is unchanged |
| `train_controller.py` | Train control logic is unchanged |
| `train_physics.py` | Train physics is unchanged |
| `train_state_manager.py` | Train state management is unchanged |
| `pathfinder.py` | Pathfinding algorithms unchanged |
| `path.py` | Path entities unchanged |
| `path_utils.py` | Path utilities unchanged |
| `model_scale.py` | Scale config unchanged |
| `speed_profile.py` | Speed profiles unchanged |

### 11.5 Implementation Order

The recommended implementation order minimizes risk and ensures testability at each step:

| Phase | Description | Files | Tests |
|-------|-------------|-------|-------|
| **Phase 1** | Platform rename (backend only) | `station.py` → `platform.py`, `network.py`, `track.py`, `routes/stations.py` → `routes/platforms.py` | All existing station tests pass under new names |
| **Phase 2** | New Station entity | New `station.py`, new `routes/stations.py`, `network.py` | New Station CRUD tests |
| **Phase 3** | Waypoint block_id change | `train_route.py`, `routes/misc.py` | Waypoint serialization tests, migration tests |
| **Phase 4** | Route multi-train + execution state | `train_route.py`, `route_executor.py` | Multi-train route tests, execution state tests |
| **Phase 5** | Flexible platform | `route_executor.py` | Flexible platform resolution tests |
| **Phase 6** | Platform rename (frontend) | All JS files, `index.html` | JavaScript syntax tests |
| **Phase 7** | Route Editor UI | New `editor-routes.js`, `api.js`, `editor.js`, `style.css` | Manual testing + syntax tests |
| **Phase 8** | Viability checking | `routes/misc.py`, Route Editor UI | API viability tests |
| **Phase 9** | Data migration | `persistence.py` | Migration tests with old save files |
| **Phase 10** | Demo data update | `demo_main.py` | Demo runs without errors |

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
    │  ────────────    │   │ ────────── │  │ ──────── │        │  ──────────  │
    │  platform_id     │◄──│ platform_  │  │ route_id │        │  train_id    │
    │  name            │   │   ids[]    │  │ name     │◄───────│  assigned_   │
    │  positions[]     │   │ station_id │  │ waypoints│        │  route_id    │
    │  block_assign    │   │ name       │  │ mode     │        │  position    │
    │  orientation     │   └────────────┘  │ assigned_│────────►  state       │
    │  platform_side   │                   │ train_ids│        └──────────────┘
    └────────┬─────────┘                   └────┬─────┘
             │                                  │
             │ tiles belong to                  │ waypoints target
             ▼                                  ▼
    ┌──────────────────┐              ┌──────────────────┐
    │     Block        │◄─────────────│    Waypoint      │
    │  ────────────    │   block_id   │  ────────────    │
    │  block_id        │              │  block_id        │
    │  state           │              │  dwell_time_sec  │
    │  occupied_by     │              │  flexible_plat.  │
    │  length_cm       │              │  timing_mode     │
    └──────────────────┘              └──────────────────┘
```

## Appendix B: Migration Compatibility Matrix

| Old Field / Structure | New Location | Migration Strategy |
|-----------------------|--------------|--------------------|
| `Station` class | `Platform` class in `platform.py` | Rename; all fields preserved |
| `network.stations` dict | `network.platforms` dict | Rename key in persistence |
| `track.station_id` | `track.platform_id` | Rename field |
| `Waypoint.station_id` | `Waypoint.block_id` | Resolve to block via platform lookup |
| `Route.current_waypoint_index` | `RouteExecutionState.current_waypoint_index` | Dropped from Route; recreated per-train at runtime |
| `Route.direction` | `RouteExecutionState.direction` | Dropped from Route; recreated per-train at runtime |
| `/api/station` endpoints | `/api/platform` endpoints | URL change; behavior identical |
| `editor-stations.js` | `editor-platforms.js` | Rename; behavior identical |
| `renderer-stations.js` | `renderer-platforms.js` | Rename; behavior identical |

## Appendix C: Editor Conflict — `editor-routes.js`

The current `static/js/editor-routes.js` is a **path editor** for infrastructure reservations (dispatcher paths), NOT a route editor. This creates a naming conflict.

**Resolution:**
1. Rename current `editor-routes.js` → `editor-paths.js` (reflects actual purpose)
2. New `editor-routes.js` = the Route Editor UI specified in §9
3. Update `index.html` script tags accordingly
4. Update any references in `editor.js` and `app.js`
