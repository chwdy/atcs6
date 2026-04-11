# ATCS6 — Master Design Document

> Entities, properties, and method-level detail for every domain class and UI panel.
> Written for human review; intended as the authoritative reference before any refactor.

---

## Table of Contents

1. [Enums & Constants](#enums--constants)
2. [TrackSegment](#tracksegment)
3. [Switch](#switch)
4. [Block](#block)
5. [BlockManager](#blockmanager)
6. [Signal](#signal)
7. [Platform](#platform)
8. [Station](#station)
9. [TrainCar / TrainPosition / ScheduleStop / TrainSchedule](#train-supporting-types)
10. [Train](#train)
11. [SpeedProfile](#speedprofile)
12. [Path (and result types)](#path)
13. [PathDispatcher](#pathdispatcher)
14. [TrainController](#traincontroller)
15. [TrainPhysics](#trainphysics)
16. [RailwayNetwork](#railwaynetwork)
17. [Route / Waypoint](#route--waypoint)

---

## Enums & Constants

**File:** `enums.py`

### Direction
| Value | Offset (dx, dy) |
|-------|----------------|
| NORTH | (0, 1) |
| EAST  | (1, 0) |
| SOUTH | (0, -1) |
| WEST  | (-1, 0) |

- `opposite()` → returns Direction 180° away
- `direction_from_delta(dx, dy)` → maps coordinate delta to Direction enum
- `DIRECTION_OFFSETS` dict and `OPPOSITE_DIRECTIONS` dict available as module-level constants

### TrainState
`OFF → IDLE → STANDBY → RUNNING_MOVING ↔ RUNNING_WAITING`
`RUNNING_MOVING → RUNNING_AT_STATION → STANDBY`
`any → MANUAL`

### BlockState
`FREE → RESERVED → OCCUPIED → CAUTION → FREE`

### HandlePosition
`P5 P4 P3 P2 P1 N B1 B2 B3 B4 B5 B6 B7 B8 EB`
- `POWER_MULTIPLIERS` — acceleration scale for P1–P5
- `BRAKE_MULTIPLIERS` — deceleration scale for B1–B8
- `HANDLE_POSITION_INDEX` — maps handle to integer slider position

### Other enums
| Enum | Values |
|------|--------|
| TrackType | EMPTY, STRAIGHT, DIAGONAL, CURVE, SWITCH, DIAMOND, WYE, COMPLEX, SCISSORS, DOUBLE_CROSS, FORBIDDEN, SIGNAL, STATION, BLOCK_BOUNDARY |
| SignalState | GREEN, RED, YELLOW, DOUBLE_YELLOW |
| PlatformSide | LEFT, RIGHT, BOTH, EITHER |
| LeadingEnd | A, B |
| PathStatus | PENDING, SETTING, SET, ACTIVE, CLEARING, COMPLETED, CANCELLED |
| RouteMode | ONE_OFF, CIRCULAR, ROUND_TRIP |
| TravelDirectionPolicy | ANY, FORWARD_ONLY, REVERSE_ONLY |
| TimingMode | NONE, RELATIVE, ABSOLUTE |
| ValidationErrorType | (error categories for path validation) |
| ConflictResolutionType | BLOCKED, PRIORITY_PROMPT |

---

## TrackSegment

**File:** `track.py` · **Type:** dataclass

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `x`, `y` | `int` | Grid coordinates |
| `track_bits` | `int` | 6-bit integer encoding physical rail segments: bits for NS, EW, NE, NW, SE, SW |
| `dead_end_ports` | `Set[str]` | Edge labels (N/E/S/W) that terminate at a buffer stop |
| `_switch_state` | `int` | Global switch state: 0=MAIN, 1=DIVERGE |
| `signal_ids` | `List[str]` | Up to 2 signal IDs on this tile |
| `station_id` | `Optional[str]` | Station ID reference |
| `block_section_id` | `Optional[str]` | Block ID reference |
| `switch_id` | `Optional[str]` | Switch entity ID reference |
| `length` | `float` | Segment length (default 1.0) |
| `curve_radius` | `Optional[float]` | Minimum curve radius for this tile |
| `occupied_by` | `Optional[str]` | Train ID currently occupying this tile |
| `reserved_by` | `Optional[str]` | Train ID that has reserved this tile |
| `wear_level` | `float` | Wear 0–1 |
| `last_maintenance` | `Optional[str]` | ISO timestamp |
| `maintenance_count` | `int` | Number of maintenances performed |
| `locked_by_path` | `Optional[str]` | Path ID that locked this switch |
| `locked_state` | `Optional[int]` | State this switch is locked to (0 or 1) |
| `path_direction` | `Optional[str]` | Direction (N/E/S/W) the locking path travels through this tile |

### Computed Properties (read-only)

| Property | Description |
|----------|-------------|
| `edges` | Set of edge labels connected by any track path or dead-end |
| `num_paths` | Popcount of `track_bits` — number of physical rail segments |
| `switch_points` | Dict: entry-edge → sorted list of exit-edges; only populated when ≥2 exits exist from an entry |
| `is_switch` | `True` if `switch_points` is non-empty |
| `has_long_path` | `True` if NS or EW bit is set |
| `has_short_path` | `True` if any diagonal bit is set |
| `is_diagonal_only_switch` | `True` if a switch that has only diagonal paths (no NS/EW) |
| `long_path_type` | `'NS'`, `'EW'`, or `None` |
| `switch_state` | Getter/setter for `_switch_state` (0 or 1) |
| `switch_states` | Per-entry-port dict of active exit for each entry edge |
| `track_type` | `TrackType` enum derived from geometry |
| `connections` | Dict: entry `Direction` → list of exit `Direction`s |
| `is_valid` | Whether the current geometry is a physically allowed combination |

### Methods

#### Geometry mutations
- **`set_track_bits(bits)`**
  - Sets `track_bits = bits`
  - Calls `_sync_switch_states()` to reset switch state to MAIN

- **`add_track_bit(bit)`**
  - ORs `bit` into `track_bits`
  - Calls `_sync_switch_states()`

- **`remove_track_bit(bit)`**
  - ANDs out `bit` from `track_bits`
  - Calls `_sync_switch_states()`

- **`_sync_switch_states()`**
  - Resets `_switch_state = 0` (MAIN) whenever geometry changes

#### Dead-end management
- **`remove_dead_end(edge)`** — removes `edge` from `dead_end_ports`
- **`has_dead_end(edge)`** — returns `True` if `edge in dead_end_ports`
- **`_update_dead_ends(network)`**
  - Iterates each edge of this tile
  - Judges: if this tile has a connection on that edge but the neighbor tile does NOT have a reciprocal connection → marks as dead-end
  - Removes edges from `dead_end_ports` that now have proper neighbors

#### Switch state
- **`set_switch_state(state)`** — sets `_switch_state` to 0 or 1

- **`propagate_update(network)`**
  - Calls `_update_dead_ends(network)` to refresh dead-end set
  - BFS-propagates updates to neighbouring tiles that may be affected

#### Exit resolution
- **`get_exits(entry_edge, use_switch_state=True)`**
  - Looks up `switch_points[entry_edge]`
  - Judgment: if only 1 exit → returns it unconditionally; if 2 exits → selects based on `_switch_state` when `use_switch_state=True`, else returns all
  - Returns list of exit edge strings

- **`get_exit_directions(entry_direction, use_switch_state=True)`**
  - Converts `entry_direction` to edge string, calls `get_exits`, converts results back to `Direction` enums

- **`get_active_exit(entry_edge)`**
  - Simplified wrapper: calls `get_exits(entry_edge, use_switch_state=True)` and returns first result or `None`

- **`get_entry_for_exit(exit_edge, use_switch_state=True)`**
  - Inverse of `get_exits`: given an exit edge, returns the entry edge that leads to it under current switch state

#### Traversal helpers
- **`get_traversal_group(entry_direction, exit_direction)`**
  - Determines orientation group (NS, EW, NE_SW, NW_SE) for the path from entry to exit
  - Used to determine if a train changes orientation

- **`is_turn(entry_direction, exit_direction)`**
  - Returns `True` if the traversal crosses orientation groups (i.e., uses a diagonal path)

- **`get_path_group(edge1, edge2)`** — returns orientation group for the pair of edges
- **`get_path_at_edge(edge)`** — returns the (edge1, edge2) pair for the path that uses this edge
- **`is_curve_connection(exit_edge, neighbor_segment)`** — `True` if the connection to a neighbor changes orientation groups

---

## Switch

**File:** `switch.py` · **Type:** dataclass

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `switch_id` | `str` | Unique ID (e.g. `"SW_5_10"`) |
| `x`, `y` | `int` | Grid coordinates of the tile |
| `main_length_cm` | `float` | Physical length of MAIN path (state 0) |
| `diverge_length_cm` | `float` | Physical length of DIVERGE path (state 1) |
| `main_radius_cm` | `Optional[float]` | Curve radius of MAIN path; `None` = straight |
| `diverge_radius_cm` | `Optional[float]` | Curve radius of DIVERGE path; `None` = straight |
| `switch_type` | `str` | `"standard"`, `"wye"`, `"crossover"`, `"slip"`, `"scissor"` |
| `name` | `str` | Human-readable name |
| `notes` | `str` | Free-text notes |
| `locked_by_path` | `Optional[str]` | Path ID holding the lock |
| `locked_state` | `Optional[int]` | State locked to (0 or 1) |

### Methods

- **`get_active_length(state)`** — returns `main_length_cm` if `state==0`, else `diverge_length_cm`
- **`get_active_radius(state)`** — returns `main_radius_cm` if `state==0`, else `diverge_radius_cm`

- **`is_locked()`** — `True` if `locked_by_path is not None`

- **`lock(path_id, state)`**
  - Judgment: if already locked by a *different* path → returns `False`
  - If free or locked by same path → sets `locked_by_path = path_id`, `locked_state = state`, returns `True`

- **`unlock(path_id)`**
  - Judgment: only clears lock if `locked_by_path == path_id`
  - Clears `locked_by_path` and `locked_state` to `None`

- **`request_state_change(new_state, network)`**
  - Judgment: if locked → returns `(False, "locked by path …")`
  - Otherwise calls `network.get_track(x, y).set_switch_state(new_state)` and returns `(True, "ok")`

- **`is_configured(state)`** — checks that the length for the given state is non-zero (i.e., user has calibrated it)

- **`get_current_state(network)`** — reads `network.get_track(x, y).switch_state`

- **`to_dict(network)`** — serializes; optionally includes TrackSegment lock info if `network` is provided
- **`from_dict(data)`** — deserializes

---

## Block

**File:** `block.py` · **Type:** dataclass

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `block_id` | `str` | Unique ID (e.g. `"BLK_001"`) |
| `name` | `str` | Human-readable name |
| `length_cm` | `float` | Total track length in cm |
| `min_radius_cm` | `Optional[float]` | Tightest curve radius in the block |
| `max_speed_kmh` | `Optional[float]` | Speed limit for this block |
| `state` | `BlockState` | `FREE / OCCUPIED / RESERVED / CAUTION` |
| `occupied_by` | `Optional[str]` | Train ID if occupied |
| `current_voltage` | `float` | Current voltage output to this block's track |
| `reserved_by_path` | `Optional[str]` | Path ID that reserved this block |
| `reserved_direction` | `Optional[str]` | Direction of travel through block for the reservation |
| `disabled` | `bool` | If `True`, block cannot be used |

### Methods

- **`can_enter(train_id)`**
  - Judgment: returns `False` if `disabled`
  - Returns `True` if state is `FREE`
  - Returns `True` if state is `RESERVED` and `occupied_by == train_id`
  - Returns `False` otherwise (OCCUPIED by another, CAUTION)

- **`occupy(train_id)`**
  - Calls `can_enter(train_id)` first; raises or returns `False` on failure
  - Sets `state = OCCUPIED`, `occupied_by = train_id`

- **`release()`** — sets `state = FREE`, clears `occupied_by`

- **`set_to_caution()`**
  - Judgment: only transitions `OCCUPIED → CAUTION`; ignores if already in other states
  - Clears `occupied_by`

- **`clear()`** — transitions `CAUTION → FREE` (also handles other states → FREE as fallback)

- **`is_free()`** — `state == FREE and not disabled`
- **`is_reserved()`** — `state == RESERVED`

- **`reserve_for_path(path_id, train_id, direction)`**
  - Judgment: sets `reserved_by_path`, `reserved_direction`, `occupied_by = train_id`
  - Handles multiple incoming states (FREE, RESERVED by same path, OCCUPIED by same train)
  - Sets `state = RESERVED`

- **`release_path_reservation()`** — clears `reserved_by_path`, `reserved_direction`; sets `state = FREE`

- **`is_available_for_path(path_id, train_id)`**
  - Returns `True` if: state is FREE, or already reserved by the same `path_id`, or OCCUPIED by `train_id`

- **`to_dict()` / `from_dict(data)`** — serialization

---

## BlockManager

**File:** `block_manager.py` · **Type:** class

Single source of truth for block state transitions. Wraps `Block` state machine calls and maintains an audit history.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `blocks` | `Dict[str, Block]` | Reference to the blocks being managed |
| `history` | `deque` | Last 100 state-change records |

### Methods

- **`_record_history(block_id, action, from_state, to_state, train_id)`**
  - Appends timestamped record to `history`

- **`occupy_block(block_id, train_id)`**
  - Judgment: allows `FREE → OCCUPIED` and `RESERVED → OCCUPIED`; rejects other transitions
  - Calls `block.occupy(train_id)` or direct state mutation
  - Calls `_record_history`

- **`set_block_to_caution(block_id)`**
  - Judgment: only acts if block is `OCCUPIED`
  - Calls `block.set_to_caution()`
  - Records history

- **`clear_block(block_id)`**
  - Calls `block.clear()`; always sets to FREE
  - Records history

- **`release_block(block_id, train_id)`**
  - High-level: calls `set_block_to_caution` then `clear_block`
  - Used when a train vacates — transitional CAUTION state signals to following trains

- **`get_block_state(block_id)`** — returns `block.state`
- **`get_block_occupant(block_id)`** — returns `block.occupied_by`

- **`is_block_available(block_id, for_train_id)`**
  - Delegates to `block.can_enter(for_train_id)`

- **`occupy_blocks(block_ids, train_id)`**
  - Iterates list; calls `occupy_block` for each
  - Judgment: if any fails, rolls back all previously occupied blocks and raises

---

## Signal

**File:** `railway_signal.py` · **Type:** dataclass

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `signal_id` | `str` | Unique ID |
| `position` | `Tuple[int, int]` | Grid (x, y) |
| `detection_direction` | `str` | Edge direction where trains approach (N/E/S/W) |
| `signal_type` | `SignalType` | BLOCK, SWITCH, PRE_SIGNAL, ROUTE |
| `state` | `SignalState` | GREEN, RED, YELLOW, DOUBLE_YELLOW |
| `name` | `Optional[str]` | Display name |
| `monitored_block_id` | `Optional[str]` | Block this signal guards (BLOCK type) |
| `monitored_switch_id` | `Optional[str]` | Switch this signal controls (SWITCH type) |
| `monitored_signal_id` | `Optional[str]` | Signal being pre-signalled (PRE_SIGNAL type) |
| `approaching_train_id` | `Optional[str]` | Train assigned by dispatcher |

### Computed Properties

- **`approach_direction`** — opposite of `detection_direction`; the direction a driver looks at this signal from

### Methods

- **`__post_init__()`** — normalises `detection_direction` to uppercase
- **`to_dict()`** — includes computed `approach_direction`
- **`from_dict(data)`** — reconstructs; normalises legacy direction formats

---

## Platform

**File:** `railway_platform.py` · **Type:** dataclass

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `platform_id` | `str` | Unique ID (e.g. `"PLT_001"`) |
| `name` | `str` | Display name |
| `positions` | `List[Tuple[int, int]]` | All tile positions in this platform |
| `length_cm` | `float` | Physical track length |
| `platform_side` | `PlatformSide` | LEFT, RIGHT, BOTH, EITHER |
| `top_speed_kmh` | `Optional[float]` | Optional speed cap |
| `created_at` | `Optional[str]` | ISO timestamp |
| `initial_orientation` | `Optional[str]` | Track orientation group at creation (NS, EW, NE_SW, NW_SE) |
| `block_assignments` | `Dict[Tuple[int,int], str]` | Maps tile (x,y) → block_id |

### Methods

- **`__post_init__()`** — sets `created_at` if absent
- **`add_position(x, y)`** / **`remove_position(x, y)`** / **`contains_position(x, y)`** — tile set management
- **`get_center_position()`** — averages all tile coordinates to get a label anchor point

- **`_get_orientation(network)`**
  - Reads track bits at each position tile via `network`
  - Returns the orientation group string (NS/EW/NE_SW/NW_SE)

- **`_sort_positions_by_orientation(orientation)`**
  - Sorts `positions` along the track axis (e.g., by x for EW platforms, by y for NS platforms)

- **`to_dict()`** — basic serialization
- **`to_dict_with_orientation(network)`**
  - Calls `_get_orientation` and `_sort_positions_by_orientation`
  - Returns extended dict with sorted positions and platform sides

- **`from_dict(data)`** — reconstructs; handles legacy `station_id` field

#### Block assignments
- **`assign_tile_to_block(x, y, block_id)`** — adds entry to `block_assignments`
- **`unassign_tile_from_block(x, y)`** — removes entry
- **`get_block_for_tile(x, y)`** — lookup; `None` if unassigned
- **`get_tiles_for_block(block_id)`** — reverse lookup: list of tiles for a block
- **`get_all_assigned_blocks()`** — set of unique block IDs assigned anywhere in platform

---

## Station

**File:** `station.py` · **Type:** dataclass

A logical grouping of one or more Platforms. Does not hold track geometry itself.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `station_id` | `str` | Unique ID (e.g. `"STA_001"`) |
| `name` | `str` | Display name |
| `platform_ids` | `List[str]` | Ordered list of Platform IDs |
| `created_at` | `Optional[str]` | ISO timestamp |

### Methods

- **`__post_init__()`** — sets `created_at` if absent
- **`add_platform(platform_id)`** — appends if not already present; returns `True`/`False`
- **`remove_platform(platform_id)`** — removes from list
- **`has_platform(platform_id)`** — membership check

- **`get_all_block_ids(network)`**
  - Iterates `platform_ids` → fetches each `Platform` from `network.platforms`
  - Calls `platform.get_all_assigned_blocks()` on each
  - Returns union set of all block IDs across all platforms

- **`get_center_position(network)`**
  - Calls `platform.get_center_position()` for each platform
  - Averages all center points → station label anchor

- **`get_platforms_with_details(network)`**
  - For each platform: fetches entity, calls `to_dict_with_orientation(network)`
  - Returns list of detail dicts for frontend rendering

- **`to_dict()` / `from_dict(data)`** — serialization

---

## Train Supporting Types

**File:** `train.py`

### TrainCar (dataclass)

| Property | Type | Description |
|----------|------|-------------|
| `car_id` | `str` | Unique ID |
| `car_type` | `str` | `LOCOMOTIVE`, `PASSENGER`, `FREIGHT` |
| `length` | `float` | Length in cm |
| `max_speed` | `float` | Speed cap for this car (default 120.0 km/h) |

---

### TrainPosition (dataclass)

Represents the complete physical footprint of a train: both ends independently tracked.

| Property | Type | Description |
|----------|------|-------------|
| `end_a_tile` | `Tuple[int,int]` | Tile where end A sits |
| `end_a_distance_cm` | `float` | Distance from entry edge on that tile |
| `end_a_entry_edge` | `str` | Which edge end A entered the tile from |
| `end_b_tile` | `Tuple[int,int]` | Tile where end B sits |
| `end_b_distance_cm` | `float` | Distance from entry edge on that tile |
| `end_b_entry_edge` | `str` | Which edge end B entered the tile from |
| `leading_end` | `Optional[LeadingEnd]` | Which end is the head (A or B) |
| `occupied_tiles` | `List[Tuple[int,int]]` | All tiles covered end A → end B |
| `occupied_blocks` | `List[str]` | Block IDs covered by the train |

**Computed properties:**
- `head_tile`, `head_distance_cm`, `head_entry_edge` — properties of whichever end is leading
- `tail_tile`, `tail_distance_cm`, `tail_entry_edge` — properties of trailing end
- `facing_direction` — calculated `Direction` enum from tile geometry

**Methods:**
- **`reverse()`** — flips `leading_end` (A↔B)
- **`_get_opposite_edge(edge)`** — returns the opposite edge string (N↔S, E↔W) for straight track traversal
- **`_edge_to_direction(edge)`** — converts edge string to `Direction` enum
- **`to_dict()` / `from_dict(data)`** — serialization

---

### ScheduleStop (dataclass)

| Property | Type | Description |
|----------|------|-------------|
| `station_pos` | `Tuple[int,int]` | Station grid coordinates |
| `station_name` | `str` | Display name |
| `arrival_time` | `datetime` | Planned arrival |
| `departure_time` | `datetime` | Planned departure |
| `platform` | `Optional[str]` | Platform ID |

**Methods:**
- **`get_dwell_time()`** — `departure_time - arrival_time` in minutes
- **`is_arrival_time_passed(current_time)`** — comparison against `arrival_time`
- **`is_departure_time_passed(current_time)`** — comparison against `departure_time`

---

### TrainSchedule (dataclass)

| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | Owner train |
| `schedule_id` | `str` | Unique ID |
| `stops` | `List[ScheduleStop]` | All stops, ordered by time |
| `start_time` | `Optional[datetime]` | Route start time |
| `end_time` | `Optional[datetime]` | Route end time |
| `priority` | `int` | Higher = higher priority |

**Methods:**
- **`get_current_target(current_time)`** — finds the first stop whose departure hasn't passed; returns `None` if all done
- **`is_delayed(current_time, current_pos)`** — checks if `current_time` is past expected arrival for the next stop
- **`get_delay_minutes(current_time, target_stop)`** — `current_time - target_stop.arrival_time` in minutes
- **`add_stop(stop)`** — appends and re-sorts `stops` by `arrival_time`

---

## Train

**File:** `train.py` · **Type:** dataclass

The core entity representing a physical train.

### Properties

#### Identity & consist
| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | Unique ID |
| `consist` | `List[TrainCar]` | Ordered cars |
| `model` | `str` | Model name (e.g. `"BR 218"`) |
| `car_number` | `str` | Car number/identifier |
| `num_cars` | `int` | Car count for display |
| `priority` | `int` | Dispatcher priority 1–10 (default 5) |

#### Physics
| Property | Type | Description |
|----------|------|-------------|
| `max_speed` | `float` | Operational max speed km/h |
| `current_speed` | `float` | Current speed km/h |
| `acceleration` | `float` | Acceleration m/s² |
| `max_braking_rate` | `float` | Maximum deceleration m/s² |
| `min_radius_cm` | `float` | Minimum curve radius the train can navigate |
| `is_emergency_braking` | `bool` | Currently emergency braking flag |
| `energy_consumed` | `float` | kWh consumed (simulation metric) |
| `efficiency` | `float` | Motor efficiency (default 0.85) |

#### Position
| Property | Type | Description |
|----------|------|-------------|
| `position` | `Optional[TrainPosition]` | Complete position; `None` if not placed |

#### Schedule
| Property | Type | Description |
|----------|------|-------------|
| `schedule` | `Optional[TrainSchedule]` | Assigned schedule |

#### Navigation
| Property | Type | Description |
|----------|------|-------------|
| `current_path` | `Optional[List[Tuple[int,int,Direction]]]` | Cached path tiles (STALE after extension/cancellation) |
| `path_index` | `int` | Current index in `current_path` |
| `reserved_blocks` | `List[str]` | Reserved block IDs |
| `assigned_path_id` | `Optional[str]` | Path ID from dispatcher |
| `assigned_route_id` | `Optional[str]` | Route (travel plan) assigned |
| `speed_profile_id` | `Optional[str]` | SpeedProfile ID |

#### State machine
| Property | Type | Description |
|----------|------|-------------|
| `state` | `TrainState` | OFF / IDLE / STANDBY / RUNNING_MOVING / RUNNING_WAITING / RUNNING_AT_STATION / MANUAL |
| `wait_reason` | `Optional[str]` | Human-readable reason for RUNNING_WAITING |
| `target_speed` | `float` | ATC-limited target speed km/h |
| `manual_target_speed` | `float` | User-set speed in MANUAL mode |
| `speed_limit_reason` | `Optional[str]` | Why current speed is limited |

#### Direction / cab
| Property | Type | Description |
|----------|------|-------------|
| `active_cab` | `Optional[Literal["A","B"]]` | Which cab is active |
| `travel_direction` | `Literal["forward","reverse"]` | Direction for MANUAL mode |

#### Emergency / ATO
| Property | Type | Description |
|----------|------|-------------|
| `emergency_brake` | `bool` | Emergency braking active |
| `ato_running` | `bool` | ATO is running |
| `ato_destination_block_id` | `Optional[str]` | ATO destination block |

### Computed Properties

- **`length`** — sum of `car.length / 100.0` for all cars in consist (meters)
- **`length_cm`** — sum of `car.length` in cm
- **`braking_distance`** — `v² / (2 * max_braking_rate)` using current speed converted to m/s
- **`facing_direction`** — delegates to `position.facing_direction`

### Methods

#### State queries
- **`is_at_station(station_pos)`** — checks if any tile in `position.occupied_tiles` matches `station_pos`
- **`is_moving()`** — `current_speed > STOPPED_SPEED_THRESHOLD_KMH`
- **`is_stopped()`** — `current_speed <= STOPPED_SPEED_THRESHOLD_KMH` (threshold ~0.1 km/h)
- **`is_delayed(current_time)`** — delegates to `schedule.is_delayed(current_time, position)`
- **`get_next_scheduled_stop(current_time)`** — delegates to `schedule.get_current_target(current_time)`

- **`get_effective_direction()`**
  - Judgment: if `travel_direction == "forward"` → returns `facing_direction`; if `"reverse"` → returns `facing_direction.opposite()`

- **`is_opposite_polarity_safe(network)`**
  - Checks voltage polarity of occupied blocks against current direction
  - Returns `True` if no polarity conflict exists for cab lighting

#### Braking
- **`get_braking_distance()`** — `v² / (2 * max_braking_rate)` (meters), using `current_speed` in m/s
- **`get_braking_distance_cm()`** — same result × 100 (centimeters)

- **`initiate_emergency_stop()`**
  - Sets `is_emergency_braking = True`, `emergency_brake = True`
  - Transitions state to `RUNNING_WAITING`

- **`update_emergency_braking(delta_time)`**
  - Calculation: reduces `current_speed` by `max_braking_rate * delta_time * 3.6`
  - Judgment: if `current_speed <= 0` → clamps to 0, clears `is_emergency_braking`, returns `False`
  - Otherwise returns `True` (still braking)

#### Power and mode control
- **`power_on()`**
  - Judgment: only transitions if `state == OFF` → `IDLE`

- **`power_off()`**
  - Judgment: must be stopped (`is_stopped()`); transitions any state → `OFF`

- **`clear_direction()`**
  - Judgment: only valid from `STANDBY`
  - Clears `active_cab`, sets `state = IDLE`

- **`enable_manual()`**
  - Judgment: must be stopped; transitions to `MANUAL` state

- **`disable_manual()`**
  - Judgment: must be stopped
  - Judgment: if `facing_direction` is set → transitions to `STANDBY`; else → `IDLE`

- **`set_manual_speed(speed)`**
  - Judgment: only acts in `MANUAL` state
  - Clamps `speed` to `[0, max_speed]`, sets `manual_target_speed`

- **`set_travel_direction(direction)`**
  - Sets `travel_direction` for MANUAL mode; `'forward'` or `'reverse'`

#### State transitions
- **`depart()`**
  - Judgment: requires `state == STANDBY` and `facing_direction` is set
  - Transitions → `RUNNING_MOVING`

- **`wait_for_path(reason)`**
  - Judgment: requires `state == RUNNING_MOVING`
  - Sets `wait_reason = reason`, transitions → `RUNNING_WAITING`

- **`clear_to_move()`**
  - Judgment: requires `state == RUNNING_WAITING`
  - Clears `wait_reason`, transitions → `RUNNING_MOVING`

- **`arrive_at_station()`**
  - Judgment: requires `state == RUNNING_MOVING`
  - Transitions → `RUNNING_AT_STATION`

- **`depart_from_station(has_more_waypoints)`**
  - Judgment: requires `state == RUNNING_AT_STATION`
  - If `has_more_waypoints` → transitions → `STANDBY`
  - Else → `IDLE`

- **`emergency_stop()`**
  - Sets `current_speed = 0`, `emergency_brake = True`
  - Transitions from any running state → `RUNNING_WAITING`

- **`stop()`**
  - Judgment: from `RUNNING_MOVING` or `RUNNING_WAITING`
  - Transitions → `STANDBY`

- **`set_direction_and_standby(direction)`**
  - Judgment: only from `IDLE`
  - Sets `active_cab` and `position.leading_end` based on `direction`
  - Transitions → `STANDBY`

- **`set_cab(cab)`**
  - If `cab` is `None` → clears `active_cab`, transitions to `IDLE`
  - If `cab` is `"A"` or `"B"`:
    - Calculates which `LeadingEnd` corresponds to the active cab
    - Updates `position.leading_end` to match
    - Sets `active_cab = cab`
    - If was `IDLE` → transitions to `STANDBY`

#### Occupancy
- **`get_occupied_blocks(network)`**
  - Iterates `position.occupied_tiles`
  - For each tile, reads `network.get_track(x, y).block_section_id`
  - Returns deduplicated list of block IDs

#### Serialization
- **`to_dict(network, path_tiles, path_index)`**
  - Serializes all properties; includes lookahead tile info from `path_tiles[path_index]` if available
  - Embeds `position.to_dict()` and `schedule` summary
- **`from_dict(data)`** — reconstructs full `Train` with nested `TrainPosition`, `consist`, and `schedule`

---

## SpeedProfile

**File:** `speed_profile.py` · **Type:** dataclass

Calibration data for converting between electrical voltage and physical speed.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `profile_id` | `str` | Unique ID |
| `name` | `str` | Display name |
| `speed_voltage_curve` | `List[Tuple[float,float]]` | Calibration points: (speed_kmh, voltage) |
| `min_start_voltage` | `float` | Minimum voltage to overcome static friction (default 2.0 V) |
| `lighting_voltage` | `float` | Voltage for lights only, no movement (default 2.5 V) |
| `acceleration_rate` | `float` | Speed increase km/h per second (default 10.0) |
| `deceleration_rate` | `float` | Service brake km/h per second (default 15.0) |
| `emergency_brake_rate` | `float` | Emergency brake km/h per second (default 30.0) |
| `friction_rate` | `float` | Coasting deceleration km/h per second (default 0.5) |
| `operational_decel_ratio` | `float` | Fraction of max brake for ATO smooth stopping (default 0.6) |
| `air_resistance_enabled` | `bool` | Enable quadratic drag simulation (default True) |
| `drag_coefficient` | `float` | Quadratic drag coefficient (default 0.001) |
| `reference_speed_kmh` | `float` | Reference speed for drag normalisation (default 100.0) |

### Methods

- **`__post_init__()`** — sorts `speed_voltage_curve` by speed ascending

- **`disable_air_resistance()`** — sets `air_resistance_enabled = False`; used for real hardware operation

- **`speed_to_voltage(speed_kmh)`**
  - Calculation: linear interpolation over `speed_voltage_curve`
  - Judgment: if `speed_kmh <= 0` → returns 0; if below first calibration point → returns `min_start_voltage`; if above last point → clamps to last voltage
  - Calls `_interpolate(x1, y1, x2, y2, x)` for in-range values

- **`_interpolate(x1, y1, x2, y2, x)`** — standard linear interpolation: `y1 + (y2 - y1) * (x - x1) / (x2 - x1)`

- **`to_dict()` / `from_dict(data)`** — serialization

---

## Path

**File:** `path.py`

### Supporting result types

#### PathSegment
| Property | Description |
|----------|-------------|
| `tiles` | Ordered tile positions |
| `distance_cm` | Total path distance |
| `required_switches` | `{(x,y): state}` switch positions needed |
| `blocks` | Ordered block IDs |

#### ValidationError
| Property | Description |
|----------|-------------|
| `type` | `ValidationErrorType` enum |
| `message` | Human-readable description |
| `tile` | Problematic tile (optional) |
| `block_id` | Problematic block (optional) |
| `locked_by_path` | Conflicting path ID |
| `occupied_by` | Occupying train ID |

#### ValidationResult
- `valid: bool`, `errors: List[ValidationError]`, `path: Optional[PathSegment]`

#### PathResult
- `success`, `path`, `error`, `attempted_paths`, `conflict_tiles`

#### ExtensionResult
- `success`, `extended_blocks`, `error`, `conflict_tiles`

#### CancellationResult
- `success`, `blocks_released`, `switches_released`, `approach_locked`, `message`

#### ConflictResolution
- `type` (BLOCKED or PRIORITY_PROMPT), `message`, `conflicting_paths`, `default_action`

---

### Path (main class)

**Type:** dataclass

A dispatcher-managed reserved route through the track network.

### Properties

#### Identity
| Property | Type | Description |
|----------|------|-------------|
| `path_id` | `str` | Human-readable ID (e.g. `"PATH_CENTRAL_TO_NORTH_001"`) |
| `status` | `PathStatus` | PENDING / SETTING / SET / ACTIVE / CLEARING / COMPLETED / CANCELLED |
| `priority` | `int` | 1–10 (default 5) |

#### Endpoints
| Property | Type | Description |
|----------|------|-------------|
| `start_block_id` | `str` | Starting block |
| `end_block_id` | `str` | Destination block |
| `start_station_id` | `Optional[str]` | If started from a station |
| `end_station_id` | `Optional[str]` | If ending at a station |

#### Path data
| Property | Type | Description |
|----------|------|-------------|
| `path_tiles` | `List[Tuple[int,int]]` | Ordered tile positions |
| `block_sequence` | `List[str]` | Ordered block IDs along path |
| `switch_positions` | `Dict[Tuple[int,int], int]` | Required switch states |

#### Ownership & state
| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `Optional[str]` | Assigned train (None until train enters) |
| `dispatcher_id` | `str` | Creator (default `"system"`) |
| `current_block_index` | `int` | Train's position in `block_sequence` |
| `blocks_cleared` | `Set[str]` | Blocks the train has fully exited |
| `rear_hold_distance_cm` | `float` | Safety distance to hold behind the train (default 300 cm) |

#### Protection zones
| Property | Type | Description |
|----------|------|-------------|
| `flank_protection_switches` | `Dict[Tuple[int,int], int]` | Switches locked for flank protection |
| `overlap_blocks` | `List[str]` | Blocks reserved beyond the destination |
| `overlap_distance_cm` | `float` | Required overlap distance (default 200 cm) |
| `approach_locked_blocks` | `Set[str]` | Blocks that cannot be released early |

#### Timestamps
| Property | Type | Description |
|----------|------|-------------|
| `created_at` | `datetime` | Creation time |
| `activated_at` | `Optional[datetime]` | When train entered path |
| `completed_at` | `Optional[datetime]` | When train completed path |

### Methods

#### State queries
- **`is_active()`** — `status == ACTIVE`
- **`is_set()`** — `status == SET`
- **`is_completed()`** — `status == COMPLETED`
- **`is_cancelled()`** — `status == CANCELLED`
- **`can_be_extended()`** — `status in (SET, ACTIVE)`
- **`can_be_cancelled()`** — `status not in (COMPLETED, CANCELLED)`
- **`has_train()`** — `train_id is not None`
- **`get_current_block()`** — `block_sequence[current_block_index]`
- **`get_remaining_blocks()`** — `block_sequence[current_block_index:]`
- **`get_cleared_block_count()`** — `len(blocks_cleared)`
- **`get_progress_percentage()`** — `current_block_index / len(block_sequence) * 100`

#### Direction queries
- **`get_exit_direction()`**
  - Calculation: computes direction from second-to-last tile to last tile in `path_tiles`
  - Returns `Direction` enum for path heading at the end

- **`get_last_tile_entry_edge()`**
  - Calls `get_exit_direction()`, then returns the opposite edge (the edge the train enters the last tile from)

- **`get_tile_directions()`**
  - Iterates consecutive tile pairs in `path_tiles`
  - Calculates `direction_from_delta(dx, dy)` for each pair
  - Returns dict: tile → travel `Direction`

#### Switch management
- **`get_all_locked_switches()`** — merges `switch_positions` and `flank_protection_switches` into one dict

#### Serialization
- **`to_dict()`** — full state serialization including computed progress percentage and timestamps
- **`from_dict(data)`** — reconstructs Path including `blocks_cleared` set and datetime fields

---

## PathDispatcher

**File:** `path_dispatcher.py` · **Type:** class

Owns the full lifecycle of all Paths: creation, validation, resource acquisition, extension, cancellation.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `network` | `RailwayNetwork` | Reference to railway network |
| `active_paths` | `Dict[str, Path]` | Currently managed paths by ID |
| `path_sequence_counter` | `int` | Monotonically increasing counter for ID generation |
| `completed_path_events` | `List[Dict]` | Event log for completed paths |

### Methods

#### Path ID generation
- **`generate_path_id(start_block_id, end_block_id)`**
  - Calls `_get_block_display_name` for each block
  - Returns `"PATH_{START}_{TO}_{END}_{counter:03d}"`
  - Increments `path_sequence_counter`

- **`_get_block_display_name(block_id)`** — strips common prefixes/suffixes, uppercases for readability

#### Endpoint discovery
- **`get_reachable_endpoints(start_block_id, include_stations)`**
  - BFS from `start_block_id` over `block_sequence` adjacency
  - Judgment: includes stations if `include_stations=True` and a block belongs to a station
  - Returns list of reachable block/station dicts

- **`get_reachable_endpoints_for_extension(path_id, include_stations)`**
  - Finds the path's current end block
  - Calls `get_reachable_endpoints` from the end, filtered to forward direction only
  - Returns endpoints a path can be extended to

#### Pathfinding
- **`find_paths(start_block_id, end_block_id, train_id)`**
  - Calls `_bfs_find_paths` to enumerate candidate paths
  - Sorts results by `distance_cm` ascending
  - Returns list of `PathSegment`s

- **`_bfs_find_paths(start_pos, start_exit, start_block_id, end_block_id)`**
  - BFS expansion over track tiles from start position
  - At each step: calls `track.get_exit_directions(entry)` to get next tiles
  - Judgment: skip tiles with no track, wrong blocks, or already visited in this path
  - Records required switch states for each switch tile encountered
  - When `end_block_id` tile reached → stores completed `PathSegment`
  - Returns all found `PathSegment`s

#### Validation
- **`validate_path(path_segment, train_id, path_id=None)`**
  - For each block in `path_segment.blocks`:
    - Calls `block.is_available_for_path(path_id, train_id)` → records `ValidationError` if blocked
  - For each switch tile:
    - Judgment: if switch is locked to a different state by another path → `ValidationError`
    - Judgment: if tile is locked by a different path → `ValidationError`
  - Returns `ValidationResult`

#### Resource acquisition
- **`acquire_path_resources(path, train_id)`**
  - Iterates `path.block_sequence` → calls `block.reserve_for_path(path.path_id, train_id, direction)`
  - Iterates `path.switch_positions` → calls `switch.lock(path.path_id, state)` and `track.locked_by_path = path.path_id`
  - Sets flank protection switches
  - Reserves overlap blocks
  - Returns `True` if all acquired, rolls back on failure

#### Path lifecycle
- **`create_path(start_block_id, end_block_id, train_id, priority, dispatcher_id)`**
  - Calls `find_paths`, `validate_path` for each candidate
  - Judgment: if no valid path found → returns `PathResult(success=False, conflict_tiles=…)`
  - Calls `acquire_path_resources`
  - Sets `path.status = SET`
  - Adds to `active_paths`
  - Returns `PathResult(success=True, path=…)`

- **`activate_path(path_id, train_id)`**
  - Sets `path.status = ACTIVE`, `path.train_id = train_id`, `path.activated_at = now`

- **`advance_path(path_id, train_id)`**
  - Increments `path.current_block_index`
  - Releases blocks behind train (calls `block.release_path_reservation()` for cleared blocks)
  - Judgment: if train has cleared enough distance behind → also releases rear-hold blocks

- **`complete_path(path_id)`**
  - Sets `path.status = COMPLETED`, `path.completed_at = now`
  - Releases all remaining reserved resources
  - Appends to `completed_path_events`
  - Removes from `active_paths`

- **`cancel_path(path_id)`**
  - Judgment: checks `path.can_be_cancelled()`
  - Releases all resources not in `approach_locked_blocks`
  - Sets `path.status = CANCELLED`
  - Returns `CancellationResult`

#### Extension
- **`extend_path(path_id, new_end_block_id, train_id)`**
  - Judgment: checks `path.can_be_extended()`
  - Finds paths from current end to `new_end_block_id`
  - Validates and acquires resources for the extension segment
  - Appends new tiles and blocks to existing path
  - Returns `ExtensionResult`

---

## TrainController

**File:** `train_controller.py` · **Type:** dataclass

Single authority for ATC (Automatic Train Control) + ATO (Automatic Train Operation) + command handling for one train.

### Properties

#### References
| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | Controlled train |
| `network` | `Optional[RailwayNetwork]` | Network reference |
| `path_dispatcher` | `Optional[PathDispatcher]` | Dispatcher reference |

#### Power & safety
| Property | Type | Description |
|----------|------|-------------|
| `power_on` | `bool` | Power state |
| `eb_active` | `bool` | Emergency brake active |
| `atc_warning_active` | `bool` | ATC overspeed warning (5–10 km/h over limit) |

#### Notifications
| Property | Type | Description |
|----------|------|-------------|
| `pending_notifications` | `List[str]` | Toast messages to send to frontend |

#### Cab & mode
| Property | Type | Description |
|----------|------|-------------|
| `active_cab` | `Optional[str]` | `'A'`, `'B'`, or `None` |
| `control_mode` | `str` | `'ato'` or `'manual'` |
| `travel_direction` | `str` | `'forward'` or `'reverse'` |

#### Operational state
| Property | Type | Description |
|----------|------|-------------|
| `state` | `TrainState` | Mirrors `Train.state` |
| `target_speed` | `float` | Target speed km/h |
| `handle_position` | `HandlePosition` | Current throttle/brake handle |

#### ATO state
| Property | Type | Description |
|----------|------|-------------|
| `ato_running` | `bool` | ATO actively running |
| `ato_destination_block_id` | `Optional[str]` | ATO destination |
| `ato_path_id` | `Optional[str]` | Path being executed |
| `path_backed_run_active` | `bool` | Backward run active |

#### ATC state (recalculated every tick)
| Property | Type | Description |
|----------|------|-------------|
| `clearance_ahead_cm` | `float` | Available distance before obstruction |
| `atc_speed_limit` | `float` | Maximum safe speed from ATC |
| `current_block_speed_limit` | `float` | Speed limit from block configuration |
| `speed_limit_reason` | `Optional[str]` | Reason for current limit |
| `safety_margin_cm` | `float` | Safety buffer added to all distance calculations (default 25 cm) |
| `signal_lookahead_blocks` | `int` | How many blocks ahead to check |

#### Lookahead & ATO info
| Property | Type | Description |
|----------|------|-------------|
| `lookahead` | `List[LookaheadPoint]` | Blocks ahead with distance/speed info |
| `ato_info` | `Optional[ATOInfo]` | ATO navigation state |
| `_previous_speed` | `float` | Previous tick speed for trend detection |
| `handle_reason` | `str` | Diagnostic string explaining handle position choice |

### Methods

#### Internal helpers
- **`_get_train()`** — fetches `Train` from `network.trains[train_id]`
- **`_get_speed_profile()`** — fetches `SpeedProfile` from `network.speed_profiles[train.speed_profile_id]`
- **`_get_block_manager()`** — fetches `network.block_manager`
- **`_get_power_controller()`** — fetches `network.power_controller`

#### Main tick
- **`tick(delta)`** — called each simulation step:
  1. Checks `power_on`; if off → returns early
  2. Checks `eb_active` → sets `handle_position = EB`
  3. Calls `_calculate_lookahead()` — shared by ATC and ATO
  4. Calls `_collect_atc_info()` — fills `atc_speed_limit`, `clearance_ahead_cm`, `speed_limit_reason`
  5. Calls `_apply_atc_action()` — issues EB or warning if overspeed detected
  6. Calls `_collect_ato_info()` — fills `ato_info`
  7. Calls `_apply_ato_action()` — corrects cab direction, calculates handle
  8. Calls `_set_effective_limits()` — resolves final `target_speed` from all constraints
  9. Calls state transition methods as needed based on ATO progress

#### Lookahead calculation
- **`_calculate_lookahead()`**
  - Starts at train's head tile, walks `path_tiles` forward
  - For each block entered: creates `LookaheadPoint(block_id, distance_cm, speed_limit)`
  - Judgment: stops at first RED signal, end of path, or occupied/reserved-by-other block
  - Populates `self.lookahead`

#### ATC
- **`_collect_atc_info()`**
  - Iterates `lookahead`
  - Finds the nearest speed constraint (block limit, signal state)
  - Calculation: `clearance_ahead_cm` = distance to first constraint
  - Sets `atc_speed_limit` = minimum speed allowed given braking distance
  - Judgment: if current speed + braking distance > clearance → limit speed to what allows safe stop

- **`_apply_atc_action()`**
  - Judgment: if `current_speed > atc_speed_limit + 10` → set `eb_active = True`
  - Judgment: if `current_speed > atc_speed_limit + 5` → set `atc_warning_active = True`
  - Else clears warning

#### ATO
- **`_collect_ato_info()`**
  - Checks `ato_running` and `ato_destination_block_id`
  - Calculates distance to destination block
  - Checks approach to stations in path
  - Populates `ato_info` with navigation state

- **`_apply_ato_action()`**
  - Judgment: if train is facing wrong direction for path → sets appropriate cab
  - Calculation: selects `HandlePosition` based on speed delta from `target_speed`:
    - If too fast → select braking handle (B1–B8 proportional to overspeed)
    - If too slow → select power handle (P1–P5 proportional to underspeed)
    - If at speed → N (neutral/coasting)
  - Judgment: if approaching station within stopping distance → begin braking
  - Sets `handle_reason` diagnostic string
  - Calls state transition methods (e.g., `train.arrive_at_station()`) when destination reached

#### Speed limit resolution
- **`_set_effective_limits()`**
  - Takes minimum of: `atc_speed_limit`, `current_block_speed_limit`, `train.max_speed`, `speed_profile.max_speed`
  - Sets `train.target_speed`

---

## TrainPhysics

**File:** `train_physics.py` · **Type:** dataclass

Handles physical movement simulation and block occupancy tracking for one train.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `train_id` | `str` | Controlled train |
| `network` | `Optional[RailwayNetwork]` | Network reference |
| `controller` | `Optional[TrainController]` | Controller reference |
| `_last_occupied_blocks` | `Set[str]` | Block IDs occupied last tick |
| `_caution_blocks` | `Set[str]` | Blocks currently in CAUTION state |
| `last_block_transitions` | `Dict[str, Set[str]]` | `{'entered': set, 'vacated': set}` |
| `_tick_count` | `int` | Tick counter |
| `_sync_validation_interval` | `int` | Validate position every N ticks |

### Methods

#### Internal helpers
- **`_get_train()`** — fetches `Train`
- **`_get_path_tiles()`** — gets path tile list from controller
- **`_get_speed_profile()`** — fetches `SpeedProfile`
- **`_get_block_manager()`** — fetches `BlockManager`
- **`_get_power_controller()`** — fetches `BlockPowerController`

#### Main tick
- **`tick(delta)`** — called each simulation step:
  1. Checks power; if off → returns
  2. Calls `_get_acceleration_for_handle(handle, …)` → gets net acceleration
  3. Calculation: `new_speed = current_speed + acceleration * delta * 3.6`
  4. Clamps `new_speed` to `[0, target_speed]`
  5. Judgment: if `new_speed` near zero and handle is braking → clamp to 0 (stopped)
  6. Applies friction: `speed -= friction_rate * delta * 3.6` if coasting (handle N)
  7. Applies air resistance (if enabled): `speed -= drag_coefficient * (speed/reference_speed)² * delta`
  8. Calculation: `distance_cm = speed_m_per_s * delta * 100`
  9. Calls `_move_train(distance_cm)` to advance position
  10. Calls `_update_block_occupancy()` to reconcile block states
  11. Every `_sync_validation_interval` ticks: calls position sync validation
  12. Calls `_update_voltage_output()` to set power controller voltage

#### Acceleration
- **`_get_acceleration_for_handle(handle, accel_rate, decel_rate, emergency_rate)`**
  - Judgment: handle P1–P5 → positive acceleration × `POWER_MULTIPLIERS[handle]`
  - Judgment: handle B1–B8 → negative deceleration × `BRAKE_MULTIPLIERS[handle]`
  - Judgment: handle EB → maximum deceleration (`-emergency_rate`)
  - Judgment: handle N → returns 0 (friction applied separately)

#### Movement
- **`_move_train(distance_cm)`**
  - Advances `train.position.head` along `path_tiles` by `distance_cm`
  - Updates `head_tile`, `head_distance_cm`, `head_entry_edge`
  - Simultaneously advances `tail` by same distance
  - Updates `occupied_tiles` list between head and tail
  - Judgment: if head reaches end of path → clamps and triggers `wait_for_path` or `arrive_at_station`

#### Block occupancy
- **`_update_block_occupancy()`**
  - Calculates current blocks from `position.occupied_tiles` → `block_section_id` lookups
  - Compares with `_last_occupied_blocks`
  - Judgment: for newly entered blocks → calls `block_manager.occupy_block(block_id, train_id)`
  - Judgment: for vacated blocks → calls `block_manager.release_block(block_id, train_id)` (OCCUPIED → CAUTION → FREE)
  - Updates `last_block_transitions`
  - Updates `_last_occupied_blocks`

#### Voltage output
- **`_update_voltage_output()`**
  - Gets `target_speed` from controller
  - Calls `speed_profile.speed_to_voltage(target_speed)` → voltage
  - Judgment: if train is stopped and only needs lighting → uses `speed_profile.lighting_voltage`
  - Calls `power_controller.set_block_voltage(block_id, voltage)` for each occupied block

---

## RailwayNetwork

**File:** `network.py` · **Type:** class

The central infrastructure repository. Holds all entities and provides the primary API for infrastructure queries and mutations.

### Properties (collections)

| Property | Type | Description |
|----------|------|-------------|
| `tracks` | `Dict[Tuple[int,int], TrackSegment]` | All track segments by (x, y) |
| `trains` | `Dict[str, Train]` | All trains by ID |
| `blocks` | `Dict[str, Block]` | All blocks by ID |
| `signals` | `Dict[str, Signal]` | All signals by ID |
| `platforms` | `Dict[str, Platform]` | All platforms by ID |
| `stations` (alias) | — | Alias for `platforms` |
| `station_groups` | `Dict[str, Station]` | Logical station groupings |
| `switches` | `Dict[str, Switch]` | Switch entities by ID |
| `schedules` | `Dict[str, TrainSchedule]` | Schedules by ID |
| `speed_profiles` | `Dict[str, SpeedProfile]` | Speed profiles by ID |
| `routes` | `Dict[str, Route]` | Routes by ID |
| `block_manager` | `Optional[BlockManager]` | Centralized block state authority |
| `power_controller` | `Optional[BlockPowerController]` | Voltage output controller |

### Key Methods

#### Track management
- **`add_track(track)`**
  - Adds to `tracks` dict
  - Calls `track.propagate_update(self)` to update dead-ends and neighbours

- **`get_track(x, y)`** — returns `tracks.get((x, y))`
- **`get_neighbor(x, y, edge)`** — calls `_get_neighbor_coords` then `get_track`
- **`_get_neighbor_coords(x, y, edge)`** — applies `DIRECTION_OFFSETS[edge]` to (x, y)

- **`propagate_from_tile(x, y)`**
  - BFS wave: starts at (x, y), visits all directly connected neighbours
  - Calls `track.propagate_update(self)` on each visited tile
  - Used after topology changes to refresh dead-ends across a region

- **`remove_track(x, y)`**
  - Removes from `tracks`
  - Judgment: if tile was last tile in a block → triggers block cleanup/deletion
  - Propagates update to neighbours

- **`is_track_available(x, y, train_id)`**
  - Judgment: `False` if no track at (x, y)
  - Judgment: `False` if `track.occupied_by` is set to a different train
  - Judgment: `False` if `track.reserved_by` is set to a different train

#### Block management
- **`create_block(block_id, name)`** — creates empty `Block` and adds to `blocks`
- **`create_block_with_tracks(block_id, positions, name, length_cm, max_speed_kmh, validate)`**
  - Creates block, assigns each tile's `block_section_id = block_id`
  - Judgment: if `validate=True` → checks that all positions have tracks and form a connected region

*(Many additional block, signal, platform, station, switch CRUD methods follow the same pattern: create entity, add to collection, optionally validate and cross-link)*

---

## Route / Waypoint

**File:** `train_route.py`

### Waypoint (dataclass)

| Property | Type | Description |
|----------|------|-------------|
| `platform_id` | `str` | Target Platform ID |
| `dwell_time_sec` | `float` | Planned stop duration in seconds (default 30.0) |
| `flexible_platform` | `bool` | Allow any compatible platform in the same Station |
| `required_platform_side` | `PlatformSide` | Side constraint for flexible platform selection |
| `travel_direction_policy` | `TravelDirectionPolicy` | Approach direction constraint (ANY / FORWARD_ONLY / REVERSE_ONLY) |
| `timing_mode` | `TimingMode` | NONE / RELATIVE / ABSOLUTE |
| `target_time` | `Optional[float]` | Target time in seconds (for RELATIVE/ABSOLUTE modes) |

**Methods:**

- **`should_depart(time_at_platform, route_start_time, simulation_time)`**
  - Judgment: if `timing_mode == NONE` → depart when `time_at_platform >= dwell_time_sec`
  - Judgment: if `timing_mode == RELATIVE` → depart when `simulation_time >= route_start_time + target_time`
  - Judgment: if `timing_mode == ABSOLUTE` → depart when `simulation_time >= target_time`

- **`get_remaining_dwell_time(time_at_platform, route_start_time, simulation_time)`**
  - Calculation mirrors `should_depart`; returns seconds remaining until departure condition met

- **`to_dict()` / `from_dict(data)`** — serialization

---

### Route (dataclass)

| Property | Type | Description |
|----------|------|-------------|
| `route_id` | `str` | Unique ID |
| `name` | `str` | Display name |
| `waypoints` | `List[Waypoint]` | Ordered stops |
| `mode` | `RouteMode` | ONE_OFF / CIRCULAR / ROUND_TRIP |
| `assigned_train_ids` | `List[str]` | Trains assigned to this route |
| `is_active` | `bool` | Currently being executed |
| `started_at` | `Optional[float]` | Simulation time when activated |
| `last_error` | `Optional[str]` | Last error message |
| `error_time` | `Optional[float]` | When the error occurred |
| `current_waypoint_index` | `int` | Runtime index (not serialized) |
| `direction` | `int` | Traversal direction for ROUND_TRIP: +1 or -1 (not serialized) |

**Methods:**

Definition:
- **`add_waypoint(waypoint)`** — appends to `waypoints`
- **`insert_waypoint(index, waypoint)`** — inserts at index
- **`remove_waypoint(index)`** — removes at index
- **`get_waypoint_count()`** — `len(waypoints)`
- **`get_waypoint_at(index)`** — `waypoints[index]` without runtime execution state

Activation:
- **`activate(simulation_time)`** — sets `is_active = True`, `started_at = simulation_time`, resets `current_waypoint_index = 0`, `direction = +1`
- **`deactivate()`** — sets `is_active = False`

Assignment:
- **`assign_train(train_id)`** — appends to `assigned_train_ids` if not already present
- **`unassign_train(train_id)`** — removes from `assigned_train_ids`
- **`is_train_assigned(train_id)`** — membership check
- **`get_assigned_train_count()`** — `len(assigned_train_ids)`

Serialization:
- **`to_dict()` / `from_dict(data)`** — serialization; `current_waypoint_index` and `direction` are runtime-only and not persisted

---

---

## UI Panels

**Files:** `static/js/editor-trains.js`, `static/js/app.js`, `static/js/train-monitor.js`

---

### Train Control Panel (Train Editor)

**File:** `editor-trains.js` → `showTrainControlPanel(trainId)`

Opened when a train is selected in the editor. Populated by two parallel API fetches:
- `api.getTrain(trainId)` → `train` object
- `api.getOnboardState(trainId)` → `onboard` object (TrainController state)

If a path is assigned, also fetches `api.getPath(assignedPathId)` and then
`api.getPlatform/getBlock/getStation` to resolve a human-readable destination name.

#### Section: State Info box

| Field shown | Source field | Notes |
|-------------|-------------|-------|
| State (large text) | `onboard.state \|\| train.state` | CSS class colored by state |
| Block badge | `train.block_section_id` | Only shown if non-null |
| Position | `train.position.end_a.tile` or `.end_b.tile` depending on `train.position.leading_end` | Head tile (x, y) |
| Direction | `train.direction \|\| train.facing_direction` | |
| Speed | `onboard.current_speed ?? train.current_speed` / `train.max_speed` | e.g. "12.3 / 100 km/h" |
| Waiting Reason | `train.wait_reason` | Only visible when state = `running_waiting` |

#### Section: Onboard Computer box

| Field shown | Source field | Notes |
|-------------|-------------|-------|
| ATO RUNNING badge | `onboard.ato_running` | Hidden when false |
| Power | `onboard.power_on` | ON / OFF with color class |
| Mode | `onboard.control_mode` | ATO / MANUAL |
| Handle | `onboard.handle_position` | P5–P1, N, B1–B8, EB; colored by handle type |
| Emergency stop badge | `onboard.emergency_stop_active` | Only shown when true |

#### Section: Train Control (buttons)

All buttons call API endpoints and trigger a panel refresh on success.

| Control | Condition | API call |
|---------|-----------|---------|
| Emergency Stop / Reset | Always visible | `api.trainEmergencyStop` / `api.trainEmergencyReset` |
| Power On / Off | Off disabled if `powerOn`; On disabled if not stopped | `api.trainPowerOn` / `api.trainPowerOff` |
| Cab A / B / Off | Disabled if not powered or not stopped | `api.trainSetCab(trainId, 'A'\|'B'\|null)` |
| ATO / Manual mode | Disabled unless stopped (`onboard.is_stopped`) | `api.setControlMode(trainId, 'ato'\|'manual')` |

#### Section: ATO Controls (visible when mode = ATO)

| Field shown | Source | Notes |
|-------------|--------|-------|
| Path name | `path.path_id` (fetched) | Falls back to `onboardAto.path_id` |
| Destination | Platform name → Block name → Station name (chain of fetches) | Falls back to `onboardAto.destination_station_name` |
| Go button | Disabled if not powered or `atoRunning` | `api.trainAtoGo(trainId)` |
| Stop button | Disabled if not `atoRunning` | `api.trainAtoStop(trainId)` |

#### Section: Manual Controls (visible when mode = Manual)

| Field shown | Source | Notes |
|-------------|--------|-------|
| Forward / Reverse buttons | `onboard.travel_direction` | Disabled unless stopped |
| Handle slider | `onboard.handle_position` | Index 0–14: P5 P4 P3 P2 P1 N B1–B8 EB |
| Handle label + description | Derived from slider index | "Accelerating" / "Coast" / "Decelerating" / "Emergency Brake" |
| Handle reason | `onboard.handle_reason` | Diagnostic string from ATO explaining why this handle was chosen |

#### Section: Properties (editable)

| Field | Source | Saves to |
|-------|--------|---------|
| Car Number | `train.car_number` | `api.updateTrain` |
| Length (cm) | `train.length_cm` | `api.updateTrain` |
| Max Speed (km/h) | `train.max_speed` | `api.updateTrain` |

---

### Train Debug Info Panel (inside Train Control Panel)

**File:** `app.js` → `updateTrainDebugPanel(train)`

Injected into `#train-control-debug-section` inside the Train Control Panel.
**Only visible when the debug checkbox** (`#debug-show-train-position`) is checked.
Populated from the `train` object from the network state (not the onboard state).

| Field | Source field |
|-------|-------------|
| Train ID | `train.id` |
| State | `train.state` |
| Length | `train.length_cm` (cm) |
| Path Index | `train.path_index + 1` / `train.path_length` |
| Distance on Track | `train.distance_on_track_cm` (cm) |
| **Current Path tiles** | `train.current_path[]` — each entry: `{x, y, dir}` |
| — current tile | highlighted by matching index to `train.path_index` |
| **End A** subsection | |
| — Tile | `train.position.end_a.tile` `[x, y]` |
| — Distance | `train.position.end_a.distance_cm` |
| — Entry Edge | `train.position.end_a.entry_edge` (N/E/S/W) |
| — Role | `HEAD` if `leading_end == 'a'`, else `TAIL` |
| **End B** subsection | same fields from `train.position.end_b` |
| **Position Info** | |
| — Leading End | `train.position.leading_end` (A or B) |
| — Facing | `train.position.facing_direction` |
| **Occupied Tiles** | `train.position.occupied_tiles[]` — list of `[x, y]` |
| **Occupied Blocks** | `train.position.occupied_blocks[]` — list of block IDs |

---

### Train Monitor Panel

**File:** `train-monitor.js` → `class TrainMonitor`

A bottom bar with:
- A set of scalar text readouts (left side)
- A canvas visualization (centre)
- A scrollable "upcoming elements" list (right side, resizable)
- View distance toggle buttons (100 cm / 200 cm / 500 cm / 1 m / 2 m / Auto)

Shown when a train is selected. Closed via X button or `deselectTrain()`.

#### Data update paths

There are two update methods:

**`update(train)`** — used for initial/fallback rendering.
- Source: `train.lookahead_tile_info[]` and `train.lookahead_tiles[]` from `Train.to_dict()`
- Builds `blocks`, `signals`, `switches`, `stations` arrays by iterating per-tile info
- Falls back to minimal data (speed only) if `lookahead_tile_info` is empty

**`updateFromController(train, payload)`** — used during live simulation (preferred).
- Source: `payload.lookahead` from TrainController tick output
- `payload.lookahead` contains pre-built `blocks`, `signals`, `switches`, `stations`, `speed_curves`, `clearance_cm`, `safety_margin_cm`, `target_stop_cm`

#### Text readouts

| Element | ID | Source | Calculation |
|---------|----|--------|-------------|
| Current speed | `#monitor-current-speed` | `train.speed` (km/h) | Rounded to integer |
| ATC speed limit | `#monitor-atc-speed` | `speed_curves[0].atc_limit` or `train.target_speed` | Rounded to integer |
| ATO operational limit | `#monitor-ato-speed` | `speed_curves[0].operational_limit` or `train.target_speed` | Rounded to integer |
| Scale-adjusted speed | `#monitor-speed-cms` | `current_speed_kmh * 27.78 / currentScale` | cm/s in model scale; `currentScale` loaded via `api.getScale()` |
| Clearance ahead | `#monitor-clearance` | `clearance_cm / 100` | Metres |
| Distance to next stop | `#monitor-next-dest-dist` | `target_stop_cm / 100` | Metres; hidden when 0 |
| Wait reason | `#monitor-wait-reason` | `train.wait_reason` | Only shown when state = `running_waiting` |
| Speed limit reason | `#monitor-speed-limit-reason` | `payload.speed_limit_reason ?? train.speed_limit_reason` | Hidden when `running_waiting` |

#### Canvas layout (rendered each tick by `render()`)

The canvas is divided into three vertical zones:

```
┌─────────────────────────────────────────────────┐
│  ATC Speed Curve area (~55% of height)           │
│  (speed_curves[] drawn as coloured line graph)   │
├─────────────────────────────────────────────────┤  60px fixed
│  Block / signal / switch track line              │
│  (blocks, switches, signals drawn here)          │
├─────────────────────────────────────────────────┤
│  Station track line (~15% of height)             │
│  (only rendered if stations[] is non-empty)       │
└─────────────────────────────────────────────────┘
     distance scale labels along bottom
```

Overlays drawn on top:
- Dark grey fill over area already passed (behind train head)
- Dark grey fill beyond clearance distance (unknown territory)
- Red dashed vertical line at `target_stop_cm` (safe stopping point)
- Red shaded zone between target stop and clearance (safety margin buffer)
- Yellow arrow triangle at train head position

#### Canvas element data sources

**Blocks** (from `lookaheadData.blocks[]`)

| Field used | Comes from |
|-----------|-----------|
| `id`, `name` | `lookahead_tile_info[i].block_id` |
| `state` | `lookahead_tile_info[i].block_state` |
| `start_cm` | `lookahead_tile_info[i].distance_to_block_start_cm` (can be negative = train already inside) |
| `length_cm` | sum of `length_cm` across consecutive tiles in same block |
| `tile_count` | count of consecutive tiles in same block |

Drawn as: filled + outlined rectangle, colour-coded by state:
- `free` → green `#04d361`
- `reserved` → purple `#8257e5`
- `occupied` → red `#e83f5b`
- `caution` → yellow `#ffcd1e`

Labels inside block: block name (truncated to fit).
Labels below block: `{length}m ({N} tiles)` and `{start}m - {end}m` position range.

**Signals** (from `lookaheadData.signals[]`)

| Field used | Comes from |
|-----------|-----------|
| `id`, `name` | `lookahead_tile_info[i].signals[j].id/name` |
| `state` | `lookahead_tile_info[i].signals[j].state` |
| `distance_cm` | `max(0, distance_to_tile_cm)` |

Filter: only signals whose `detection_direction` matches the train's `travel_direction` on that tile.

Drawn as: vertical pole with circle at top, colour-coded by state.
- `double_yellow` → second circle drawn above first.

**Switches** (from `lookaheadData.switches[]`)

| Field used | Comes from |
|-----------|-----------|
| `name` | `lookahead_tile_info[i].switch_name` or `"Switch (x,y)"` |
| `position` | `lookahead_tile_info[i].switch_state` (0=main, 1=diverging) |
| `distance_cm` | `max(0, distance_to_tile_cm)` |
| `locked` | `lookahead_tile_info[i].switch_locked` |

Drawn as: X-cross symbol, green if main, yellow if diverging.
Orange ring around symbol if `locked = true`.

**Stations** (from `lookaheadData.stations[]`)

| Field used | Comes from |
|-----------|-----------|
| `name` | station name |
| `distance_cm` | distance from head to station start |
| `length_cm` | platform length |

Drawn as: blue filled + outlined rectangle on the separate station track row.

**Speed curves** (from `lookaheadData.speed_curves[]`, only in `updateFromController` path)

Each curve point: `{atc_limit, operational_limit, distance_cm}`.
- ATC limit drawn as one coloured curve in the top speed area
- Operational (ATO) limit drawn as a second curve

#### Upcoming Elements list (`updateUpcomingElements`)

Collects all blocks, stations, signals, switches into a flat list.
Sorted by `distance_cm` ascending (then by `sequence` for ties).

Each row shows:
- Icon: `▣` block / `🚉` station / `⇋` switch / `🚦` signal
- Name
- Distance to start in metres
- Distance to end in metres (for area elements)
- Length in metres (for area elements)

#### Configuration warnings

If `lookaheadData.warnings[]` is present, shows toast notifications for:
- `block_length_not_configured` — block has no length set
- `station_length_not_configured` — platform has no length set
- `switch_not_configured` — switch at position has no length for its current state
- `switch_entity_missing` — switch tile has no Switch entity
- `block_not_found` / `station_not_found` — entity missing from network

Warnings are de-duplicated across ticks (only shown once per unique set).

---

*End of master design document.*
