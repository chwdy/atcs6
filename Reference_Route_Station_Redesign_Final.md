# Route, Station & Platform System Redesign — Final Technical Specification

> **Document Type:** Technical Implementation Specification  
> **Version:** 1.0 (Final)  
> **Created:** February 2026  
> **Status:** Design Complete — Implementation Ready  
> **Cross-Reference:** See [`docs/plans/2026-02-06-route-station-redesign-implementation.md`](docs/plans/2026-02-06-route-station-redesign-implementation.md) for implementation phases

---

## Table of Contents

1. [Overview & Core Concepts](#1-overview--core-concepts)
2. [Platform Entity Specification](#2-platform-entity-specification)
3. [Station Entity Specification](#3-station-entity-specification)
4. [Waypoint Specification](#4-waypoint-specification)
5. [Route Entity Specification](#5-route-entity-specification)
6. [RouteExecutionState Specification](#6-routeexecutionstate-specification)
7. [RouteExecutor Specification](#7-routeexecutor-specification)
8. [API Endpoints](#8-api-endpoints)
9. [Route Editor UI Specification](#9-route-editor-ui-specification)
10. [Migration Plan](#10-migration-plan)
11. [File Changes Summary](#11-file-changes-summary)

---

## 1. Overview & Core Concepts

### 1.1 Purpose

This document specifies the final design for the Route, Station, and Platform system redesign, incorporating decisions from all iteration documents:

- **1770188561738-quiet-orchid.md**: Bug fix stabilization (incorporated testing requirements)
- **1770407583141-hidden-star.md**: Initial route/schedule editor spec
- **1770414040422-neon-lagoon.md**: 10-phase roadmap with Station→Platform rename
- **1770415262616-eager-nebula.md**: Final consolidated design with platform-first approach

### 1.2 Core Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Waypoint target** | `platform_id` (platform-first) | Clear semantic meaning; natural flexibility within stations |
| **Platform** | Renamed from Station | Physical entity with tiles and blocks |
| **Station** | New grouping entity | Logical collection of Platform IDs; no physical tiles |
| **Multi-train routes** | Supported via `assigned_train_ids` | Single route definition, per-train execution |
| **Execution state** | In `RouteExecutionState` per-train | Enables independent train progress on same route |
| **Flexible platform** | Direction/side compatibility filters | Safe platform alternatives within station |
| **Unassign behavior** | Temporary one-off auto-park route | Train safely reaches nearest platform |

### 1.3 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ROUTE / STATION SYSTEM                              │
│                                                                             │
│  DEFINITION LAYER (persisted in network)                                    │
│  ─────────────────────────────────────────                                  │
│                                                                             │
│  ┌─────────────┐       ┌─────────────┐       ┌─────────────────────┐       │
│  │   Station    │──────►│  Platform   │──────►│   Block             │       │
│  │  (grouping)  │ 1..N  │  (physical) │       │  (track section)    │       │
│  │             │       │             │       │                     │       │
│  │  station_id  │       │  platform_id│       │  block_id           │       │
│  │  platform_ids│       │  positions[]│       │  state              │       │
│  └─────────────┘       │  block_assign│       └─────────────────────┘       │
│                        └─────────────┘                ▲                     │
│                                                       │ references          │
│  ┌────────────────────────────────────────────────────┤                     │
│  │                        Route                        │                     │
│  │  ─────────────────────────────────────────────────  │                     │
│  │  route_id, name, mode, assigned_train_ids: []       │                     │
│  │                                                     │                     │
│  │  waypoints: [Waypoint, ...]  ← targets platform_id  │                     │
│  │                                                     │                     │
│  │  Waypoint: platform_id, flexible_platform, ...      │                     │
│  └─────────────────────────────────────────────────────┘                     │
│                                                                             │
│  EXECUTION LAYER (runtime only)                                             │
│  ──────────────────────────────                                             │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                       RouteExecutor                                  │   │
│  │  ─────────────────────────────────────────────────────────────────  │   │
│  │  execution_states: Dict[str, RouteExecutionState]                    │   │
│  │                                                                     │   │
│  │  Per-train state: current_waypoint_index, direction,                 │   │
│  │                   resolved_platform_id, route_start_time            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Terminology

| Term | Definition |
|------|------------|
| **Platform** | Physical multi-tile entity representing a railway platform (renamed from `Station`). Has positions, block assignments, orientation, platform_side. |
| **Station** | Logical grouping of one or more Platforms under a single name. No physical tiles. |
| **Route** | Train's travel plan — a sequence of Waypoints defining where to go. Shared definition, per-train execution. |
| **Waypoint** | A single stop or pass-through point in a Route. Targets a `platform_id`. |
| **Path** | Dispatcher's reserved route through blocks — infrastructure reservation. Not a Route. |
| **Block** | Electrically isolated track section. The fundamental unit of navigation. |
| **Flexible Platform** | When enabled on a Waypoint, the executor may route to any compatible platform within the same Station. |

---

## 2. Platform Entity Specification

### 2.1 Purpose

Platform is the **renamed** `Station` class. It represents a physical multi-tile railway platform on the track. All existing functionality is preserved — this is a name change with the entity remaining physical.

### 2.2 Rename Mapping

| Aspect | Before | After |
|--------|--------|-------|
| Class name | `Station` | `Platform` |
| File name | `station.py` | `platform.py` |
| Network dict | `network.stations` (physical) | `network.platforms` |
| API base URL | `/api/station` | `/api/platform` |
| Track reference | `track.station_id` | `track.platform_id` |
| Route file | `routes/stations.py` | `routes/platforms.py` |
| JS references | `station`, `stationId` | `platform`, `platformId` |

### 2.3 Properties (Unchanged from Current Station)

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

### 2.4 Class Definition

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

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "platform_id": self.platform_id,
            "name": self.name,
            "positions": [list(pos) for pos in self.positions],
            "length_cm": self.length_cm,
            "platform_side": self.platform_side.value,
            "top_speed_kmh": self.top_speed_kmh,
            "created_at": self.created_at,
            "initial_orientation": self.initial_orientation,
            "block_assignments": {f"{x},{y}": bid for (x, y), bid in self.block_assignments.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Platform":
        """Reconstruct from dictionary after deserialization."""
        # Handle migration: station_id -> platform_id
        platform_id = data.get("platform_id") or data.get("station_id", "")
        
        block_assignments = {}
        for key, bid in data.get("block_assignments", {}).items():
            x, y = map(int, key.split(","))
            block_assignments[(x, y)] = bid
        
        return cls(
            platform_id=platform_id,
            name=data.get("name", ""),
            positions=[tuple(pos) for pos in data.get("positions", [])],
            length_cm=float(data.get("length_cm", 0.0)),
            platform_side=PlatformSide(data.get("platform_side", "both")),
            top_speed_kmh=data.get("top_speed_kmh"),
            created_at=data.get("created_at"),
            initial_orientation=data.get("initial_orientation"),
            block_assignments=block_assignments,
        )

    def add_position(self, x: int, y: int) -> None:
        """Add tile to platform."""
        if (x, y) not in self.positions:
            self.positions.append((x, y))

    def remove_position(self, x: int, y: int) -> bool:
        """Remove tile from platform."""
        if (x, y) in self.positions:
            self.positions.remove((x, y))
            self.block_assignments.pop((x, y), None)
            return True
        return False

    def contains_position(self, x: int, y: int) -> bool:
        """Check if tile belongs to platform."""
        return (x, y) in self.positions

    def get_all_assigned_blocks(self) -> Set[str]:
        """Get all block IDs assigned to this platform."""
        return set(self.block_assignments.values())

    def assign_tile_to_block(self, x: int, y: int, block_id: str) -> bool:
        """Assign tile to block."""
        if (x, y) not in self.positions:
            return False
        self.block_assignments[(x, y)] = block_id
        return True

    def get_block_for_tile(self, x: int, y: int) -> Optional[str]:
        """Get block for specific tile."""
        return self.block_assignments.get((x, y))
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

    def get_platforms_with_details(self, network: "RailwayNetwork") -> List[Dict[str, Any]]:
        """
        Get detailed information about all Platforms in this Station.

        Args:
            network: Railway network for Platform lookup

        Returns:
            List of Platform detail dictionaries
        """
        platforms = []
        for platform_id in self.platform_ids:
            platform = network.platforms.get(platform_id)
            if platform:
                platforms.append({
                    "platform_id": platform.platform_id,
                    "name": platform.name,
                    "positions": [list(pos) for pos in platform.positions],
                    "block_ids": list(platform.get_all_assigned_blocks()),
                    "platform_side": platform.platform_side.value,
                })
        return platforms

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

self.platforms: Dict[str, Platform] = {}   # Renamed from self.stations (physical)
self.stations: Dict[str, Station] = {}     # NEW — grouping entities (logical)

# Platform management methods
def add_platform(self, platform: Platform) -> None:
    self.platforms[platform.platform_id] = platform

def get_platform(self, platform_id: str) -> Optional[Platform]:
    return self.platforms.get(platform_id)

def remove_platform(self, platform_id: str) -> bool:
    """Remove platform and clean up station references."""
    if platform_id not in self.platforms:
        return False
    
    # Remove from any stations
    for station in self.stations.values():
        station.remove_platform(platform_id)
    
    del self.platforms[platform_id]
    return True

# Station management methods
def add_station(self, station: Station) -> None:
    self.stations[station.station_id] = station

def get_station(self, station_id: str) -> Optional[Station]:
    return self.stations.get(station_id)

def get_station_for_platform(self, platform_id: str) -> Optional[str]:
    """Get Station ID that contains this Platform, if any."""
    for station in self.stations.values():
        if station.has_platform(platform_id):
            return station.station_id
    return None

def get_platforms_for_station(self, station_id: str) -> List[Platform]:
    """Get all Platform objects belonging to a Station."""
    station = self.stations.get(station_id)
    if not station:
        return []
    return [self.platforms[pid] for pid in station.platform_ids if pid in self.platforms]
```

### 3.5 Station ↔ Platform Relationship Diagram

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

---

## 4. Waypoint Specification

### 4.1 Purpose

Waypoint represents a single stop or pass-through point in a Route. The key change is targeting `platform_id` instead of `station_id`, with support for flexible platform routing.

### 4.2 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `platform_id` | `str` | — | Target platform ID (required) |
| `dwell_time_sec` | `float` | `30.0` | Stop duration in seconds (0 for pass-through) |
| `flexible_platform` | `bool` | `False` | If true, executor may route to any compatible platform in the same Station |
| `required_platform_side` | `PlatformSide` | `EITHER` | Side constraint for flexible selection: `LEFT`, `RIGHT`, `BOTH`, `EITHER` |
| `travel_direction_policy` | `TravelDirectionPolicy` | `EITHER` | Approach direction constraint: `PRESERVE`, `EITHER` |
| `timing_mode` | `TimingMode` | `NONE` | How timing is applied: `NONE`, `RELATIVE`, `ABSOLUTE` |
| `target_time` | `Optional[float]` | `None` | For RELATIVE/ABSOLUTE modes (seconds) |

### 4.3 New Enums

```python
from enum import Enum

class PlatformSide(Enum):
    """Which side of the train has the platform."""
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"
    EITHER = "either"  # For flexible platform selection

class TravelDirectionPolicy(Enum):
    """Policy for train approach direction to platform."""
    PRESERVE = "preserve"  # Must approach from same direction as previous
    EITHER = "either"      # Can approach from any direction
```

### 4.4 Class Definition

```python
# train_route.py

@dataclass
class Waypoint:
    """
    Single stop or pass-through point in a train's Route.

    Targets a platform_id rather than a block_id or station_id. If the platform
    belongs to a Station and flexible_platform is enabled, the executor may
    route to alternative platforms within the same station.

    Attributes:
        platform_id: Target platform ID
        dwell_time_sec: Stop duration in seconds (0 for pass-through)
        flexible_platform: Allow routing to any compatible platform in same Station
        required_platform_side: Side constraint for flexible selection
        travel_direction_policy: Approach direction constraint
        timing_mode: How timing is applied (NONE, RELATIVE, ABSOLUTE)
        target_time: For RELATIVE/ABSOLUTE modes (seconds)
    """

    platform_id: str
    dwell_time_sec: float = 30.0
    flexible_platform: bool = False
    required_platform_side: PlatformSide = PlatformSide.EITHER
    travel_direction_policy: TravelDirectionPolicy = TravelDirectionPolicy.EITHER
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None

    def should_depart(
        self, 
        time_at_platform: float, 
        route_start_time: float, 
        simulation_time: float
    ) -> bool:
        """
        Determine if train should depart based on timing mode.

        Args:
            time_at_platform: Seconds since train arrived at this platform
            route_start_time: Simulation time when route was activated
            simulation_time: Current simulation time

        Returns:
            True if train should depart, False if it should wait
        """
        if self.timing_mode == TimingMode.NONE:
            return time_at_platform >= self.dwell_time_sec

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return time_at_platform >= self.dwell_time_sec
            elapsed = simulation_time - route_start_time
            return elapsed >= self.target_time

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return time_at_platform >= self.dwell_time_sec
            return simulation_time >= self.target_time

        return False

    def get_remaining_dwell_time(
        self, 
        time_at_platform: float, 
        route_start_time: float, 
        simulation_time: float
    ) -> float:
        """Get remaining time before departure."""
        if self.timing_mode == TimingMode.NONE:
            return max(0.0, self.dwell_time_sec - time_at_platform)

        elif self.timing_mode == TimingMode.RELATIVE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_platform)
            elapsed = simulation_time - route_start_time
            return max(0.0, self.target_time - elapsed)

        elif self.timing_mode == TimingMode.ABSOLUTE:
            if self.target_time is None:
                return max(0.0, self.dwell_time_sec - time_at_platform)
            return max(0.0, self.target_time - simulation_time)

        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "platform_id": self.platform_id,
            "dwell_time_sec": self.dwell_time_sec,
            "flexible_platform": self.flexible_platform,
            "required_platform_side": self.required_platform_side.value,
            "travel_direction_policy": self.travel_direction_policy.value,
            "timing_mode": self.timing_mode.value,
            "target_time": self.target_time,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Waypoint":
        """Reconstruct from dictionary after deserialization."""
        # Migration: accept platform_id and legacy station_id
        platform_id = data.get("platform_id", "")
        if not platform_id:
            platform_id = data.get("station_id", "")  # Legacy fallback

        return cls(
            platform_id=platform_id,
            dwell_time_sec=float(data.get("dwell_time_sec", 30.0)),
            flexible_platform=bool(data.get("flexible_platform", False)),
            required_platform_side=PlatformSide(
                data.get("required_platform_side", "either")
            ),
            travel_direction_policy=TravelDirectionPolicy(
                data.get("travel_direction_policy", "either")
            ),
            timing_mode=TimingMode(data.get("timing_mode", "none")),
            target_time=data.get("target_time"),
        )
```

---

## 5. Route Entity Specification

### 5.1 Purpose

Route defines a train's travel plan — a sequence of Waypoints. In this redesign, Route becomes a **shared definition** that multiple trains can follow simultaneously, each with their own independent execution state.

### 5.2 Changes from Current Route

| Aspect | Before | After |
|--------|--------|-------|
| Execution state on Route | `current_waypoint_index`, `direction` | Removed — moved to `RouteExecutionState` |
| Train assignment | Single `Train.assigned_route_id` | Route has `assigned_train_ids: List[str]`; Train keeps `assigned_route_id` (bidirectional) |
| Waypoint target | `station_id` | `platform_id` |
| Route modes | ONE_WAY, LOOP, ROUND_TRIP | one_off, circular, round_trip (naming) |

### 5.3 Properties

#### Definition Properties (Persisted)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `route_id` | `str` | — | Unique identifier (e.g., `"ROUTE_001"`) |
| `name` | `str` | `""` | Display name (e.g., `"Express Line A"`) |
| `waypoints` | `List[Waypoint]` | `[]` | Ordered list of stops |
| `mode` | `RouteMode` | `ONE_OFF` | Traversal mode: `ONE_OFF`, `CIRCULAR`, `ROUND_TRIP` |
| `is_active` | `bool` | `False` | Whether route is being executed |
| `started_at` | `Optional[float]` | `None` | Simulation time when activated |

#### Assignment Properties (Persisted)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `assigned_train_ids` | `List[str]` | `[]` | Train IDs currently assigned to this route |

#### Error Tracking (Runtime)

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `last_error` | `Optional[str]` | `None` | Last error message |
| `error_time` | `Optional[float]` | `None` | When error occurred |

### 5.4 Class Definition

```python
# train_route.py

class RouteMode(Enum):
    """How a route behaves during execution."""
    ONE_OFF = "one_off"           # Run once, then deactivate
    CIRCULAR = "circular"         # Continuously cycle through waypoints
    ROUND_TRIP = "round_trip"     # Go to end, then reverse


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
        mode: How to traverse waypoints (ONE_OFF, CIRCULAR, ROUND_TRIP)
        assigned_train_ids: Trains currently assigned to this route
        is_active: Whether route is being executed
        started_at: Simulation time when activated
        last_error: Last error message
        error_time: When error occurred
    """

    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_OFF

    # Assignment
    assigned_train_ids: List[str] = field(default_factory=list)

    # Activation state
    is_active: bool = False
    started_at: Optional[float] = None

    # Error tracking
    last_error: Optional[str] = None
    error_time: Optional[float] = None

    # ── Definition Methods ─────────────────────────────────────────

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

    def get_assigned_train_count(self) -> int:
        """Get number of trains assigned to this route."""
        return len(self.assigned_train_ids)

    # ── Progress (requires external waypoint_index) ─────────────────

    def get_progress(self, waypoint_index: int) -> float:
        """
        Get route progress as fraction (0.0 to 1.0) for ONE_OFF mode.

        Args:
            waypoint_index: Current waypoint index (from RouteExecutionState)

        Returns:
            Progress fraction (0.0 = start, 1.0 = complete)
        """
        if self.mode != RouteMode.ONE_OFF or not self.waypoints:
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
        mode_str = data.get("mode", "one_off")
        try:
            mode = RouteMode(mode_str)
        except ValueError:
            # Migration: old route mode names
            mode_map = {
                "one_way": RouteMode.ONE_OFF,
                "loop": RouteMode.CIRCULAR,
            }
            mode = mode_map.get(mode_str, RouteMode.ONE_OFF)

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

### 5.5 Route Modes

| Mode | Behavior | Completion |
|------|----------|------------|
| `ONE_OFF` | Train travels through waypoints once, then stops | Route deactivates when last train finishes all waypoints |
| `CIRCULAR` | Continuously cycles through waypoints | Never completes; runs until deactivated |
| `ROUND_TRIP` | Travels to end, then reverses through waypoints | Never completes; bounces at ends |

### 5.6 Round-Trip Advancement Logic

```python
def _advance_waypoint_round_trip(
    self, 
    exec_state: RouteExecutionState, 
    waypoint_count: int
) -> Optional[int]:
    """
    Advance waypoint for ROUND_TRIP mode.
    
    Bounces at ends: A → B → C → B → A → B → ...
    
    Returns:
        New waypoint index, or None if invalid
    """
    exec_state.current_waypoint_index += exec_state.direction

    # Bounce at end
    if exec_state.current_waypoint_index >= waypoint_count:
        exec_state.direction = -1
        exec_state.current_waypoint_index = waypoint_count - 2
        if exec_state.current_waypoint_index < 0:
            exec_state.current_waypoint_index = 0

    # Bounce at start
    elif exec_state.current_waypoint_index < 0:
        exec_state.direction = 1
        exec_state.current_waypoint_index = 1
        if exec_state.current_waypoint_index >= waypoint_count:
            exec_state.current_waypoint_index = 0

    return exec_state.current_waypoint_index
```

---

## 6. RouteExecutionState Specification

### 6.1 Purpose

RouteExecutionState tracks per-train execution progress through a Route. This enables multiple trains to independently traverse the same Route definition.

### 6.2 Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `train_id` | `str` | — | Train being tracked |
| `route_id` | `str` | — | Route being executed |
| `current_waypoint_index` | `int` | `0` | Current position in waypoints list |
| `direction` | `int` | `1` | `+1` forward, `-1` reverse (for ROUND_TRIP) |
| `route_start_time` | `Optional[float]` | `None` | When route was activated for this train |
| `arrived_at_platform_time` | `Optional[float]` | `None` | When train arrived at current waypoint |
| `waiting_for_path` | `bool` | `False` | Whether waiting for path reservation |
| `last_waypoint_platform_id` | `Optional[str]` | `None` | Last platform visited |
| `resolved_platform_id` | `Optional[str]` | `None` | Actual target platform after flexible resolution |

### 6.3 Class Definition

```python
# route_executor.py

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
        arrived_at_platform_time: When train arrived at current platform
        waiting_for_path: Whether waiting for path reservation
        last_waypoint_platform_id: Last platform visited
        resolved_platform_id: Actual target platform after flexible resolution
    """

    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1  # +1 forward, -1 reverse (for ROUND_TRIP)
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for status API."""
        return {
            "train_id": self.train_id,
            "route_id": self.route_id,
            "current_waypoint_index": self.current_waypoint_index,
            "direction": self.direction,
            "route_start_time": self.route_start_time,
            "arrived_at_platform_time": self.arrived_at_platform_time,
            "waiting_for_path": self.waiting_for_path,
            "last_waypoint_platform_id": self.last_waypoint_platform_id,
            "resolved_platform_id": self.resolved_platform_id,
        }
```

---

## 7. RouteExecutor Specification

### 7.1 Overview

RouteExecutor undergoes significant changes to support:

1. **Multi-train routes**: Iterate `Route.assigned_train_ids` instead of scanning all trains
2. **Platform-first waypoints**: Use `waypoint.platform_id` instead of `waypoint.station_id`
3. **Flexible platform**: Resolve best available platform at departure time with compatibility filters
4. **Per-train execution state**: All state reads/writes go through `RouteExecutionState`

### 7.2 Key Methods

```python
class RouteExecutor:
    """Executes routes for assigned trains."""

    def __init__(self, network: RailwayNetwork):
        self.network = network
        self.execution_states: Dict[str, RouteExecutionState] = {}
        self.simulation_time: float = 0.0

    def update(self, delta_time: float) -> None:
        """
        Main update loop - called each simulation tick.
        
        Iterates active routes and their assigned trains.
        """
        self.simulation_time += delta_time
        routes = getattr(self.network, "routes", {})

        for route in routes.values():
            if not route.is_active:
                continue
            
            for train_id in route.assigned_train_ids:
                train = self.network.trains.get(train_id)
                if train:
                    self._execute_route_for_train(train, route)

    def _execute_route_for_train(self, train: Train, route: Route) -> None:
        """Execute route logic for a single train."""
        # Get or create execution state
        exec_state = self.execution_states.get(train.train_id)
        if not exec_state:
            exec_state = RouteExecutionState(
                train_id=train.train_id,
                route_id=route.route_id,
                route_start_time=self.simulation_time
            )
            self.execution_states[train.train_id] = exec_state

        # Get current waypoint
        waypoint = route.get_waypoint_at(exec_state.current_waypoint_index)
        if not waypoint:
            # Route complete for this train
            self._handle_route_complete(train, route, exec_state)
            return

        # Resolve target platform (handles flexible platform)
        target_platform_id = self._resolve_target_platform(waypoint, exec_state)
        if not target_platform_id:
            target_platform_id = waypoint.platform_id

        exec_state.resolved_platform_id = target_platform_id

        # Check if at platform
        if self._is_train_at_platform(train, target_platform_id):
            if exec_state.arrived_at_platform_time is None:
                exec_state.arrived_at_platform_time = self.simulation_time
            
            # Check if should depart
            if waypoint.should_depart(
                self.simulation_time - exec_state.arrived_at_platform_time,
                exec_state.route_start_time or self.simulation_time,
                self.simulation_time
            ):
                self._advance_to_next_waypoint(train, route, exec_state)
        else:
            # Navigate to platform
            self._navigate_to_platform(train, target_platform_id, exec_state)

    def _resolve_target_platform(
        self, 
        waypoint: Waypoint, 
        exec_state: RouteExecutionState
    ) -> Optional[str]:
        """
        Resolve the actual target platform for a waypoint.
        
        For non-flexible waypoints, returns waypoint.platform_id directly.
        For flexible platform waypoints, finds the best available compatible
        platform within the same Station.
        """
        if not waypoint.flexible_platform:
            return waypoint.platform_id

        # Already resolved for this waypoint
        if exec_state.resolved_platform_id:
            # Check if still valid (block still available)
            if self._is_platform_available(
                exec_state.resolved_platform_id, 
                exec_state.train_id
            ):
                return exec_state.resolved_platform_id

        # Find the Station containing the target platform
        station_id = self.network.get_station_for_platform(waypoint.platform_id)
        if not station_id:
            # Platform not in any station — use original
            return waypoint.platform_id

        station = self.network.stations.get(station_id)
        if not station:
            return waypoint.platform_id

        # Get target platform for side reference
        target_platform = self.network.platforms.get(waypoint.platform_id)
        if not target_platform:
            return waypoint.platform_id

        # Find compatible platforms
        compatible_platforms = self._find_compatible_platforms(
            station, 
            waypoint, 
            exec_state.train_id,
            target_platform
        )

        if not compatible_platforms:
            # No compatible alternatives — use original
            return waypoint.platform_id

        # Prefer the originally specified platform if available and compatible
        if waypoint.platform_id in compatible_platforms:
            return waypoint.platform_id

        # Use first available compatible platform
        return compatible_platforms[0]

    def _find_compatible_platforms(
        self,
        station: Station,
        waypoint: Waypoint,
        train_id: str,
        target_platform: Platform
    ) -> List[str]:
        """
        Find all platforms in station that are compatible with constraints.
        
        Filters by:
        1. Block availability (FREE or RESERVED for this train)
        2. Required platform side
        3. Travel direction policy
        """
        compatible = []

        for platform_id in station.platform_ids:
            platform = self.network.platforms.get(platform_id)
            if not platform:
                continue

            # Check block availability
            if not self._is_platform_available(platform_id, train_id):
                continue

            # Check side compatibility
            if not self._is_side_compatible(
                platform, 
                target_platform, 
                waypoint.required_platform_side
            ):
                continue

            # Check direction policy
            if waypoint.travel_direction_policy == TravelDirectionPolicy.PRESERVE:
                # Would need previous platform to determine approach direction
                # For now, accept all and let pathfinder handle it
                pass

            compatible.append(platform_id)

        return compatible

    def _is_platform_available(self, platform_id: str, train_id: str) -> bool:
        """Check if any block of the platform is available for the train."""
        platform = self.network.platforms.get(platform_id)
        if not platform:
            return False

        block_ids = platform.get_all_assigned_blocks()
        if not block_ids:
            # Check track block references
            for x, y in platform.positions:
                track = self.network.get_track(x, y)
                if track and track.block_section_id:
                    block_ids.add(track.block_section_id)

        for block_id in block_ids:
            block = self.network.blocks.get(block_id)
            if block and (block.is_free() or 
                         (block.is_reserved() and block.occupied_by == train_id)):
                return True

        return False

    def _is_side_compatible(
        self, 
        candidate: Platform, 
        target: Platform, 
        required_side: PlatformSide
    ) -> bool:
        """Check if candidate platform is side-compatible with target."""
        if required_side == PlatformSide.EITHER:
            return True
        if required_side == PlatformSide.BOTH:
            return candidate.platform_side in (PlatformSide.BOTH, PlatformSide.LEFT, PlatformSide.RIGHT)
        # Require specific side match
        return candidate.platform_side in (required_side, PlatformSide.BOTH)

    def _is_train_at_platform(self, train: Train, platform_id: str) -> bool:
        """Check if train's head is at the platform."""
        if not train.position:
            return False

        head_tile = train.position.head_tile
        platform = self.network.platforms.get(platform_id)
        
        if not platform:
            return False

        # Check if head tile is in platform positions
        if head_tile in platform.positions:
            return True

        # Also check if head tile's block matches platform's blocks
        track = self.network.get_track(*head_tile)
        if track and track.block_section_id:
            return track.block_section_id in platform.get_all_assigned_blocks()

        return False

    def _advance_to_next_waypoint(
        self, 
        train: Train, 
        route: Route, 
        exec_state: RouteExecutionState
    ) -> None:
        """Advance to the next waypoint based on route mode."""
        exec_state.last_waypoint_platform_id = exec_state.resolved_platform_id
        exec_state.arrived_at_platform_time = None
        exec_state.resolved_platform_id = None

        waypoint_count = route.get_waypoint_count()
        if waypoint_count == 0:
            return

        if route.mode == RouteMode.ONE_OFF:
            exec_state.current_waypoint_index += 1
            if exec_state.current_waypoint_index >= waypoint_count:
                self._handle_route_complete(train, route, exec_state)

        elif route.mode == RouteMode.CIRCULAR:
            exec_state.current_waypoint_index += 1
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.current_waypoint_index = 0  # Wrap around

        elif route.mode == RouteMode.ROUND_TRIP:
            exec_state.current_waypoint_index += exec_state.direction

            # Bounce at ends
            if exec_state.current_waypoint_index >= waypoint_count:
                exec_state.direction = -1
                exec_state.current_waypoint_index = waypoint_count - 2
                if exec_state.current_waypoint_index < 0:
                    exec_state.current_waypoint_index = 0

            elif exec_state.current_waypoint_index < 0:
                exec_state.direction = 1
                exec_state.current_waypoint_index = 1
                if exec_state.current_waypoint_index >= waypoint_count:
                    exec_state.current_waypoint_index = 0

    def _handle_route_complete(
        self, 
        train: Train, 
        route: Route, 
        exec_state: RouteExecutionState
    ) -> None:
        """Handle route completion for ONE_OFF mode."""
        # Remove train from route
        route.unassign_train(train.train_id)
        train.assigned_route_id = None

        # Clean up execution state
        if train.train_id in self.execution_states:
            del self.execution_states[train.train_id]

        # Deactivate route if no more trains
        if route.mode == RouteMode.ONE_OFF and not route.assigned_train_ids:
            route.deactivate()

    def create_temporary_park_route(self, train: Train) -> Optional[str]:
        """
        Create temporary one-off route to nearest available platform.
        Called when unassigning train from route.
        
        Returns:
            Temporary route ID if created, None if no platform available
        """
        # Find nearest reachable platform
        nearest = self._find_nearest_reachable_platform(train)
        if not nearest:
            return None

        # Create temporary route
        temp_route_id = f"TEMP_{train.train_id}_{int(self.simulation_time)}"
        temp_route = Route(
            route_id=temp_route_id,
            name=f"Auto-park for {train.train_id}",
            waypoints=[Waypoint(platform_id=nearest, dwell_time_sec=0)],
            mode=RouteMode.ONE_OFF
        )
        
        if not hasattr(self.network, 'routes'):
            self.network.routes = {}
        self.network.routes[temp_route_id] = temp_route

        # Assign train
        temp_route.assign_train(train.train_id)
        train.assigned_route_id = temp_route_id
        temp_route.activate(self.simulation_time)

        return temp_route_id

    def _find_nearest_reachable_platform(self, train: Train) -> Optional[str]:
        """Find nearest platform with viable path from train's current position."""
        # Get train's current block
        if not train.position:
            return None

        head_tile = train.position.head_tile
        track = self.network.get_track(*head_tile)
        if not track:
            return None

        current_block_id = track.block_section_id
        if not current_block_id:
            return None

        # Search for nearest reachable platform
        # This would use pathfinder to check viability
        for platform in self.network.platforms.values():
            platform_blocks = platform.get_all_assigned_blocks()
            for block_id in platform_blocks:
                if self._is_path_viable(current_block_id, block_id):
                    return platform.platform_id

        return None

    def _is_path_viable(self, from_block: str, to_block: str) -> bool:
        """Check if path exists between blocks using pathfinder."""
        # Delegate to pathfinder/path_dispatcher
        # Placeholder implementation
        return True
```

---

## 8. API Endpoints

### 8.1 Platform CRUD (Renamed from Station)

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| POST | `/api/platform` | Create platform | `{x, y, name?, platform_id?, ...}` | Platform dict |
| GET | `/api/platform/<id>` | Get platform | — | Platform dict |
| PATCH | `/api/platform/<id>` | Update platform | `{name?, length_cm?, ...}` | Platform dict |
| DELETE | `/api/platform/<id>` | Delete platform | — | `{success: true}` |
| POST | `/api/platform/<id>/extend` | Extend platform | `{positions: [[x,y],...]}` | Platform dict |
| POST | `/api/platform/<id>/assign_tile` | Assign tile to block | `{x, y, block_id}` | Platform dict |

### 8.2 Station CRUD (New Grouping)

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| POST | `/api/station` | Create station | `{name, station_id?, platform_ids?}` | Station dict |
| GET | `/api/station` | List all stations | — | `{stations: [...]}` |
| GET | `/api/station/<id>` | Get station | — | Station dict with platform details |
| PATCH | `/api/station/<id>` | Update station | `{name?, platform_ids?}` | Station dict |
| DELETE | `/api/station/<id>` | Delete station | — | `{success: true}` |
| POST | `/api/station/<id>/add-platform` | Add platform | `{platform_id}` | Station dict |
| POST | `/api/station/<id>/remove-platform` | Remove platform | `{platform_id}` | Station dict |

**Create Station:**
```
POST /api/station
{
    "name": "Central Station",
    "station_id": "STA_001",       // optional
    "platform_ids": ["PLT_001"]    // optional
}

Response:
{
    "station_id": "STA_001",
    "name": "Central Station",
    "platform_ids": ["PLT_001"],
    "created_at": "2026-02-06T10:00:00"
}
```

**Get Station (Enriched):**
```
GET /api/station/STA_001

Response:
{
    "station_id": "STA_001",
    "name": "Central Station",
    "platform_ids": ["PLT_001", "PLT_002"],
    "created_at": "2026-02-06T10:00:00",
    "platforms": [
        {
            "platform_id": "PLT_001",
            "name": "Platform 1",
            "positions": [[5, 10], [6, 10]],
            "block_ids": ["BLK_A"]
        }
    ],
    "center_position": [5.5, 10.0],
    "all_block_ids": ["BLK_A", "BLK_B"]
}
```

### 8.3 Route CRUD

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| GET | `/api/train-routes` | List routes | — | `{routes: [...]}` |
| POST | `/api/train-routes` | Create route | `{name, mode?, waypoints?}` | Route dict |
| GET | `/api/train-routes/<id>` | Get route | — | Route dict |
| PUT | `/api/train-routes/<id>` | Update route | `{name?, mode?, waypoints?}` | Route dict |
| DELETE | `/api/train-routes/<id>` | Delete route | — | `{success}` |
| POST | `/api/train-routes/<id>/activate` | Activate route | — | Route dict |
| POST | `/api/train-routes/<id>/deactivate` | Deactivate route | — | Route dict |

**Create Route (Platform-First Waypoints):**
```
POST /api/train-routes
{
    "name": "Express Line A",
    "mode": "circular",
    "waypoints": [
        {
            "platform_id": "PLT_001",
            "dwell_time_sec": 30,
            "flexible_platform": true,
            "required_platform_side": "either",
            "travel_direction_policy": "either"
        },
        {
            "platform_id": "PLT_003",
            "dwell_time_sec": 45,
            "flexible_platform": false
        }
    ]
}
```

### 8.4 Route Execution Status

| Method | URL | Description | Response |
|--------|-----|-------------|----------|
| GET | `/api/train-routes/<id>/status` | Get execution status for all assigned trains | See below |

**Response:**
```json
{
    "route_id": "ROUTE_001",
    "is_active": true,
    "mode": "circular",
    "trains": [
        {
            "train_id": "T-001",
            "current_waypoint_index": 2,
            "direction": 1,
            "current_waypoint_platform_id": "PLT_003",
            "resolved_platform_id": "PLT_003",
            "at_platform": true,
            "waiting_for_path": false,
            "dwell_remaining_sec": 15.3
        },
        {
            "train_id": "T-002",
            "current_waypoint_index": 0,
            "direction": 1,
            "current_waypoint_platform_id": "PLT_001",
            "resolved_platform_id": "PLT_001",
            "at_platform": false,
            "waiting_for_path": true,
            "dwell_remaining_sec": null
        }
    ]
}
```

### 8.5 Train-Route Assignment

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| PUT | `/api/trains/<train_id>/assign-route` | Assign/unassign route | `{route_id, force?, next_waypoint_index?, auto_park?}` | Train dict |
| POST | `/api/trains/<train_id>/set-waypoint` | Override waypoint index | `{waypoint_index}` | Execution status |

**Assign Route:**
```
PUT /api/trains/T-001/assign-route
{
    "route_id": "ROUTE_001",
    "force": true,
    "next_waypoint_index": 0,
    "auto_park": true
}
```

**Unassign (Creates Temporary Auto-Park Route):**
```
PUT /api/trains/T-001/assign-route
{
    "route_id": null,
    "auto_park": true
}

Response:
{
    "status": "success",
    "train": { ... },
    "temporary_route_id": "TEMP_001",
    "target_platform_id": "PLT_002"
}
```

### 8.6 Viability Check

| Method | URL | Description | Request Body | Response |
|--------|-----|-------------|--------------|----------|
| POST | `/api/train-routes/check-viability` | Check path viability between platforms | `{pairs: [[from, to], ...]}` | Viability results |

**Request:**
```json
{
    "pairs": [
        ["PLT_001", "PLT_003"],
        ["PLT_003", "PLT_005"]
    ]
}
```

**Response:**
```json
{
    "results": [
        {
            "from": "PLT_001",
            "to": "PLT_003",
            "viable": true,
            "hop_count": 3,
            "distance_cm": 4500
        },
        {
            "from": "PLT_003",
            "to": "PLT_005",
            "viable": false,
            "error": "No path found"
        }
    ]
}
```

---

## 9. Route Editor UI Specification

### 9.1 Overview

The Route Editor is a **new secondary sidebar panel** for creating and managing routes visually. There is currently NO route editing UI — this is entirely new.

### 9.2 Panel Layout

```
┌─────────────────────────────────────────────┐
│  🛤️ Route Editor                       [X]  │
├─────────────────────────────────────────────┤
│                                             │
│  Route Name: [Express Line A          ]     │
│                                             │
│  Mode: [Circular ▼]                         │
│  • One-off: Stop after last waypoint        │
│                                             │
│  ─── Waypoints ───────────────────────────  │
│                                             │
│  1. 🚉 PLT_001 (Platform 1)        [🗑] [⇅] │
│     🔀 Flexible Platform [✓]                │
│     📍 Side: Either                         │
│     ⏱ Dwell: [30] sec                       │
│     ✅ → viable (3 blocks)                  │
│                                             │
│  2. 🚉 PLT_003 (Platform 3)        [🗑] [⇅] │
│     🔀 Flexible Platform [ ]                │
│     ⏱ Dwell: [45] sec                       │
│     ❌ → no path found                      │
│                                             │
│  [+ Click platform on map to add]           │
│                                             │
│  ─── Assigned Trains ─────────────────────  │
│                                             │
│  🚂 T-001  WP: 2/3  [Unassign]              │
│  🚂 T-002  WP: 1/3  [Unassign]              │
│                                             │
│  Assign: [Select train ▼] [Assign]          │
│  [ ] Force assign                           │
│                                             │
│  ─── Controls ────────────────────────────  │
│                                             │
│  [Activate] [Deactivate]                    │
│  [Save] [Delete]                            │
│                                             │
└─────────────────────────────────────────────┘
```

### 9.3 Component Description

#### Route Name Field
- Editable text input
- Changes saved on blur or Enter
- Sends `PUT /api/train-routes/<id>` with `{name: ...}`

#### Mode Selector
- Dropdown: `one_off`, `circular`, `round_trip`
- Changes saved immediately
- Shows mode description below selector
- Sends `PUT /api/train-routes/<id>` with `{mode: ...}`

#### Waypoint List
Each waypoint row shows:

| Element | Description |
|---------|-------------|
| **Index** | 1-based position number |
| **Platform indicator** | Colored icon matching platform |
| **Platform ID + name** | Platform ID and name if available |
| **Delete button** | `[🗑]` removes waypoint |
| **Reorder buttons** | `[⇅]` or drag handles to reorder |
| **Flexible Platform toggle** | Checkbox, only shown when platform is in a station |
| **Side selector** | Dropdown: `left`, `right`, `both`, `either` |
| **Dwell time input** | Number input in seconds |
| **Viability indicator** | ✅ (green) or ❌ (red) between consecutive waypoints |

#### Add Waypoint
- When Route Editor is open, clicking a platform tile on the canvas adds a waypoint
- The editor enters "add waypoint" mode (cursor changes)
- After adding, viability is immediately checked between the new waypoint and its neighbors

#### Viability Indicators
Between each pair of consecutive waypoints, a viability indicator shows whether a path exists:

```
Waypoint A (PLT_001)
   ✅ → viable (3 blocks, 45m)
Waypoint B (PLT_003)
   ❌ → no path found
Waypoint C (PLT_005)
```

**Implementation:**
1. On waypoint add/reorder/delete, collect all consecutive pairs
2. Send `POST /api/train-routes/check-viability` with platform_id pairs
3. Display results inline
4. Non-blocking: UI remains responsive during check

#### Train Assignment Panel

| Element | Description |
|---------|-------------|
| **Assigned train list** | Shows each assigned train with current waypoint progress |
| **Unassign button** | Per-train button to remove from route (with auto-park) |
| **Assign dropdown** | Dropdown of unassigned trains |
| **Assign button** | Assigns selected train to route |
| **Force checkbox** | If checked, removes train from current route before assigning |
| **Waypoint override** | Click on a train's "WP: X/Y" to change its current waypoint |

#### Controls

| Button | Action |
|--------|--------|
| **Activate** | `POST /api/train-routes/<id>/activate` |
| **Deactivate** | `POST /api/train-routes/<id>/deactivate` |
| **Save** | `PUT /api/train-routes/<id>` with full waypoint list |
| **Delete** | `DELETE /api/train-routes/<id>` (with confirmation dialog) |

### 9.4 Route Editor Handler Structure

**File:** `static/js/editor-routes.js` (new file)

```javascript
class RouteEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._currentRouteId = null;
        this._routeData = null;
        this._viabilityResults = {};
        this._isAddingWaypoint = false;
        this._unsubscribe = null;
    }

    async showRouteEditor(routeId = null) {
        // Load route data or create new
        // Render panel with all sections
        // Setup StateManager subscription
    }

    renderRoutePanel() {
        // Return HTML string with:
        // - Title input
        // - Mode selector
        // - Waypoint editor with viability indicators
        // - Train assignment panel
        // - Action buttons
    }

    async addWaypointFromMap(gridPos) {
        // 1. Find platform at position
        // 2. Add to waypoints list
        // 3. Check viability with neighbors
    }

    async moveWaypointUp(index) {
        // Swap with previous waypoint
        // Recheck viability for affected pairs
    }

    async moveWaypointDown(index) {
        // Swap with next waypoint
        // Recheck viability for affected pairs
    }

    async removeWaypoint(index) {
        // Remove from list
        // Recheck viability
    }

    async checkViability() {
        // Call backend API to check path between consecutive waypoints
        // Update UI with viability status
    }

    async assignTrain(trainId, options = {}) {
        // Assign train to this route
        // Options: force, nextWaypointIndex, autoPark
    }

    async unassignTrain(trainId, autoPark = true) {
        // Remove train from route
        // Creates temporary auto-park route if autoPark=true
    }

    async saveRoute() {
        // Save to backend
        // Refresh network state
    }

    async deleteRoute() {
        // Confirm dialog
        // Delete from backend
        // Close panel
    }

    async activateRoute() {
        // Call activate endpoint
        // Show success message
    }
}
```

### 9.5 Interaction Flow: Creating a Route

```
1. User clicks "New Route" button in sidebar
2. POST /api/train-routes with empty waypoints → get route_id
3. Route Editor panel opens with empty waypoint list
4. User enters route name
5. User clicks on canvas platform tiles to add waypoints:
   a. Tile click → find platform_id for that tile
   b. Add waypoint to list
   c. If platform is in a station → show flexible_platform toggle
   d. Trigger viability check for affected pairs
6. User adjusts dwell times, flexible_platform flags
7. User selects mode (one_off, circular, round_trip)
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

#### Backend Rename Steps

1. **Rename `station.py` → `platform.py`**
   - Class `Station` → `Platform`
   - All `station_id` → `platform_id`
   - All `station.*` → `platform.*`

2. **Update `network.py`**
   - `self.stations` (physical) → `self.platforms`
   - New `self.stations` = Station dict (grouping entities)
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
   - `waypoint.station_id` → `waypoint.platform_id`

7. **Update `train_route.py`**
   - `Waypoint.station_id` → `Waypoint.platform_id`
   - Add `flexible_platform`, `required_platform_side`, `travel_direction_policy`
   - Remove execution state from `Route`
   - Add `assigned_train_ids` to `Route`

8. **Update `routes/misc.py`**
   - Route CRUD: `station_id` → `platform_id` in waypoint handling

9. **Update all other backend files**
   - `persistence.py` — save/load migration
   - `demo_main.py` — demo data
   - `simulation.py` — if any station references

#### Frontend Rename Steps

1. **Rename `static/js/editor-stations.js` → `static/js/editor-platforms.js`**
   - All API calls: `/api/station` → `/api/platform`
   - All variable names: `station` → `platform`

2. **Create new `static/js/editor-stations.js`**
   - New Station grouping editor

3. **Rename `static/js/renderer-stations.js` → `static/js/renderer-platforms.js`**
   - Update rendering code

4. **Update `static/js/api.js`**
   - `createStation()` → `createPlatform()`
   - Add new `createStation()` for grouping
   - All station API methods renamed

5. **Update `static/js/editor.js`, `app.js`, etc.**
   - All references updated

6. **Rename `static/js/editor-routes.js` → `static/js/editor-paths.js`**
   - Current file is a PATH editor, not route editor

7. **Create new `static/js/editor-routes.js`**
   - True route editor as specified in §9

8. **Update `static/index.html`**
   - Script tags updated for renamed/new JS files

### 10.2 Data Migration (Persistence)

Saved JSON layouts need backward-compatible loading:

```python
# In persistence.py

def migrate_old_save(data: dict) -> dict:
    """Migrate old format save to new schema."""
    
    # Phase 1: Rename stations -> platforms
    if "stations" in data and "platforms" not in data:
        data["platforms"] = data.pop("stations")
        for entry in data["platforms"]:
            if "station_id" in entry:
                entry["platform_id"] = entry.pop("station_id")
    
    # Phase 2: Track references
    for track in data.get("tracks", {}).values():
        if "station_id" in track:
            track["platform_id"] = track.pop("station_id")
    
    # Phase 3: Waypoint migration (station_id was platform reference)
    for route in data.get("routes", []):
        for wp in route.get("waypoints", []):
            if "station_id" in wp and "platform_id" not in wp:
                wp["platform_id"] = wp.pop("station_id")
    
    # Phase 4: Route mode migration
    mode_map = {
        "one_way": "one_off",
        "loop": "circular",
    }
    for route in data.get("routes", []):
        old_mode = route.get("mode", "")
        if old_mode in mode_map:
            route["mode"] = mode_map[old_mode]
    
    # Phase 5: Route execution state cleanup
    for route in data.get("routes", []):
        route.pop("current_waypoint_index", None)
        route.pop("direction", None)
    
    # Phase 6: Create default Station groups
    if "stations" not in data:
        data["stations"] = {}
        for platform in data.get("platforms", []):
            plat_id = platform.get("platform_id", "")
            station_id = f"STA_{plat_id.split('_')[-1] if '_' in plat_id else plat_id}"
            data["stations"][station_id] = {
                "station_id": station_id,
                "name": platform.get("name", "Station") + " Group",
                "platform_ids": [plat_id],
                "created_at": datetime.now().isoformat()
            }
    
    return data
```

### 10.3 Compatibility Strategy

| Old Field / Structure | New Location | Migration Strategy |
|-----------------------|--------------|--------------------|
| `Station` class | `Platform` class in `platform.py` | Rename; all fields preserved |
| `network.stations` dict (physical) | `network.platforms` dict | Rename key in persistence |
| `track.station_id` | `track.platform_id` | Rename field |
| `Waypoint.station_id` | `Waypoint.platform_id` | Direct rename (was already platform reference) |
| `Route.current_waypoint_index` | `RouteExecutionState.current_waypoint_index` | Dropped from Route; recreated per-train at runtime |
| `Route.direction` | `RouteExecutionState.direction` | Dropped from Route; recreated per-train at runtime |
| `RouteMode.ONE_WAY` | `RouteMode.ONE_OFF` | Rename in persistence |
| `RouteMode.LOOP` | `RouteMode.CIRCULAR` | Rename in persistence |
| `/api/station` endpoints | `/api/platform` endpoints | URL change; behavior identical |
| `editor-stations.js` | `editor-platforms.js` | Rename; behavior identical |
| `renderer-stations.js` | `renderer-platforms.js` | Rename; behavior identical |
| `editor-routes.js` | `editor-paths.js` | Rename to reflect actual purpose (path editor) |

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
| `static/js/editor-routes.js` | Route editor UI (true route editor) |
| `static/js/renderer-platforms.js` | Platform renderer (renamed from `renderer-stations.js`) |

### 11.2 Modified Files

| File | Changes |
|------|---------|
| `network.py` | `self.stations` (physical) → `self.platforms`, new `self.stations` (grouping). All station methods renamed to platform. New station grouping methods. |
| `train_route.py` | `Waypoint.station_id` → `platform_id`, add flexible fields. `Route` loses execution state, gains `assigned_train_ids`. |
| `route_executor.py` | `RouteExecutionState` gains per-train state fields. Platform lookups, flexible resolution. Multi-train iteration. |
| `track.py` | `TrackSegment.station_id` → `TrackSegment.platform_id` |
| `routes/misc.py` | Route CRUD uses `platform_id` in waypoints, supports `assigned_train_ids`. |
| `routes/trains.py` | `assign-route` endpoint updates both `Train.assigned_route_id` and `Route.assigned_train_ids`. Add `set-waypoint` endpoint. |
| `routes/__init__.py` | Register new platform and station routes |
| `persistence.py` | Migration logic for old saves. Platform/station rename handling. |
| `enums.py` | Add `PlatformSide.EITHER`, `TravelDirectionPolicy` enum |
| `simulation.py` | Update any station references to platform |
| `demo_main.py` | Update demo data to use new Platform/Station/Waypoint format |
| `static/js/api.js` | Rename station API → platform API. Add station grouping API. Add route viability API. |
| `static/js/app.js` | Update station references. Add route editor initialization. |
| `static/js/editor.js` | Update station references to platform. Add route editor panel trigger. |
| `static/js/editor-paths.js` | Renamed from `editor-routes.js` (current path editor) |
| `static/js/train-monitor.js` | Update station references in monitor display |
| `static/js/constants.js` | May need new color constants for route/station rendering |
| `static/js/helpers.js` | Check for station references |
| `static/index.html` | Update script tags for renamed/new JS files |
| `static/css/style.css` | Add styles for route editor panel components |

### 11.3 Renamed Files

| Old | New |
|-----|-----|
| `station.py` | `platform.py` |
| `routes/stations.py` | `routes/platforms.py` |
| `static/js/editor-stations.js` | `static/js/editor-platforms.js` |
| `static/js/renderer-stations.js` | `static/js/renderer-platforms.js` |
| `static/js/editor-routes.js` | `static/js/editor-paths.js` |

### 11.4 Test File Changes

| File | Changes |
|------|---------|
| `tests/test_stations.py` | Rename to `tests/test_platforms.py`, update all references |
| `tests/test_stations.py` | Create new for Station grouping entity |
| `tests/test_backend.py` | Update all station tests to platform. Add Station grouping tests. |
| `tests/test_trains.py` | Update route assignment tests for multi-train routes |
| `tests/test_route_execution.py` | Major updates: test per-train execution state, multi-train routes, platform-based waypoints, flexible platform |
| `tests/test_dispatcher.py` | Add viability check tests |
| `tests/test_javascript_syntax.py` | Update expected class names for renamed JS files |
| `tests/test_app_smoke.py` | Update expected file list |
| `tests/test_api_routes.py` | Update station → platform endpoints. Add station grouping endpoint tests |

### 11.5 Unchanged Files

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

---

## Appendix A: Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         RailwayNetwork                                   │
│                                                                          │
│  platforms: Dict[str, Platform]      stations: Dict[str, Station]       │
│  routes: Dict[str, Route]            trains: Dict[str, Train]           │
│  blocks: Dict[str, Block]                                                │
└───────┬──────────────┬──────────────┬──────────────┬─────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌──────────────┐ ┌──────────┐ ┌──────────┐  ┌──────────────┐
│   Platform   │ │ Station  │ │  Route   │  │    Train     │
│  ──────────  │ │──────────│ │──────────│  │  ──────────  │
│  platform_id │ │station_id│ │ route_id │  │  train_id    │
│  positions[] │ │platform_ │ │ name     │◄─┤  assigned_   │
│  block_assign│ │  ids[]   │ │ waypoints│  │  route_id    │
└──────┬───────┘ └──────────┘ │ mode     │  │  state       │
       │                      │ assigned_│──┘  position    │
       │ tiles in             │ train_ids│                 └──────────────┘
       ▼                      └────┬─────┘                          │
┌──────────────┐                   │ waypoints target                │
│    Block     │◄──────────────────┤                                 │
│  ──────────  │    (via Platform) │                                 │
│  block_id    │                   │                                 │
│  state       │                   ▼                                 │
└──────────────┐            ┌──────────────┐                         │
               │            │   Waypoint   │                         │
               │            │  ──────────  │                         │
               │            │  platform_id │                         │
               │            │  dwell_time  │                         │
               │            │  flexible_   │                         │
               │            │  platform    │                         │
               │            └──────────────┘                         │
               │                                                      │
               │              RouteExecutor:                          │
               │              execution_states:                       │
               │              Dict[train_id,                          │
               │                     RouteExecutionState] ◄───────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     RouteExecutionState                                  │
│  ──────────────────────────────────────                                  │
│  train_id, route_id, current_waypoint_index, direction,                  │
│  resolved_platform_id, route_start_time, ...                             │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Appendix B: Implementation Checklist

### Backend
- [ ] Platform entity (renamed Station)
- [ ] Station entity (new grouping)
- [ ] Network integration for both
- [ ] Waypoint platform-first changes
- [ ] Route multi-train support
- [ ] RouteExecutionState per-train
- [ ] RouteExecutor flexible platform resolution
- [ ] API endpoints for Platform, Station, Route
- [ ] Viability check endpoint
- [ ] Train assignment/unassignment with auto-park
- [ ] Persistence migration

### Frontend
- [ ] Platform editor (renamed from station)
- [ ] Station grouping editor
- [ ] Path editor rename (editor-routes → editor-paths)
- [ ] Route editor UI (true route editor)
- [ ] API.js updates
- [ ] Editor.js integration
- [ ] App.js initialization
- [ ] CSS styles

### Testing
- [ ] Platform CRUD tests
- [ ] Station grouping tests
- [ ] Route multi-train tests
- [ ] Flexible platform resolution tests
- [ ] Migration tests
- [ ] JavaScript syntax tests
- [ ] App smoke tests
- [ ] Full integration tests

### Documentation
- [ ] Update API.md
- [ ] Update ARCHITECTURE.md
- [ ] This specification document
- [ ] Implementation plan

---

> **See Also:** [`docs/plans/2026-02-06-route-station-redesign-implementation.md`](docs/plans/2026-02-06-route-station-redesign-implementation.md) for phased implementation instructions.
