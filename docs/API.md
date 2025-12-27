# ATCS6 REST API Documentation

Base URL: `http://localhost:5000`

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

### POST /api/network/update

Load/restore network state.

**Request Body:** Same format as GET /api/network response

**Response:**

```json
{
  "status": "success",
  "message": "Network updated"
}
```

---

## Track Endpoints

### POST /api/track

Create a new track segment.

**Request:**

```json
{
  "x": 0,
  "y": 0,
  "track_bits": 3,
  "signal_state": "GREEN",
  "signal_direction": "NORTH",
  "station_name": "Main Station"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| x | int | Yes | X coordinate |
| y | int | Yes | Y coordinate |
| track_bits | int | No | 6-bit track geometry (0-63) |
| signal_state | string | No | RED, GREEN, YELLOW, DOUBLE_YELLOW |
| signal_direction | string | No | NORTH, EAST, SOUTH, WEST |
| station_name | string | No | Station name |

**Response:**

```json
{
  "success": true,
  "track_bits": 3,
  "dead_end_ports": ["N", "S"]
}
```

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
  "signal_state": null,
  "signal_direction": null,
  "station_name": null
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
  "signal_state": "RED",
  "dead_end_ports": ["N"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| track_bits | int | New track geometry |
| switch_state | int | 0 = MAIN, 1 = DIVERGE |
| signal_state | string | Signal color |
| dead_end_ports | array | Manual dead-end override |

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

Create a new train.

**Request:**

```json
{
  "id": "train_1",
  "length": 1.0,
  "max_speed": 100.0,
  "min_curve_radius": 50.0,
  "position": {"x": 0, "y": 0},
  "direction": "NORTH"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| id | string | Required | Unique train identifier |
| length | float | 1.0 | Train length in tiles |
| max_speed | float | 100.0 | Maximum speed |
| min_curve_radius | float | 50.0 | Minimum curve radius |
| position | object | null | Initial {x, y} position |
| direction | string | null | NORTH, EAST, SOUTH, WEST |

**Response:**

```json
{
  "status": "success",
  "train": { ... }
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

### GET /api/simulation/state

Get current simulation state. Returns same format as `/api/network`.

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

## Legacy API (Deprecated)

These endpoints use the old directed_paths system. Use track_bits instead.

### POST /api/track/{x}/{y}/path

Add a single path to track.

```json
{
  "path": {"port1": "N", "port2": "S", "direction": "both"}
}
```

### DELETE /api/track/{x}/{y}/path

Remove a single path from track.

```json
{
  "path": {"port1": "N", "port2": "S"}
}
```
