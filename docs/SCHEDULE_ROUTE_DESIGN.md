# Schedule and Route System Design

**Version:** 1.1  
**Status:** Partially Implemented  
**Created:** 2026-01-11  
**Updated:** 2026-01-16

**Implementation Status:**
- Route data structures: ✅ Implemented in `train_route.py`
- Route CRUD operations: ✅ Implemented via `/api/train-routes` endpoints
- RouteExecutor: ✅ Implemented in `route_executor.py`
- Route modes (ONE_WAY, LOOP, SHUTTLE): ✅ Implemented
- Route Editor UI: ⏳ Not implemented
- Schedule system: ⏳ Not implemented

---

## Table of Contents

1. [Overview](#1-overview)
2. [Core Concepts](#2-core-concepts)
3. [Route Management](#3-route-management)
4. [Route Editor](#4-route-editor)
5. [Schedule System](#5-schedule-system)
6. [Route Modes](#6-route-modes)
7. [Waypoint Management](#7-waypoint-management)
8. [Data Structures](#8-data-structures)
9. [API Endpoints](#9-api-endpoints)
10. [UI/UX Specification](#10-uiux-specification)
11. [Implementation Phases](#11-implementation-phases)

---

## 1. Overview

The Schedule and Route System enables automated train operations by allowing operators to:

- Define reusable routes (sequences of waypoints/stations)
- Assign routes to trains for automated execution
- Configure route behavior (one-way, loop, shuttle)
- Set timing constraints and dwell times
- Monitor route execution progress

### 1.1 Terminology

| Term | Definition |
|------|------------|
| **Route** | A named sequence of waypoints defining a train's travel plan |
| **Path** | A low-level dispatcher reservation (blocks + switches) for immediate movement |
| **Schedule** | Time-based execution plan associating routes with trains |
| **Waypoint** | A stop point along a route (usually a station or block) |
| **Dwell Time** | How long a train waits at a waypoint before departing |

### 1.2 Relationship: Route vs Path

```
Route (high-level travel plan)
  └── Waypoint 1 (Station A)
  └── Waypoint 2 (Station B)
  └── Waypoint 3 (Station C)

During execution, RouteExecutor creates Paths:
  Route Waypoint 1→2 triggers: Path from Block_A to Block_B
  Route Waypoint 2→3 triggers: Path from Block_B to Block_C
```

Routes are reusable templates; Paths are transient reservations.

---

## 2. Core Concepts

### 2.1 Route Definition

A **Route** defines a train's complete journey as an ordered list of waypoints.

```python
@dataclass
class Route:
    route_id: str                          # Unique identifier: "ROUTE_MAIN_LINE_001"
    name: str                              # Human-readable name: "Main Line Express"
    description: Optional[str]             # Optional description
    
    # Waypoints (ordered)
    waypoints: List[RouteWaypoint]         # Sequence of stops
    
    # Route behavior
    mode: RouteMode                        # ONE_WAY, LOOP, SHUTTLE, POINT_TO_POINT
    
    # Timing
    default_dwell_time_sec: float          # Default stop duration at waypoints
    
    # State
    is_active: bool                        # Whether route is available for assignment
    
    # Metadata
    created_at: datetime
    updated_at: datetime
```

### 2.2 Waypoint Definition

A **Waypoint** represents a stop or checkpoint along a route.

```python
@dataclass
class RouteWaypoint:
    waypoint_id: str                       # Unique within route
    sequence: int                          # Order in route (0-indexed)
    
    # Location (one of these must be set)
    station_id: Optional[str]              # Reference to station
    block_id: Optional[str]                # Reference to block (for non-station stops)
    position: Optional[Tuple[int, int]]    # Specific tile position
    
    # Behavior
    stop_required: bool                    # True = train must stop, False = pass through
    dwell_time_sec: Optional[float]        # Override route default, None = use default
    
    # Direction hint (for stations with multiple platforms)
    arrival_direction: Optional[str]       # N, E, S, W - which direction to arrive from
    
    # Constraints
    min_arrival_time: Optional[datetime]   # Earliest allowed arrival
    max_wait_time_sec: Optional[float]     # Maximum wait time before forced departure
```

---

## 3. Route Management

### 3.1 Route Lifecycle

```
DRAFT ──► ACTIVE ──► ARCHIVED
  │                    ▲
  └────────────────────┘
```

| State | Description |
|-------|-------------|
| `DRAFT` | Route is being edited, cannot be assigned to trains |
| `ACTIVE` | Route is available for train assignment |
| `ARCHIVED` | Route is deactivated but preserved for history |

### 3.2 Route Operations

| Operation | Description |
|-----------|-------------|
| Create | Define new route with waypoints |
| Edit | Modify waypoints, mode, timing (only when not in use) |
| Duplicate | Copy route as new draft |
| Activate | Transition DRAFT → ACTIVE |
| Archive | Transition ACTIVE → ARCHIVED |
| Delete | Remove route (only DRAFT/ARCHIVED, never with history) |

### 3.3 Route Assignment

Routes are assigned to trains for execution:

```python
@dataclass
class RouteAssignment:
    assignment_id: str
    route_id: str
    train_id: str
    
    # Execution state
    current_waypoint_index: int            # Which waypoint train is at/heading to
    status: RouteAssignmentStatus          # PENDING, RUNNING, PAUSED, COMPLETED, CANCELLED
    
    # Schedule (optional)
    scheduled_start_time: Optional[datetime]
    actual_start_time: Optional[datetime]
    
    # Loop control
    loops_completed: int                   # For LOOP mode
    max_loops: Optional[int]               # None = infinite
```

---

## 4. Route Editor

### 4.1 Editor UI Components

1. **Route List Panel** - Shows all routes with status
2. **Route Details Panel** - Edit route properties
3. **Waypoint List** - Ordered list with drag-to-reorder
4. **Map Overlay** - Visual representation of route on track

### 4.2 Waypoint Editing

| Action | UI Element |
|--------|------------|
| Add waypoint | Click station/block on map, or "Add" button |
| Remove waypoint | Delete button on waypoint row |
| Reorder waypoints | Drag handle on waypoint row |
| Edit waypoint | Expand waypoint row for details |

### 4.3 Visual Feedback

- Route path drawn on map as colored line
- Waypoints shown as numbered markers
- Direction arrows along route
- Conflict indicators if route crosses itself

---

## 5. Schedule System

### 5.1 Schedule Definition

```python
@dataclass
class TrainSchedule:
    schedule_id: str
    train_id: str
    
    # Time-based entries
    entries: List[ScheduleEntry]
    
    # Overall timing
    start_time: datetime
    end_time: Optional[datetime]           # None = indefinite
    
    # Recurrence
    repeat_mode: ScheduleRepeatMode        # ONCE, DAILY, WEEKLY, CUSTOM
    repeat_days: Optional[List[int]]       # For WEEKLY: 0=Mon, 6=Sun
```

### 5.2 Schedule Entry

```python
@dataclass
class ScheduleEntry:
    entry_id: str
    route_id: str                          # Route to execute
    
    # Timing
    departure_time: time                   # When to start route
    
    # Flexibility
    departure_window_sec: float            # Allowed delay before warning
    
    # Priority
    priority: int                          # For conflict resolution
```

---

## 6. Route Modes

### 6.1 ONE_WAY Mode

Train travels from first waypoint to last, then stops.

```
Waypoint 1 ──► Waypoint 2 ──► Waypoint 3 ──► [STOP]
```

- Train state after completion: IDLE at final waypoint
- Requires manual re-assignment for next journey

### 6.2 LOOP Mode

Train continuously travels through all waypoints in order.

```
Waypoint 1 ──► Waypoint 2 ──► Waypoint 3 ──┐
     ▲                                       │
     └───────────────────────────────────────┘
```

- After last waypoint, returns to first waypoint
- Continues until `max_loops` reached or manually stopped
- Train reverses direction at endpoints (no turning track needed)

### 6.3 SHUTTLE Mode

Train travels back and forth between endpoints.

```
Waypoint 1 ◄──► Waypoint 2 ◄──► Waypoint 3
```

- Forward: 1 → 2 → 3
- Backward: 3 → 2 → 1
- Train turns around at endpoints
- Intermediate waypoints visited in both directions

### 6.4 POINT_TO_POINT Mode

Train travels between two endpoints only (no intermediate stops).

```
Waypoint 1 ────────────────────► Waypoint 2
```

- Only two waypoints allowed
- Optimized pathfinding (no intermediate constraints)
- Used for express services

---

## 7. Waypoint Management

### 7.1 Waypoint Types

| Type | Description | Use Case |
|------|-------------|----------|
| Station Stop | Stop at platform | Passenger exchange |
| Block Stop | Stop at specific block | Signal/crossing wait |
| Passthrough | No stop, just path constraint | Via point |
| Timing Point | Optional stop for schedule sync | Schedule alignment |

### 7.2 Waypoint Constraints

```python
@dataclass
class WaypointConstraints:
    # Speed
    approach_speed_limit: Optional[float]  # km/h before waypoint
    departure_speed_limit: Optional[float] # km/h after waypoint
    
    # Platform
    required_platform: Optional[str]       # Specific platform ID
    platform_side: Optional[str]           # LEFT, RIGHT, BOTH
    
    # Operations
    door_side: Optional[str]               # LEFT, RIGHT, BOTH
    announcement_id: Optional[str]         # PA announcement to play
```

### 7.3 Waypoint Validation

Before activating a route, validate:

1. All waypoints are reachable from previous waypoint
2. No impossible direction changes (90° turns in tunnel)
3. Platform assignments match station capabilities
4. Timing constraints are achievable at max speed

---

## 8. Data Structures

### 8.1 Complete Route Schema

```python
@dataclass
class Route:
    route_id: str
    name: str
    description: Optional[str]
    
    waypoints: List[RouteWaypoint]
    mode: RouteMode
    
    default_dwell_time_sec: float
    default_approach_speed: Optional[float]
    
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: datetime
    
    # Statistics
    total_distance_cm: Optional[float]
    estimated_duration_sec: Optional[float]
    times_executed: int
```

### 8.2 Route Execution State

```python
@dataclass
class RouteExecutionState:
    assignment_id: str
    route_id: str
    train_id: str
    
    status: ExecutionStatus  # WAITING, DEPARTING, EN_ROUTE, ARRIVING, DWELLING, COMPLETED
    
    current_waypoint_index: int
    next_waypoint_index: int
    
    # Current path (dispatcher level)
    active_path_id: Optional[str]
    
    # Timing
    started_at: Optional[datetime]
    current_waypoint_arrived_at: Optional[datetime]
    scheduled_departure_time: Optional[datetime]
    
    # Loop tracking
    current_direction: str  # FORWARD, BACKWARD (for SHUTTLE mode)
    loops_completed: int
```

---

## 9. API Endpoints

> **Note:** The actual implementation uses `/api/train-routes` prefix instead of `/api/routes`.
> See [`docs/API.md`](API.md) for complete API documentation.

### 9.1 Route CRUD (Implemented)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/train-routes` | List all routes |
| GET | `/api/train-routes/{id}` | Get route details |
| POST | `/api/train-routes` | Create new route |
| PUT | `/api/train-routes/{id}` | Update route |
| DELETE | `/api/train-routes/{id}` | Delete route |
| POST | `/api/train-routes/{id}/activate` | Activate route for a train |
| POST | `/api/train-routes/{id}/deactivate` | Deactivate route |

### 9.2 Waypoint Management (Future)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/train-routes/{id}/waypoints` | Add waypoint |
| PUT | `/api/train-routes/{id}/waypoints/{idx}` | Update waypoint |
| DELETE | `/api/train-routes/{id}/waypoints/{idx}` | Remove waypoint |
| POST | `/api/train-routes/{id}/waypoints/reorder` | Reorder waypoints |

### 9.3 Route Assignment (Future)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/assignments` | List route assignments |
| POST | `/api/assignments` | Assign route to train |
| DELETE | `/api/assignments/{id}` | Cancel assignment |
| POST | `/api/assignments/{id}/pause` | Pause execution |
| POST | `/api/assignments/{id}/resume` | Resume execution |

### 9.4 Route Validation (Future)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/train-routes/{id}/validate` | Validate route is executable |
| GET | `/api/train-routes/{id}/preview` | Get path preview without execution |

---

## 10. UI/UX Specification

### 10.1 Route Editor Panel

```
┌─────────────────────────────────────────┐
│ 🛤️ Route Editor                    [×] │
├─────────────────────────────────────────┤
│ Name: [Main Line Express         ]      │
│ Mode: [Loop         ▼]                  │
│ Default Dwell: [30] sec                 │
├─────────────────────────────────────────┤
│ Waypoints                          [+]  │
│ ┌───┬─────────────────────────────────┐ │
│ │ ≡ │ 1. Central Station      [✏][🗑]│ │
│ │ ≡ │ 2. North Junction       [✏][🗑]│ │
│ │ ≡ │ 3. Terminal             [✏][🗑]│ │
│ └───┴─────────────────────────────────┘ │
├─────────────────────────────────────────┤
│ [Validate] [Preview] [Save] [Cancel]    │
└─────────────────────────────────────────┘
```

### 10.2 Waypoint Editor (Expanded)

```
┌─────────────────────────────────────────┐
│ ▼ 2. North Junction                     │
├─────────────────────────────────────────┤
│ Type: ○ Station  ● Block  ○ Position    │
│ Station: [North Junction    ▼]          │
│                                         │
│ □ Stop Required                         │
│ Dwell Time: [──●──────] 15 sec          │
│                                         │
│ Arrival Direction: [East    ▼]          │
│ Platform: [Any        ▼]                │
└─────────────────────────────────────────┘
```

### 10.3 Route Assignment Dialog

```
┌─────────────────────────────────────────┐
│ 🚂 Assign Route                         │
├─────────────────────────────────────────┤
│ Train: [TRAIN_001       ▼]              │
│ Route: [Main Line Express▼]             │
│                                         │
│ Start: ○ Immediately                    │
│        ● Scheduled: [14:30]             │
│                                         │
│ Loops: ○ Infinite                       │
│        ● Limited: [3]                   │
│                                         │
│           [Cancel] [Assign]             │
└─────────────────────────────────────────┘
```

---

## 11. Implementation Phases

### Phase 1: Core Route System ✅ Complete
- Route data structure and storage (`train_route.py`)
- Basic CRUD operations (`/api/train-routes` endpoints)
- Waypoint management
- ONE_WAY mode only

### Phase 2: Route Editor UI ⏳ Not Started
- Route list panel
- Waypoint editor
- Map visualization
- Route validation

### Phase 3: Route Execution ✅ Complete
- RouteExecutor integration (`route_executor.py`)
- Automatic path creation between waypoints
- Dwell time management
- Status tracking

### Phase 4: Advanced Modes ✅ Complete
- LOOP mode
- SHUTTLE mode
- POINT_TO_POINT mode
- Direction management

### Phase 5: Schedule System ⏳ Not Started
- Schedule data structure
- Time-based triggers
- Schedule editor UI
- Recurrence patterns

### Phase 6: Advanced Features ⏳ Not Started
- Route conflicts detection
- Priority-based scheduling
- Delay propagation
- Route optimization suggestions

---

## Future Considerations

1. **Route Templates** - Pre-defined route patterns for common operations
2. **Conditional Waypoints** - Skip waypoints based on time/conditions
3. **Dynamic Re-routing** - Automatically adjust route for delays
4. **Multi-train Coordination** - Ensure trains don't conflict on shared segments
5. **Energy Optimization** - Suggest speed profiles for energy efficiency
