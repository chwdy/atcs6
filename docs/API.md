# ATCS6 REST API Documentation

Base URL: `http://localhost:5001`

## Infrastructure Endpoints

### GET /

Serves the main application HTML page.

**Response:** `index.html` (Content-Type: text/html)

### GET /<path:path>

Serves static files (CSS, JavaScript, images, etc.) from the `static/` directory.

**Examples:**

- `GET /css/style.css` - Stylesheet
- `GET /js/app.js` - JavaScript application code
- `GET /js/api.js` - API client library
- `GET /images/logo.png` - Images

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

**Note:** The `length` parameter creates a single LOCOMOTIVE car with that length. For more complex consists, use the internal TrainCar model:

```python
# TrainCar model (internal)
TrainCar(
    car_id: str,
    car_type: str,    # "LOCOMOTIVE", "PASSENGER", "FREIGHT"
    length: float,    # Length in meters
    mass: float,      # Mass in kg
    max_speed: float  # Max speed for this car
)
```

**Response:**

```json
{
  "status": "success",
  "train": {
    "id": "train_1",
    "length": 1.0,
    "speed": 0,
    "max_speed": 100.0,
    "status": "IDLE",
    "position": {"x": 0, "y": 0},
    "direction": "NORTH"
  }
}
```

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
- Tracks cannot be station tiles
- Tracks with signals require `force: true` to proceed (signals will be removed)
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