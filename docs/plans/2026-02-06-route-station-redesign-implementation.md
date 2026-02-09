# Route, Station & Platform System Redesign — Implementation Plan

> **Document Type:** Implementation Plan  
> **Version:** 1.0  
> **Created:** 2026-02-06  
> **Status:** Ready for Implementation  
> **Cross-Reference:** See [`Reference_Route_Station_Redesign_Final.md`](../Reference_Route_Station_Redesign_Final.md) for complete technical specification

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Final Design Decisions](#2-final-design-decisions)
3. [Domain Model](#3-domain-model)
4. [Implementation Phases](#4-implementation-phases)
5. [API Specification](#5-api-specification)
6. [Frontend Changes](#6-frontend-changes)
7. [Migration Strategy](#7-migration-strategy)
8. [Testing Requirements](#8-testing-requirements)
9. [File List](#9-file-list)
10. [Risk and Mitigation](#10-risk-and-mitigation)

---

## 1. Executive Summary

This plan implements a comprehensive redesign of the Route, Station, and Platform system to support multi-train routes, flexible platform routing, and a new Route Editor UI.

### Key Changes

| Aspect | Before | After |
|--------|--------|-------|
| **Station entity** | Physical multi-tile platform | Renamed to `Platform` |
| **Station (new)** | — | Logical grouping of Platforms |
| **Waypoint target** | `station_id` | `platform_id` (platform-first approach) |
| **Route assignment** | Single train | Multiple trains via `assigned_train_ids` |
| **Execution state** | Stored on Route | Per-train in RouteExecutionState |
| **Platform flexibility** | Not supported | `flexible_platform` with direction/side compatibility |
| **Route Editor** | Path editor only | New dedicated route setup UI |

### Architecture Overview

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
│  └─────────────┘       └─────────────┘       └─────────────────────┘       │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        Route                                         │   │
│  │  ─────────────────────────────────────────────────────────────────  │   │
│  │  route_id, name, mode, assigned_train_ids: List[str]                │   │
│  │                                                                     │   │
│  │  waypoints: [Waypoint, ...]  ← targets platform_id                  │   │
│  │                                                                     │   │
│  │  Waypoint: platform_id, flexible_platform, dwell_time_sec, ...      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
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

---

## 2. Final Design Decisions

### 2.1 Platform-First Waypoints (Confirmed)

Waypoints target `platform_id` directly, not `block_id`. This provides:
- Clear semantic meaning ("go to Platform A")
- Natural platform flexibility within stations
- Block resolution happens at execution time via Platform → Block mapping

```python
@dataclass
class Waypoint:
    platform_id: str                    # Target platform (required)
    flexible_platform: bool = False     # Allow alternative platforms in same Station
    required_platform_side: PlatformSide = PlatformSide.EITHER
    travel_direction_policy: TravelDirectionPolicy = TravelDirectionPolicy.EITHER
    dwell_time_sec: float = 30.0
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None
```

### 2.2 Platform vs Station Responsibilities

| Entity | Responsibility | Has Physical Tiles? |
|--------|---------------|---------------------|
| **Platform** | Physical stopping location with track positions, block assignments, platform_side | Yes |
| **Station** | Logical grouping of Platform IDs for unified naming and flexible routing | No |

### 2.3 Multi-Train Routes

A Route can be assigned to multiple trains simultaneously:

```python
@dataclass
class Route:
    route_id: str
    name: str
    waypoints: List[Waypoint]
    mode: RouteMode                    # one_off, circular, round_trip
    assigned_train_ids: List[str]      # Multiple trains supported
    is_active: bool
```

Each train has independent execution state:

```python
@dataclass
class RouteExecutionState:
    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1                 # +1 forward, -1 reverse
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    resolved_platform_id: Optional[str] = None  # After flexible resolution
```

### 2.4 Route Modes

| Mode | Behavior | Completion |
|------|----------|------------|
| `one_off` | A → B → C → stop and deactivate | Yes, route deactivates |
| `circular` | A → B → C → A → B → C ... | No, runs until deactivated |
| `round_trip` | A → B → C → B → A → B ... | No, bounces at terminals |

### 2.5 Flexible Platform Resolution

When `flexible_platform=True`, the RouteExecutor:

1. Finds the Station containing the target Platform
2. Gets all Platforms in that Station
3. Filters by `required_platform_side` compatibility
4. Filters by `travel_direction_policy` (approach direction)
5. Checks block availability (FREE or RESERVED for this train)
6. Selects first viable alternative if primary is occupied

### 2.6 Execution State Separation

- **Route** stores only definition (waypoints, mode, assigned trains)
- **RouteExecutionState** stores per-train progress (waypoint index, direction, resolved platform)
- **RouteExecutor** manages all execution states and updates

### 2.7 Unassign Behavior

When a train is removed from a route:
1. Create temporary one-off route to nearest reachable platform
2. Execute auto-park route
3. Train enters safe-idle state at platform

---

## 3. Domain Model

### 3.1 Platform (Renamed from Station)

**File:** [`platform.py`](../../platform.py) (renamed from station.py)

```python
@dataclass
class Platform:
    """Physical multi-tile railway platform."""
    
    platform_id: str
    name: str
    positions: List[Tuple[int, int]] = field(default_factory=list)
    length_cm: float = 0.0
    platform_side: PlatformSide = PlatformSide.BOTH
    top_speed_kmh: Optional[float] = None
    initial_orientation: Optional[str] = None
    block_assignments: Dict[Tuple[int, int], str] = field(default_factory=dict)
    created_at: Optional[str] = None
```

### 3.2 Station (New Grouping Entity)

**File:** [`station.py`](../../station.py) (new logical entity)

```python
@dataclass
class Station:
    """Logical grouping of Platforms under a single station name."""
    
    station_id: str
    name: str = ""
    platform_ids: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    
    def add_platform(self, platform_id: str) -> bool:
        """Add a Platform to this Station."""
        if platform_id in self.platform_ids:
            return False
        self.platform_ids.append(platform_id)
        return True
    
    def get_all_block_ids(self, network: RailwayNetwork) -> Set[str]:
        """Get all block IDs across all Platforms in this Station."""
        block_ids = set()
        for pid in self.platform_ids:
            platform = network.platforms.get(pid)
            if platform:
                block_ids.update(platform.get_all_assigned_blocks())
        return block_ids
```

### 3.3 Waypoint (Platform-First)

**File:** [`train_route.py`](../../train_route.py)

```python
@dataclass
class Waypoint:
    """Single stop or pass-through point in a Route."""
    
    platform_id: str                    # Target platform (required)
    dwell_time_sec: float = 30.0
    flexible_platform: bool = False
    required_platform_side: PlatformSide = PlatformSide.EITHER  # left/right/both/either
    travel_direction_policy: TravelDirectionPolicy = TravelDirectionPolicy.EITHER
    timing_mode: TimingMode = TimingMode.NONE
    target_time: Optional[float] = None
```

### 3.4 Route

**File:** [`train_route.py`](../../train_route.py)

```python
@dataclass
class Route:
    """Train's travel plan - sequence of waypoints."""
    
    route_id: str
    name: str = ""
    waypoints: List[Waypoint] = field(default_factory=list)
    mode: RouteMode = RouteMode.ONE_OFF
    assigned_train_ids: List[str] = field(default_factory=list)
    is_active: bool = False
    started_at: Optional[float] = None
    
    def assign_train(self, train_id: str) -> bool:
        """Assign a train to this route."""
        if train_id in self.assigned_train_ids:
            return False
        self.assigned_train_ids.append(train_id)
        return True
    
    def unassign_train(self, train_id: str) -> bool:
        """Remove a train from this route."""
        if train_id not in self.assigned_train_ids:
            return False
        self.assigned_train_ids.remove(train_id)
        return True
```

### 3.5 RouteExecutionState

**File:** [`route_executor.py`](../../route_executor.py)

```python
@dataclass
class RouteExecutionState:
    """Tracks execution state for a single train's route."""
    
    train_id: str
    route_id: str
    current_waypoint_index: int = 0
    direction: int = 1                  # +1 forward, -1 reverse (for ROUND_TRIP)
    route_start_time: Optional[float] = None
    arrived_at_platform_time: Optional[float] = None
    waiting_for_path: bool = False
    last_waypoint_platform_id: Optional[str] = None
    resolved_platform_id: Optional[str] = None  # After flexible resolution
```

### 3.6 Entity Relationship

```
┌────────────────────────────────────────────────────────────────────────┐
│                         RailwayNetwork                                  │
│                                                                         │
│  platforms: Dict[str, Platform]      stations: Dict[str, Station]      │
│  routes: Dict[str, Route]            trains: Dict[str, Train]          │
│  blocks: Dict[str, Block]                                               │
└───────┬──────────────┬──────────────┬──────────────┬────────────────────┘
        │              │              │              │
        ▼              ▼              ▼              ▼
┌──────────────┐ ┌──────────┐ ┌──────────┐  ┌──────────────┐
│   Platform   │ │ Station  │ │  Route   │  │    Train     │
│  ──────────  │ │──────────│ │──────────│  │  ──────────  │
│  platform_id │ │station_id│ │ route_id │  │  train_id    │
│  positions[] │ │platform_ │ │ waypoints│  │  assigned_   │
│  block_assign│ │  ids[]   │ │ mode     │◄─┤  route_id    │
└──────┬───────┘ └──────────┘ │ assigned_│──┘  state       │
       │                      │ train_ids│                 └──────────────┘
       │ tiles in             └────┬─────┘                          │
       ▼                           │ waypoints target                │
┌──────────────┐                   ▼                                  │
│    Block     │◄──────────┌──────────────┐                         │
│  ──────────  │  block_id │   Waypoint   │                         │
│  block_id    │  (via     │  ──────────  │                         │
│  state       │  Platform)│  platform_id │                         │
└──────────────┘           │  dwell_time  │                         │
                           │  flexible_   │                         │
                           │  platform    │                         │
                           └──────────────┘                         │
                                                                    │
                           RouteExecutor:                           │
                           execution_states:                        │
                           Dict[train_id,                          │
                                  RouteExecutionState] ◄────────────┘
```

---

## 4. Implementation Phases

### Phase 1: Domain Split Scaffold (Backend)

**Goal:** Create Platform entity (renamed Station) and new Station grouping entity.

**Files:**
- **New:** [`platform.py`](../../platform.py) (rename from station.py)
- **New:** [`station.py`](../../station.py) (new logical Station)
- **New:** [`routes/platforms.py`](../../routes/platforms.py) (rename from routes/stations.py)
- **New:** [`routes/stations.py`](../../routes/stations.py) (new grouping endpoints)
- **Modify:** [`network.py`](../../network.py) - add platforms and stations dicts
- **Modify:** [`track.py`](../../track.py) - station_id → platform_id
- **Modify:** [`routes/__init__.py`](../../routes/__init__.py) - register new routes

**Tests:**
```bash
conda run -n general pytest tests/test_platforms.py tests/test_stations.py -v
```

**Acceptance:**
- Existing layout loads successfully with compatibility mapping
- Platform CRUD works at `/api/platform/*`
- Station grouping CRUD works at `/api/station/*`

---

### Phase 2: Route Model + Waypoint Platform-First (Backend)

**Goal:** Update Waypoint to use platform_id, Route to support multi-train.

**Files:**
- **Modify:** [`train_route.py`](../../train_route.py)
  - Waypoint: station_id → platform_id
  - Add flexible_platform, required_platform_side, travel_direction_policy
  - Route: add assigned_train_ids, remove execution state fields
- **Modify:** [`routes/misc.py`](../../routes/misc.py) - update route CRUD for new waypoint schema
- **Modify:** [`routes/trains.py`](../../routes/trains.py) - update assign-route endpoint

**Tests:**
```bash
conda run -n general pytest tests/test_trains.py tests/test_route_execution.py -v
```

**Acceptance:**
- Waypoint serialization/deserialization works
- Route assigned_train_ids populated correctly
- Old station_id waypoints migrate to platform_id on load

---

### Phase 3: RouteExecutor Per-Train State (Backend)

**Goal:** Move execution state to RouteExecutionState, support multi-train iteration.

**Files:**
- **Modify:** [`route_executor.py`](../../route_executor.py)
  - RouteExecutionState gains current_waypoint_index, direction, resolved_platform_id
  - Update loop iterates Route.assigned_train_ids
  - _advance_waypoint() operates on execution state
  - Add flexible platform resolution logic
- **Modify:** [`routes/misc.py`](../../routes/misc.py) - add route status endpoint

**Tests:**
```bash
conda run -n general pytest tests/test_route_execution.py -v
```

**Acceptance:**
- Multiple trains can traverse same route independently
- ONE_OFF mode deactivates when last train finishes
- ROUND_TRIP direction bounces correctly per-train

---

### Phase 4: Flexible Platform Resolution (Backend)

**Goal:** Implement direction/side-compatible platform selection.

**Files:**
- **Modify:** [`route_executor.py`](../../route_executor.py)
  - Add _resolve_flexible_platform() method
  - Filter by required_platform_side
  - Filter by travel_direction_policy
  - Check block availability via BlockManager

**Tests:**
```bash
conda run -n general pytest tests/test_route_execution.py -v
```

**Acceptance:**
- Flexible platform selects available alternative in same Station
- Side compatibility filtering works
- Direction policy filtering works
- Falls back to primary platform if no alternatives viable

---

### Phase 5: Viability Checking API (Backend)

**Goal:** Add endpoint to check path viability between consecutive waypoints.

**Files:**
- **Modify:** [`routes/paths.py`](../../routes/paths.py) or [`routes/misc.py`](../../routes/misc.py)
  - Add POST `/api/train-routes/check-viability`
  - Accept pairs of platform_ids
  - Return viability status, hop count, distance

**Tests:**
```bash
conda run -n general pytest tests/test_api_routes.py -v
```

**Acceptance:**
- Viability endpoint returns correct status for reachable platforms
- Returns error reason for unreachable platforms
- Performance acceptable for UI feedback (< 100ms)

---

### Phase 6: Frontend Platform Rename + Split

**Goal:** Rename all Station references to Platform, create new Station editor.

**Files:**
- **Rename:** [`static/js/editor-stations.js`](../../static/js/editor-stations.js) → [`static/js/editor-platforms.js`](../../static/js/editor-platforms.js)
- **Rename:** [`static/js/renderer-stations.js`](../../static/js/renderer-stations.js) → [`static/js/renderer-platforms.js`](../../static/js/renderer-platforms.js)
- **New:** [`static/js/editor-stations.js`](../../static/js/editor-stations.js) (station grouping editor)
- **Modify:** [`static/js/api.js`](../../static/js/api.js) - rename station API → platform API, add new station API
- **Modify:** [`static/js/editor.js`](../../static/js/editor.js), [`static/js/app.js`](../../static/js/app.js) - update references
- **Modify:** [`static/index.html`](../../static/index.html) - update script tags

**Tests:**
```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py -v
```

**Acceptance:**
- All JS syntax valid
- Platform editor works (renamed from station)
- Station grouping editor available

---

### Phase 7: Route Editor UI (Frontend)

**Goal:** Create new Route Editor panel with waypoint management.

**Files:**
- **Rename:** [`static/js/editor-routes.js`](../../static/js/editor-routes.js) → [`static/js/editor-paths.js`](../../static/js/editor-paths.js) (current path editor)
- **New:** [`static/js/editor-routes.js`](../../static/js/editor-routes.js) (true route editor)
- **Modify:** [`static/js/api.js`](../../static/js/api.js) - add route viability, assignment APIs
- **Modify:** [`static/js/editor.js`](../../static/js/editor.js) - integrate route editor panel
- **Modify:** [`static/css/style.css`](../../static/css/style.css) - add route editor styles
- **Modify:** [`static/index.html`](../../static/index.html) - add script tag

**Features:**
- Waypoint add/delete/reorder
- Map-click waypoint capture
- Viability feedback after each order change
- Flexible platform toggle per waypoint
- Train assignment with force-assign
- Next-waypoint override
- Running mode selector

**Tests:**
```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py -v
```

**Acceptance:**
- User can create/edit routes via UI
- Waypoints added via map click
- Viability updates dynamically
- Train assignment works with all options

---

### Phase 8: Data Migration (Persistence)

**Goal:** Auto-migrate old saves on load.

**Files:**
- **Modify:** [`persistence.py`](../../persistence.py)
  - Add schema_version tracking
  - Migrate stations → platforms
  - Migrate waypoint station_id → platform_id
  - Remove old execution state fields from Route
  - Create default Station groups (1:1 with Platforms) if none exist

**Tests:**
```bash
conda run -n general pytest tests/test_backend.py -v
```

**Acceptance:**
- Old format saves load without error
- Migration produces valid new format
- No data loss

---

### Phase 9: Demo Data Update

**Goal:** Update demo to use new Platform/Station/Route format.

**Files:**
- **Modify:** [`demo_main.py`](../../demo_main.py)
  - Update Station references → Platform
  - Create Station groupings
  - Update waypoints to use platform_id

**Tests:**
```bash
python3 demo_main.py
conda run -n general pytest tests/test_backend.py -v
```

**Acceptance:**
- Demo runs without errors
- All demo routes work correctly

---

### Phase 10: Documentation & Cleanup

**Goal:** Update docs, verify all tests pass.

**Files:**
- **New:** [`Reference_Route_Station_Redesign_Final.md`](../../Reference_Route_Station_Redesign_Final.md) (comprehensive spec)
- **Modify:** [`docs/API.md`](../../docs/API.md)
- **Modify:** [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)

**Tests:**
```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```

---

## 5. API Specification

### 5.1 Platform APIs (Renamed from Station)

| Method | URL | Description |
|--------|-----|-------------|
| POST | `/api/platform` | Create platform |
| GET | `/api/platform/<id>` | Get platform |
| PATCH | `/api/platform/<id>` | Update platform |
| DELETE | `/api/platform/<id>` | Delete platform |
| POST | `/api/platform/<id>/extend` | Extend platform |
| POST | `/api/platform/<id>/assign_tile` | Assign tile to block |

### 5.2 Station APIs (New Grouping)

| Method | URL | Description | Request Body |
|--------|-----|-------------|--------------|
| POST | `/api/station` | Create station | `{name, platform_ids?}` |
| GET | `/api/station` | List all stations | — |
| GET | `/api/station/<id>` | Get station | — |
| PATCH | `/api/station/<id>` | Update station | `{name?, platform_ids?}` |
| DELETE | `/api/station/<id>` | Delete station | — |
| POST | `/api/station/<id>/add-platform` | Add platform | `{platform_id}` |
| POST | `/api/station/<id>/remove-platform` | Remove platform | `{platform_id}` |

**Get Station Response:**
```json
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

### 5.3 Route APIs

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/train-routes` | List routes |
| POST | `/api/train-routes` | Create route |
| GET | `/api/train-routes/<id>` | Get route |
| PUT | `/api/train-routes/<id>` | Update route |
| DELETE | `/api/train-routes/<id>` | Delete route |
| POST | `/api/train-routes/<id>/activate` | Activate route |
| POST | `/api/train-routes/<id>/deactivate` | Deactivate route |
| GET | `/api/train-routes/<id>/status` | Get execution status |
| POST | `/api/train-routes/check-viability` | Check path viability |

**Create Route Request:**
```json
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

**Route Status Response:**
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
        }
    ]
}
```

### 5.4 Train Assignment APIs

| Method | URL | Description |
|--------|-----|-------------|
| PUT | `/api/trains/<id>/assign-route` | Assign/unassign route |
| POST | `/api/trains/<id>/set-waypoint` | Override waypoint index |

**Assign Route Request:**
```json
{
    "route_id": "ROUTE_001",
    "force": true,
    "next_waypoint_index": 0,
    "auto_park": true
}
```

**Unassign (creates temporary one-off route):**
```json
{
    "route_id": null,
    "auto_park": true
}
```

Response when unassigning:
```json
{
    "status": "success",
    "temporary_route_id": "TEMP_001",
    "target_platform_id": "PLT_002"
}
```

### 5.5 Viability Check API

**Request:**
```json
POST /api/train-routes/check-viability
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

## 6. Frontend Changes

### 6.1 File Renames

| Old Name | New Name | Purpose |
|----------|----------|---------|
| `editor-stations.js` | `editor-platforms.js` | Platform editor (physical) |
| `renderer-stations.js` | `renderer-platforms.js` | Platform renderer |
| `editor-routes.js` | `editor-paths.js` | Move current path editor |
| — | `editor-stations.js` | New station grouping editor |
| — | `editor-routes.js` | New route setup editor |

### 6.2 Route Editor UI Components

```
┌─────────────────────────────────────────────┐
│  🛤️ Route Editor                       [X]  │
├─────────────────────────────────────────────┤
│                                             │
│  Route Name: [Express Line A          ]     │
│                                             │
│  Mode: [Circular ▼]                         │
│                                             │
│  ─── Waypoints ───────────────────────────  │
│                                             │
│  1. 🚉 PLT_001 (Platform 1)        [🗑] [⇅] │
│     🔀 Flexible Platform [✓]                │
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

### 6.3 API.js Additions

```javascript
// Platform APIs (renamed from station)
async createPlatform(data) { return this._post('/api/platform', data); }
async updatePlatform(id, data) { return this._patch(`/api/platform/${id}`, data); }
async deletePlatform(id) { return this._delete(`/api/platform/${id}`); }

// Station APIs (new grouping)
async createStation(data) { return this._post('/api/station', data); }
async getStation(id) { return this._get(`/api/station/${id}`); }
async updateStation(id, data) { return this._patch(`/api/station/${id}`, data); }
async addPlatformToStation(stationId, platformId) { ... }

// Route APIs
async checkViability(pairs) { 
    return this._post('/api/train-routes/check-viability', { pairs }); 
}
async getRouteStatus(routeId) { 
    return this._get(`/api/train-routes/${routeId}/status`); 
}

// Train assignment
async assignRoute(trainId, routeId, options = {}) {
    return this._put(`/api/trains/${trainId}/assign-route`, {
        route_id: routeId,
        force: options.force || false,
        auto_park: options.auto_park !== false,
        next_waypoint_index: options.nextWaypointIndex
    });
}
```

---

## 7. Migration Strategy

### 7.1 Dual-Read, New-Write Approach

- Read legacy fields (station_id-based waypoints, old station structures)
- Write canonical new schema (platform_id waypoints, station-platform split)

### 7.2 On-Load Migration Steps

1. **Platform rename:** `stations` key → `platforms` key, rename `station_id` → `platform_id`
2. **Track references:** `track.station_id` → `track.platform_id`
3. **Waypoint migration:** `waypoint.station_id` → `waypoint.platform_id` (was already platform reference)
4. **Route cleanup:** Remove `current_waypoint_index`/`direction` from route entries
5. **Station creation:** Create default logical Station groups (1:1 with Platforms) if none exist

### 7.3 Migration Code (persistence.py)

```python
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
    
    # Phase 4: Route execution state cleanup
    for route in data.get("routes", []):
        route.pop("current_waypoint_index", None)
        route.pop("direction", None)
    
    # Phase 5: Create default Station groups
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

---

## 8. Testing Requirements

### 8.1 Mandatory Test Suite (After Each Phase)

```bash
source ~/.nvm/nvm.sh && nvm use 22

# JavaScript syntax validation
conda run -n general pytest tests/test_javascript_syntax.py -v

# App smoke tests
conda run -n general pytest tests/test_app_smoke.py -v

# Backend tests
conda run -n general pytest tests/test_backend.py -v
```

### 8.2 Phase-Specific Tests

| Phase | Test File | Coverage |
|-------|-----------|----------|
| 1 | `tests/test_platforms.py` | Platform CRUD |
| 1 | `tests/test_stations.py` | Station grouping CRUD |
| 2 | `tests/test_trains.py` | Multi-train assignment |
| 3 | `tests/test_route_execution.py` | Per-train execution state |
| 4 | `tests/test_route_execution.py` | Flexible platform resolution |
| 5 | `tests/test_api_routes.py` | Viability endpoint |
| 8 | `tests/test_backend.py` | Migration tests |

### 8.3 New Test Requirements

**Flexible Platform Tests:**
- Test side compatibility filtering
- Test direction policy filtering
- Test fallback to primary when no alternatives
- Test all occupied scenario

**Multi-Train Route Tests:**
- Test independent waypoint indices
- Test ONE_OFF deactivation on last train
- Test ROUND_TRIP direction per-train

**Migration Tests:**
- Load old format save, verify migration
- Verify no data loss
- Verify default Station creation

---

## 9. File List

### 9.1 New Files (8)

| File | Purpose |
|------|---------|
| [`platform.py`](../../platform.py) | Platform entity (renamed Station) |
| [`station.py`](../../station.py) | New logical Station grouping |
| [`routes/platforms.py`](../../routes/platforms.py) | Platform API endpoints |
| [`routes/stations.py`](../../routes/stations.py) | Station grouping API endpoints |
| [`static/js/editor-platforms.js`](../../static/js/editor-platforms.js) | Platform editor UI |
| [`static/js/editor-stations.js`](../../static/js/editor-stations.js) | Station grouping editor UI |
| [`static/js/editor-routes.js`](../../static/js/editor-routes.js) | Route setup editor UI |
| [`static/js/editor-paths.js`](../../static/js/editor-paths.js) | Moved path editor |

### 9.2 Modified Files (15+)

| File | Changes |
|------|---------|
| [`network.py`](../../network.py) | Add platforms/stations dicts, update methods |
| [`train_route.py`](../../train_route.py) | Waypoint platform-first, Route multi-train |
| [`route_executor.py`](../../route_executor.py) | Per-train execution state, flexible resolution |
| [`track.py`](../../track.py) | station_id → platform_id |
| [`routes/misc.py`](../../routes/misc.py) | Route CRUD updates, viability endpoint |
| [`routes/trains.py`](../../routes/trains.py) | Assignment endpoint updates |
| [`routes/__init__.py`](../../routes/__init__.py) | Register new routes |
| [`persistence.py`](../../persistence.py) | Migration logic |
| [`demo_main.py`](../../demo_main.py) | Update to new format |
| [`static/js/api.js`](../../static/js/api.js) | API updates, rename station→platform |
| [`static/js/editor.js`](../../static/js/editor.js) | Integrate new editors |
| [`static/js/app.js`](../../static/js/app.js) | Update references |
| [`static/js/train-monitor.js`](../../static/js/train-monitor.js) | Update display |
| [`static/index.html`](../../static/index.html) | Script tag updates |
| [`static/css/style.css`](../../static/css/style.css) | Route editor styles |

### 9.3 Renamed Files (3)

| Old | New |
|-----|-----|
| `station.py` | `platform.py` |
| `routes/stations.py` | `routes/platforms.py` |
| `static/js/editor-routes.js` | `static/js/editor-paths.js` |

### 9.4 Test Files to Update (8+)

| File | Action |
|------|--------|
| `tests/test_stations.py` | Rename to `test_platforms.py`, update refs |
| `tests/test_stations.py` | Create new for grouping entity |
| `tests/test_backend.py` | Update imports, add tests |
| `tests/test_trains.py` | Multi-train assignment tests |
| `tests/test_route_execution.py` | Per-train state, flexible platform |
| `tests/test_api_routes.py` | New endpoint tests |
| `tests/test_app_smoke.py` | Update file list |
| `tests/test_javascript_syntax.py` | Update class names |

---

## 10. Risk and Mitigation

| Risk | Mitigation |
|------|------------|
| Broad rename blast radius | Phased implementation with compatibility layers; run tests after each file |
| Old saves break on load | Phase 8 migration logic + test with old-format mock data |
| UI confusion between path and route | Physically split handlers/files and labels; clear naming |
| Multi-train execution complexity | Phase 4 isolated; thorough tests for each state field |
| Frontend JS syntax errors | Run `test_javascript_syntax.py` after every JS change |
| No reachable platform on unassign | Return explicit warning; keep train in safe-idle state |
| Performance of viability checks | Cache pairwise results; invalidate only affected segments on reorder |

---

## Appendix: End-to-End Verification Checklist

After all phases complete, verify:

- [ ] Create physical platforms via editor
- [ ] Create logical Station groups and add platforms
- [ ] Build route with map-selected waypoints
- [ ] Reorder waypoints and observe viability updates
- [ ] Test route modes: one-off deactivation, circular wrap, round-trip reverse
- [ ] Assign multiple trains to one route
- [ ] Verify each train has independent next waypoint state
- [ ] Remove train from route and verify temporary one-off route creation
- [ ] Verify flexible platform picks only compatible alternatives in station group
- [ ] Load old save file and verify migration
- [ ] Run full test suite: all tests pass

---

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan phase-by-phase.
