# ATCS6 REST API Documentation

Base URL: `http://localhost:5001`

## Infrastructure Endpoints

### GET /

Serves the main application HTML page.

**Response:** `index.html` (Content-Type: text/html)

### GET /<path:path>

Serves static files (CSS, JavaScript, images, etc.) from the `static/` directory.

**Examples:**

- `GET /static/css/style.css` - Stylesheet
- `GET /static/js/app.js` - JavaScript application code
- `GET /static/js/api.js` - API client library
- `GET /static/images/logo.png` - Images

**Response:** File contents with appropriate Content-Type header

---

## Network Endpoints

### GET /api/network

Get complete railway network state.

**Response:**

```json
{
  "tracks": [{
    "x": 0,
    "y": 0,
    "track_bits": 3,
    "dead_end_ports": ["N"],
    "type": "track",
    "is_switch": false,
    "edges": ["N", "S", "E", "W"],
    "switch_points": {}
  }],
  "trains": [{
    "id": "train_1",
    "position": {"x": 0, "y": 0},
    "direction": "NORTH",
    "speed": 0
  }]
}
```

---

## Track Endpoints

### GET /api/track/{x}/{y}

Get track at coordinates. Supports negative coordinates: `/api/track/-5/10`

**Response:**

```json
{
  "x": 0,
  "y": 0,
  "track_bits": 3,
  "dead_end_ports": ["N"],
  "type": "track",
  "is_switch": false,
  "edges": ["N", "S"],
  "switch_points": {},
  "station_name": null,
  "block_section_id": null,
  "signal_ids": []
}
```

**Error (404):**

```json
{
  "error": "Track not found"
}
```

### PATCH /api/track/{x}/{y}

Update track properties.

**Request:**

```json
{
  "track_bits": 7,
  "switch_state": 0,
  "dead_end_ports": ["N"],
  "station_name": "Station A",
  "block_section_id": "BLK_001"
}
```

| Field | Type | Description |
|-------|------|-------------|
| track_bits | int | New track geometry |
| switch_state | int | 0 = MAIN, 1 = DIVERGE |
| dead_end_ports | array | Manual dead-end override |
| station_name | string | Station name (or null to clear) |
| block_section_id | string | Block ID (or null to clear) |

**Note:** Signal management via /api/track is deprecated. Use `/api/signal` endpoints instead.

**Response:**

```json
{
  "status": "success",
  "track": { ... }
}
```

### DELETE /api/track/{x}/{y}

Remove track segment.

**Response:**

```json
{
  "status": "success",
  "message": "Track at (0, 0) removed"
}
```

### POST /api/tracks/bulk

Add multiple tracks at once (used for drag-to-build).

**Request:**

```json
{
  "tracks": [
    {"x": 0, "y": 0, "track_bits": 1},
    {"x": 0, "y": 1, "track_bits": 1},
    {"x": 0, "y": 2, "track_bits": 5}
  ]
}
```

**Response:**

```json
{
  "status": "success",
  "added": 2,
  "updated": 1,
  "tracks": [...]
}
```

**Notes:**
- If track exists at position, new bits are merged (OR operation)
- Forbidden configurations are silently skipped
- Changes propagate to neighbors automatically

---

## Train Endpoints

Trains use a DC analog control model with a state machine for realistic operation.

### Train States

| State | Description |
|-------|-------------|
| `off` | Train is powered off |
| `idle` | Powered on but no direction set |
| `standby` | Direction set, ready to depart |
| `running_moving` | Currently moving under ATC control |
| `running_waiting` | Stopped waiting for path/signal |
| `running_at_station` | Stopped at a station platform |
| `manual` | Manual control mode (direct speed control) |

### POST /api/trains

Create a new train. Trains use a consist model where the train is composed of individual cars.

**Request:**

```json
{
  "id": "train_1",
  "length": 1.0,
  "max_speed": 100.0,
  "position": {"x": 0, "y": 0},
  "direction": "NORTH"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| id | string | Required | Unique train identifier |
| length | float | 1.0 | Train length (used to create a LOCOMOTIVE car) |
| max_speed | float | 100.0 | Maximum operational speed (km/h) |
| position | object | null | Initial {x, y} position |
| direction | string | null | NORTH, EAST, SOUTH, WEST |

**Response:**

```json
{
  "status": "success",
  "train": {
    "id": "train_1",
    "length": 1.0,
    "speed": 0,
    "max_speed": 100.0,
    "state": "off",
    "position": {"x": 0, "y": 0},
    "direction": "NORTH",
    "facing_direction": "NORTH",
    "travel_direction": "forward",
    "manual_target_speed": 0
  }
}
```

### GET /api/trains

List all trains in the network.

**Response:**

```json
{
  "trains": [
    {
      "id": "train_1",
      "state": "idle",
      "position": {"x": 0, "y": 0},
      "facing_direction": "NORTH"
    }
  ]
}
```

### GET /api/trains/{train_id}

Get detailed information about a specific train.

**Response:** Train object with all properties

**Error (404):** `{"error": "Train not found"}`

### DELETE /api/trains/{train_id}

Delete a train from the network.

**Response:** `{"success": true}`

### POST /api/trains/{train_id}/power-on

Power on a train. Transitions from `off` to `idle` state.

**Response:**

```json
{
  "success": true,
  "state": "idle",
  "message": "Train powered on"
}
```

### POST /api/trains/{train_id}/power-off

Power off a train. Only works when train is stopped.

**Response:**

```json
{
  "success": true,
  "state": "off",
  "message": "Train powered off"
}
```

**Error (400):** `{"error": "Cannot power off while moving"}`

### POST /api/trains/{train_id}/set-direction

Set train facing direction. Transitions from `idle` to `standby` state.

**Request:**

```json
{
  "direction": "N"
}
```

| Field | Type | Description |
|-------|------|-------------|
| direction | string | N, E, S, W (or NORTH, EAST, SOUTH, WEST) |

**Response:**

```json
{
  "success": true,
  "state": "standby",
  "facing_direction": "N"
}
```

### POST /api/trains/{train_id}/depart

Start train moving. Transitions from `standby` to `running_moving` state.

**Response:**

```json
{
  "success": true,
  "state": "running_moving",
  "message": "Train departing"
}
```

### POST /api/trains/{train_id}/arrive

Signal that train is arriving at station. Transitions to `running_at_station` state.

**Response:**

```json
{
  "success": true,
  "state": "running_at_station",
  "message": "Train arrived at station"
}
```

### POST /api/trains/{train_id}/turn-around

Flip the train's facing direction 180 degrees. Only works in `standby` state.

**Response:**

```json
{
  "success": true,
  "facing_direction": "S",
  "message": "Train turned around"
}
```

### POST /api/trains/{train_id}/enable-manual

Enable manual control mode. Bypasses ATC for direct speed control.

**Response:**

```json
{
  "success": true,
  "state": "manual",
  "message": "Manual mode enabled"
}
```

### POST /api/trains/{train_id}/disable-manual

Disable manual control mode. Returns to `idle` state.

**Response:**

```json
{
  "success": true,
  "state": "idle",
  "message": "Manual mode disabled"
}
```

### POST /api/trains/{train_id}/manual-speed

Set target speed in manual mode.

**Request:**

```json
{
  "speed": 50.0
}
```

| Field | Type | Description |
|-------|------|-------------|
| speed | float | Target speed in km/h (0 to max_speed) |

**Response:**

```json
{
  "success": true,
  "manual_target_speed": 50.0
}
```

### POST /api/trains/{train_id}/travel-direction

Set travel direction in manual mode (forward or reverse relative to facing direction).

**Request:**

```json
{
  "direction": "forward"
}
```

| Field | Type | Description |
|-------|------|-------------|
| direction | string | `forward` or `reverse` |

**Response:**

```json
{
  "success": true,
  "travel_direction": "forward"
}
```

### POST /api/trains/{train_id}/emergency-stop

Activate emergency stop. Immediately stops the train.

**Response:**

```json
{
  "success": true,
  "state": "running_waiting",
  "message": "Emergency stop activated"
}
```

### GET /api/trains/{train_id}/lookahead

Get lookahead data for train monitor visualization. Returns blocks, signals, stations, and speed limits ahead of the train up to a specified distance.

**Query Parameters:**
- `distance_cm` (optional): Maximum lookahead distance in cm (default: 100000 = 1km)

**Response:**

```json
{
  "train_id": "TRAIN_01",
  "current_speed_kmh": 45.5,
  "target_speed_kmh": 60.0,
  "max_speed_kmh": 100.0,
  "facing_direction": "EAST",
  "travel_direction": "forward",
  "state": "running_moving",
  "lookahead_items": [
    {
      "type": "signal",
      "id": "SIG_001",
      "distance_cm": 500.0,
      "aspect": "green",
      "direction": "E"
    },
    {
      "type": "block",
      "id": "BLK_A",
      "distance_cm": 1000.0,
      "state": "free",
      "length_cm": 5000.0
    },
    {
      "type": "station",
      "id": "STA_MAIN",
      "distance_cm": 3500.0,
      "name": "Main Station",
      "platform_side": "right"
    },
    {
      "type": "speed_limit",
      "distance_cm": 2000.0,
      "speed_kmh": 40.0,
      "reason": "curve"
    }
  ],
  "clearance_cm": 8500.0,
  "atc_target_speed_kmh": 60.0
}
```

**Lookahead Item Types:**

| Type | Fields | Description |
|------|--------|-------------|
| `signal` | id, distance_cm, aspect, direction | Signal ahead with current aspect |
| `block` | id, distance_cm, state, length_cm | Block section with occupancy state |
| `station` | id, distance_cm, name, platform_side | Station platform ahead |
| `speed_limit` | distance_cm, speed_kmh, reason | Speed restriction (curve, signal, etc.) |

**Error (404):**

```json
{
  "error": "Train not found"
}
```

---

## Train Routes Endpoints

Train routes are travel plans for trains, consisting of multiple waypoints (stations) with specific timing and dwell instructions. This is distinct from dispatcher paths, which are interlocking routes.

### GET /api/train-routes

List all defined train routes in the network.

**Response:**

```json
{
  "routes": [
    {
      "route_id": "ROUTE-ABC123",
      "name": "Main Line Express",
      "waypoints": [
        {
          "station_id": "STA_A",
          "dwell_time_sec": 30.0,
          "timing_mode": "none",
          "target_time": null
        }
      ],
      "mode": "loop",
      "is_active": true,
      "current_waypoint_index": 0
    }
  ]
}
```

### POST /api/train-routes

Create a new train route travel plan.

**Request:**

```json
{
  "name": "Morning Commuter",
  "mode": "loop",
  "waypoints": [
    {
      "station_id": "STA_A",
      "dwell_time_sec": 30.0,
      "timing_mode": "none"
    },
    {
      "station_id": "STA_B",
      "dwell_time_sec": 45.0,
      "timing_mode": "relative",
      "target_time": 300
    }
  ]
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| name | string | Yes | "" | Name of the route |
| mode | string | No | "one_way" | `one_way`, `loop`, or `round_trip` |
| waypoints | array | Yes | [] | List of waypoint objects |
| route_id | string | No | Auto | Custom route identifier |

**Waypoint Object:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| station_id | string | Yes | - | Station identifier |
| dwell_time_sec | float | No | 30.0 | Stop duration at station |
| timing_mode | string | No | "none" | `none`, `relative`, or `absolute` |
| target_time | float | No | null | Target arrival/departure time |

**Response:**

```json
{
  "status": "success",
  "route": { ... }
}
```

### GET /api/train-routes/{route_id}

Get detailed information about a specific route.

**Response:** Route object (JSON)

**Error (404):** `{"error": "Route not found"}`

### DELETE /api/train-routes/{route_id}

Delete a train route and unassign it from any trains.

**Response:**

```json
{
  "status": "success",
  "message": "Route ROUTE-ABC123 deleted"
}
```

**Error (404):** `{"error": "Route not found"}`

### PUT /api/trains/{train_id}/assign-route

Assign a travel plan route to a specific train.

**Request:**

```json
{
  "route_id": "ROUTE-ABC123"
}
```

**Note:** Set `route_id` to `null` to unassign the current route.

**Response:**

```json
{
  "status": "success",
  "train": { ... }
}
```

**Error (404):** `{"error": "Train not found"}` or `{"error": "Route not found"}`

### POST /api/train-routes/{route_id}/activate

Start or resume route execution for any trains assigned to this route.

**Response:**

```json
{
  "status": "success",
  "route": {
    "route_id": "ROUTE-ABC123",
    "is_active": true,
    ...
  }
}
```

### POST /api/train-routes/{route_id}/deactivate

Pause route execution.

**Response:**

```json
{
  "status": "success",
  "route": {
    "route_id": "ROUTE-ABC123",
    "is_active": false,
    ...
  }
}
```

---

## Schedule Endpoints

Schedules define time-based plans for trains, consisting of a list of station stops with arrival/departure times. Schedules are stored in the network and can be assigned to a train.

### Schedule Object Format

Schedules are returned in the following JSON format:

```json
{
  "schedule_id": "SCH-ABC123",
  "train_id": "train_1",
  "stops": [
    {
      "station_pos": [0, 0],
      "station_name": "Station A",
      "arrival_time": "2024-01-03T12:00:00",
      "departure_time": "2024-01-03T12:02:00",
      "platform": null
    }
  ],
  "start_time": "2024-01-03T12:00:00",
  "end_time": "2024-01-03T14:00:00",
  "priority": 0
}
```

**Stop Object:**

| Field | Type | Description |
|-------|------|-------------|
| station_pos | array | `[x, y]` coordinates of the station tile |
| station_name | string | Human-readable station name |
| arrival_time | string \| null | ISO 8601 arrival timestamp |
| departure_time | string \| null | ISO 8601 departure timestamp |
| platform | string \| null | Optional platform identifier |

**Notes:**
- Timestamps use ISO 8601 strings (parsed by Python `datetime.fromisoformat()`).
- If `arrival_time`/`departure_time` are omitted or invalid when creating a schedule, the server assigns defaults (current time for arrival, and +2 minutes for departure).

### GET /api/schedules

List all train schedules.

**Response:**

```json
{
  "schedules": [
    {
      "schedule_id": "SCH-ABC123",
      "train_id": "train_1",
      "stops": [
        {
          "station_pos": [0, 0],
          "station_name": "Station A",
          "arrival_time": "2024-01-03T12:00:00",
          "departure_time": "2024-01-03T12:02:00",
          "platform": null
        }
      ],
      "start_time": "2024-01-03T12:00:00",
      "end_time": "2024-01-03T14:00:00",
      "priority": 0
    }
  ]
}
```

### POST /api/schedules

Create a new schedule and assign it to a train.

**Request:**

```json
{
  "train_id": "train_1",
  "schedule_id": "SCH-ABC123",
  "priority": 0,
  "stops": [
    {
      "station_name": "Station A",
      "station_pos": [0, 0],
      "arrival_time": "2024-01-03T12:00:00",
      "departure_time": "2024-01-03T12:02:00",
      "platform": null
    }
  ]
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| train_id | string | Yes | - | Train to assign the schedule to |
| schedule_id | string | No | Auto | Custom schedule identifier (e.g., `SCH-ABC123`) |
| priority | int | No | 0 | Schedule priority |
| stops | array | No | [] | List of stop objects |

**Response:**

```json
{
  "status": "success",
  "schedule_id": "SCH-ABC123",
  "message": "Schedule created and assigned to train train_1"
}
```

**Error (400):**

```json
{
  "error": "train_id is required"
}
```

**Error (404):**

```json
{
  "error": "Train 'train_1' not found"
}
```

### GET /api/schedules/{schedule_id}

Get a specific schedule by ID.

**Response:** Schedule object (JSON)

**Error (404):** `{"error": "Schedule not found"}`

### DELETE /api/schedules/{schedule_id}

Delete a schedule. If the schedule is currently assigned to its train, the train's schedule assignment is cleared.

**Response:**

```json
{
  "status": "success",
  "message": "Schedule SCH-ABC123 deleted"
}
```

**Error (404):** `{"error": "Schedule not found"}`

---

## Simulation Endpoints

### POST /api/simulation/control

Control simulation execution.

**Request:**

```json
{
  "action": "start",
  "speed": 1.0
}
```

| Action | Description |
|--------|-------------|
| start | Start simulation loop |
| stop | Pause simulation |
| reset | Reset to initial state |
| set_speed | Change simulation speed multiplier |

**Response:**

```json
{
  "status": "success",
  "running": true,
  "speed": 1.0
}
```

---

## Block Endpoints

Blocks are track sections used for signaling and train separation.

### GET /api/blocks

List all blocks with track counts and positions.

**Response:**

```json
[
  {
    "block_id": "BLK_abc123",
    "name": "Main Line Block 1",
    "state": "FREE",
    "track_count": 5,
    "positions": [[0, 0], [0, 1], [0, 2], [0, 3], [0, 4]],
    "signal_count": 2
  }
]
```

### POST /api/blocks

Create a new empty block.

**Request:**

```json
{
  "block_id": "BLK_MAIN_1",
  "name": "Main Line Block 1"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| block_id | string | No | Custom block ID (auto-generated if not provided) |
| name | string | No | Human-readable name |

**Response:**

```json
{
  "success": true,
  "block_id": "BLK_MAIN_1"
}
```

### POST /api/blocks/create-with-tracks

Create a new block with validated track assignments.

**Request:**

```json
{
  "name": "Platform Block",
  "positions": [[0, 0], [0, 1], [0, 2]]
}
```

**Response:**

```json
{
  "success": true,
  "block_id": "BLK_abc123",
  "tracks_assigned": 3
}
```

**Validation:**
- All positions must have tracks
- All tracks must form a continuous connected path
- Tracks cannot already belong to another block

### GET /api/blocks/{block_id}

Get block details including validation status and connected signals.

**Response:**

```json
{
  "block_id": "BLK_abc123",
  "name": "Main Block",
  "state": "FREE",
  "track_count": 3,
  "positions": [[0, 0], [0, 1], [0, 2]],
  "is_valid": true,
  "signals": ["SIG_001", "SIG_002"]
}
```

### PATCH /api/blocks/{block_id}

Update block properties.

**Request:**

```json
{
  "name": "New Block Name",
  "state": "RESERVED"
}
```

| Field | Type | Description |
|-------|------|-------------|
| name | string | Block name |
| state | string | FREE, RESERVED, OCCUPIED, or CAUTION |

**Response:** Updated block object

### DELETE /api/blocks/{block_id}

Delete block and clear track references.

**Response:** `{"success": true}`

### POST /api/blocks/{block_id}/cycle-state

Cycle block state through: FREE → RESERVED → OCCUPIED → CAUTION → FREE

**Response:**

```json
{
  "success": true,
  "new_state": "RESERVED"
}
```

### POST /api/blocks/{block_id}/extend

Extend an existing block with new track positions.

**Request:**

```json
{
  "positions": [[0, 3], [0, 4]],
  "force": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| positions | array | Yes | List of [x, y] coordinates to add |
| force | boolean | No | If true, removes signals in the path to allow extension |

**Validation Rules:**
- Block must exist
- All positions must have tracks
- Tracks cannot be switches (switches are block boundaries)
- Tracks cannot be crossings (+ or X shape - crossings isolate paths)
- **Block cannot cross station boundaries** (blocks must be fully inside or fully outside stations)
- Tracks with signals require `force: true` to proceed (signals will be invalidated)
- New positions must be adjacent to existing block
- Extended block must remain continuous

**Response (success):**

```json
{
  "block_id": "BLK_abc123",
  "name": "Main Block",
  "state": "FREE",
  "track_count": 5
}
```

**Response with signal invalidation (when force=true):**

When extending a block over tiles that have signals, the signals are not removed.
Instead, their `monitored_block_id` is cleared and they are set to RED state.
These invalidated signals will show an orange warning triangle in the UI until
they are reconfigured to monitor a block.

```json
{
  "block_id": "BLK_abc123",
  "name": "Main Block",
  "state": "FREE",
  "track_count": 5,
  "invalidated_signals": [
    {
      "signal_id": "SIG_001",
      "position": [0, 3],
      "direction": "NORTH",
      "previous_block": "BLK_old456"
    }
  ]
}
```

**Error Response:**

```json
{
  "error": "Track at (0, 3): Has signal(s) - use force=true to invalidate and extend"
}
```

### POST /api/blocks/{block_id}/tracks

Add tracks to a block by positions.

**Request:**

```json
{
  "positions": [[0, 3], [0, 4]],
  "force": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| positions | array | List of [x, y] coordinates |
| force | boolean | If true, reassign tracks from other blocks |

### DELETE /api/blocks/{block_id}/tracks

Remove tracks from a block.

**Request:**

```json
{
  "positions": [[0, 3], [0, 4]]
}
```

**Response:** `{"success": true, "removed": 2}`

### GET /api/blocks/{block_id}/end_tiles

Get the start and end tiles of a block sorted by orientation.

**Response:**

```json
{
  "start_tile": [0, 0],
  "end_tile": [0, 4],
  "orientation": "NS"
}
```

### POST /api/blocks/{block_id}/can_extend

Check if a position can be added to extend a block.

**Request:**

```json
{
  "x": 0,
  "y": 5
}
```

**Response:**

```json
{
  "can_extend": true,
  "reason": null
}
```

**Error Response:**

```json
{
  "can_extend": false,
  "reason": "Track is a switch - switches are block boundaries"
}
```

---

## Signal Endpoints

Signals control train movements and are associated with blocks.

### POST /api/signal

Create a new signal with validation.

**Request:**

```json
{
  "x": 0,
  "y": 0,
  "detection_direction": "N",
  "signal_type": "block",
  "name": "Home Signal",
  "auto_connect": true
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| x | int | Yes | - | X coordinate |
| y | int | Yes | - | Y coordinate |
| detection_direction | string | Yes | - | N, E, S, W (direction the signal detects trains from) |
| signal_type | string | No | "block" | Signal type |
| name | string | No | null | Human-readable name |
| auto_connect | boolean | No | true | Auto-connect to reachable block |

**Response:**

```json
{
  "success": true,
  "signal": {
    "signal_id": "SIG_abc123",
    "x": 0,
    "y": 0,
    "direction": "NORTH",
    "signal_type": "block",
    "state": "RED",
    "monitored_block_id": "BLK_001"
  }
}
```

**Validation Errors:**
- No track at position
- Track has no valid exit in specified direction
- Signal already exists at this position/direction

### GET /api/signal/{signal_id}

Get signal by ID.

**Response:** Signal object

### GET /api/signal/{x}/{y}

Get signal at position.

**Response:** Signal object or 404

### PATCH /api/signal/{signal_id}

Update signal properties.

**Request:**

```json
{
  "state": "GREEN",
  "name": "Updated Name",
  "monitored_block_id": "BLK_002"
}
```

### DELETE /api/signal/{signal_id}

Delete signal by ID.

**Response:** `{"success": true}`

### DELETE /api/signal/{x}/{y}

Delete signal at position.

**Response:** `{"success": true}`

### GET /api/signal/validate/{x}/{y}

Check if signal can be placed at position and get valid directions.

**Response:**

```json
{
  "can_place": true,
  "valid_directions": ["NORTH", "SOUTH"],
  "existing_block": "BLK_001"
}
```

### GET /api/signal/{signal_id}/reachable-blocks

Get all blocks reachable from a signal (for manual connection UI).

**Response:**

```json
{
  "blocks": [
    {"block_id": "BLK_001", "distance": 0},
    {"block_id": "BLK_002", "distance": 3}
  ]
}
```

---

## Bulk Track Operations

### POST /api/tracks/bulk-delete

Delete multiple tracks at once.

**Request:**

```json
{
  "positions": [[0, 0], [0, 1], [0, 2]]
}
```

**Response:**

```json
{
  "status": "success",
  "deleted": 3
}
```

---

## Station Endpoints

Stations are multi-tile platform entities with physical properties.

### POST /api/station

Create a new station at specified position.

**Request:**

```json
{
  "x": 0,
  "y": 0,
  "station_id": "STA_MAIN",
  "name": "Main Station",
  "length_cm": 100.0,
  "platform_side": "both",
  "top_speed_kmh": 30.0
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| x | int | Yes | - | X coordinate |
| y | int | Yes | - | Y coordinate |
| station_id | string | No | Auto | Custom station ID |
| name | string | No | "" | Human-readable name |
| length_cm | float | No | 0.0 | Physical track length |
| platform_side | string | No | "both" | `left`, `right`, or `both` |
| top_speed_kmh | float | No | null | Speed limit |

**Response:** Station object (same as GET response)

### GET /api/station/{station_id}

Get station by ID.

**Response:** Station object

**Error (404):** `{"error": "Station not found"}`

### PATCH /api/station/{station_id}

Update station properties.

**Request:**

```json
{
  "name": "New Name",
  "length_cm": 200.0,
  "platform_side": "left",
  "top_speed_kmh": 50.0
}
```

**Response:** Updated station object

### DELETE /api/station/{station_id}

Delete station.

**Response:** `{"success": true}`

### GET /api/station/validate/{x}/{y}

Check if station can be placed at position.

**Response:**

```json
{
  "can_place": true,
  "existing_block": "BLK_001"
}
```

**Validation errors:**
- No track at position
- Cannot place on switch
- Cannot place on tile with signal
- Tile already belongs to station

### POST /api/station/{station_id}/extend

Extend station platform with new tiles.

**Request:**

```json
{
  "positions": [[0, 3], [0, 4]]
}
```

**Response:** Updated station object

**Validation:**
- New tiles must be adjacent to existing station
- All tiles must form continuous path

### POST /api/station/{station_id}/remove_tile

Remove an edge tile from a station.

**Request:**

```json
{
  "x": 0,
  "y": 4
}
```

**Response:**

```json
{
  "success": true,
  "message": "Tile (0, 4) removed from station"
}
```

### GET /api/station/{station_id}/end_tiles

Get the start and end tiles of a station sorted by orientation.

**Response:**

```json
{
  "start_tile": [0, 0],
  "end_tile": [0, 4],
  "orientation": "NS"
}
```

### POST /api/station/{station_id}/can_extend

Check if a position can be added to extend a station.

**Request:**

```json
{
  "x": 0,
  "y": 5
}
```

**Response:**

```json
{
  "can_extend": true,
  "reason": null
}
```

### POST /api/station/{station_id}/assign_tile

Assign a station tile to a block.

**Request:**

```json
{
  "x": 0,
  "y": 0,
  "block_id": "BLK_001"
}
```

**Response:**

```json
{
  "success": true,
  "message": "Tile (0, 0) assigned to block BLK_001"
}
```

### POST /api/station/{station_id}/unassign_tile

Remove block assignment from a station tile.

**Request:**

```json
{
  "x": 0,
  "y": 0
}
```

**Response:**

```json
{
  "success": true,
  "message": "Block assignment removed from tile (0, 0)"
}
```

### GET /api/station/{station_id}/block_assignments

Get all block assignments for a station.

**Response:**

```json
{
  "assignments": {
    "(0, 0)": "BLK_001",
    "(0, 1)": "BLK_001",
    "(0, 2)": "BLK_002"
  }
}
```

---

## Path and Dispatcher Endpoints

Paths are interlocking routes through the network with block reservations and switch locking.

**Note:** The API uses `/api/paths` for dispatcher paths (interlocking routes). Train travel plans use `/api/train-routes`.

### GET /api/paths

List all active paths.

**Response:**

```json
{
  "paths": [
    {
      "path_id": "PATH_abc123",
      "train_id": "train_1",
      "status": "ACTIVE",
      "block_sequence": ["BLK_001", "BLK_002", "BLK_003"],
      "current_block_index": 1
    }
  ]
}
```

### POST /api/paths

Create a new path between two blocks.

**Request:**

```json
{
  "start_block_id": "BLK_001",
  "end_block_id": "BLK_003",
  "train_id": "train_1"
}
```

**Response:**

```json
{
  "success": true,
  "path": {
    "path_id": "PATH_abc123",
    "status": "SETTING",
    "block_sequence": ["BLK_001", "BLK_002", "BLK_003"]
  }
}
```

### GET /api/paths/{path_id}

Get path details.

**Response:** Path object

### DELETE /api/paths/{path_id}

Cancel and release a path.

**Response:** `{"success": true}`

### PUT /api/paths/{path_id}/extend

Extend an existing path with additional blocks.

**Request:**

```json
{
  "block_id": "BLK_004"
}
```

**Response:** Updated path object

### GET /api/paths/{path_id}/reachable-extensions

Get blocks reachable for extending a path (direction-aware).

**Response:**

```json
{
  "reachable_blocks": [
    {
      "block_id": "BLK_004",
      "distance": 1,
      "direction": "N"
    }
  ],
  "exit_direction": "N"
}
```

### POST /api/paths/{path_id}/complete

Manually complete a path and release all resources.

**Response:**

```json
{
  "success": true,
  "message": "Path completed"
}
```

### GET /api/dispatcher/reachable/{block_id}

Get all endpoint blocks reachable from a given block.

**Response:**

```json
{
  "reachable": [
    {
      "block_id": "BLK_002",
      "distance": 3
    }
  ]
}
```

### POST /api/dispatcher/paths

Get all possible paths between two blocks.

**Request:**

```json
{
  "start_block_id": "BLK_001",
  "end_block_id": "BLK_003"
}
```

**Response:**

```json
{
  "paths": [
    {
      "block_sequence": ["BLK_001", "BLK_002", "BLK_003"],
      "distance": 5
    }
  ]
}
```

---

## Layout Persistence Endpoints

The system automatically saves layouts every 5 minutes to `layouts/_autosave.json`. Layouts are saved as JSON files in the `layouts/` directory. Each file contains both a snapshot (for fast loading) and construction steps (for recovery if the snapshot format changes).

### GET /api/layouts

List all saved layouts.

**Response:**

```json
{
  "layouts": [
    {
      "name": "My Layout",
      "filename": "my_layout",
      "description": "A sample layout",
      "modified": "2024-01-03T12:00:00",
      "size_bytes": 12345,
      "has_snapshot": true,
      "has_steps": true
    }
  ]
}
```

### POST /api/layouts

Save the current network state as a layout.

**Request:**

```json
{
  "name": "My Layout",
  "description": "A sample layout"
}
```

**Response:**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' saved successfully",
  "path": "layouts/my_layout.json",
  "steps_generated": true,
  "steps_count": 42
}
```

**Response with warning (if steps generation fails):**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' saved successfully",
  "path": "layouts/my_layout.json",
  "steps_generated": false,
  "steps_count": 0,
  "warning": "Steps could not be generated: error message"
}
```

### POST /api/layouts/{name}/load

Load a layout, replacing the current network state.

**Response (snapshot load):**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' loaded successfully",
  "method": "snapshot"
}
```

**Response (recovery mode - when snapshot fails):**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' recovered from steps (snapshot was corrupted)",
  "method": "steps",
  "recovery": true
}
```

**Error Response:**

```json
{
  "error": "Layout not found" | "Failed to load layout",
  "method": "snapshot" | "steps" | null
}
```

### DELETE /api/layouts/{name}

Delete a saved layout.

**Response:**

```json
{
  "status": "success",
  "message": "Layout 'My Layout' deleted"
}
```

**Error (404):**

```json
{
  "error": "Layout 'My Layout' not found"
}
```

### POST /api/network/clear

Clear the current network (empty canvas).

**Response:**

```json
{
  "status": "success",
  "message": "Network cleared",
  "cleared": {
    "tracks": 100,
    "blocks": 10,
    "signals": 20,
    "stations": 5,
    "trains": 2
  }
}
```

### GET /api/network/stats

Get counts of items in the current network.

**Response:**

```json
{
  "tracks": 100,
  "blocks": 10,
  "signals": 20,
  "stations": 5,
  "trains": 2,
  "is_empty": false
}
```

---

## Track Bits Reference

| Bit | Value | Path | Description |
|-----|-------|------|-------------|
| 0 | 1 | NS | North-South (vertical) |
| 1 | 2 | EW | East-West (horizontal) |
| 2 | 4 | NE | North-East diagonal |
| 3 | 8 | NW | North-West diagonal |
| 4 | 16 | SE | South-East diagonal |
| 5 | 32 | SW | South-West diagonal |

**Examples:**
- `track_bits: 1` = NS path only (vertical track)
- `track_bits: 3` = NS + EW (crossover)
- `track_bits: 5` = NS + NE (Y-junction)
- `track_bits: 36` = NE + SW (diagonal crossing)

**Forbidden Configurations:**
- 3-way switches (e.g., NS + NE + NW)
- Invalid diagonal combinations
- See `Reference_6Bit_Track_System.md` for complete list

---

## Error Responses

All errors return JSON with `error` field:

```json
{
  "error": "Error message",
  "success": false
}
```

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad request (invalid data, forbidden configuration) |
| 404 | Track not found |
| 500 | Server error |

---